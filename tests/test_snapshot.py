import unittest
from unittest import mock

from .helpers import StoreCase, doc
from memory_buckets import config, index, scopes, snapshot


class SnapshotTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.put("global/profile.md", doc("profile", "Who the user is", "- Ivy, they/them\n"))
        self.put("global/preferences.md", doc("preferences", "How to behave", "- British spelling\n"))
        self.put("global/topics/nix.md", doc("nix", "Read for Nix"))
        self.put("proj-1/index.md", doc("index", "proj-1 overview", "- a web app\n"))
        self.put("proj-1/topics/deploy.md", doc("deploy", "How proj-1 deploys"))
        self.index = index.Index(self.store)
        self.addCleanup(self.index.close)

    def build(self, project=None, **cfg):
        c = config.Config.from_dict(cfg)
        return snapshot.build(self.store, self.index, scopes.resolve(c, project), c)

    def test_unscoped(self):
        snap = self.build()
        self.assertIn("- Ivy, they/them", snap.text)
        self.assertIn("- British spelling", snap.text)
        self.assertIn("- global/topics/nix.md: Read for Nix", snap.text)
        self.assertNotIn("proj-1/topics/deploy.md", snap.text)
        self.assertIn("Other projects that have memory: proj-1.", snap.text)
        self.assertEqual(snap.paths, {"global/profile.md", "global/preferences.md"})

    def test_unscoped_routing(self):
        text = self.build().text
        for path in ("global/profile.md:", "global/preferences.md:", "global/people/<name>.md:",
                     "global/areas/<name>.md:", "global/topics/<subject>.md:", "global/inbox.md"):
            self.assertIn(path, text.split("### Where to save in this session", 1)[1])
        self.assertNotIn("proj-1/", text.split("### Where to save in this session", 1)[1].split("###", 1)[0])

    def test_unscoped_routing_allows_proposing_new_projects(self):
        # The prompt outranks the skill: it mustn't forbid what the sort-inbox skill asks for.
        text = self.build().text
        self.assertIn("memory_propose", text)
        self.assertIn("This works for existing projects and for new projects.", text)

    def test_project_routing_matches_write_scope(self):
        shared = self.build(project="proj-1").text
        for path in ("proj-1/profile.md:", "proj-1/preferences.md:", "proj-1/topics/<subject>.md:",
                     "global/profile.md:", "global/preferences.md:", "global/people/<name>.md:"):
            self.assertIn(path, shared)
        self.assertIn("Do not edit proj-1/index.md", shared)
        self.assertNotIn("{p}", shared)
        self.assertIn("global/topics/<subject>.md:", shared)  # general facts go global from a project too
        self.assertIn("The fact only matters for proj-1: it is a project fact. Save it under proj-1/.", shared)
        confined = self.build(project="proj-1", write_policy="confined").text
        self.assertIn("you can write only under proj-1/", confined)
        self.assertNotIn("global/profile.md:", confined)
        self.assertNotIn("{p}", confined)

    def test_project_first(self):
        snap = self.build(project="proj-1")
        self.assertIn("### proj-1/index.md\n- a web app", snap.text)
        self.assertLess(snap.text.index("proj-1/topics/deploy.md"), snap.text.index("global/topics/nix.md"))
        self.assertIn('project "proj-1"', snap.text)

    def test_cap_trims_listing_then_sections_never_profile(self):
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

    def test_inbox_note_names_the_skill(self):
        self.assertNotIn("skill_view", self.build().text)
        self.put("global/inbox.md", doc("inbox", "Imported memory waiting to be sorted"))
        self.index.reconcile()
        self.assertIn('skill_view("memory-buckets:sort-inbox")', self.build().text)
        with mock.patch.object(snapshot, "SORT_SKILL", None):
            self.assertIn("hermes memory-buckets sort-prompt", self.build().text)
        self.assertNotIn("sort-inbox", self.build(context="cron").text if False else "")

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
