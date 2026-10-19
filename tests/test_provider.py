import json
import os
import threading
import unittest
from unittest import mock

from .helpers import FakeEmbedder, StoreCase, doc, fake_hermes
from memory_buckets import inbox, projects
from memory_buckets import provider as prov


class ProviderTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.put("global/profile.md", doc("profile", "Who the user is", "- Alex\n"))
        self.put("global/people/sam.md", doc("sam", "Who Sam is", "- Sam likes climbing\n"))
        # Embeddings off unless a test opts in (a bundled model would otherwise switch them on).
        self.env = {"MEMORY_BUCKETS_EMBEDDINGS_BACKEND": "off"}

    def make(self, session="s1", embedder=None, **kw):
        # The built-in model stays off unless a test passes a stand-in for it.
        make_embedder = (lambda *a, **k: embedder) if embedder is not None else prov.make_embedder
        with mock.patch.dict(os.environ, self.env), mock.patch.object(prov, "make_embedder", make_embedder):
            p = prov.MemoryBucketsProvider()
            p.initialize(session, hermes_home=str(self.home), platform=kw.pop("platform", "telegram"), **kw)
        self.addCleanup(p.shutdown)
        return p

    def call(self, p, name, **args):
        return json.loads(p.handle_tool_call(name, args))

    def test_tool_call_roundtrip(self):
        p = self.make()
        self.assertTrue(self.call(p, "memory_append", path="global/people/sam.md", lines=["has a dog"])["ok"])
        got = self.call(p, "memory_read", paths=["global/people/sam.md"])
        self.assertIn("- has a dog", got["files"][0]["content"])
        self.assertIn("  - telegram", got["files"][0]["content"])

    def test_snapshot_is_frozen_per_session(self):
        p = self.make()
        first = p.system_prompt_block()
        self.assertIn("- Alex", first)
        self.call(p, "memory_write", path="global/topics/nix.md", description="Nix", body="- x", if_version="new")
        self.assertEqual(p.system_prompt_block(), first, "same session: frozen for prefix caching")
        p.on_session_switch("s2", parent_session_id="s1", reset=True)
        self.assertIn("global/topics/nix.md", p.system_prompt_block(), "new session: fresh snapshot")

    def test_in_place_compression_refreshes_snapshot_while_rewind_keeps_it(self):
        p = self.make()
        first = p.system_prompt_block()
        self.call(p, "memory_write", path="global/topics/nix.md", description="Nix", body="- x", if_version="new")
        p.on_session_switch("s1", rewound=True)
        self.assertEqual(p.system_prompt_block(), first, "rewind keeps the cached snapshot")
        p.on_session_switch("s1", parent_session_id="s1")
        self.assertIsNone(p._state("s1").snapshot)
        self.assertIn("global/topics/nix.md", p.system_prompt_block(), "in-place compression rebuilds snapshot")

    def test_initialize_expands_tilde_in_store_path_and_backup_paths(self):
        # expanduser reads HOME on POSIX and USERPROFILE on Windows.
        with mock.patch.dict(os.environ, {**self.env, "HOME": str(self.home), "USERPROFILE": str(self.home)}):
            uninit = prov.MemoryBucketsProvider()
            self.assertEqual(uninit.backup_paths(), [])
            with mock.patch.dict(os.environ, {"MEMORY_BUCKETS_STORE": "~/custom-store"}):
                self.assertEqual(uninit.backup_paths(), [str(self.home / "custom-store")])
                p = prov.MemoryBucketsProvider()
                p.initialize("s1", hermes_home=str(self.home), platform="cli")
                self.addCleanup(p.shutdown)
                self.assertEqual(p.store.root, self.home / "custom-store")
                self.assertTrue((self.home / "custom-store/memories/global").is_dir())
                self.assertEqual(p.backup_paths(), [str(self.home / "custom-store")])
            # Once initialised, backup_paths reads self.config without reloading env.
            self.assertEqual(p.backup_paths(), [str(self.home / "custom-store")])
            p.config.store_path = ""
            self.assertEqual(p.backup_paths(), [])

    def test_project_comes_from_the_hermes_project_owning_the_cwd(self):
        with fake_hermes({"proj-1": "/src/proj-1"}):
            p = self.make(cwd="/src/proj-1/app")
            self.assertEqual(p._scope().project, "proj-1")
            p.on_session_switch("s2", parent_session_id="s1")  # compression / branch
            self.assertEqual(p._scope().project, "proj-1")
            self.assertEqual(p._state().platform, "telegram")
            p.on_session_switch("s3", reset=True)  # /new: same agent, same workspace
            self.assertEqual(p._scope().project, "proj-1")
            p.on_session_switch("s3", rewound=True)
            self.assertEqual(p._current, "s3")

    def test_outside_every_project_is_unscoped(self):
        with fake_hermes({"proj-1": "/src/proj-1"}, agent_cwd="/home/user"):
            self.assertIsNone(self.make()._scope().project)
            self.assertIsNone(self.make(cwd="/src/proj-10")._scope().project)

    def test_a_moved_session_follows_its_workspace(self):
        # A gateway pins each turn's cwd; moving the chat to another project applies on the next call.
        with fake_hermes({"proj-1": "/src/proj-1"}):
            p = self.make(cwd="/elsewhere")
            self.assertIsNone(p._scope().project)
        with fake_hermes({"proj-1": "/src/proj-1"}, pinned="/src/proj-1"):
            self.assertEqual(p._scope().project, "proj-1")

    def test_workspace_override_uses_session_id_and_prefetch_skips_project_list(self):
        import sys
        import types
        term = types.SimpleNamespace(
            get_session_cwd=lambda session_key=None: "/tmp/terminal-cd-ignored",
            resolve_task_overrides=lambda task_id: (
                {"cwd": "/src/proj-1", "cwd_source": "session"} if task_id == "s1" else {}
            ),
        )
        tools_pkg = types.SimpleNamespace(__path__=[], terminal_tool=term)
        with fake_hermes({"proj-1": "/src/proj-1"}):
            with mock.patch.dict(sys.modules, {"tools": tools_pkg, "tools.terminal_tool": term}):
                p = self.make(session="s1", cwd="/elsewhere")
                self.assertEqual(p._scope("s1").project, "proj-1")
                with mock.patch("memory_buckets.projects.list_projects",
                                side_effect=projects.list_projects) as list_reads, \
                        mock.patch.object(p.store, "projects", wraps=p.store.projects) as store_reads:
                    p.prefetch("anything", session_id="s1")
                self.assertEqual(list_reads.call_count, 0, "prefetch must not list all Hermes projects")
                self.assertEqual(store_reads.call_count, 0, "prefetch must not scan store project dirs")

    def test_without_hermes_nothing_is_scoped(self):
        self.assertIsNone(self.make(cwd="/src/proj-1")._scope().project)

    def test_the_project_list_is_read_once_per_tool_call(self):
        # One read per tool call (inside _scope_and_project); handle_tool_call
        # used to read it a second time for a Context field nobody read.
        with fake_hermes({"proj-1": "/src/proj-1"}):
            p = self.make(cwd="/src/proj-1")
            with mock.patch("memory_buckets.projects.list_projects",
                            side_effect=projects.list_projects) as reads:
                self.call(p, "memory_list")
            self.assertEqual(reads.call_count, 1, "one read per tool call")

    def test_the_project_list_is_read_once_per_prompt_build(self):
        with fake_hermes({"proj-1": "/src/proj-1"}):
            p = self.make(cwd="/src/proj-1")
            with mock.patch("memory_buckets.projects.list_projects",
                            side_effect=projects.list_projects) as reads:
                p.system_prompt_block()
            self.assertEqual(reads.call_count, 1, "one read per prompt build")

    def test_a_create_in_a_hermes_project_with_no_bucket_yet_is_allowed(self):
        # The known buckets are on-disk ∪ Hermes's: a project Hermes knows but
        # that has no memory files yet is still addable (D2).
        with fake_hermes({"fresh-proj": "/src/fresh"}):
            p = self.make()  # unscoped: the session's cwd is nowhere
            result = self.call(p, "memory_write", path="fresh-proj/topics/x.md",
                               description="What fresh-proj is", body="- a fact\n", if_version="new")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["path"], "fresh-proj/topics/x.md")
        self.assertTrue((self.store.memories / "fresh-proj/topics/x.md").exists())

    def test_project_index_header_comes_from_hermes(self):
        with fake_hermes({"proj-1": "/src/proj-1"}):
            block = self.make(cwd="/src/proj-1/app").system_prompt_block()
        self.assertIn("### Project: proj-1\nHermes project name: Proj-1\nFolders: /src/proj-1\n"
                      "This project has no memory yet.", block)

    def test_hermes_projects_are_listed(self):
        with fake_hermes({"proj-1": "/src/proj-1", "nixos-config": "/etc/nixos"}):
            block = self.make(cwd="/src/proj-1").system_prompt_block()
        self.assertIn("Projects: nixos-config.", block)

    def cross_write(self, p, line="a"):
        """An unscoped session adds to another project's bucket: the one write that shows a status line."""
        if not (self.store.memories / "addon-2/topics/x.md").exists():
            self.put("addon-2/topics/x.md", doc("x", "X", "- first\n"))
        return self.call(p, "memory_append", path="addon-2/topics/x.md", lines=[line])

    def test_status_callback_shows_cross_project_writes(self):
        shown = []
        p = self.make(platform="cli", status_callback=shown.append)
        result = self.cross_write(p)
        self.assertTrue(result["ok"], result)
        self.assertEqual(shown, ["📝 Saved to addon-2/topics/x.md"])
        self.assertNotIn("next", result)
        p.on_session_switch("s2", reset=True)  # /new keeps the channel
        self.cross_write(p, "another")
        self.assertEqual(len(shown), 2)
        # A write inside the session's own scope shows nothing.
        self.call(p, "memory_append", path="global/people/sam.md", lines=["has a dog"])
        self.assertEqual(len(shown), 2)

    def test_a_gateway_session_has_no_status_line_and_no_fallback(self):
        # D5: the notice is best-effort. The agent is never asked to relay it.
        gateway = self.make(session="g1", platform="telegram")
        result = self.cross_write(gateway)
        self.assertTrue(result["ok"], result)
        self.assertNotIn("next", result)

    def fake_tui(self, provider, status_callback, **agent_flags):
        """``tui_gateway.server._sessions`` as the TUI keeps it: an agent whose memory manager holds ``provider``."""
        import sys
        import types
        agent = types.SimpleNamespace(status_callback=status_callback,
                                      _memory_manager=types.SimpleNamespace(providers=[provider]), **agent_flags)
        server = types.ModuleType("tui_gateway.server")
        setattr(server, "_sessions", {"sid-1": {"agent": agent}, "other": {"agent": None}})
        return mock.patch.dict(sys.modules, {"tui_gateway.server": server})

    def test_tui_status_line(self):
        shown = []
        p = self.make(platform="tui")
        with self.fake_tui(p, lambda kind, text=None: shown.append((kind, text))):
            result = self.cross_write(p)
        self.assertEqual(shown, [("lifecycle", "📝 Saved to addon-2/topics/x.md")])
        self.assertNotIn("next", result)

    def test_tui_without_a_status_line_stays_quiet(self):
        p = self.make(session="t1", platform="tui")
        # Outside the TUI process, or another provider's agent: nothing to show it on.
        self.assertNotIn("next", self.cross_write(p, "a"))
        with self.fake_tui(object(), lambda kind, text=None: None):
            self.assertNotIn("next", self.cross_write(p, "b"))
        # A callback that raises never fails the write.
        with self.fake_tui(p, lambda kind, text=None: 1 / 0):
            result = self.cross_write(p, "c")
        self.assertTrue(result["ok"], result)
        self.assertNotIn("next", result)
        # Desktop ignores "lifecycle" status updates, and gateways have no status
        # line: neither looks for the TUI, even when it owns the provider.
        for platform in ("desktop", "telegram"):
            other = self.make(session=f"x-{platform}", platform=platform)
            with self.fake_tui(other, lambda kind, text=None: self.fail(f"{platform} isn't the TUI")):
                result = self.cross_write(other, "d")
            self.assertTrue(result["ok"], result)
            self.assertNotIn("next", result)

    def test_quiet_and_muted_sessions_show_nothing(self):
        # `hermes chat -q`, one-shot runs and muted turns print nothing, so there the
        # notice isn't shown (and nobody asks the agent to say it).
        class Agent:
            suppress_status_output = False
            _mute_notification_reply = False

            def __init__(self):
                self.printed = []

            def _emit_status(self, message):
                self.printed.append(message)

        for flag in ("suppress_status_output", "_mute_notification_reply"):
            with self.subTest(flag):
                agent = Agent()
                p = self.make(session=f"q-{flag}", platform="cli", status_callback=agent._emit_status)
                setattr(agent, flag, True)
                result = self.cross_write(p, "x")
                self.assertEqual(agent.printed, [])
                self.assertNotIn("next", result)
                setattr(agent, flag, False)  # read live: a muted turn ends, notices resume
                self.assertNotIn("next", self.cross_write(p, "y"))
                self.assertEqual(agent.printed, ["📝 Saved to addon-2/topics/x.md"])
        # The TUI agent's flags count too.
        p = self.make(session="q-tui", platform="tui")
        with self.fake_tui(p, lambda kind, text=None: self.fail("a muted turn showed a notice"),
                           _mute_notification_reply=True):
            self.assertNotIn("next", self.cross_write(p, "z"))

    def test_cron_is_read_only(self):
        p = self.make(agent_context="cron", platform="cron")
        result = self.call(p, "memory_append", path="global/people/sam.md", lines=["x"])
        self.assertEqual(result["error"]["code"], "read_only")

    def test_nudge_every_n_turns(self):
        p = self.make()
        p.config.nudge_interval = 3
        seen = []
        for turn in range(1, 7):
            p.on_turn_start(turn, "hello there")
            seen.append(prov.NUDGE in p.prefetch("what's new", session_id="s1"))
        self.assertEqual(seen, [False, False, True, False, False, True])

    def test_nudge_copy(self):
        # "new" is the Saving rules' word for "not saved yet"; it keeps the question under 15 words.
        self.assertEqual(prov.NUDGE, "Memory check: has this conversation produced new durable facts about the user or "
                                     "their work? If yes, save them now with the memory tools, following your memory "
                                     "instructions. If no, do nothing. Do not mention this check to the user.")

    def test_no_nudge_when_read_only(self):
        p = self.make(agent_context="cron")
        p.config.nudge_interval = 1
        p.on_turn_start(1, "x")
        self.assertEqual(p.prefetch("x", session_id="s1"), "")

    def test_pre_compress_asks_the_summary_to_keep_unsaved_facts_and_queues_the_nudge(self):
        p = self.make()
        p.config.nudge_interval = 10
        p.on_turn_start(1, "hello")
        self.assertEqual(p.prefetch("hello", session_id="s1"), "", "interval not reached yet")
        note = p.on_pre_compress([{"role": "user", "content": "I switched to Neovim"}])
        self.assertEqual(note, prov.PRE_COMPRESS_NOTE)
        self.assertIn("not yet been saved", note)
        self.assertIn(prov.NUDGE, p.prefetch("what's new", session_id="s1"))
        self.assertNotIn(prov.NUDGE, p.prefetch("and now", session_id="s1"), "one reminder per compression")

    def test_pre_compress_nudge_survives_compression_rotating_the_session_id(self):
        p = self.make()
        p.on_pre_compress([])
        p.on_session_switch("s2", parent_session_id="s1", reset=False, reason="compression")
        self.assertIn(prov.NUDGE, p.prefetch("hello", session_id="s2"))

    def test_pre_compress_nudge_survives_in_place_compression(self):
        p = self.make()
        p.on_pre_compress([])
        p.on_session_switch("s1", parent_session_id="s1", reset=False, reason="compression")
        self.assertIn(prov.NUDGE, p.prefetch("hello", session_id="s1"))

    def test_a_reset_drops_a_pending_pre_compress_nudge(self):
        p = self.make()
        p.on_pre_compress([])
        p.on_session_switch("s2", parent_session_id="s1", reset=True)
        self.assertEqual(p.prefetch("hello", session_id="s2"), "")

    def test_pre_compress_does_nothing_when_read_only(self):
        p = self.make(agent_context="cron")
        self.assertEqual(p.on_pre_compress([]), "")
        self.assertEqual(p.prefetch("x", session_id="s1"), "")
        self.assertFalse(p._state("s1").nudge_due)

    def test_pre_compress_does_nothing_when_reminders_are_off(self):
        p = self.make()
        p.config.nudge_interval = 0
        self.assertEqual(p.on_pre_compress([]), "")
        self.assertEqual(p.prefetch("x", session_id="s1"), "")

    def test_pre_compress_before_initialize_is_empty(self):
        self.assertEqual(prov.MemoryBucketsProvider().on_pre_compress([]), "")

    def test_prefetch_is_empty_without_embeddings(self):
        p = self.make()
        with self.assertNoLogs("memory_buckets", "WARNING"):
            self.assertEqual(p.prefetch("Sam likes climbing", session_id="s1"), "")
        self.assertIsNone(p.recall_status())

    def test_prefetch_hints_with_embeddings(self):
        p = self.make(embedder=FakeEmbedder())
        p.config.prefetch_min_similarity = 0.3
        p.system_prompt_block()  # profile is inlined, so never hinted
        p.queue_prefetch("x", session_id="s1")
        out = p.prefetch("Sam likes climbing", session_id="s1")
        self.assertIn("- global/people/sam.md: Who Sam is", out)
        self.assertNotIn("global/profile.md", out)
        self.assertEqual(p.recall_status().count, 1)

    def test_prefetch_forwards_project_and_boost_to_hints(self):
        with fake_hermes({"proj-1": "/src/proj-1"}):
            p = self.make(embedder=FakeEmbedder(), cwd="/src/proj-1")
            p.config.project_boost = 1.75
            with mock.patch.object(p.index, "hints",
                                   return_value=[("proj-1/topics/arch.md", "Proj-1 architecture", 0.5)]) as hints:
                out = p.prefetch("architecture", session_id="s1")
            self.assertIn("- proj-1/topics/arch.md: Proj-1 architecture", out)
            hints.assert_called_once()
            self.assertEqual(hints.call_args.kwargs.get("project"), "proj-1")
            self.assertEqual(hints.call_args.kwargs.get("boost"), 1.75)
            unscoped = self.make(session="s2", embedder=FakeEmbedder(), cwd="/elsewhere")
            with mock.patch.object(unscoped.index, "hints", return_value=[]) as unscoped_hints:
                unscoped.prefetch("architecture", session_id="s2")
            unscoped_hints.assert_called_once()
            self.assertIn("project", unscoped_hints.call_args.kwargs)
            self.assertIsNone(unscoped_hints.call_args.kwargs["project"])
            self.assertEqual(unscoped_hints.call_args.kwargs.get("boost"), unscoped.config.project_boost)

    def test_prefetch_embeds_backlog_on_turn_one_and_after_external_edit(self):
        p = self.make(embedder=FakeEmbedder())
        p.config.prefetch_min_similarity = 0.3
        p.system_prompt_block()
        # Turn 1: no prior queue_prefetch call.
        out1 = p.prefetch("Sam likes climbing", session_id="s1")
        self.assertIn("- global/people/sam.md: Who Sam is", out1)
        # External file edit on disk without queue_prefetch or tool calls.
        self.put("global/topics/gardening.md", doc("gardening", "Gardening notes", "- grows heirloom tomatoes\n"))
        out2 = p.prefetch("grows heirloom tomatoes", session_id="s1")
        self.assertIn("- global/topics/gardening.md: Gardening notes", out2)
        # When _embed_running is held by a background worker, prefetch skips embedding the backlog.
        self.put("global/topics/baking.md", doc("baking", "Baking notes", "- bakes sourdough bread\n"))
        self.assertTrue(p._embed_running.acquire(blocking=False))
        try:
            self.assertEqual(p.prefetch("bakes sourdough bread", session_id="s1"), "")
        finally:
            p._embed_running.release()
        self.assertIn("- global/topics/baking.md: Baking notes",
                      p.prefetch("bakes sourdough bread", session_id="s1"))

    def test_prefetch_hint_cooldown_suppresses_and_rehints_after_five_turns(self):
        p = self.make(embedder=FakeEmbedder())
        p.config.prefetch_min_similarity = 0.3
        p.system_prompt_block()
        p.queue_prefetch("x", session_id="s1")
        out1 = p.prefetch("Sam likes climbing", session_id="s1")
        self.assertIn("global/people/sam.md", out1)
        self.assertEqual(p._state("s1").prefetch_turns, 1)
        self.assertEqual(p._state("s1").hint_seen.get("global/people/sam.md"), 1)
        # Suppressed for the next 4 turns (turns 2..5) in the same session.
        for expected_turn in range(2, 1 + prov.HINT_COOLDOWN_TURNS):
            self.assertEqual(p.prefetch("Sam likes climbing", session_id="s1"), "")
            self.assertEqual(p._state("s1").prefetch_turns, expected_turn)
            self.assertIsNone(p.recall_status())
        # Another session is unaffected by s1's cooldown.
        self.assertIn("global/people/sam.md", p.prefetch("Sam likes climbing", session_id="s2"))
        # Turn 6 (1 + 5): cooldown has elapsed and the file can be hinted again.
        out6 = p.prefetch("Sam likes climbing", session_id="s1")
        self.assertIn("global/people/sam.md", out6)
        self.assertEqual(p._state("s1").prefetch_turns, 1 + prov.HINT_COOLDOWN_TURNS)
        self.assertEqual(p._state("s1").hint_seen.get("global/people/sam.md"), 1 + prov.HINT_COOLDOWN_TURNS)

    def test_tool_calls_suppress_prefetch_hints_for_cooldown_and_delete_clears(self):
        p = self.make(embedder=FakeEmbedder())
        p.config.prefetch_min_similarity = 0.3
        p.system_prompt_block()
        p.queue_prefetch("x", session_id="s1")
        # Reading a file before the first prefetch records seen_turn = 1.
        self.assertTrue(self.call(p, "memory_read", paths=["global/people/sam.md"])["ok"])
        self.assertEqual(p._state("s1").hint_seen.get("global/people/sam.md"), 1)
        # Turns 1..5 are suppressed because seen_turn is 1.
        for _ in range(prov.HINT_COOLDOWN_TURNS):
            self.assertEqual(p.prefetch("Sam likes climbing", session_id="s1"), "")
        # Turn 6 (1 + 5) hints the file again.
        self.assertIn("global/people/sam.md", p.prefetch("Sam likes climbing", session_id="s1"))
        self.assertEqual(p._state("s1").prefetch_turns, 6)
        # Writing a new file at turn 6 records seen_turn = 6.
        self.assertTrue(self.call(
            p, "memory_write", path="global/topics/climbing.md",
            description="Climbing notes", body="- Sam likes climbing\n", if_version="new",
        )["ok"])
        p.queue_prefetch("x", session_id="s1")
        self.assertEqual(p._state("s1").hint_seen.get("global/topics/climbing.md"), 6)
        # Turns 7..10: both sam.md and climbing.md are in cooldown.
        for _ in range(prov.HINT_COOLDOWN_TURNS - 1):
            self.assertEqual(p.prefetch("Sam likes climbing", session_id="s1"), "")
        self.assertEqual(p._state("s1").prefetch_turns, 10)
        # Appending to sam.md at turn 10 refreshes its cooldown while climbing.md reaches turn 6 + 5 = 11.
        self.assertTrue(self.call(p, "memory_append", path="global/people/sam.md", lines=["indoor bouldering"])["ok"])
        p.queue_prefetch("x", session_id="s1")
        self.assertEqual(p._state("s1").hint_seen.get("global/people/sam.md"), 10)
        out11 = p.prefetch("Sam likes climbing", session_id="s1")
        self.assertIn("global/topics/climbing.md", out11)
        self.assertNotIn("global/people/sam.md", out11)
        # Editing sam.md with memory_str_replace at turn 11 refreshes its cooldown to turn 11.
        self.assertTrue(self.call(
            p, "memory_str_replace", path="global/people/sam.md",
            old="- indoor bouldering", new="- outdoor bouldering",
        )["ok"])
        p.queue_prefetch("x", session_id="s1")
        self.assertEqual(p._state("s1").hint_seen.get("global/people/sam.md"), 11)
        for _ in range(prov.HINT_COOLDOWN_TURNS - 1):
            self.assertEqual(p.prefetch("Sam likes climbing", session_id="s1"), "")
        out16 = p.prefetch("Sam likes climbing", session_id="s1")
        self.assertIn("global/people/sam.md", out16)
        # Deleting climbing.md removes it from hint_seen.
        ver = self.store.read("global/topics/climbing.md")["version"]
        self.assertTrue(self.call(p, "memory_delete", path="global/topics/climbing.md", if_version=ver)["ok"])
        self.assertNotIn("global/topics/climbing.md", p._state("s1").hint_seen)

    def test_in_place_compaction_clears_hint_seen_while_rewind_keeps_it(self):
        p = self.make(embedder=FakeEmbedder())
        p.config.prefetch_min_similarity = 0.3
        p.system_prompt_block()
        p.queue_prefetch("x", session_id="s1")
        self.assertIn("global/people/sam.md", p.prefetch("Sam likes climbing", session_id="s1"))
        self.assertEqual(p.prefetch("Sam likes climbing", session_id="s1"), "")
        # Rewind keeps hint_seen intact.
        p.on_session_switch("s1", rewound=True)
        self.assertEqual(p.prefetch("Sam likes climbing", session_id="s1"), "")
        # In-place compaction clears hint_seen and resets prefetch_turns so hints can appear immediately.
        p.on_session_switch("s1", parent_session_id="s1")
        self.assertEqual(p._state("s1").hint_seen, {})
        self.assertEqual(p._state("s1").prefetch_turns, 0)
        self.assertIn("global/people/sam.md", p.prefetch("Sam likes climbing", session_id="s1"))

    def test_prefetch_excludes_inlined_files_when_snapshot_was_not_built(self):
        self.put("global/preferences.md", doc("preferences", "How to work", "- Alex prefers concise answers\n"))
        self.put("proj-1/profile.md", doc("profile", "What proj-1 is", "- Alex builds proj-1\n"))
        self.put("proj-1/preferences.md", doc("preferences", "Proj-1 conventions", "- Alex pins Python\n"))
        self.put("proj-1/topics/arch.md", doc("arch", "Proj-1 architecture", "- Alex uses SQLite\n"))
        with fake_hermes({"proj-1": "/src/proj-1"}):
            p = self.make(embedder=FakeEmbedder(), cwd="/src/proj-1")
            p.config.prefetch_min_similarity = 0.1
            p.queue_prefetch("x", session_id="s1")
            self.assertIsNone(p._state("s1").snapshot)
            out = p.prefetch("Alex", session_id="s1")
        self.assertIn("proj-1/topics/arch.md", out)
        for inlined in ("global/profile.md", "global/preferences.md", "proj-1/profile.md", "proj-1/preferences.md"):
            self.assertNotIn(inlined, out)

    def put_two_projects(self):
        for bucket, who in (("proj-a", "Ann"), ("proj-b", "Bea")):
            self.put(f"{bucket}/profile.md", doc("profile", f"What {bucket} is", f"- {who} runs {bucket}\n"))
            self.put(f"{bucket}/preferences.md", doc("preferences", f"How to work on {bucket}", f"- {who} wants tests\n"))
            self.put(f"{bucket}/topics/arch.md", doc("arch", f"{bucket} architecture", f"- {who} uses SQLite\n"))

    def test_a_project_switch_is_noted_once_and_the_block_stays_frozen(self):
        self.put_two_projects()
        projects_ab = {"proj-a": "/src/proj-a", "proj-b": "/src/proj-b"}
        with fake_hermes(projects_ab):
            p = self.make(cwd="/src/proj-a")
            block = p.system_prompt_block()
            self.assertIn("This session is in the project proj-a.", block)
            self.assertEqual(p.prefetch("hello", session_id="s1"), "", "no switch, no note")
        with fake_hermes(projects_ab, pinned="/src/proj-b"):
            note = p.prefetch("hello", session_id="s1")
            self.assertIn("It is now in the project proj-b.", note)
            self.assertIn("This session is in the project proj-b. Save each fact once, in one place:", note)
            self.assertIn("- proj-b/preferences.md: how to work on the project", note)
            self.assertIn("### proj-b/profile.md\n- Bea runs proj-b", note)
            self.assertIn("### proj-b/preferences.md\n- Bea wants tests", note)
            self.assertNotIn("Ann", note)
            self.assertEqual(p.prefetch("hello again", session_id="s1"), "", "once per switch, not every turn")
            self.assertEqual(p.system_prompt_block(), block, "the frozen block is kept for prefix caching")
            # The note and the tools agree: proj-b is the session's own bucket, proj-a is add-only.
            ok = self.call(p, "memory_str_replace", path="proj-b/topics/arch.md", old="SQLite", new="Postgres")
            self.assertTrue(ok["ok"], ok)
            refused = self.call(p, "memory_str_replace", path="proj-a/topics/arch.md", old="SQLite", new="Postgres")
            self.assertEqual(refused["error"]["code"], "out_of_scope")
            self.assertIn("proj-a is now another project. You may add to its files", note)
            self.assertTrue(self.call(p, "memory_append", path="proj-a/topics/arch.md", lines=["- more"])["ok"])

    def test_the_note_overrules_the_frozen_blocks_other_project_sections(self):
        # The block frozen in proj-a lists proj-b as "another project" in two places, with
        # add-only rules. After the move the note has to say that no longer holds for proj-b.
        self.put_two_projects()
        projects_ab = {"proj-a": "/src/proj-a", "proj-b": "/src/proj-b"}
        with fake_hermes(projects_ab):
            p = self.make(cwd="/src/proj-a")
            block = p.system_prompt_block()
        self.assertIn("Projects: proj-b.", block)
        self.assertIn("Other projects with memory: proj-b", block)
        with fake_hermes(projects_ab, pinned="/src/proj-b"):
            note = p.prefetch("hello", session_id="s1")
        self.assertIn(
            "proj-b is now this session's own project. Where the earlier \"Facts for another project\" "
            "and \"Other projects with memory\" sections list it as another project, ignore that.",
            note,
        )
        # Out of a project there is no new own project to claim, so no such line.
        with fake_hermes(projects_ab, pinned="/elsewhere"):
            self.assertNotIn("own project", p.prefetch("hello", session_id="s1"))

    def test_moving_back_or_out_of_a_project_is_noted_too(self):
        self.put_two_projects()
        projects_ab = {"proj-a": "/src/proj-a", "proj-b": "/src/proj-b"}
        with fake_hermes(projects_ab):
            p = self.make(cwd="/src/proj-a")
            p.system_prompt_block()
        with fake_hermes(projects_ab, pinned="/src/proj-b"):
            self.assertIn("project proj-b", p.prefetch("x", session_id="s1"))
        # The model was last told proj-b, so going back to proj-a needs its own note,
        # even though the frozen block still describes proj-a.
        with fake_hermes(projects_ab, pinned="/src/proj-a"):
            back = p.prefetch("x", session_id="s1")
            self.assertIn("This session was in the project proj-b. It is now in the project proj-a.", back)
            self.assertIn("### proj-a/profile.md\n- Ann runs proj-a", back)
            self.assertEqual(p.prefetch("x", session_id="s1"), "")
        with fake_hermes(projects_ab, pinned="/elsewhere"):
            out = p.prefetch("x", session_id="s1")
            self.assertIn("It is no longer in a project.", out)
            self.assertIn("This session is not in a project. Save general facts under global/.", out)
            self.assertIn("proj-a is now another project.", out)
            self.assertNotIn("### proj-a/", out)
            self.assertEqual(p.prefetch("x", session_id="s1"), "")

    def test_the_note_inlines_files_that_prefetch_then_does_not_hint(self):
        self.put_two_projects()
        projects_ab = {"proj-a": "/src/proj-a", "proj-b": "/src/proj-b"}
        with fake_hermes(projects_ab):
            p = self.make(embedder=FakeEmbedder(), cwd="/src/proj-a")
            p.config.prefetch_min_similarity = 0.1
            p.system_prompt_block()
            p.queue_prefetch("x", session_id="s1")
        with fake_hermes(projects_ab, pinned="/src/proj-b"):
            out = p.prefetch("Bea", session_id="s1")
        self.assertIn("### proj-b/profile.md", out)
        self.assertIn("- proj-b/topics/arch.md: proj-b architecture", out)
        # A hint line carries the file's description; the routing text only names the paths.
        for description in ("What proj-b is", "How to work on proj-b"):
            self.assertNotIn(description, out, "inlined by the note, so not hinted as well")

    def test_in_place_compression_after_a_switch_rebuilds_for_the_new_project(self):
        self.put_two_projects()
        projects_ab = {"proj-a": "/src/proj-a", "proj-b": "/src/proj-b"}
        with fake_hermes(projects_ab):
            p = self.make(cwd="/src/proj-a")
            p.system_prompt_block()
        with fake_hermes(projects_ab, pinned="/src/proj-b"):
            self.assertIn("project proj-b", p.prefetch("x", session_id="s1"))
            p.on_session_switch("s1", parent_session_id="s1")
            self.assertIsNone(p._state("s1").note)
            self.assertIn("This session is in the project proj-b.", p.system_prompt_block())
            self.assertEqual(p.prefetch("x", session_id="s1"), "", "the rebuilt block already says proj-b")

    def test_no_note_without_a_snapshot_to_compare_with(self):
        # A resumed session reuses a persisted prompt that this provider never built.
        self.put_two_projects()
        with fake_hermes({"proj-b": "/src/proj-b"}, pinned="/src/proj-b"):
            p = self.make(cwd="/elsewhere")
            self.assertIsNone(p._state("s1").snapshot)
            self.assertEqual(p.prefetch("x", session_id="s1"), "")

    def test_a_failing_note_is_retried_next_turn(self):
        self.put_two_projects()
        projects_ab = {"proj-a": "/src/proj-a", "proj-b": "/src/proj-b"}
        with fake_hermes(projects_ab):
            p = self.make(cwd="/src/proj-a")
            p.system_prompt_block()
        with fake_hermes(projects_ab, pinned="/src/proj-b"):
            with mock.patch.object(prov.snapshot, "switch_note", side_effect=RuntimeError("boom")), \
                    self.assertLogs("memory_buckets", "WARNING"):
                self.assertEqual(p.prefetch("x", session_id="s1"), "")
            self.assertIn("project proj-b", p.prefetch("x", session_id="s1"))

    def test_queue_prefetch_skips_embed_backlog_while_after_write_embeds(self):
        p = self.make(embedder=FakeEmbedder())
        self.assertTrue(p._embed_running.acquire(blocking=False))
        try:
            with mock.patch.object(p.index, "reconcile", wraps=p.index.reconcile) as reconcile, \
                    mock.patch.object(p.index, "embed_backlog") as embed_backlog:
                p.queue_prefetch("x", session_id="s1")
            self.assertEqual(reconcile.call_count, 1)
            embed_backlog.assert_not_called()
        finally:
            p._embed_running.release()
        with mock.patch.object(p.index, "embed_backlog", return_value=0) as embed_backlog:
            p.queue_prefetch("x", session_id="s1")
        embed_backlog.assert_called_once_with(max_batches=4)
        self.assertTrue(p._embed_running.acquire(blocking=False))
        p._embed_running.release()

    def test_shutdown_waits_for_the_embedding_worker(self):
        # Regression: shutdown closed SQLite under a running worker thread and segfaulted.
        slow = FakeEmbedder()
        slow.delay = 1.0
        p = self.make(embedder=slow)
        self.call(p, "memory_append", path="global/people/sam.md", lines=["has a dog"])
        self.assertIsNotNone(p._embed_thread)
        p.shutdown()
        self.assertFalse(p._embed_thread.is_alive())

    def test_shutdown_waits_for_an_inflight_queue_prefetch(self):
        # Hermes drains its executor only up to a timeout, so queue_prefetch can still be
        # inside SQLite when shutdown runs; closing the index then can crash the interpreter.
        p = self.make(embedder=FakeEmbedder())
        entered, release = threading.Event(), threading.Event()

        def slow_backlog(max_batches=1):
            entered.set()
            release.wait(10)
            return 0

        with mock.patch.object(p.index, "embed_backlog", slow_backlog), \
                mock.patch.object(p.index, "close", wraps=p.index.close) as close:
            prefetch = threading.Thread(target=p.queue_prefetch, args=("x",), kwargs={"session_id": "s1"})
            prefetch.start()
            self.assertTrue(entered.wait(10))
            stopper = threading.Thread(target=p.shutdown)
            stopper.start()
            stopper.join(0.3)
            self.assertTrue(stopper.is_alive(), "shutdown returned while queue_prefetch was running")
            close.assert_not_called()
            release.set()
            prefetch.join(10)
            stopper.join(10)
            self.assertFalse(stopper.is_alive())
            close.assert_called_once()

    def test_shutdown_leaves_the_index_open_if_a_prefetch_outlasts_it(self):
        p = self.make(embedder=FakeEmbedder())
        stuck = mock.Mock()
        stuck.wait.return_value = False  # what Event.wait returns on timeout
        p._prefetch_idle = stuck
        with mock.patch.object(p.index, "close") as close, self.assertLogs("memory_buckets", "WARNING") as logs:
            p.shutdown()
        close.assert_not_called()
        self.assertTrue(any("prefetch still running at shutdown" in line for line in logs.output), logs.output)

    def test_queue_prefetch_after_shutdown_does_nothing(self):
        p = self.make(embedder=FakeEmbedder())
        p.shutdown()
        with mock.patch.object(p.index, "reconcile") as reconcile, \
                mock.patch.object(p.index, "embed_backlog") as embed_backlog:
            p.queue_prefetch("x", session_id="s1")
        reconcile.assert_not_called()
        embed_backlog.assert_not_called()
        self.assertTrue(p._prefetch_idle.is_set())

    def test_queue_prefetch_counts_even_when_it_fails(self):
        p = self.make(embedder=FakeEmbedder())
        with mock.patch.object(p.index, "reconcile", side_effect=RuntimeError("boom")), \
                self.assertLogs("memory_buckets", "WARNING"):
            p.queue_prefetch("x", session_id="s1")
        self.assertTrue(p._prefetch_idle.is_set())

    def test_on_memory_write_mirrors_into_inbox(self):
        p = self.make()
        p.on_memory_write("add", "user", "Prefers tea")
        p.on_memory_write("replace", "memory", "Uses NixOS", {"previous_content": "Uses Arch"})
        text = self.store.read("global/inbox.md")["content"]
        self.assertIn(f"name: inbox\ndescription: {inbox.DESCRIPTION}", text)
        self.assertIn("- [user add] Prefers tea\n- [memory replace] Uses NixOS (was: Uses Arch)\n", text)
        self.assertEqual(text.count("## mirrored from built-in memory"), 1)

    def test_on_memory_write_recreates_a_deleted_inbox(self):
        p = self.make()
        p.on_memory_write("add", "user", "Prefers tea")
        path = self.store.memories / "global/inbox.md"
        v = self.store.read("global/inbox.md")["version"]
        self.assertTrue(self.call(p, "memory_delete", path="global/inbox.md", if_version=v)["ok"])
        self.assertFalse(path.exists())
        p.on_memory_write("add", "user", "Uses NixOS")
        text = self.store.read("global/inbox.md")["content"]
        self.assertIn(f"name: inbox\ndescription: {inbox.DESCRIPTION}", text)
        self.assertIn("- [user add] Uses NixOS\n", text)
        self.assertNotIn("Prefers tea", text)

    def test_nothing_to_set_up_and_uninitialised(self):
        p = prov.MemoryBucketsProvider()
        # The setup schema is static (it describes config.py, not the store), so an
        # uninitialised provider reports it; the full shape is tests/test_setup.py's.
        self.assertEqual(len(p.get_config_schema()), 12)
        self.assertEqual(p.system_prompt_block(), "")
        self.assertEqual(json.loads(p.handle_tool_call("memory_list", {}))["ok"], False)

    def test_corrupt_index_is_rebuilt_not_a_crash(self):
        # The index is only a cache: a broken SQLite file must be rebuilt and the
        # tool call retried, not propagate sqlite3.Error to Hermes (review finding).
        p = self.make()
        self.assertTrue(self.call(p, "memory_list")["ok"])
        assert p.index is not None
        p.index.close()
        (self.store.root / ".index" / "memory.sqlite").write_bytes(b"not a sqlite file")
        result = self.call(p, "memory_list")
        self.assertTrue(result["ok"], result)
        self.assertIn("global/profile.md", [f["path"] for f in result["files"]])

    def test_trace_logs_hooks(self):
        p = self.make()
        with mock.patch.dict(os.environ, {"MEMORY_BUCKETS_TRACE": "1"}), self.assertLogs("memory_buckets", "INFO") as logs:
            p.on_turn_start(1, "hi")
        self.assertIn("hook on_turn_start", logs.output[0])


if __name__ == "__main__":
    unittest.main()
