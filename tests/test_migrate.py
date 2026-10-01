import os
import unittest
from unittest import mock

from .helpers import StoreCase, doc
from memory_buckets import inbox, migrate
from memory_buckets.store import StoreError

OLD_INBOX_DESCRIPTION = ("Imported memory waiting to be sorted into proper files (migration only); "
                         "sort it, then delete it")  # 0.0.1's default


class IndexMigrationTest(StoreCase):
    def body_of(self, path):
        return self.store.read(path)["content"].split("\n---\n", 1)[1]

    def kept(self, project, name):
        return (migrate.backup_dir(self.store) / project / name).read_text()

    def test_index_becomes_the_profile(self):
        original = doc("index", "proj-1 overview", "- a web app\n- deploys with colmena\n")
        self.put("proj-1/index.md", original)
        steps = migrate.run(self.store)
        self.assertEqual([(s.project, s.action) for s in steps], [("proj-1", "rename")])
        self.assertFalse((self.store.memories / "proj-1/index.md").exists())
        profile = self.store.read("proj-1/profile.md")["content"]
        self.assertIn("name: profile\n", profile)
        self.assertIn("description: proj-1 overview\n", profile)
        self.assertIn("  - migration\n", profile)
        self.assertTrue(profile.endswith("---\n- a web app\n- deploys with colmena\n"))
        self.assertEqual(self.kept("proj-1", "index.md"), original)

    def test_missing_description_gets_a_default(self):
        self.put("proj-1/index.md", "- a web app\n")  # no frontmatter at all
        migrate.run(self.store)
        self.assertIn("description: What the proj-1 project is", self.store.read("proj-1/profile.md")["content"])

    def test_appends_the_index_verbatim_to_an_existing_profile(self):
        old_profile = doc("profile", "What proj-1 is", "- a web app\n")
        self.put("proj-1/profile.md", old_profile)
        self.put("proj-1/index.md", doc("index", "overview", "- a web app\n- deploys with colmena\n"))
        steps = migrate.run(self.store)
        self.assertEqual((steps[0].action, steps[0].detail), ("merge", "2 lines"))
        self.assertEqual(self.body_of("proj-1/profile.md"),
                         "- a web app\n\n## Moved from index.md\n- a web app\n- deploys with colmena\n")
        self.assertEqual(self.kept("proj-1", "profile.md"), old_profile)
        self.assertFalse((self.store.memories / "proj-1/index.md").exists())

    def test_merge_keeps_structure_the_profile_already_has(self):
        # Line-by-line dedup used to drop the closing fence and "end", leaving an open code block.
        self.put("proj-1/profile.md", doc("profile", "What proj-1 is", "```\nold\n```\nend\n"))
        index_body = "- paused for winter\n## Deploy\n```sh\nmake deploy\nend\n```\n"
        self.put("proj-1/index.md", doc("index", "overview", index_body))
        migrate.run(self.store)
        body = self.body_of("proj-1/profile.md")
        self.assertTrue(body.endswith("## Moved from index.md\n" + index_body), body)
        self.assertEqual(body.count("```") % 2, 0)

    def test_rerun_after_profile_was_written_just_removes_the_index(self):
        self.put("proj-1/profile.md", doc("profile", "What proj-1 is", "- a web app\n"))
        self.put("proj-1/index.md", doc("index", "overview", "- deploys with colmena\n"))
        with mock.patch("pathlib.Path.unlink", side_effect=OSError("crash")):
            steps = migrate.run(self.store)
        self.assertEqual(steps[0].action, "skip")  # reported, not raised
        self.assertIn("run `hermes memory-buckets migrate` again", steps[0].detail)
        merged = self.body_of("proj-1/profile.md")
        steps = migrate.run(self.store)
        self.assertEqual((steps[0].action, steps[0].detail), ("remove", "proj-1/profile.md already has all of it"))
        self.assertEqual(self.body_of("proj-1/profile.md"), merged, "not appended twice")

    def test_a_bad_profile_skips_that_project_only(self):
        for bad in ("symlink", "directory"):
            with self.subTest(bad):
                self.put("aaa/index.md", doc("index", "overview", "- a\n"))
                self.put("zzz/index.md", doc("index", "overview", "- z\n"))
                target = self.store.memories / "aaa/profile.md"
                if bad == "symlink":
                    os.symlink(self.store.memories / "zzz/index.md", target)
                else:
                    target.mkdir()
                steps = {s.project: s for s in migrate.run(self.store)}
                self.assertEqual(steps["aaa"].action, "skip")
                self.assertIn("move its content by hand", steps["aaa"].detail)
                self.assertTrue((self.store.memories / "aaa/index.md").exists())
                self.assertEqual(steps["zzz"].action, "rename")
                self.assertFalse((self.store.memories / "zzz/index.md").exists())
                if bad == "symlink":
                    target.unlink()
                else:
                    target.rmdir()
                for leftover in ("aaa/index.md", "zzz/profile.md"):
                    (self.store.memories / leftover).unlink(missing_ok=True)

    def test_unparseable_index_is_skipped(self):
        self.put("proj-1/index.md", "---\nname: index\n- no closing fence\n")
        steps = migrate.run(self.store)
        self.assertEqual(steps[0].action, "skip")
        self.assertIn("can't parse its frontmatter", steps[0].detail)
        self.assertFalse((self.store.memories / "proj-1/profile.md").exists())

    def test_nothing_new_just_removes_the_index(self):
        self.put("proj-1/profile.md", doc("profile", "What proj-1 is", "- a web app\n"))
        self.put("proj-1/index.md", doc("index", "overview", "- a web app\n"))
        before = self.store.read("proj-1/profile.md")["version"]
        steps = migrate.run(self.store)
        self.assertEqual(steps[0].action, "remove")
        self.assertEqual(self.store.read("proj-1/profile.md")["version"], before, "profile untouched")
        self.assertFalse((self.store.memories / "proj-1/index.md").exists())
        self.assertTrue((migrate.backup_dir(self.store) / "proj-1/index.md").exists())

    def test_unparseable_profile_is_skipped_not_clobbered(self):
        self.put("proj-1/profile.md", "---\nname: profile\n")  # no closing ---
        self.put("proj-1/index.md", doc("index", "overview", "- deploys with colmena\n"))
        steps = migrate.run(self.store)
        self.assertEqual(steps[0].action, "skip")
        self.assertIn("can't parse proj-1/profile.md", steps[0].detail)
        self.assertTrue((self.store.memories / "proj-1/index.md").exists())
        self.assertEqual((self.store.memories / "proj-1/profile.md").read_text(), "---\nname: profile\n")

    def test_too_large_to_merge_is_skipped(self):
        self.put("proj-1/profile.md", doc("profile", "What proj-1 is", "- x\n" * 9000))
        self.put("proj-1/index.md", doc("index", "overview", "- y\n" * 9000))
        self.assertEqual(migrate.run(self.store)[0].action, "skip")
        self.assertTrue((self.store.memories / "proj-1/index.md").exists())

    def test_idempotent_and_ignores_global(self):
        self.put("proj-1/index.md", doc("index", "overview", "- a web app\n"))
        self.put("global/topics/index.md", doc("index", "a topic that happens to be called index"))
        self.assertEqual(len(migrate.run(self.store)), 1)
        self.assertEqual(migrate.run(self.store), [])
        self.assertTrue((self.store.memories / "global/topics/index.md").exists())

    def test_dry_run_changes_nothing(self):
        self.put("proj-1/index.md", doc("index", "overview", "- a web app\n"))
        steps = migrate.run(self.store, dry_run=True)
        self.assertEqual(steps[0].action, "rename")
        self.assertTrue((self.store.memories / "proj-1/index.md").exists())
        self.assertFalse((self.store.memories / "proj-1/profile.md").exists())
        self.assertFalse(migrate.backup_dir(self.store).exists())

    def test_backup_survives_a_rerun(self):
        # A crash between backup and unlink must not let a rerun overwrite the first copy,
        # and an index.md that comes back with different content gets its own backup.
        first = doc("index", "overview", "- first\n")
        self.put("proj-1/index.md", first)
        with mock.patch("pathlib.Path.unlink", side_effect=OSError("crash")):
            self.assertEqual(migrate.run(self.store)[0].action, "skip")
        second = doc("index", "overview", "- second\n")
        self.put("proj-1/index.md", second)
        migrate.run(self.store)
        self.assertEqual(self.kept("proj-1", "index.md"), first)
        kept = sorted(p.name for p in (migrate.backup_dir(self.store) / "proj-1").glob("index*.md"))
        self.assertEqual(len(kept), 2, kept)
        self.assertIn(second, [(migrate.backup_dir(self.store) / "proj-1" / n).read_text() for n in kept])

    def test_old_inbox_description_is_replaced(self):
        old = doc("inbox", OLD_INBOX_DESCRIPTION, "## imported USER.md\n- Ivy\n")
        self.put("global/inbox.md", old)
        self.assertTrue(migrate.inbox_needs_migrating(self.store))
        self.assertEqual([s.action for s in migrate.run(self.store, dry_run=True)], ["inbox"])
        self.assertEqual([s.action for s in migrate.run(self.store)], ["inbox"])
        text = self.store.read("global/inbox.md")["content"]
        self.assertIn(f"description: {inbox.DESCRIPTION}\n", text)
        self.assertTrue(text.endswith("## imported USER.md\n- Ivy\n"))
        self.assertEqual(self.kept("global", "inbox.md"), old)
        self.assertEqual(migrate.run(self.store), [])

    def test_any_other_inbox_description_is_replaced(self):
        # e.g. one an older skill wrote, which contradicts the prompt's inbox note.
        old = doc("inbox", "Staging area for project facts; sort with another skill.", "- x\n")
        self.put("global/inbox.md", old)
        steps = migrate.run(self.store)
        self.assertEqual([s.action for s in steps], ["inbox"])
        self.assertIn("was: 'Staging area for project facts", steps[0].describe())
        self.assertIn(f"description: {inbox.DESCRIPTION}\n", self.store.read("global/inbox.md")["content"])
        self.assertEqual(self.kept("global", "inbox.md"), old)

    def test_current_inbox_description_is_left_alone(self):
        self.put("global/inbox.md", doc("inbox", inbox.DESCRIPTION, "- x\n"))
        self.assertFalse(migrate.inbox_needs_migrating(self.store))
        self.assertEqual(migrate.run(self.store), [])

    def test_locked_store_raises(self):
        self.put("proj-1/index.md", doc("index", "overview"))
        with self.store.lock(), mock.patch("memory_buckets.store.LOCK_TIMEOUT_S", 0.1):
            with self.assertRaises(StoreError) as caught:
                migrate.run(self.store)
        self.assertEqual(caught.exception.code, "locked")


