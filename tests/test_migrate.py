import json
import os
import unittest
from unittest import mock

from .helpers import StoreCase, doc, symlink_or_skip
from memory_buckets import inbox, migrate
from memory_buckets.store import StoreError, version

OLD_INBOX_DESCRIPTION = ("Imported memory waiting to be sorted into proper files (migration only); "
                         "sort it, then delete it")  # 0.0.1's default


class IndexMigrationTest(StoreCase):
    def body_of(self, path):
        return self.store.read(path)["content"].split("\n---\n", 1)[1]

    def kept(self, project, name):
        return (migrate.backup_dir(self.store) / project / name).read_text(encoding="utf-8")

    def kept_020(self, project, name):
        return (migrate.backup_dir(self.store, migrate.INBOX_MIGRATION) / project / name).read_text(encoding="utf-8")

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
                    symlink_or_skip(self, self.store.memories / "zzz/index.md", target)
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
        self.assertEqual((self.store.memories / "proj-1/profile.md").read_text(encoding="utf-8"), "---\nname: profile\n")

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
        self.assertIn(second, [(migrate.backup_dir(self.store) / "proj-1" / n).read_text(encoding="utf-8") for n in kept])

    def test_old_inbox_description_is_replaced(self):
        old = doc("inbox", OLD_INBOX_DESCRIPTION, "## imported USER.md\n- Sam\n")
        self.put("global/inbox.md", old)
        self.assertTrue(migrate.inbox_needs_migrating(self.store))
        self.assertEqual([s.action for s in migrate.run(self.store, dry_run=True)], ["inbox"])
        self.assertEqual([s.action for s in migrate.run(self.store)], ["inbox"])
        text = self.store.read("global/inbox.md")["content"]
        self.assertIn(f"description: {inbox.DESCRIPTION}\n", text)
        self.assertTrue(text.endswith("## imported USER.md\n- Sam\n"))
        self.assertEqual(self.kept_020("global", "inbox.md"), old)
        self.assertEqual(migrate.run(self.store), [])

    def test_the_inbox_backup_lands_in_the_current_migration_folder(self):
        # 0.2.0 changed the description, so its backup belongs in _backup/0.2.0/,
        # not _backup/0.1.0/ (where the index-migration keeps its copies).
        old = doc("inbox", OLD_INBOX_DESCRIPTION, "- x\n")
        self.put("global/inbox.md", old)
        migrate.run(self.store)
        self.assertEqual(self.kept_020("global", "inbox.md"), old)
        self.assertFalse((migrate.backup_dir(self.store) / "global/inbox.md").exists())

    def test_any_other_inbox_description_is_replaced(self):
        # e.g. one an older skill wrote, which contradicts the prompt's inbox note.
        old = doc("inbox", "Staging area for project facts; sort with another skill.", "- x\n")
        self.put("global/inbox.md", old)
        steps = migrate.run(self.store)
        self.assertEqual([s.action for s in steps], ["inbox"])
        self.assertIn("was: 'Staging area for project facts", steps[0].describe())
        self.assertIn(f"description: {inbox.DESCRIPTION}\n", self.store.read("global/inbox.md")["content"])
        self.assertEqual(self.kept_020("global", "inbox.md"), old)

    def test_current_inbox_description_is_left_alone(self):
        self.put("global/inbox.md", doc("inbox", inbox.DESCRIPTION, "- x\n"))
        self.assertFalse(migrate.inbox_needs_migrating(self.store))
        self.assertEqual(migrate.run(self.store), [])

    def test_empty_headings_are_removed_but_filled_sections_are_kept(self):
        body = ("## imported USER.md\n"
                "- Sam\n"
                "\n"
                "## imported PROJECTS.md\n"
                "\n"
                "## added in conversation (cli)\n"
                "- Likes tea\n"
                "\n"
                "## added in conversation (telegram)\n")
        self.put("global/inbox.md", doc("inbox", inbox.DESCRIPTION, body))
        self.assertFalse(migrate.inbox_needs_migrating(self.store), "headings alone don't ask for a migration")
        steps = migrate.run(self.store, dry_run=True)
        self.assertEqual([(s.action, s.detail) for s in steps],
                         [("tidy", "## imported PROJECTS.md; ## added in conversation (telegram)")])
        self.assertTrue((self.store.memories / "global/inbox.md").exists(), "a dry run changes nothing")
        steps = migrate.run(self.store)
        self.assertEqual([(s.action, s.detail) for s in steps],
                         [("tidy", "## imported PROJECTS.md; ## added in conversation (telegram)")])
        text = self.store.read("global/inbox.md")["content"]
        self.assertIn("## imported USER.md\n- Sam\n\n## added in conversation (cli)\n- Likes tea\n", text)
        self.assertNotIn("## imported PROJECTS.md", text)
        self.assertNotIn("## added in conversation (telegram)", text)
        self.assertFalse((migrate.backup_dir(self.store, migrate.INBOX_MIGRATION) / "global/inbox.md").exists(),
                         "removing empty headings keeps no backup")

    def test_a_section_with_only_a_code_fence_is_kept(self):
        # Raw-line emptiness: dropping the heading would turn the fenced lines into plain ones.
        body = "## a\n- x\n\n## code\n```\njust a block\n```\n\n## blank\n\n\n## end\n"
        self.put("global/inbox.md", doc("inbox", inbox.DESCRIPTION, body))
        steps = migrate.run(self.store)
        self.assertEqual([(s.action, s.detail) for s in steps], [("tidy", "## blank; ## end")])
        text = self.store.read("global/inbox.md")["content"]
        self.assertIn("## code\n```\njust a block\n```\n", text)
        self.assertNotIn("## blank", text)

    def test_headings_only_run_is_idempotent(self):
        self.put("global/inbox.md", doc("inbox", inbox.DESCRIPTION, "## a\n- x\n\n## empty\n"))
        self.assertEqual([(s.action, s.detail) for s in migrate.run(self.store)], [("tidy", "## empty")])
        text = self.store.read("global/inbox.md")["content"]
        self.assertEqual(migrate.run(self.store), [])
        self.assertEqual(migrate.run(self.store, dry_run=True), [])
        self.assertEqual(self.store.read("global/inbox.md")["content"], text)

    def test_headings_only_tidy_fills_missing_inbox_name(self):
        self.put("global/inbox.md", f"---\ndescription: {inbox.DESCRIPTION}\n---\n## a\n- x\n\n## empty\n")
        dry = migrate.run(self.store, dry_run=True)
        self.assertEqual([(s.action, s.detail) for s in dry], [("tidy", "## empty")])
        steps = migrate.run(self.store)
        self.assertEqual([(s.action, s.detail) for s in steps], [("tidy", "## empty")])
        text = self.store.read("global/inbox.md")["content"]
        self.assertIn("name: inbox\n", text)
        self.assertNotIn("## empty", text)

    def test_dry_run_and_apply_agree_when_tidied_inbox_fails_validation(self):
        oversized = doc("inbox", inbox.DESCRIPTION, "## a\n" + ("- " + "x" * 100 + "\n") * 500 + "\n## empty\n")
        self.put("global/inbox.md", oversized)
        dry = migrate.run(self.store, dry_run=True)
        real = migrate.run(self.store)
        self.assertEqual([(s.project, s.action, s.detail) for s in dry],
                         [(s.project, s.action, s.detail) for s in real])
        self.assertEqual(real[0].action, "skip")
        self.assertIn("over the", real[0].detail)

    def test_a_0_1_0_inbox_gets_the_description_and_the_cleanup_in_one_run(self):
        old = doc("inbox", OLD_INBOX_DESCRIPTION,
                  "## imported USER.md\n"
                  "- Sam studies botany\n"
                  "\n"
                  "## added in conversation 2026-10-02 (cli)\n"
                  "\n"
                  "## added in conversation 2026-10-05 (telegram)\n"
                  "- Likes tea\n")
        self.put("global/inbox.md", old)
        dry = migrate.run(self.store, dry_run=True)
        self.assertEqual(len(dry), 1, dry)
        self.assertTrue(dry[0].describe().endswith("; removed ## added in conversation 2026-10-02 (cli)"),
                        "the dry run names the headings too")
        steps = migrate.run(self.store)
        self.assertEqual(len(steps), 1, steps)
        self.assertEqual(steps[0].action, "inbox", "one step, not one per change")
        self.assertIn("was: 'Imported memory waiting to be sorted into", steps[0].describe())
        self.assertTrue(steps[0].describe().endswith("; removed ## added in conversation 2026-10-02 (cli)"),
                        steps[0].describe())
        text = self.store.read("global/inbox.md")["content"]
        self.assertIn(f"description: {inbox.DESCRIPTION}\n", text)
        self.assertNotIn("## added in conversation 2026-10-02", text)
        self.assertIn("## added in conversation 2026-10-05 (telegram)\n- Likes tea\n", text,
                      "the section with an entry stays")
        self.assertEqual(self.kept_020("global", "inbox.md"), old, "the description change is backed up, once")
        self.assertEqual(len(list((migrate.backup_dir(self.store, migrate.INBOX_MIGRATION) / "global").iterdir())), 1)

    def test_a_heading_line_inside_a_code_fence_is_not_a_heading(self):
        # memory_write and hand edits can put "## " lines in a fence; they are content.
        body = "## notes\n```\n## step one\n\n## step two\n```\n\n## end\n"
        self.put("global/inbox.md", doc("inbox", inbox.DESCRIPTION, body))
        steps = migrate.run(self.store)
        self.assertEqual([(s.action, s.detail) for s in steps], [("tidy", "## end")])
        text = self.store.read("global/inbox.md")["content"]
        self.assertIn("## notes\n```\n## step one\n\n## step two\n```\n", text, "every fenced line stays")
        self.assertEqual(migrate.run(self.store), [])

    def test_broken_inbox_frontmatter_does_not_stop_the_rest_of_the_run(self):
        # lint reports it; the other steps still go ahead and run() doesn't raise.
        broken = "---\nname: inbox\n## a\n- x\n"  # no closing ---
        self.put("global/inbox.md", broken)
        self.put("proj-1/index.md", doc("index", "proj-1 overview", "- a web app\n"))
        pending = self.store.root / migrate.PENDING_DIR
        pending.mkdir()
        (pending / "a1b2c3.json").write_bytes(b"{}\n")
        dry = migrate.run(self.store, dry_run=True)
        self.assertEqual([s.action for s in dry], ["rename", "pending", "remove"])
        steps = migrate.run(self.store)
        self.assertEqual([s.action for s in steps], ["rename", "pending", "remove"])
        self.assertFalse(os.path.lexists(pending))
        self.assertTrue((self.store.memories / "proj-1/profile.md").exists())
        self.assertEqual((self.store.memories / "global/inbox.md").read_text(encoding="utf-8"), broken, "left for lint")

    def test_a_missing_inbox_is_a_no_op(self):
        self.assertEqual(migrate.run(self.store), [])
        self.assertEqual(migrate.run(self.store, dry_run=True), [])
        self.assertFalse((self.store.memories / "global/inbox.md").exists())

    def test_locked_store_raises(self):
        self.put("proj-1/index.md", doc("index", "overview"))
        with self.store.lock(), mock.patch("memory_buckets.store.LOCK_TIMEOUT_S", 0.1):
            with self.assertRaises(StoreError) as caught:
                migrate.run(self.store)
        self.assertEqual(caught.exception.code, "locked")


