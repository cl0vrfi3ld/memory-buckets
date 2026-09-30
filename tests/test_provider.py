import json
import os
import unittest
from unittest import mock

from .helpers import FakeEmbedder, StoreCase, doc, fake_hermes
from memory_buckets import provider as prov


class ProviderTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.put("global/profile.md", doc("profile", "Who the user is", "- Ivy\n"))
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
        self.assertIn("- Ivy", first)
        self.call(p, "memory_write", path="global/topics/nix.md", description="Nix", body="- x", if_version="new")
        self.assertEqual(p.system_prompt_block(), first, "same session: frozen for prefix caching")
        p.on_session_switch("s2", parent_session_id="s1", reset=True)
        self.assertIn("global/topics/nix.md", p.system_prompt_block(), "new session: fresh snapshot")

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
        with fake_hermes({"proj-1": "/src/proj-1"}, agent_cwd="/home/ivy"):
            self.assertIsNone(self.make()._scope().project)
            self.assertIsNone(self.make(cwd="/src/proj-10")._scope().project)

    def test_a_moved_session_follows_its_workspace(self):
        # A gateway pins each turn's cwd; moving the chat to another project applies on the next call.
        with fake_hermes({"proj-1": "/src/proj-1"}):
            p = self.make(cwd="/elsewhere")
            self.assertIsNone(p._scope().project)
        with fake_hermes({"proj-1": "/src/proj-1"}, pinned="/src/proj-1"):
            self.assertEqual(p._scope().project, "proj-1")

    def test_without_hermes_nothing_is_scoped(self):
        self.assertIsNone(self.make(cwd="/src/proj-1")._scope().project)

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

    def test_no_nudge_when_read_only(self):
        p = self.make(agent_context="cron")
        p.config.nudge_interval = 1
        p.on_turn_start(1, "x")
        self.assertEqual(p.prefetch("x", session_id="s1"), "")

    def test_prefetch_is_empty_without_embeddings(self):
        p = self.make()
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

    def test_shutdown_waits_for_the_embedding_worker(self):
        # Regression: shutdown closed SQLite under a running worker thread and segfaulted.
        slow = FakeEmbedder()
        slow.delay = 1.0
        p = self.make(embedder=slow)
        self.call(p, "memory_append", path="global/people/sam.md", lines=["has a dog"])
        self.assertIsNotNone(p._embed_thread)
        p.shutdown()
        self.assertFalse(p._embed_thread.is_alive())

    def test_on_memory_write_mirrors_into_inbox(self):
        p = self.make()
        p.on_memory_write("add", "user", "Prefers tea")
        p.on_memory_write("replace", "memory", "Uses NixOS", {"previous_content": "Uses Arch"})
        text = self.store.read("global/inbox.md")["content"]
        self.assertIn("name: inbox", text)
        self.assertIn("- [user add] Prefers tea\n- [memory replace] Uses NixOS (was: Uses Arch)\n", text)
        self.assertEqual(text.count("## mirrored from built-in memory"), 1)

    def test_nothing_to_set_up_and_uninitialised(self):
        p = prov.MemoryBucketsProvider()
        self.assertEqual(p.get_config_schema(), [])
        self.assertEqual(p.system_prompt_block(), "")
        self.assertEqual(json.loads(p.handle_tool_call("memory_list", {}))["ok"], False)

    def test_trace_logs_hooks(self):
        p = self.make()
        with mock.patch.dict(os.environ, {"MEMORY_BUCKETS_TRACE": "1"}), self.assertLogs("memory_buckets", "INFO") as logs:
            p.on_turn_start(1, "hi")
        self.assertIn("hook on_turn_start", logs.output[0])


if __name__ == "__main__":
    unittest.main()
