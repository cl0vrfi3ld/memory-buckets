import json
import unittest

from .helpers import StoreCase, doc
from memory_buckets import config, frontmatter, inbox, index, scopes, tools
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
        ctx = tools.Context(self.store, self.index,
                            scopes.resolve(c, project, context, buckets=["clogpt", "proj-1"]), c,
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
        result = self.call("memory_write", path="unknown-bucket/topics/x.md", description="x", body="y", if_version="new")
        self.assertEqual(result["error"]["code"], "out_of_scope")
        self.assertEqual(result["allowed_prefixes"], ["global/"])
        cron = self.call("memory_append", context="cron", path="global/topics/nix.md", lines=["x"])
        self.assertEqual(cron["error"]["code"], "read_only")

    def test_unscoped_session_writes_into_another_bucket(self):
        # Append to an existing file: allowed, provenance records the cross-project write.
        got = self.call("memory_append", path="proj-1/topics/deploy.md", lines=["also on fly.io"])
        self.assertTrue(got["ok"], got)
        text = self.content("proj-1/topics/deploy.md")
        self.assertIn("- also on fly.io\n", text)
        self.assertIn("sources:\n  - telegram@*\n", text)
        # Create a new file: allowed.
        made = self.call("memory_write", path="proj-1/topics/notes.md", description="Notes",
                         body="- x", if_version="new")
        self.assertTrue(made["ok"], made)
        self.assertIn("sources:\n  - telegram@*\n", self.content("proj-1/topics/notes.md"))
        # Edit and delete: refused, and the error points at memory_append.
        got = self.call("memory_str_replace", path="proj-1/topics/deploy.md", old="- colmena", new="- x")
        self.assertEqual(got["error"]["code"], "out_of_scope")
        self.assertIn("memory_append", got["error"]["message"])
        got = self.call("memory_write", path="proj-1/topics/deploy.md", description="x", body="- y",
                        if_version=version(self.content("proj-1/topics/deploy.md").encode()))
        self.assertEqual(got["error"]["code"], "out_of_scope")
        self.assertIn("memory_append", got["error"]["message"])
        v = version(self.content("proj-1/topics/deploy.md").encode())
        got = self.call("memory_delete", path="proj-1/topics/deploy.md", if_version=v)
        self.assertEqual(got["error"]["code"], "out_of_scope")

    def test_project_session_writes(self):
        self.assertTrue(self.call("memory_append", project="proj-1", path="proj-1/topics/deploy.md",
                                  lines=["uses flakes"])["ok"])
        self.assertTrue(self.call("memory_write", project="proj-1", path="global/people/sam.md", description="Sam",
                                  body="- x", if_version="new")["ok"])
        self.assertTrue(self.call("memory_append", project="proj-1", path="global/topics/nix.md", lines=["x"])["ok"])
        self.assertEqual(self.call("memory_append", project="proj-1", path="clogpt/topics/x.md",
                                   lines=["x"])["error"]["code"],
                         "not_found", "clogpt is not a bucket in this store")

    def test_cross_project_write_from_a_project_session(self):
        self.put("clogpt/topics/x.md", doc("x", "X", "- a\n"))
        self.index.reconcile()
        got, seen = self.call_notified("memory_append", True, project="proj-1",
                                       path="clogpt/topics/x.md", lines=["b"])
        self.assertTrue(got["ok"], got)
        self.assertEqual(seen, ["📝 Saved to clogpt/topics/x.md"])
        self.assertIn("sources:\n  - cli@proj-1\n", self.content("clogpt/topics/x.md"))
        # A write inside the session's own scope stays quiet and unadorned.
        got, seen = self.call_notified("memory_append", True, project="proj-1",
                                       path="proj-1/topics/deploy.md", lines=["x"])
        self.assertTrue(got["ok"], got)
        self.assertEqual(seen, [])
        self.assertIn("  - cli\n", self.content("proj-1/topics/deploy.md"))

    def test_cross_project_status_line_without_a_channel_adds_no_next(self):
        # D5: the status line is best-effort; unlike the inbox notice it never
        # falls back to asking the agent to relay it.
        self.put("clogpt/topics/x.md", doc("x", "X", "- a\n"))
        self.index.reconcile()
        for notify in (None, False):
            got, _ = self.call_notified("memory_append", notify, project="proj-1",
                                        path="clogpt/topics/x.md", lines=["x"])
            self.assertTrue(got["ok"], got)
            self.assertNotIn("next", got)

    def test_at_star_source_round_trips_through_frontmatter(self):
        # `tui@*` (a cross-project write from an unscoped session) must survive a
        # write/read cycle: written as YAML, parsed back to the same string.
        got = self.call("memory_write", path="proj-1/topics/fresh.md", description="Fresh",
                        body="- x", if_version="new")
        self.assertTrue(got["ok"], got)
        fm, body = frontmatter.parse(self.content("proj-1/topics/fresh.md"))
        self.assertEqual(fm.get("sources"), ["telegram@*"])

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
        ctx = tools.Context(self.store, self.index,
                            scopes.resolve(c, project, buckets=["clogpt", "proj-1"]), c, source="cli",
                            notify=(lambda m: seen.append(m) or shown) if shown is not None else None)
        return tools.handle(name, args, ctx), seen

    def test_append_to_the_inbox_is_refused(self):
        refusal = "the inbox only holds imported memory; save the fact where it belongs."
        for project in (None, "proj-1"):
            with self.subTest(project=project, inbox="missing"):
                got = self.call("memory_append", project=project, path="global/inbox.md", lines=["ex-luna: uses a SID"])
                self.assertEqual(got["error"], {"code": "invalid_args", "message": refusal})
                self.assertFalse((self.store.memories / "global/inbox.md").exists(), "refusing must not create it")
        original = doc("inbox", inbox.DESCRIPTION, "## imported USER.md\n- Ivy\n")
        self.put("global/inbox.md", original)
        for project in (None, "proj-1"):
            with self.subTest(project=project, inbox="present"):
                got = self.call("memory_append", project=project, path="global/inbox.md", lines=["x"])
                self.assertEqual(got["error"]["message"], refusal)
        self.assertEqual(self.content("global/inbox.md"), original)
        self.assertEqual(self.written, [])

    def test_the_inbox_description_does_not_offer_it_as_a_target(self):
        schemas = {s["name"]: s for s in tools.SCHEMAS}
        self.assertNotIn("inbox", json.dumps(schemas["memory_append"]))
        self.assertNotIn("inbox", json.dumps(schemas["memory_write"]))

    def test_inbox_lines_can_be_removed_with_str_replace(self):
        # What the sort-imports skill does for every entry it files or drops.
        self.put("global/inbox.md", doc("inbox", inbox.DESCRIPTION, "## imported USER.md\n- Ivy\n- Uses tea\n"))
        got = self.call("memory_str_replace", path="global/inbox.md", old="- Ivy\n", new="")
        self.assertTrue(got["ok"], got)
        text = self.content("global/inbox.md")
        self.assertTrue(text.endswith("## imported USER.md\n- Uses tea\n"), text)
        self.assertNotIn("- Ivy", text)
        self.assertEqual(inbox.count_entries(text.split("\n---\n", 1)[1]), 1)
        # Project sessions write global/ in full, so they can sort too.
        self.assertTrue(self.call("memory_str_replace", project="proj-1", path="global/inbox.md",
                                  old="- Uses tea\n", new="")["ok"])

    def test_inbox_without_frontmatter_is_repaired_on_import(self):
        # Hand-written (e.g. in Obsidian): every import and mirrored write used to fail.
        (self.store.memories / "global/inbox.md").write_text("- jotted down by hand\n")
        self.assertIsNotNone(inbox.append(self.store, "imported MEMORY.md", ["y"], source="cli:import"))
        text = self.content("global/inbox.md")
        self.assertTrue(text.startswith(f"---\nname: inbox\ndescription: {inbox.DESCRIPTION}"), text)
        self.assertIn("- jotted down by hand\n", text)
        self.assertTrue(text.endswith("## imported MEMORY.md\n- y\n"), text)

    def test_unparseable_inbox_is_refused_not_clobbered(self):
        broken = "---\nname: inbox\n- no closing fence\n"
        (self.store.memories / "global/inbox.md").write_text(broken)
        with self.assertRaises(StoreError) as caught:
            inbox.append(self.store, "imported", ["y"], source="cli:import")
        self.assertEqual(caught.exception.code, "invalid_frontmatter")
        got = self.call("memory_str_replace", path="global/inbox.md", old="no closing", new="x")
        self.assertEqual(got["error"]["code"], "invalid_frontmatter")
        self.assertEqual((self.store.memories / "global/inbox.md").read_text(), broken)

    def test_inbox_heading_inside_a_code_block_is_not_a_section(self):
        inbox.append(self.store, "imported MEMORY.md", ["a"], source="cli:import")
        path = self.store.memories / "global/inbox.md"
        path.write_text(path.read_text() + "```\n## not a heading\n- not an entry\n```\n")
        inbox.append(self.store, "imported MEMORY.md", ["b"], source="cli:import")
        text = self.content("global/inbox.md")
        self.assertEqual(text.count("## imported MEMORY.md"), 1, "a fenced heading is not the last section")
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
        self.put("clogpt/topics/x.md", doc("x", "X", "- a\n"))
        self.index.reconcile()
        c = config.Config()
        ctx = tools.Context(self.store, self.index, scopes.resolve(c, buckets=["clogpt", "proj-1"]), c, source="cli",
                            notify=lambda m: bool(1 / 0))
        got = tools.handle("memory_append", {"path": "clogpt/topics/x.md", "lines": ["x"]}, ctx)
        self.assertTrue(got["ok"], got)
        self.assertNotIn("next", got)

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
