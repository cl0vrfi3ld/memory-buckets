import json
import unittest

from .helpers import StoreCase, doc
from memory_buckets import config, inbox, index, scopes, tools
from memory_buckets.store import StoreError, version


class ToolsTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.put("global/topics/nix.md", doc("nix", "Read for Nix", "- flakes\n", extra="obsidian:\n  cssclass: wide\n"))
        self.put("proj-1/topics/deploy.md", doc("deploy", "How proj-1 deploys", "- colmena\n"))
        self.index = index.Index(self.store)
        self.addCleanup(self.index.close)
        self.written = []

    def call(self, name, project=None, context="primary", **args):
        c = config.Config()
        ctx = tools.Context(self.store, self.index, scopes.resolve(c, project, context), c,
                            source="telegram", on_write=self.written.append)
        result = tools.handle(name, args, ctx)
        json.dumps(result)  # always serialisable
        return result

    def content(self, path):
        return self.store.read(path)["content"]

    def test_schemas_are_openai_function_shape(self):
        for schema in tools.SCHEMAS:
            self.assertEqual(set(schema), {"name", "description", "parameters"})
            self.assertEqual(schema["parameters"]["type"], "object")

    def test_list_defaults_to_scope(self):
        listed = self.call("memory_list")
        self.assertEqual([f["path"] for f in listed["files"]], ["global/topics/nix.md"])
        self.assertEqual(listed["files"][0]["description"], "Read for Nix")
        self.assertEqual(listed["scope"]["writes"], ["global/"])
        everything = self.call("memory_list", include_projects=True)
        self.assertEqual(len(everything["files"]), 2)
        self.assertEqual(len(self.call("memory_list", prefix="proj-1/")["files"]), 1)

    def test_read_partial_success(self):
        got = self.call("memory_read", paths=["global/topics/nix.md", "global/topics/nope.md", "bad"])
        self.assertTrue(got["ok"])
        self.assertEqual(len(got["files"]), 1)
        self.assertEqual({e["code"] for e in got["errors"]}, {"not_found", "invalid_path"})
        self.assertFalse(self.call("memory_read", paths=["global/topics/nope.md"])["ok"])
        self.assertEqual(self.call("memory_read", paths=[])["error"]["code"], "invalid_args")

    def test_write_create_and_replace(self):
        made = self.call("memory_write", path="global/people/sam.md", description="Who Sam is",
                         body="- climbs", if_version="new", aliases=["samuel"])
        self.assertTrue(made["ok"], made)
        text = self.content("global/people/sam.md")
        self.assertRegex(text, r"^---\nname: sam\ndescription: Who Sam is\naliases:\n  - samuel\nsources:\n  - telegram\n"
                               r"edited_at: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\n---\n- climbs\n$")
        self.assertEqual(made["version"], version(text.encode()))
        self.assertEqual(self.written, ["global/people/sam.md"])

    def test_write_keeps_unknown_frontmatter(self):
        v = self.call("memory_read", paths=["global/topics/nix.md"])["files"][0]["version"]
        self.assertTrue(self.call("memory_write", path="global/topics/nix.md", description="Nix", body="- new\n", if_version=v)["ok"])
        self.assertIn("obsidian:\n  cssclass: wide\n", self.content("global/topics/nix.md"))

    def test_conflict_returns_current(self):
        result = self.call("memory_write", path="global/topics/nix.md", description="x", body="y", if_version="new")
        self.assertEqual(result["error"]["code"], "conflict")
        self.assertEqual(result["current"]["path"], "global/topics/nix.md")
        self.assertIn("- flakes", result["current"]["content"])
        self.assertEqual(result["current"]["version"], version(self.content("global/topics/nix.md").encode()))

    def test_out_of_scope_and_read_only(self):
        result = self.call("memory_write", path="proj-1/topics/x.md", description="x", body="y", if_version="new")
        self.assertEqual(result["error"]["code"], "out_of_scope")
        self.assertEqual(result["allowed_prefixes"], ["global/"])
        cron = self.call("memory_append", context="cron", path="global/topics/nix.md", lines=["x"])
        self.assertEqual(cron["error"]["code"], "read_only")

    def test_project_session_writes(self):
        self.assertTrue(self.call("memory_append", project="proj-1", path="proj-1/topics/deploy.md", lines=["uses flakes"])["ok"])
        self.assertTrue(self.call("memory_write", project="proj-1", path="global/people/sam.md", description="Sam",
                                  body="- x", if_version="new")["ok"])
        self.assertTrue(self.call("memory_append", project="proj-1", path="global/topics/nix.md", lines=["x"])["ok"])
        self.assertEqual(self.call("memory_append", project="proj-1", path="clogpt/topics/x.md", lines=["x"])["error"]["code"],
                         "out_of_scope")

    def test_every_edit_stamps_edited_at(self):
        import re
        from unittest import mock
        stamp_re = r"(?m)^edited_at: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"
        self.assertNotIn("edited_at", self.content("global/topics/nix.md"))
        made = self.call("memory_write", path="global/topics/tea.md", description="Tea", body="- earl grey", if_version="new")
        self.assertTrue(made["ok"], made)
        self.assertRegex(self.content("global/topics/tea.md"), stamp_re)
        for name, args in [("memory_append", {"lines": ["x"]}),
                           ("memory_str_replace", {"old": "- flakes", "new": "- flakes pin inputs"})]:
            with mock.patch("memory_buckets.store.datetime") as dt:
                dt.now.return_value.strftime.return_value = "2031-01-02T03:04:05Z"
                self.assertTrue(self.call(name, path="global/topics/nix.md", **args)["ok"])
            self.assertIn("edited_at: 2031-01-02T03:04:05Z\n", self.content("global/topics/nix.md"), name)
        self.assertEqual(self.content("global/topics/nix.md").count("edited_at"), 1)
        self.assertIn("  cssclass: wide", self.content("global/topics/nix.md"), "other keys untouched")

    def test_str_replace(self):
        ok = self.call("memory_str_replace", path="global/topics/nix.md", old="- flakes", new="- flakes pin inputs")
        self.assertTrue(ok["ok"], ok)
        self.assertIn("- flakes pin inputs\n", self.content("global/topics/nix.md"))
        self.assertIn("  - telegram", self.content("global/topics/nix.md"))
        self.assertEqual(self.call("memory_str_replace", path="global/topics/nix.md", old="zzz", new="y")["error"]["code"], "no_match")
        self.call("memory_append", path="global/topics/nix.md", lines=["dup", "dup"])
        self.assertEqual(self.call("memory_str_replace", path="global/topics/nix.md", old="dup", new="y")["error"]["code"],
                         "ambiguous_match")
        self.assertEqual(self.call("memory_str_replace", path="global/topics/nix.md", old="name: nix", new="name: x")["error"]["code"],
                         "no_match", "frontmatter isn't editable through str_replace")

    def test_append(self):
        self.assertTrue(self.call("memory_append", path="global/topics/nix.md", lines=["a", "- b", "multi\nline"])["ok"])
        self.assertTrue(self.content("global/topics/nix.md").endswith("- flakes\n- a\n- b\n- multi line\n"))
        self.assertEqual(self.call("memory_append", path="global/topics/new.md", lines=["a"])["error"]["code"], "not_found")

    def call_notified(self, name, shown, project=None, **args):
        """Call with a ``notify`` that records messages and returns ``shown``."""
        c = config.Config.from_dict(args.pop("cfg", {}))
        seen = []
        ctx = tools.Context(self.store, self.index, scopes.resolve(c, project), c, source="cli",
                            notify=(lambda m: seen.append(m) or shown) if shown is not None else None)
        return tools.handle(name, args, ctx), seen

    def test_inbox_append_creates_it_and_tells_the_user(self):
        got, seen = self.call_notified("memory_append", True, path="global/inbox.md", lines=["ex-luna: uses a SID"])
        self.assertTrue(got["ok"], got)
        self.assertEqual(seen, ["📥 Memory inbox: 1 new entry (global/inbox.md)"])
        self.assertNotIn("next", got, "the user was told directly; the agent mustn't repeat it")
        text = self.content("global/inbox.md")
        self.assertIn("name: inbox\ndescription: Memory waiting to be sorted", text)
        self.assertRegex(text, r"## added in conversation \d{4}-\d\d-\d\d \(cli\)\n- ex-luna: uses a SID\n$")
        # A second append joins the same section.
        self.call_notified("memory_append", True, path="global/inbox.md", lines=["a", "b"])
        self.assertTrue(self.content("global/inbox.md").endswith("- ex-luna: uses a SID\n- a\n- b\n"))

    def test_inbox_append_without_a_status_channel_asks_the_agent(self):
        for notify in (None, False):  # no callback (gateways), or one that couldn't show it
            got, _ = self.call_notified("memory_append", notify, path="global/inbox.md", lines=["x"])
            self.assertIn("Tell the user in one line", got["next"])

    def test_inbox_without_frontmatter_is_repaired_on_append(self):
        # Hand-written (e.g. in Obsidian): every append and mirrored write used to fail.
        (self.store.memories / "global/inbox.md").write_text("- jotted down by hand\n")
        got, _ = self.call_notified("memory_append", True, path="global/inbox.md", lines=["x"])
        self.assertTrue(got["ok"], got)
        text = self.content("global/inbox.md")
        self.assertTrue(text.startswith("---\nname: inbox\ndescription: Memory waiting to be sorted"), text)
        self.assertIn("- jotted down by hand\n", text)
        self.assertTrue(text.endswith("- x\n"))
        (self.store.memories / "global/inbox.md").write_text("- by hand again\n")
        self.assertIsNotNone(inbox.append(self.store, "imported MEMORY.md", ["y"], source="cli:import"))
        self.assertIn("name: inbox", self.content("global/inbox.md"))

    def test_unparseable_inbox_is_refused_not_clobbered(self):
        broken = "---\nname: inbox\n- no closing fence\n"
        (self.store.memories / "global/inbox.md").write_text(broken)
        got, _ = self.call_notified("memory_append", True, path="global/inbox.md", lines=["x"])
        self.assertEqual(got["error"]["code"], "invalid_frontmatter")
        with self.assertRaises(StoreError) as caught:
            inbox.append(self.store, "imported", ["y"], source="cli:import")
        self.assertEqual(caught.exception.code, "invalid_frontmatter")
        self.assertEqual((self.store.memories / "global/inbox.md").read_text(), broken)

    def test_inbox_heading_inside_a_code_block_is_not_a_section(self):
        self.call_notified("memory_append", True, path="global/inbox.md", lines=["a"])
        heading = self.content("global/inbox.md").split("\n## ", 1)[1].split("\n", 1)[0]
        path = self.store.memories / "global/inbox.md"
        path.write_text(path.read_text() + "```\n## not a heading\n- not an entry\n```\n")
        self.call_notified("memory_append", True, path="global/inbox.md", lines=["b"])
        text = self.content("global/inbox.md")
        self.assertEqual(text.count(f"## {heading}"), 1, text)
        self.assertEqual(inbox.count_entries(text.split("\n---\n", 1)[1]), 2)

    def test_no_tool_can_edit_a_file_with_broken_frontmatter(self):
        # Spec for the header's "tell the user" line: every write path refuses the file.
        (self.store.memories / "global/topics/nix.md").write_text("- flakes\n")
        got = self.call("memory_append", path="global/topics/nix.md", lines=["x"])
        self.assertEqual(got["error"]["code"], "invalid_frontmatter")
        got = self.call("memory_write", path="global/topics/nix.md", description="Read for Nix",
                        body="- flakes\n", if_version="new")
        self.assertEqual(got["error"]["code"], "conflict")
        current = (self.store.memories / "global/topics/nix.md").read_bytes()
        got = self.call("memory_str_replace", path="global/topics/nix.md", old="- flakes", new="- y",
                        if_version=version(current))
        self.assertEqual(got["error"]["code"], "invalid_frontmatter")
        self.assertEqual((self.store.memories / "global/topics/nix.md").read_text(), "- flakes\n")

    def test_a_broken_status_channel_never_fails_the_write(self):
        c = config.Config()
        ctx = tools.Context(self.store, self.index, scopes.resolve(c), c, source="cli",
                            notify=lambda m: bool(1 / 0))
        got = tools.handle("memory_append", {"path": "global/inbox.md", "lines": ["x"]}, ctx)
        self.assertTrue(got["ok"], got)
        self.assertIn("next", got)

    def test_confined_sessions_may_only_append_to_the_inbox(self):
        cfg = {"write_policy": "confined"}
        got, _ = self.call_notified("memory_append", True, project="proj-1", cfg=cfg,
                                    path="global/inbox.md", lines=["unsure"])
        self.assertTrue(got["ok"], got)
        got, _ = self.call_notified("memory_write", True, project="proj-1", cfg=cfg, path="global/inbox.md",
                                    description="x", body="- gone", if_version=version(self.content("global/inbox.md").encode()))
        self.assertEqual(got["error"]["code"], "out_of_scope")
        self.assertIn("may only append to global/inbox.md", got["error"]["message"])
        got, _ = self.call_notified("memory_append", True, project="proj-1", cfg=cfg,
                                    path="global/topics/nix.md", lines=["x"])
        self.assertEqual(got["error"]["code"], "out_of_scope")

    def test_delete(self):
        v = version(self.content("global/topics/nix.md").encode())
        self.assertEqual(self.call("memory_delete", path="global/topics/nix.md", if_version="new")["error"]["code"], "invalid_args")
        self.assertTrue(self.call("memory_delete", path="global/topics/nix.md", if_version=v)["ok"])

    def test_search(self):
        found = self.call("memory_search", query="flakes")
        self.assertEqual(found["mode"], "fts")
        self.assertEqual(found["results"][0]["path"], "global/topics/nix.md")
        self.assertEqual(self.call("memory_search", query="colmena")["results"], [], "other projects not by default")
        self.assertEqual(self.call("memory_search", query="colmena", prefix="proj-1/")["results"][0]["path"],
                         "proj-1/topics/deploy.md")
        self.assertEqual(self.call("memory_search", query="x", limit=99)["error"]["code"], "invalid_args")

    def test_refuses_to_rewrite_unparseable_frontmatter(self):
        self.put("global/topics/broken.md", "---\nname: broken\n: nope\n---\n- a\n")
        result = self.call("memory_append", path="global/topics/broken.md", lines=["b"])
        self.assertEqual(result["error"]["code"], "invalid_frontmatter")

    def test_bad_args(self):
        self.assertEqual(self.call("memory_write", path="global/topics/x.md")["error"]["code"], "invalid_args")
        self.assertEqual(self.call("memory_nope")["error"]["code"], "invalid_args")


if __name__ == "__main__":
    unittest.main()