OLD_DESCRIPTION_0_1_0 = "Memory waiting to be sorted, from imports and facts with no clear home yet"
INBOX_0_1_0_BODY = (
    "## imported USER.md from /home/sam/.hermes (2026-09-30)\n"
    "- Sam studies botany\n"
    "- proj-2 deploys with colmena\n"
    "\n"
    "## added in conversation 2026-10-02 (cli)\n"
    "- Prefers British spelling\n"
    "- nixos-config pins nixpkgs unstable\n"
    "\n"
    "## added in conversation 2026-10-05 (telegram)\n"
    "- Likes tea\n"
)


def proposal_0_1_0(pid, project, lines):
    """A proposal as 0.1.0's pending.propose wrote it."""
    p = {"id": pid, "project": project, "new_project": False, "summary": f"Files {len(lines)} fact(s) for {project} — é",
         "files": [{"path": f"{project}/profile.md", "lines": lines}], "inbox_lines": lines,
         "session": "s1", "created": "2026-10-05T12:00:00Z"}
    return (json.dumps(p, indent=2, ensure_ascii=False) + "\n").encode()


class PendingMigrationTest(StoreCase):
    """0.2.0: ``_pending/`` is backed up and removed, nothing is applied, the inbox's lines stay."""

    def setUp(self):
        super().setUp()
        self.pending = self.store.root / migrate.PENDING_DIR
        self.kept_dir = migrate.backup_dir(self.store, migrate.PENDING_MIGRATION) / migrate.PENDING_DIR
        self.proposals = {
            "a1b2c3.json": proposal_0_1_0("a1b2c3", "proj-2", ["- proj-2 deploys with colmena"]),
            "d4e5f6.json": proposal_0_1_0("d4e5f6", "nixos-config", ["- nixos-config pins nixpkgs unstable"]),
            "0f0f0f.json": b"{ not json, but still copied\n",
        }
        self.pending.mkdir()
        for name, data in self.proposals.items():
            (self.pending / name).write_bytes(data)
        self.put("global/inbox.md", doc("inbox", OLD_DESCRIPTION_0_1_0, INBOX_0_1_0_BODY))
        self.put("proj-2/profile.md", doc("profile", "What proj-2 is", "- a web app\n"))

    def tree(self, root):
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}

    def test_backs_up_and_removes_pending_and_applies_nothing(self):
        before = self.tree(self.store.memories)
        steps = migrate.run(self.store)
        self.assertEqual([(s.action, s.name) for s in steps],
                         [("inbox", ""), ("pending", "0f0f0f.json"), ("pending", "a1b2c3.json"),
                          ("pending", "d4e5f6.json"), ("remove", "")])
        self.assertFalse(os.path.lexists(self.pending))
        self.assertEqual(self.tree(self.kept_dir), self.proposals, "byte for byte")
        after = self.tree(self.store.memories)
        self.assertEqual(sorted(after), sorted(before), "no file created or removed under memories/")
        self.assertEqual({k for k in after if after[k] != before[k]}, {"global/inbox.md"})
        inbox_text = after["global/inbox.md"].decode()
        self.assertIn(f"description: {inbox.DESCRIPTION}\n", inbox_text)
        self.assertTrue(inbox_text.endswith("\n---\n" + INBOX_0_1_0_BODY), "every inbox line in place")
        self.assertIsNone(migrate.leftover_pending(self.store))

    def test_describes_each_step(self):
        lines = [s.describe() for s in migrate.run(self.store, dry_run=True)]
        self.assertEqual(lines[1:], [
            "_pending/0f0f0f.json kept in _backup/0.2.0/_pending/ and removed",
            "_pending/a1b2c3.json (a proposal for proj-2) kept in _backup/0.2.0/_pending/ and removed",
            "_pending/d4e5f6.json (a proposal for nixos-config) kept in _backup/0.2.0/_pending/ and removed",
            "_pending/ removed: its proposals weren't applied. Their lines are still in global/inbox.md; "
            "/sort-imports files them",
        ])

    def test_dry_run_changes_nothing(self):
        before = self.tree(self.store.root)
        steps = migrate.run(self.store, dry_run=True)
        self.assertEqual(len(steps), 5)
        self.assertEqual(self.tree(self.store.root), before)
        self.assertFalse((self.store.root / migrate.BACKUP_DIR).exists())

    def test_rerun_is_a_no_op(self):
        migrate.run(self.store)
        kept = self.tree(self.store.root)
        self.assertEqual(migrate.run(self.store), [])
        self.assertEqual(migrate.run(self.store, dry_run=True), [])
        self.assertEqual(self.tree(self.store.root), kept)

    def test_missing_or_empty_pending_is_not_an_error(self):
        for name in self.proposals:
            (self.pending / name).unlink()
        steps = [s for s in migrate.run(self.store) if s.project == migrate.PENDING_DIR]
        self.assertEqual([(s.action, s.describe()) for s in steps], [("remove", "_pending/ removed: it was empty")])
        self.assertFalse(self.pending.exists())
        self.assertEqual(migrate.run(self.store), [])  # missing: nothing to do

    def test_a_file_that_cant_be_backed_up_is_not_removed(self):
        # Something already in the way at the backup path: the copy can't be made or checked.
        (self.kept_dir / "a1b2c3.json").mkdir(parents=True)
        steps = {s.name: s for s in migrate.run(self.store) if s.project == migrate.PENDING_DIR}
        self.assertEqual(steps["a1b2c3.json"].action, "skip")
        self.assertIn("run `hermes memory-buckets migrate` again", steps["a1b2c3.json"].describe())
        self.assertTrue(steps["a1b2c3.json"].describe().startswith("_pending/a1b2c3.json left in place: "))
        self.assertNotIn("", steps, "the folder stays while a file is still in it")
        self.assertEqual((self.pending / "a1b2c3.json").read_bytes(), self.proposals["a1b2c3.json"])
        self.assertEqual(sorted(p.name for p in self.pending.iterdir()), ["a1b2c3.json"])
        self.assertEqual(migrate.leftover_pending(self.store), ["a1b2c3.json"])
        (self.kept_dir / "a1b2c3.json").rmdir()
        steps = migrate.run(self.store)
        self.assertEqual([s.action for s in steps], ["pending", "remove"])
        self.assertEqual(self.tree(self.kept_dir), self.proposals)

    def test_a_failed_copy_is_not_removed(self):
        real = self.store._atomic_write

        def flaky(target, data):
            if target.name == "d4e5f6.json":
                raise OSError("disk full")
            return real(target, data)

        with mock.patch.object(self.store, "_atomic_write", side_effect=flaky):
            steps = {s.name: s for s in migrate.run(self.store) if s.project == migrate.PENDING_DIR}
        self.assertEqual(steps["d4e5f6.json"].describe(),
                         "_pending/d4e5f6.json left in place: disk full; run `hermes memory-buckets migrate` again")
        self.assertTrue((self.pending / "d4e5f6.json").exists())
        self.assertFalse((self.pending / "a1b2c3.json").exists())

    def test_a_copy_that_doesnt_match_leaves_the_original(self):
        # Failure injection for the read-back check: a "successful" copy with
        # wrong bytes must not let the original be removed.
        real = self.store._atomic_write

        def corrupting(target, data):
            return real(target, data + b"tampered") if target.name == "d4e5f6.json" else real(target, data)

        with mock.patch.object(self.store, "_atomic_write", side_effect=corrupting):
            steps = {s.name: s for s in migrate.run(self.store) if s.project == migrate.PENDING_DIR}
        self.assertEqual(steps["d4e5f6.json"].action, "skip")
        self.assertIn("doesn't match", steps["d4e5f6.json"].describe())
        self.assertIn("run `hermes memory-buckets migrate` again", steps["d4e5f6.json"].describe())
        self.assertTrue((self.pending / "d4e5f6.json").exists())
        self.assertEqual((self.pending / "d4e5f6.json").read_bytes(), self.proposals["d4e5f6.json"])
        self.assertNotIn("", steps, "the folder stays while a file is still in it")

    def test_rerun_after_a_crash_between_copy_and_removal(self):
        with mock.patch("pathlib.Path.unlink", side_effect=OSError("crash")):
            steps = [s for s in migrate.run(self.store) if s.project == migrate.PENDING_DIR]
        self.assertEqual({s.action for s in steps}, {"skip"})
        self.assertEqual(len(steps), 3, "no folder step while files are still in it")
        migrate.run(self.store)
        self.assertFalse(self.pending.exists())
        self.assertEqual(self.tree(self.kept_dir), self.proposals, "no second copy")

    def test_an_earlier_backup_is_never_overwritten(self):
        self.kept_dir.mkdir(parents=True)
        (self.kept_dir / "a1b2c3.json").write_bytes(b"an older copy\n")
        migrate.run(self.store)
        self.assertEqual((self.kept_dir / "a1b2c3.json").read_bytes(), b"an older copy\n")
        data = self.proposals["a1b2c3.json"]
        self.assertEqual((self.kept_dir / f"a1b2c3-{version(data)}.json").read_bytes(), data)
        self.assertFalse(self.pending.exists())

    def test_anything_but_a_regular_file_is_left_with_the_folder(self):
        (self.pending / "stray").mkdir()
        steps = migrate.run(self.store, dry_run=True)
        self.assertNotIn("remove", [s.action for s in steps])
        steps = {s.name: s for s in migrate.run(self.store) if s.project == migrate.PENDING_DIR}
        self.assertEqual(steps["stray"].describe(), "_pending/stray left in place: it isn't a regular file. "
                                                    "Move it out by hand, then run `hermes memory-buckets migrate`")
        self.assertEqual([p.name for p in self.pending.iterdir()], ["stray"])
        self.assertEqual(self.tree(self.kept_dir), self.proposals)

    def test_provider_runs_it_on_startup(self):
        from memory_buckets import provider as prov
        with mock.patch.dict(os.environ, {"MEMORY_BUCKETS_EMBEDDINGS_BACKEND": "off"}), \
                self.assertLogs("memory_buckets", "INFO") as logs:
            p = prov.MemoryBucketsProvider()
            p.initialize("s1", hermes_home=str(self.home), platform="cli")
        self.addCleanup(p.shutdown)
        self.assertFalse(self.pending.exists())
        self.assertTrue(any("_pending/ removed" in line for line in logs.output))


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
