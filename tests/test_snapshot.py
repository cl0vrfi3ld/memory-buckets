import os
import unittest
from unittest import mock

from .helpers import StoreCase, doc
from memory_buckets import config, index, scopes, snapshot
from memory_buckets.projects import Project


def header_of(text, bucket):
    """The generated lines under ``### Project: <bucket>``."""
    return text.split(f"### Project: {bucket}\n", 1)[1].split("\n\n", 1)[0]


class SnapshotTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.put("global/profile.md", doc("profile", "Who the user is", "- Ivy, they/them\n"))
        self.put("global/preferences.md", doc("preferences", "How to behave", "- British spelling\n"))
        self.put("global/topics/nix.md", doc("nix", "Read for Nix"))
        self.put("proj-1/profile.md", doc("profile", "What proj-1 is", "- a web app\n"))
        self.put("proj-1/preferences.md", doc("preferences", "How to work on proj-1", "- run the linter first\n"))
        self.put("proj-1/topics/deploy.md", doc("deploy", "How proj-1 deploys"))
        self.index = index.Index(self.store)
        self.addCleanup(self.index.close)

    def build(self, project=None, hermes=None, context="primary", hermes_buckets=None, **cfg):
        c = config.Config.from_dict(cfg)
        return snapshot.build(self.store, self.index, scopes.resolve(c, project, context), c, hermes,
                              hermes_buckets=hermes_buckets)

    @staticmethod
    def section(text, heading):
        """The text under ``### <heading>``, up to the next heading."""
        return text.split(f"### {heading}\n", 1)[1].split("\n###", 1)[0]

    def test_unscoped(self):
        snap = self.build()
        self.assertIn("- Ivy, they/them", snap.text)
        self.assertIn("- British spelling", snap.text)
        self.assertIn("- global/topics/nix.md: Read for Nix", snap.text)
        self.assertNotIn("proj-1/topics/deploy.md", snap.text)
        self.assertNotIn("### Project:", snap.text)
        self.assertIn("Projects with memory: proj-1. Read their files only when the user asks", snap.text)
        self.assertEqual(snap.paths, {"global/profile.md", "global/preferences.md"})

    def test_unscoped_routing(self):
        where = self.section(self.build().text, "Where facts go")
        self.assertIn("This session is not in a project. You can write only under global/.", where)
        for path in ("global/profile.md:", "global/preferences.md:", "global/people/<name>.md:",
                     "global/areas/<name>.md:", "global/topics/<subject>.md:"):
            self.assertIn(path, where)
        self.assertIn("When you are not sure where a fact goes, append it to global/inbox.md.", where)
        self.assertNotIn("proj-1/", where)
        self.assertNotIn("Their role in a project", where)

    def test_facts_for_another_project(self):
        # Ivy's call: write the fact to the inbox, then propose it from there.
        steps = self.section(self.build().text, "Facts for another project")
        self.assertIn('1. Append the fact to global/inbox.md as "<project>: <fact>".', steps)
        self.assertIn("2. Call memory_propose for that project. Copy the inbox line exactly into inbox_lines.", steps)
        self.assertIn("Projects: proj-1.", steps)
        # Hermes projects with no memory yet can be proposed into; the session's own project can't.
        steps = self.section(self.build(project="proj-1", hermes_buckets=["proj-1", "nixos-config"]).text,
                             "Facts for another project")
        self.assertIn("Projects: nixos-config.", steps)
        self.assertIn("Projects: none yet.", self.build(project="proj-1").text)

    def test_project_routing_matches_write_scope(self):
        shared = self.build(project="proj-1").text
        where = self.section(shared, "Where facts go")
        for path in ("proj-1/profile.md:", "proj-1/preferences.md:", "proj-1/topics/<subject>.md:",
                     "global/profile.md:", "global/preferences.md:", "global/people/<name>.md:"):
            self.assertIn(path, where)
        self.assertNotIn("index.md", shared)
        self.assertNotIn("{p}", shared)
        self.assertIn("Save each fact once, in one place:", where)
        self.assertIn("When a fact only matters to proj-1, save it under proj-1/.", where)
        self.assertIn("When a fact is also true outside proj-1, save it under global/ only.", where)
        self.assertIn("in proj-1/preferences.md.", where)  # the split example names the project
        confined = self.build(project="proj-1", write_policy="confined").text
        where = self.section(confined, "Where facts go")
        self.assertIn("You can write only under proj-1/, and append to global/inbox.md.", where)
        self.assertIn("When a fact is also true outside proj-1, do not save it.", where)
        self.assertNotIn("global/profile.md:", where)
        self.assertNotIn("{p}", confined)

    def test_read_only_sessions_get_no_save_rules(self):
        text = self.build(project="proj-1", context="cron").text
        self.assertIn("Memory is read-only in this session (cron context). Read it; do not try to save.", text)
        for gone in ("### Saving", "### Where facts go", "### Facts for another project", "### Inbox"):
            self.assertNotIn(gone, text)

    def test_prompt_style(self):
        # Short lines for small models: no instruction line over 40 words, in any session type.
        for kw in ({}, {"project": "proj-1"}, {"project": "proj-1", "write_policy": "confined"}):
            rules = self.build(**kw).text.split("### global/profile.md", 1)[0]
            longest = max(rules.splitlines(), key=lambda line: len(line.split()))
            self.assertLessEqual(len(longest.split()), 40, longest)

    def test_project_index_inlines_the_projects_files(self):
        snap = self.build(project="proj-1")
        text = snap.text
        self.assertIn("### proj-1/profile.md\n- a web app", text)
        self.assertIn("### proj-1/preferences.md\n- run the linter first", text)
        self.assertEqual(snap.paths, {"global/profile.md", "global/preferences.md",
                                      "proj-1/profile.md", "proj-1/preferences.md"})
        # Inlined files aren't listed again; the rest of the bucket is, before global files.
        listing = text.split("### Other files", 1)[1]
        self.assertNotIn("proj-1/profile.md", listing)
        self.assertLess(listing.index("proj-1/topics/deploy.md"), listing.index("global/topics/nix.md"))
        # Global files, then the project header, then the project's files.
        self.assertLess(text.index("### global/preferences.md"), text.index("### Project: proj-1"))
        self.assertLess(text.index("### Project: proj-1"), text.index("### proj-1/profile.md"))
        self.assertIn("This session is in the project proj-1.", text)

    def test_project_header_from_hermes(self):
        home = os.path.expanduser("~")
        hermes = Project(bucket="proj-1", slug="proj_1", name="Project One", description="A web app for Sam",
                         primary_path=f"{home}/src/proj-1", folders=(f"{home}/src/proj-1", "/srv/proj-1"))
        self.assertEqual(header_of(self.build(project="proj-1", hermes=hermes).text, "proj-1"),
                         "Hermes project name: Project One\n"
                         "Hermes project description: A web app for Sam\n"
                         "Folders: ~/src/proj-1 (primary), /srv/proj-1")

    def test_project_header_skips_what_adds_nothing(self):
        # Name equal to the bucket, no description, one folder: just the folder.
        hermes = Project(bucket="proj-1", slug="proj-1", name="proj-1", folders=("/src/proj-1",))
        self.assertEqual(header_of(self.build(project="proj-1", hermes=hermes).text, "proj-1"),
                         "Folders: /src/proj-1")

    def test_empty_bucket_gets_a_cold_start_line(self):
        self.assertEqual(header_of(self.build(project="fresh").text, "fresh"),
                         "This project has no memory yet. When you learn what the project is, create "
                         "fresh/profile.md with memory_write: its purpose, technology, status, and where things are.")
        self.assertEqual(header_of(self.build(project="fresh", context="cron").text, "fresh"),
                         "This project has no memory yet.")

    def test_bucket_without_a_profile(self):
        self.put("proj-2/topics/x.md", doc("x", "Something about proj-2"))
        self.index.reconcile()
        text = self.build(project="proj-2").text
        self.assertTrue(header_of(text, "proj-2").startswith("proj-2/profile.md does not exist yet. When you learn"))
        self.assertIn("- proj-2/topics/x.md: Something about proj-2", text)

    def test_an_empty_profile_is_not_a_missing_one(self):
        self.put("proj-1/profile.md", doc("profile", "What proj-1 is", ""))
        text = self.build(project="proj-1").text
        self.assertEqual(header_of(text, "proj-1"),
                         "proj-1/profile.md is empty. When you learn what the project is, add to it with "
                         "memory_append: its purpose, technology, status, and where things are.")
        self.assertNotIn("does not exist yet", text)
        # The nudge is a save instruction: read-only sessions don't get it.
        self.assertNotIn("is empty", self.build(project="proj-1", context="cron").text)

    def test_an_unparseable_profile_tells_the_user(self):
        self.put("proj-1/profile.md", "- a web app\n")  # no frontmatter: no tool can edit it
        text = self.build(project="proj-1").text
        self.assertEqual(
            header_of(text, "proj-1"),
            "proj-1/profile.md exists, but its frontmatter is missing or can't be parsed: no memory tool "
            "can edit it. Tell the user in one line to fix it (hermes memory-buckets lint names the problem).")
        self.assertNotIn("does not exist yet", text)
        self.assertIn("### proj-1/profile.md\n- a web app", text, "the content is still readable")
        # The broken line is a fact, not a save instruction: it shows read-only too.
        self.assertIn("can't be parsed", self.build(project="proj-1", context="cron").text)

    def test_cap_trims_listing_then_files_never_profile(self):
        for i in range(200):
            self.put(f"global/topics/t{i}.md", doc(f"t{i}", "A rather long description of this particular file"))
        snap = self.build(snapshot_max_chars=5500)
        self.assertLessEqual(len(snap.text), 5500)
        self.assertIn("more files. Call memory_list", snap.text)
        self.assertIn("- British spelling", snap.text)
        self.put("global/preferences.md", doc("preferences", "How to behave", "- x\n" * 1500))
        self.index.reconcile()
        snap = self.build(snapshot_max_chars=5500)
        self.assertIn("- Ivy, they/them", snap.text)
        self.assertIn("memory_read when they are relevant: global/preferences.md", snap.text)
        self.assertNotIn("global/preferences.md", snap.paths)

    def test_listing_fills_the_cap(self):
        # Regression: the trim assumed ~40 characters a line and cut the listing far below the cap.
        for i in range(1000):
            self.put(f"global/topics/t-{i}.md",
                     doc(f"t-{i}", f"what the user thinks about topic number {i} and how it relates to the rest"))
        self.index.reconcile()
        snap = self.build(snapshot_max_chars=12000)
        self.assertLessEqual(len(snap.text), 12000)
        self.assertGreater(len(snap.text), 12000 - 200, "the listing should fill the space up to the cap")

    def test_a_cap_the_full_text_meets_drops_nothing(self):
        # Regression: the search skipped "keep every line" (which also drops the
        # "… and N more" line), then dropped files and still ended up over the cap.
        for i in range(2):
            self.put(f"global/topics/t-{i}.md", doc(f"t-{i}", f"topic {i}"))
        self.index.reconcile()
        full = self.build(snapshot_max_chars=100_000).text
        for cap in (len(full), len(full) + 1):
            with self.subTest(cap=cap):
                snap = self.build(snapshot_max_chars=cap)
                self.assertEqual(snap.text, full)
                self.assertIn("global/preferences.md", snap.paths)

    def test_a_drop_that_grows_the_text_is_skipped(self):
        # A file shorter than the notice that would name it isn't worth dropping.
        self.put("global/preferences.md", doc("preferences", "How to behave", "- x\n"))
        for i in range(300):
            self.put(f"global/topics/t-{i}.md", doc(f"t-{i}", f"what the user thinks about topic {i}"))
        self.index.reconcile()
        snap = self.build(snapshot_max_chars=4000)
        self.assertIn("global/preferences.md", snap.paths)
        self.assertNotIn("too long to show", snap.text)
        self.assertLessEqual(len(snap.text), 4000)

    def test_global_preferences_drop_before_the_projects_own(self):
        # The settled order (drop_order): the global preferences first, the
        # project's own preferences next, its profile last, the global profile never.
        self.put("global/preferences.md", doc("preferences", "How to behave", "- y\n" * 1500))
        self.put("proj-1/preferences.md", doc("preferences", "How to work on proj-1", "- run the linter first\n"))
        self.index.reconcile()
        snap = self.build(project="proj-1", snapshot_max_chars=9000)
        self.assertLessEqual(len(snap.text), 9000)
        self.assertIn("### Project: proj-1", snap.text)
        self.assertIn("memory_read when they are relevant: global/preferences.md\n", snap.text)
        self.assertNotIn("global/preferences.md", snap.paths)
        self.assertIn("proj-1/preferences.md", snap.paths, "the project's own preferences outlast the global ones")
        self.assertIn("### proj-1/profile.md", snap.text)
        # Both too big: both go, the profile still stays.
        self.put("proj-1/preferences.md", doc("preferences", "How to work on proj-1", "- x\n" * 1500))
        self.index.reconcile()
        snap = self.build(project="proj-1", snapshot_max_chars=9000)
        self.assertLessEqual(len(snap.text), 9000)
        self.assertIn("relevant: global/preferences.md, proj-1/preferences.md", snap.text)
        self.assertIn("### proj-1/profile.md", snap.text, "the profile is kept the longest")

    def test_inbox_note_names_the_skill(self):
        self.assertNotIn("skill_view", self.build().text)
        self.put("global/inbox.md", doc("inbox", "Imported memory waiting to be sorted"))
        self.index.reconcile()
        self.assertIn('skill_view("memory-buckets:sort-inbox")', self.build().text)
        with mock.patch.object(snapshot, "SORT_SKILL", None):
            self.assertIn("hermes memory-buckets sort-prompt", self.build().text)

    def test_empty_store(self):
        snap = self.build()
        self.assertIn("## Memory", snap.text)


class EmptySnapshotTest(StoreCase):
    def test_no_files_at_all(self):
        ix = index.Index(self.store)
        self.addCleanup(ix.close)
        c = config.Config()
        snap = snapshot.build(self.store, ix, scopes.resolve(c), c)
        self.assertTrue(snap.text.startswith("## Memory"))
        self.assertEqual(snap.paths, set())


if __name__ == "__main__":
    unittest.main()