class ProviderRunsTheMigrationTest(StoreCase):
    def make(self, **kw):
        from memory_buckets import provider as prov
        with mock.patch.dict(os.environ, {"MEMORY_BUCKETS_EMBEDDINGS_BACKEND": "off"}):
            p = prov.MemoryBucketsProvider()
            p.initialize("s1", hermes_home=str(self.home), platform="cli", **kw)
        self.addCleanup(p.shutdown)
        return p

    def test_runs_on_startup(self):
        self.put("proj-1/index.md", doc("index", "overview", "- a web app\n"))
        with self.assertLogs("memory_buckets", "INFO") as logs:
            self.make()
        self.assertTrue((self.store.memories / "proj-1/profile.md").exists())
        self.assertTrue(any("proj-1/index.md becomes proj-1/profile.md" in line for line in logs.output))

    def test_read_only_sessions_leave_it(self):
        self.put("proj-1/index.md", doc("index", "overview", "- a web app\n"))
        self.make(agent_context="cron")
        self.assertTrue((self.store.memories / "proj-1/index.md").exists())

    def test_a_failed_migration_never_fails_startup(self):
        self.put("proj-1/index.md", doc("index", "overview", "- a web app\n"))
        with mock.patch.object(migrate, "run", side_effect=StoreError("locked", "store is locked")), \
                self.assertLogs("memory_buckets", "WARNING") as logs:
            p = self.make()
        self.assertIsNotNone(p.store)
        self.assertTrue(any("migrate` retries it" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
