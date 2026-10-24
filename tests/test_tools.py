import json
import unittest
from unittest import mock

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
                            scopes.resolve(c, project, context, buckets=["addon-2", "proj-1"]), c,
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
        # A file under the cap reads whole, exactly as before: no paging fields.
        small = self.call("memory_read", paths=["global/topics/nix.md"])["files"][0]
        self.assertNotIn("truncated", small)
        self.assertNotIn("total_bytes", small)

    def test_a_file_over_the_write_cap_reads_in_pages(self):
        # Issue 17: MAX_FILE_BYTES is checked on writes only, so a hand-edited or
        # synced file can be any size; one read must not flood the context.
        body = "- fact\n" * 720_000  # about 5 MB
        self.put("global/topics/big.md", doc("big", "Read for a big topic", body))
        size = (self.store.memories / "global/topics/big.md").stat().st_size
        first = self.call("memory_read", paths=["global/topics/big.md"])["files"][0]
        self.assertTrue(first["truncated"])
        self.assertEqual(first["total_bytes"], size)
        self.assertLessEqual(len(first["content"].encode("utf-8")), tools.MAX_FILE_BYTES)
        # Paging: next_offset feeds the next read until the file is done.
        offset, pages = first["next_offset"], [first["content"]]
        while True:
            entry = self.call("memory_read", paths=["global/topics/big.md"], offset=offset)["files"][0]
            pages.append(entry["content"])
            if not entry.get("truncated"):
                self.assertEqual(entry["total_bytes"], size)
                break
            self.assertEqual(entry["next_offset"], offset + tools.MAX_FILE_BYTES)
            offset = entry["next_offset"]
        self.assertEqual("".join(pages), self.store.read_bytes("global/topics/big.md").decode("utf-8"))

    def test_a_page_never_cuts_inside_a_letter(self):
        self.put("global/topics/cafe.md", doc("cafe", "Read for accents", "café " * 40))
        whole = self.store.read_bytes("global/topics/cafe.md").decode("utf-8")
        with mock.patch.object(tools, "MAX_FILE_BYTES", 10):
            offset, pages = 0, []
            while True:
                entry = self.call("memory_read", paths=["global/topics/cafe.md"], offset=offset)["files"][0]
                pages.append(entry["content"])
                if not entry.get("truncated"):
                    break
                offset = entry["next_offset"]
        joined = "".join(pages)
        self.assertEqual(joined, whole)
        self.assertNotIn("\ufffd", joined)

    def test_read_of_a_continuation_byte_file_pages_instead_of_raising(self):
        # A hand-made file whose whole first window is UTF-8 continuation bytes
        # (invalid UTF-8, so only a hand edit or sync can make one) must not
        # walk the page boundary past the start of the file (it raised
        # IndexError), and paging must still make progress.
        path = self.store.memories / "global/topics/garbage.md"
        path.write_bytes(b"\xa9" * 60_000)
        first = self.call("memory_read", paths=["global/topics/garbage.md"])["files"][0]
        self.assertTrue(first["truncated"])
        self.assertEqual(first["total_bytes"], 60_000)
        # Cut at the cap, not past the start of the file: the next page advances.
        self.assertEqual(first["next_offset"], tools.MAX_FILE_BYTES)
        second = self.call("memory_read", paths=["global/topics/garbage.md"],
                           offset=first["next_offset"])["files"][0]
        self.assertFalse(second.get("truncated"))
        self.assertEqual(second["total_bytes"], 60_000)

    def test_bad_read_args(self):
        self.call("memory_read", paths=["global/topics/nix.md"])
        got = self.call("memory_read", paths=["global/topics/nix.md"], offset=-1)
        self.assertEqual(got["error"]["code"], "invalid_args")
        got = self.call("memory_read", paths=["global/topics/nix.md"], offset="one")
        self.assertEqual(got["error"]["code"], "invalid_args")

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

    def test_write_strips_duplicate_leading_frontmatter_in_body(self):
        read_entry = self.call("memory_read", paths=["global/topics/nix.md"])["files"][0]
        full_content = read_entry["content"] + "- nixos\n"
        got = self.call("memory_write", path="global/topics/nix.md", description="Read for Nix",
                        body=full_content, if_version=read_entry["version"])
        self.assertTrue(got["ok"], got)
        text = self.content("global/topics/nix.md")
        self.assertEqual(text.count("\n---\n"), 1, text)
        _, body = frontmatter.parse(text)
        self.assertEqual(body, "- flakes\n- nixos\n")

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
        # An unknown bucket: the refusal points where the prompt does (D2).
        self.assertEqual(result["error"]["message"],
                         "unknown-bucket/topics/x.md is outside what an unscoped session may write; "
                         "unknown-bucket is not a known project, so use global/topics/unknown-bucket.md")
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
        # Creating over an existing file in another project's bucket directs the model to memory_append,
        # not to a versioned overwrite that would fail with out_of_scope.
        conflict = self.call("memory_write", path="proj-1/topics/deploy.md", description="Deploy",
                             body="- x", if_version="new")
        self.assertEqual(conflict["error"]["code"], "conflict")
        self.assertEqual(
            conflict["error"]["message"],
            "proj-1/topics/deploy.md already exists and you may only add to proj-1/; "
            "use memory_append to add new facts to it",
        )
        self.assertIn("- colmena", conflict["current"]["content"])
        # Edit and delete: refused; the error names both add tools and the add-only buckets.
        got = self.call("memory_str_replace", path="proj-1/topics/deploy.md", old="- colmena", new="- x")
        self.assertEqual(got["error"]["code"], "out_of_scope")
        self.assertIn("you may only add to proj-1/", got["error"]["message"])
        self.assertIn("memory_append", got["error"]["message"])
        self.assertIn('memory_write with if_version "new"', got["error"]["message"])
        self.assertEqual(got["allowed_prefixes"], ["global/"])
        self.assertEqual(got["add_only_buckets"], ["addon-2", "proj-1"])
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
        self.assertEqual(self.call("memory_append", project="proj-1", path="addon-2/topics/x.md",
                                   lines=["x"])["error"]["code"],
                         "not_found", "addon-2 is not a bucket in this store")

    def test_cross_project_write_from_a_project_session(self):
        self.put("addon-2/topics/x.md", doc("x", "X", "- a\n"))
        self.index.reconcile()
        got, seen = self.call_notified("memory_append", True, project="proj-1",
                                       path="addon-2/topics/x.md", lines=["b"])
        self.assertTrue(got["ok"], got)
        self.assertEqual(seen, ["📝 Saved to addon-2/topics/x.md"])
        self.assertIn("sources:\n  - cli@proj-1\n", self.content("addon-2/topics/x.md"))
        # A write inside the session's own scope stays quiet and unadorned.
        got, seen = self.call_notified("memory_append", True, project="proj-1",
                                       path="proj-1/topics/deploy.md", lines=["x"])
        self.assertTrue(got["ok"], got)
        self.assertEqual(seen, [])
        self.assertIn("  - cli\n", self.content("proj-1/topics/deploy.md"))

    def test_cross_project_status_line_without_a_channel_adds_no_next(self):
        # D5: the status line is best-effort; unlike the inbox notice it never
        # falls back to asking the agent to relay it.
        self.put("addon-2/topics/x.md", doc("x", "X", "- a\n"))
        self.index.reconcile()
        for notify in (None, False):
            got, _ = self.call_notified("memory_append", notify, project="proj-1",
                                        path="addon-2/topics/x.md", lines=["x"])
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
        fm_err = self.call("memory_str_replace", path="global/topics/nix.md",
                           old="description: Read for Nix", new="description: Nix")["error"]
        self.assertEqual(fm_err["code"], "no_match")
        self.assertEqual(
            fm_err["message"],
            "memory_str_replace edits global/topics/nix.md's body only, not its frontmatter; "
            "use memory_write to change description or aliases",
        )

    def test_str_replace_deletes_whole_bullet_line_and_preserves_trailing_newline(self):
        self.put("global/inbox.md", doc("inbox", inbox.DESCRIPTION, "- Sam\n- Sam studies botany\n- Sam drinks tea\n"))
        got = self.call("memory_str_replace", path="global/inbox.md", old="- Sam studies botany", new="")
        self.assertTrue(got["ok"], got)
        _, body = frontmatter.parse(self.content("global/inbox.md"))
        self.assertEqual(body, "- Sam\n- Sam drinks tea\n")
        got = self.call("memory_str_replace", path="global/inbox.md", old="- Sam", new="")
        self.assertTrue(got["ok"], got)
        _, body = frontmatter.parse(self.content("global/inbox.md"))
        self.assertEqual(body, "- Sam drinks tea\n")
        got = self.call("memory_str_replace", path="global/inbox.md",
                        old="- Sam drinks tea\n", new="- Sam studies botany and photography")
        self.assertTrue(got["ok"], got)
        text = self.content("global/inbox.md")
        self.assertTrue(text.endswith("- Sam studies botany and photography\n"), text)

    def test_str_replace_suffix_deletion_keeps_newline_and_checks_ambiguity(self):
        self.put(
            "global/profile.md",
            doc("profile", "Who the user is", "- Speaks English, Spanish, and a bit of Italian\n- Taking MAND204B\n"),
        )
        got = self.call("memory_str_replace", path="global/profile.md", old=", and a bit of Italian", new="")
        self.assertTrue(got["ok"], got)
        _, body = frontmatter.parse(self.content("global/profile.md"))
        self.assertEqual(body, "- Speaks English, Spanish\n- Taking MAND204B\n")

        self.put(
            "global/topics/nix.md",
            doc("nix", "Read for Nix", "- Uses flakes\n- Uses flakes for servers\n"),
        )
        ambiguous = self.call("memory_str_replace", path="global/topics/nix.md", old="flakes", new="")
        self.assertFalse(ambiguous["ok"])
        self.assertEqual(ambiguous["error"]["code"], "ambiguous_match")
        _, nix_body = frontmatter.parse(self.content("global/topics/nix.md"))
        self.assertEqual(nix_body, "- Uses flakes\n- Uses flakes for servers\n")

    def test_append(self):
        self.assertTrue(self.call("memory_append", path="global/topics/nix.md", lines=["a", "- b", "multi\nline"])["ok"])
        self.assertTrue(self.content("global/topics/nix.md").endswith("- flakes\n- a\n- b\n- multi line\n"))
        self.assertEqual(self.call("memory_append", path="global/topics/new.md", lines=["a"])["error"],
                         {"code": "not_found", "message": "global/topics/new.md doesn't exist; to create it, "
                                                          "use memory_write with if_version \"new\""})

    def test_schema_copy_says_one_thing_one_way(self):
        # The create instruction reads as in the prompt's Saving rules; no sentence runs long.
        described = {s["name"]: s["description"] for s in tools.SCHEMAS}
        self.assertTrue(described["memory_append"].endswith(
            'The file must already exist. To create a file, use memory_write with if_version "new".'))
        self.assertTrue(described["memory_list"].endswith(
            "pass its directory as prefix (for example 'home-server/'). To list every project, "
            "set include_projects to true."))
        schemas = {s["name"]: s["parameters"]["properties"] for s in tools.SCHEMAS}
        self.assertEqual(schemas["memory_write"]["path"]["description"], tools._PATH_HELP)
        self.assertEqual(schemas["memory_read"]["paths"]["description"], tools._EXISTING_PATH_HELP)
        for name in ("memory_str_replace", "memory_append", "memory_delete"):
            self.assertEqual(schemas[name]["path"]["description"], tools._EXISTING_PATH_HELP)

    def call_notified(self, name, shown, project=None, **args):
        """Call with a ``notify`` that records messages and returns ``shown``."""
        c = config.Config.from_dict(args.pop("cfg", {}))
        seen = []
        ctx = tools.Context(self.store, self.index,
                            scopes.resolve(c, project, buckets=["addon-2", "proj-1"]), c, source="cli",
                            notify=(lambda m: seen.append(m) or shown) if shown is not None else None)
        return tools.handle(name, args, ctx), seen

    def test_append_to_the_inbox_is_refused(self):
        refusal = "the inbox only holds imported memory; save the fact where it belongs."
        for project in (None, "proj-1"):
            with self.subTest(project=project, inbox="missing"):
                got = self.call("memory_append", project=project, path="global/inbox.md", lines=["proj-2: uses a cache"])
                self.assertEqual(got["error"], {"code": "out_of_scope", "message": refusal})
                self.assertFalse((self.store.memories / "global/inbox.md").exists(), "refusing must not create it")
        original = doc("inbox", inbox.DESCRIPTION, "## imported USER.md\n- Sam\n")
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
        self.put("global/inbox.md", doc("inbox", inbox.DESCRIPTION, "## imported USER.md\n- Sam\n- Uses tea\n"))
        got = self.call("memory_str_replace", path="global/inbox.md", old="- Sam\n", new="")
        self.assertTrue(got["ok"], got)
        text = self.content("global/inbox.md")
        self.assertTrue(text.endswith("## imported USER.md\n- Uses tea\n"), text)
        self.assertNotIn("- Sam", text)
        self.assertEqual(inbox.count_entries(text.split("\n---\n", 1)[1]), 1)
        # Project sessions write global/ in full, so they can sort too.
        self.assertTrue(self.call("memory_str_replace", project="proj-1", path="global/inbox.md",
                                  old="- Uses tea\n", new="")["ok"])

    def test_inbox_can_be_rewritten_and_deleted_from_a_writable_session(self):
        # Only memory_append is refused on the inbox.
        refusal = "the inbox only holds imported memory; save the fact where it belongs."
        for project in (None, "proj-1"):
            with self.subTest(project=project, tool="memory_write"):
                self.put("global/inbox.md", doc("inbox", inbox.DESCRIPTION, "## imported USER.md\n- Sam\n- Uses tea\n"))
                v = version(self.content("global/inbox.md").encode())
                got = self.call("memory_write", project=project, path="global/inbox.md",
                                description=inbox.DESCRIPTION, body="## imported USER.md\n- Uses tea\n", if_version=v)
                self.assertTrue(got["ok"], got)
                text = self.content("global/inbox.md")
                self.assertTrue(text.startswith(f"---\nname: inbox\ndescription: {inbox.DESCRIPTION}\n"), text)
                self.assertTrue(text.endswith("---\n## imported USER.md\n- Uses tea\n"), text)
                self.assertNotIn("- Sam", text)
                self.assertEqual(self.call("memory_append", project=project, path="global/inbox.md",
                                           lines=["x"])["error"]["code"], "out_of_scope")
            with self.subTest(project=project, tool="memory_delete"):
                self.put("global/inbox.md", doc("inbox", inbox.DESCRIPTION, "## imported USER.md\n"))
                v = version(self.content("global/inbox.md").encode())
                got = self.call("memory_delete", project=project, path="global/inbox.md", if_version=v)
                self.assertTrue(got["ok"], got)
                self.assertFalse((self.store.memories / "global/inbox.md").exists())
                self.assertEqual(self.call("memory_append", project=project, path="global/inbox.md",
                                           lines=["x"])["error"], {"code": "out_of_scope", "message": refusal})
                self.assertFalse((self.store.memories / "global/inbox.md").exists())

    def test_a_deleted_inbox_is_recreated_with_the_current_description(self):
        # The skill says deleting is safe: the import and the mirror both go through inbox.append.
        self.put("global/inbox.md", doc("inbox", "Imported memory waiting to be sorted (old)", "## imported\n- a\n"))
        v = version(self.content("global/inbox.md").encode())
        self.assertTrue(self.call("memory_delete", path="global/inbox.md", if_version=v)["ok"])
        self.assertIsNotNone(inbox.append(self.store, "imported MEMORY.md", ["b"], source="cli:import"))
        text = self.content("global/inbox.md")
        self.assertTrue(text.startswith(f"---\nname: inbox\ndescription: {inbox.DESCRIPTION}\n"), text)
        self.assertTrue(text.endswith("## imported MEMORY.md\n- b\n"), text)
        self.assertNotIn("- a", text)

    def test_inbox_without_frontmatter_is_repaired_on_import(self):
        # Hand-written (e.g. in Obsidian): every import and mirrored write used to fail.
        (self.store.memories / "global/inbox.md").write_text("- jotted down by hand\n", encoding="utf-8")
        self.assertIsNotNone(inbox.append(self.store, "imported MEMORY.md", ["y"], source="cli:import"))
        text = self.content("global/inbox.md")
        self.assertTrue(text.startswith(f"---\nname: inbox\ndescription: {inbox.DESCRIPTION}"), text)
        self.assertIn("- jotted down by hand\n", text)
        self.assertTrue(text.endswith("## imported MEMORY.md\n- y\n"), text)

    def test_unparseable_inbox_is_refused_not_clobbered(self):
        broken = "---\nname: inbox\n- no closing fence\n"
        (self.store.memories / "global/inbox.md").write_text(broken, encoding="utf-8")
        with self.assertRaises(StoreError) as caught:
            inbox.append(self.store, "imported", ["y"], source="cli:import")
        self.assertEqual(caught.exception.code, "invalid_frontmatter")
        got = self.call("memory_str_replace", path="global/inbox.md", old="no closing", new="x")
        self.assertEqual(got["error"]["code"], "invalid_frontmatter")
        self.assertEqual((self.store.memories / "global/inbox.md").read_text(encoding="utf-8"), broken)

    def test_inbox_heading_inside_a_code_block_is_not_a_section(self):
        inbox.append(self.store, "imported MEMORY.md", ["a"], source="cli:import")
        path = self.store.memories / "global/inbox.md"
        path.write_text(path.read_text(encoding="utf-8") + "```\n## not a heading\n- not an entry\n```\n", encoding="utf-8")
        inbox.append(self.store, "imported MEMORY.md", ["b"], source="cli:import")
        text = self.content("global/inbox.md")
        self.assertEqual(text.count("## imported MEMORY.md"), 1, "a fenced heading is not the last section")
        self.assertEqual(inbox.count_entries(text.split("\n---\n", 1)[1]), 2)

    def test_no_tool_can_edit_a_file_with_broken_frontmatter(self):
        # Spec for the header's "tell the user" line: every write path refuses the file.
        (self.store.memories / "global/topics/nix.md").write_text("- flakes\n", encoding="utf-8")
        got = self.call("memory_append", path="global/topics/nix.md", lines=["x"])
        self.assertEqual(got["error"]["code"], "invalid_frontmatter")
        got = self.call("memory_write", path="global/topics/nix.md", description="Read for Nix",
                        body="- flakes\n", if_version="new")
        self.assertEqual(got["error"]["code"], "conflict")
        current = (self.store.memories / "global/topics/nix.md").read_bytes()
        got = self.call("memory_str_replace", path="global/topics/nix.md", old="- flakes", new="- y",
                        if_version=version(current))
        self.assertEqual(got["error"]["code"], "invalid_frontmatter")
        self.assertEqual((self.store.memories / "global/topics/nix.md").read_text(encoding="utf-8"), "- flakes\n")

    def test_unparseable_frontmatter_refusal_names_the_user(self):
        # Only the user can fix frontmatter that won't parse; the refusal says who.
        (self.store.memories / "global/topics/nix.md").write_text("---\nname: nix\n- flakes\n", encoding="utf-8")
        got = self.call("memory_append", path="global/topics/nix.md", lines=["x"])
        self.assertEqual(got["error"]["code"], "invalid_frontmatter")
        self.assertTrue(got["error"]["message"].endswith("; tell the user to fix it by hand"), got)

    def test_a_broken_status_channel_never_fails_the_write(self):
        self.put("addon-2/topics/x.md", doc("x", "X", "- a\n"))
        self.index.reconcile()
        c = config.Config()
        ctx = tools.Context(self.store, self.index, scopes.resolve(c, buckets=["addon-2", "proj-1"]), c, source="cli",
                            notify=lambda m: bool(1 / 0))
        got = tools.handle("memory_append", {"path": "addon-2/topics/x.md", "lines": ["x"]}, ctx)
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

    def test_no_private_adr_references_in_source_comments(self):
        from pathlib import Path
        root = Path(tools.__file__).resolve().parent.parent
        for py_file in sorted([*root.glob("memory_buckets/*.py"), *root.glob("tests/*.py")]):
            self.assertNotRegex(py_file.read_text(encoding="utf-8"), r"A" + r"DR-\d+", f"found private decision ref in {py_file.name}")

    def test_text_io_always_names_its_encoding(self):
        # Without encoding=, Windows reads and writes text as cp1252 (UTF-8 mode is off by default).
        import ast
        from pathlib import Path
        root = Path(tools.__file__).resolve().parent.parent
        bare = []
        for py_file in sorted([*root.glob("memory_buckets/*.py"), *root.glob("tests/*.py"), root / "hatch_build.py"]):
            for node in ast.walk(ast.parse(py_file.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call) or any(k.arg == "encoding" for k in node.keywords):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
                    bare.append(f"{py_file.name}:{node.lineno} {func.attr}")
                elif isinstance(func, ast.Name) and func.id == "open":
                    mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
                    if not (isinstance(mode, ast.Constant) and "b" in str(mode.value)):
                        bare.append(f"{py_file.name}:{node.lineno} open")
        self.assertEqual(bare, [], "text I/O without encoding='utf-8'")


if __name__ == "__main__":
    unittest.main()
