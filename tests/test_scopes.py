import unittest

from .helpers import StoreCase
from memory_buckets import config, scopes
from memory_buckets.store import StoreError


class ScopeTest(StoreCase):
    def resolve(self, project=None, context="primary", **cfg):
        return scopes.resolve(config.Config.from_dict(cfg), project, context)

    def test_unscoped(self):
        scope = self.resolve()
        self.assertIsNone(scope.project)
        self.assertEqual(scope.write_prefixes, ["global/"])
        self.assertTrue(scope.reads_by_default("global/topics/nix.md"))
        self.assertFalse(scope.reads_by_default("proj-1/index.md"))
        with self.assertRaises(StoreError) as caught:
            scope.check_write("proj-1/topics/x.md")
        self.assertEqual(caught.exception.code, "out_of_scope")
        self.assertEqual(caught.exception.fields["allowed_prefixes"], ["global/"])

    def test_project_scopes_the_session(self):
        scope = self.resolve("proj-1")
        self.assertEqual((scope.project, scope.source), ("proj-1", "hermes-project"))
        self.assertEqual(scope.read_prefixes, ["global/", "proj-1/"])
        self.assertIsNone(self.resolve("global").project, "global is never a project")
        self.assertIsNone(self.resolve("../etc").project)

    def test_shared_and_confined_writes(self):
        shared = self.resolve("proj-1")
        for ok in ("proj-1/topics/a.md", "global/profile.md", "global/people/sam.md", "global/topics/nix.md",
                   "global/areas/work.md", "global/inbox.md"):
            shared.check_write(ok)
        for bad in ("clogpt/index.md", "clogpt/topics/x.md"):
            self.assertFalse(shared.can_write(bad), bad)
        confined = self.resolve("proj-1", write_policy="confined")
        self.assertFalse(confined.can_write("global/profile.md"))
        self.assertTrue(confined.can_write("proj-1/index.md"))

    def test_read_only_contexts(self):
        for context in ("cron", "subagent", "flush"):
            with self.assertRaises(StoreError) as caught:
                self.resolve(context=context).check_write("global/profile.md")
            self.assertEqual(caught.exception.code, "read_only")
        self.resolve(context="cron", cron_writes=True).check_write("global/profile.md")
        self.assertTrue(self.resolve(readonly=True).read_only)


if __name__ == "__main__":
    unittest.main()
