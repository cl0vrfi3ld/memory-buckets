import unittest

from .helpers import StoreCase
from memory_buckets import config, scopes
from memory_buckets.store import StoreError

BUCKETS = ["addon-2", "proj-1"]


class ScopeTest(StoreCase):
    def resolve(self, project=None, context="primary", **cfg):
        return scopes.resolve(config.Config.from_dict(cfg), project, context, buckets=BUCKETS)

    def test_unscoped(self):
        scope = self.resolve()
        self.assertIsNone(scope.project)
        self.assertEqual(scope.write_prefixes, ["global/"])
        self.assertEqual(scope.other_buckets, BUCKETS)
        self.assertTrue(scope.reads_by_default("global/topics/nix.md"))
        self.assertFalse(scope.reads_by_default("proj-1/profile.md"))

    def test_project_scopes_the_session(self):
        scope = self.resolve("proj-1")
        self.assertEqual((scope.project, scope.source), ("proj-1", "hermes-project"))
        self.assertEqual(scope.read_prefixes, ["global/", "proj-1/"])
        self.assertEqual(scope.other_buckets, ["addon-2"], "the session's own bucket is not 'other'")
        self.assertIsNone(self.resolve("global").project, "global is never a project")
        self.assertIsNone(self.resolve("../etc").project)

    def test_write_rights_table(self):
        # Unscoped: full in global/, append+create in other buckets.
        unscoped = self.resolve()
        unscoped.check_write("global/topics/nix.md")
        with self.assertRaises(StoreError):
            unscoped.check_write("proj-1/profile.md")
        unscoped.check_write("proj-1/topics/deploy.md", append=True)
        unscoped.check_write("proj-1/topics/new.md", create=True)
        with self.assertRaises(StoreError):
            unscoped.check_write("proj-1/topics/deploy.md")
        with self.assertRaises(StoreError):
            unscoped.check_write("unknown-bucket/topics/x.md", append=True)
        # Project: full in its own bucket and global/, append+create elsewhere.
        scoped = self.resolve("proj-1")
        for path in ("proj-1/topics/deploy.md", "global/inbox.md"):
            scoped.check_write(path)
        scoped.check_write("addon-2/topics/deploy.md", append=True)
        with self.assertRaises(StoreError):
            scoped.check_write("addon-2/topics/deploy.md")
        scoped.check_write("addon-2/profile.md", create=True)
        with self.assertRaises(StoreError):
            scoped.check_write("addon-2/profile.md")

    def test_refused_edit_names_both_add_tools(self):
        scoped = self.resolve("proj-1")
        for check in ({"append": False}, {}):
            with self.assertRaises(StoreError) as caught:
                scoped.check_write("addon-2/topics/deploy.md", **check)
            self.assertEqual(caught.exception.code, "out_of_scope")
            self.assertIn("you may only add to addon-2/", caught.exception.message)
            self.assertIn("memory_append", caught.exception.message)
            self.assertIn("memory_write", caught.exception.message)
            self.assertEqual(caught.exception.fields,
                             {"allowed_prefixes": ["proj-1/", "global/"],
                              "add_only_buckets": ["addon-2"]})

    def test_read_only_contexts(self):
        for context in ("cron", "subagent", "flush"):
            with self.assertRaises(StoreError) as caught:
                self.resolve(context=context).check_write("global/profile.md")
            self.assertEqual(caught.exception.code, "read_only")
            with self.assertRaises(StoreError):
                self.resolve(context=context).check_write("addon-2/topics/deploy.md", append=True)
        self.resolve(context="cron", cron_writes=True).check_write("global/profile.md")
        self.assertTrue(self.resolve(readonly=True).read_only)


class KnownBucketsTest(unittest.TestCase):
    def test_union_is_sorted_and_validated(self):
        self.assertEqual(scopes.known_buckets(["proj-b", "proj-a"], ["proj-c", "global", "bad_slug"]),
                         ["proj-a", "proj-b", "proj-c"])
        self.assertEqual(scopes.known_buckets([], None), [])


if __name__ == "__main__":
    unittest.main()
