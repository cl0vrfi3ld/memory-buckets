import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from .helpers import StoreCase, doc, fake_hermes, run_hermes_cli
from memory_buckets import config, index, pending, scopes, tools
from memory_buckets.store import StoreError

INBOX = """---
name: inbox
description: Imported memory waiting to be sorted
---
## imported MEMORY.md
- proj-1 deploys with colmena
- Ivy likes Earl Grey
- the home server runs Jellyfin behind Caddy
- the home server is a Ryzen box in the loft
"""


class PendingTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.put("global/inbox.md", INBOX)
        self.put("proj-1/topics/deploy.md", doc("deploy", "How proj-1 deploys", "- uses flakes\n"))
        self.index = index.Index(self.store)
        self.addCleanup(self.index.close)
        self.patch = mock.patch.object(pending, "open_store", lambda: self.store)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def call(self, name, context="primary", **args):
        c = config.Config()
        ctx = tools.Context(self.store, self.index, scopes.resolve(c, None, context), c, source="cli")
        return tools.handle(name, args, ctx)

    def propose_existing(self, **over):
        args = dict(project="proj-1", files=[{"path": "proj-1/topics/deploy.md", "lines": ["deploys with colmena"]}],
                    inbox_lines=["- proj-1 deploys with colmena"], summary="proj-1's deploy tool")
        args.update(over)
        return self.call("memory_propose", **args)

    def propose_new(self):
        return self.call("memory_propose", project="home-server", new_project=True, summary="The home server",
                         files=[{"path": "home-server/profile.md", "description": "What the home server is and runs",
                                 "lines": ["Ryzen box in the loft", "runs Jellyfin behind Caddy"]}],
                         inbox_lines=["the home server runs Jellyfin behind Caddy",
                                      "- the home server is a Ryzen box in the loft"])

    def test_propose_writes_nothing_under_memories(self):
        before = {p: self.store.read(p)["version"] for p in self.store.iter_paths()}
        got = self.propose_existing()
        self.assertTrue(got["ok"], got)
        self.assertRegex(got["id"], r"^[0-9a-f]{6}$")
        self.assertIn("/memory-apply " + got["id"], got["next"])
        self.assertIn("proj-1's deploy tool", got["next"], "the agent passes the summary on to the user")
        self.assertIn("proj-1/topics/deploy.md (append)", got["preview"])
        self.assertEqual({p: self.store.read(p)["version"] for p in self.store.iter_paths()}, before)
        self.assertTrue((self.store.root / "_pending" / f"{got['id']}.json").is_file())

    def test_propose_tells_the_user_directly_when_it_can(self):
        seen = []
        c = config.Config()
        ctx = tools.Context(self.store, self.index, scopes.resolve(c, None), c, source="cli",
                            notify=lambda m: seen.append(m) or True)
        got = tools.handle("memory_propose", dict(
            project="proj-1", files=[{"path": "proj-1/topics/deploy.md", "lines": ["deploys with colmena"]}],
            inbox_lines=["- proj-1 deploys with colmena"], summary="proj-1's deploy tool."), ctx)
        self.assertTrue(got["ok"], got)
        self.assertEqual(seen, [f"📥 Memory proposal {got['id']} for proj-1: proj-1's deploy tool. "
                                f"/memory-apply {got['id']} saves it, /memory-reject {got['id']} discards it."])
        self.assertEqual(got["next"], "The user has been shown the proposal. Nothing is saved until they apply it.")

    def test_propose_into_a_hermes_project_with_no_memory_yet(self):
        # nixos-config has a Hermes project but no bucket: no new_project flag, no profile needed.
        self.put("global/inbox.md", INBOX + "- nixos-config: uses flake-parts\n")
        c = config.Config()
        ctx = tools.Context(self.store, self.index, scopes.resolve(c, None), c, source="cli",
                            hermes_buckets=["proj-1", "nixos-config"])
        args = dict(project="nixos-config", summary="How nixos-config is structured.",
                    files=[{"path": "nixos-config/topics/layout.md", "description": "How the flake is laid out",
                            "lines": ["uses flake-parts"]}],
                    inbox_lines=["- nixos-config: uses flake-parts"])
        got = tools.handle("memory_propose", args, ctx)
        self.assertTrue(got["ok"], got)
        with fake_hermes({"nixos-config": "/etc/nixos"}):
            done = pending.apply(self.store, got["id"])
        self.assertEqual(done["written"], ["nixos-config/topics/layout.md"])
        self.assertNotIn("flake-parts", self.store.read("global/inbox.md")["content"])
        # Without Hermes knowing the project, it's still refused.
        self.put("global/inbox.md", INBOX + "- other: x\n")
        got = self.call("memory_propose", project="other", summary="x", inbox_lines=["- other: x"],
                        files=[{"path": "other/topics/x.md", "description": "x", "lines": ["x"]}])
        self.assertIn("there's no project other", got["error"]["message"])

    def test_apply_rechecks_the_hermes_project(self):
        # A proposal into a bucket-less Hermes project whose project was deleted (or a
        # hand-edited proposal claiming one) must not create a bucket no session reads.
        self.put("global/inbox.md", INBOX + "- gone: a fact\n")
        c = config.Config()
        ctx = tools.Context(self.store, self.index, scopes.resolve(c, None), c, source="cli",
                            hermes_buckets=["gone"])
        got = tools.handle("memory_propose", dict(
            project="gone", summary="A fact for gone.", inbox_lines=["- gone: a fact"],
            files=[{"path": "gone/topics/x.md", "description": "x", "lines": ["a fact"]}]), ctx)
        self.assertTrue(got["ok"], got)
        with fake_hermes({}), self.assertRaises(StoreError) as caught:
            pending.apply(self.store, got["id"])
        self.assertEqual(caught.exception.code, "conflict")
        self.assertIn("Hermes has no project gone any more", caught.exception.message)
        self.assertFalse((self.store.memories / "gone").exists())
        self.assertIn("- gone: a fact", self.store.read("global/inbox.md")["content"])
        # Outside Hermes there's nothing to check against: the flag stands.
        self.assertEqual(pending.apply(self.store, got["id"])["written"], ["gone/topics/x.md"])

    def test_apply_commits_one_proposal_and_clears_its_inbox_lines(self):
        a, b = self.propose_existing(), self.propose_new()
        out = pending.cmd_apply(a["id"])
        self.assertIn(f"Applied {a['id']} to proj-1", out)
        self.assertIn("- deploys with colmena\n", self.store.read("proj-1/topics/deploy.md")["content"])
        self.assertIn("inbox-sort", self.store.read("proj-1/topics/deploy.md")["content"])
        self.assertIn("edited_at: ", self.store.read("proj-1/topics/deploy.md")["content"])
        self.assertIn("edited_at: ", self.store.read("global/inbox.md")["content"])
        inbox = self.store.read("global/inbox.md")["content"]
        self.assertNotIn("colmena", inbox)
        self.assertIn("Earl Grey", inbox)
        self.assertFalse((self.store.memories / "home-server").exists(), "the other proposal is still pending")
        self.assertEqual([p["id"] for p in pending.list_all(self.store)], [b["id"]])

    def test_new_project(self):
        got = self.propose_new()
        self.assertTrue(got["ok"], got)
        self.assertIn("new project home-server", got["preview"])
        pending.apply(self.store, got["id"])
        profile = self.store.read("home-server/profile.md")["content"]
        self.assertIn("description: What the home server is and runs", profile)
        self.assertIn("- runs Jellyfin behind Caddy", profile)
        self.assertIn("edited_at: ", profile)
        inbox = self.store.read("global/inbox.md")["content"]
        self.assertNotIn("home server", inbox)

    def test_emptied_section_heading_goes(self):
        self.put("global/inbox.md", INBOX.replace("- Ivy likes Earl Grey\n", ""))
        pending.apply(self.store, self.propose_existing()["id"])
        pending.apply(self.store, self.propose_new()["id"])
        self.assertNotIn("## imported", self.store.read("global/inbox.md")["content"])

    def test_refusals(self):
        def code_and_msg(r):
            return r["error"]["code"], r["error"]["message"]
        code, msg = code_and_msg(self.propose_existing(project="nope"))
        self.assertIn("there's no project nope", msg)
        self.assertIn("proj-1", msg)
        code, msg = code_and_msg(self.propose_existing(new_project=True))
        self.assertIn("already exists", msg)
        code, msg = code_and_msg(self.propose_existing(files=[{"path": "global/topics/x.md", "description": "x", "lines": ["x"]}]))
        self.assertIn("isn't under proj-1/", msg)
        code, msg = code_and_msg(self.propose_existing(files=[{"path": "proj-1/index.md", "lines": ["x"]}]))
        self.assertEqual(code, "invalid_path")
        code, msg = code_and_msg(self.propose_existing(files=[{"path": "proj-1/topics/new.md", "lines": ["x"]}]))
        self.assertIn("needs a description", msg)
        code, msg = code_and_msg(self.propose_existing(inbox_lines=["proj-1 deploys with nixops"]))
        self.assertIn("aren't lines in global/inbox.md", msg)
        code, msg = code_and_msg(self.call("memory_propose", project="home-server", new_project=True, summary="x",
                                           files=[{"path": "home-server/topics/x.md", "description": "x", "lines": ["x"]}],
                                           inbox_lines=["Ivy likes Earl Grey"]))
        self.assertIn("needs home-server/profile.md", msg)
        self.assertEqual(self.propose_existing(context="cron")["error"]["code"], "read_only")
        self.assertEqual(pending.list_all(self.store), [])

    def test_lines_claimed_by_a_pending_proposal(self):
        first = self.propose_existing()
        again = self.propose_existing()
        self.assertIn(first["id"], again["error"]["message"])

    def test_stale_proposal_fails_whole(self):
        got = self.propose_new()
        self.put("home-server/profile.md", doc("profile", "someone else made it"))
        with self.assertRaises(StoreError) as caught:
            pending.apply(self.store, got["id"])
        self.assertEqual(caught.exception.code, "conflict")
        self.assertIn("the home server runs Jellyfin", self.store.read("global/inbox.md")["content"], "inbox untouched")
        self.assertEqual(len(pending.list_all(self.store)), 1)

    def test_reject_and_slash_commands(self):
        got = self.propose_existing()
        listing = pending.cmd_pending("")
        self.assertIn(got["id"], listing)
        self.assertIn("proj-1/topics/deploy.md", pending.cmd_pending(got["id"]))
        self.assertIn("Usage", pending.cmd_apply(""))
        self.assertIn("Usage", pending.cmd_apply(f"{got['id']} {got['id']}"), "one at a time")
        self.assertIn("Rejected", pending.cmd_reject(got["id"]))
        self.assertIn("colmena", self.store.read("global/inbox.md")["content"])
        self.assertIn("No pending", pending.cmd_pending(""))
        self.assertIn("isn't a proposal id", pending.cmd_apply("../../etc"))

    def test_pending_dir_is_outside_the_tools(self):
        self.propose_existing()
        self.assertFalse(any(p.startswith("_pending") for p in self.store.iter_paths()))
        self.assertEqual(self.call("memory_read", paths=["_pending/x.md"])["errors"][0]["code"], "invalid_path")

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            with mock.patch.dict(os.environ, {"MEMORY_BUCKETS_STORE": str(self.store.root)}):
                code = run_hermes_cli(*argv)
        return code, buf.getvalue()

    def test_cli_apply_needs_a_person(self):
        got = self.propose_existing()
        with mock.patch("sys.stdin", io.StringIO("y\n")):  # piped: not a terminal
            code, out = self.run_cli("apply", got["id"])
        self.assertEqual(code, 2)
        self.assertIn("interactive terminal", out)
        self.assertEqual(len(pending.list_all(self.store)), 1)

        tty = io.StringIO("y\n")
        tty.isatty = lambda: True
        with mock.patch("sys.stdin", tty):
            code, out = self.run_cli("apply", got["id"])
        self.assertEqual(code, 0, out)
        self.assertIn("applied", out)
        self.assertEqual(pending.list_all(self.store), [])

    def test_cli_pending_and_reject(self):
        got = self.propose_existing()
        self.assertIn(got["id"], self.run_cli("pending")[1])
        self.assertEqual(self.run_cli("reject", got["id"])[0], 0)
        self.assertEqual(json.loads(json.dumps(pending.list_all(self.store))), [])


if __name__ == "__main__":
    unittest.main()
