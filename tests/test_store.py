import multiprocessing
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from .helpers import StoreCase, doc, symlink_or_skip
from memory_buckets import frontmatter
from memory_buckets import store as st


def _hold_lock(root, ready, release):
    s = st.Store(root)
    with s.lock():
        ready.set()
        release.wait(10)


class StoreTest(StoreCase):
    def test_edited_at_reads_what_stamp_writes(self):
        fm = frontmatter.parse(doc("nix", "Nix"))[0]
        self.assertIsNone(st.edited_at(fm))
        st.stamp(fm)
        self.assertAlmostEqual(st.edited_at(fm), time.time(), delta=5)
        fm.set(st.EDITED_AT, "2031-01-02T03:04:05Z")
        self.assertEqual(st.edited_at(fm), 1925089445.0)
        # Anything stamp() would not write is not a stamp: the caller falls back to mtime.
        for odd in ("last Tuesday", "2031-01-02", "2031-01-02T03:04:05+01:00", ""):
            fm.set(st.EDITED_AT, odd)
            self.assertIsNone(st.edited_at(fm), odd)
        fm.set(st.EDITED_AT, ["2031-01-02T03:04:05Z"])
        self.assertIsNone(st.edited_at(fm))

    def test_write_read_delete(self):
        v1 = self.store.write("global/topics/nix.md", doc("nix", "Nix"), "new")["version"]
        got = self.store.read("global/topics/nix.md")
        self.assertEqual(got["version"], v1)
        self.store.delete("global/topics/nix.md", v1)
        self.assertIsNone(self.store.read_bytes("global/topics/nix.md"))

    def test_update_without_version_is_atomic_under_the_lock(self):
        self.store.write("global/topics/nix.md", doc("nix", "Nix"), "new")
        self.store.update("global/topics/nix.md", lambda cur: cur + b"- more\n")
        self.assertTrue(self.store.read("global/topics/nix.md")["content"].endswith("- more\n"))

    def test_no_temp_files_left_behind(self):
        self.store.write("global/topics/nix.md", doc("nix", "Nix"), "new")
        leftovers = [n for _, _, names in os.walk(self.store.root) for n in names if ".tmp." in n]
        self.assertEqual(leftovers, [])

    def test_failed_write_leaves_no_temp_file(self):
        # _atomic_write must unlink its .tmp when the write itself fails (review finding).
        with mock.patch("os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.write("global/topics/nix.md", doc("nix", "Nix"), "new")
        leftovers = [n for _, _, names in os.walk(self.store.root) for n in names if ".tmp." in n]
        self.assertEqual(leftovers, [])
        self.assertIsNone(self.store.read_bytes("global/topics/nix.md"))

    def test_lock_times_out_while_another_process_holds_it(self):
        # multiprocessing spawn re-imports this module in the child under the name
        # pytest gave it (<checkout>.tests.test_store). With --import-mode=importlib
        # the checkout directory is on sys.path but its parent is not, so the child
        # cannot import the top-level package: Nix builds the checkout as
        # /build/source and the unpickle fails there every time. The child inherits
        # sys.path, so put the checkout's parent on it before spawning.
        parent = str(Path(__file__).resolve().parents[2])
        if parent not in sys.path:
            sys.path.append(parent)
            self.addCleanup(sys.path.remove, parent)
        ctx = multiprocessing.get_context("spawn")  # Windows has no fork
        ready, release = ctx.Event(), ctx.Event()
        proc = ctx.Process(target=_hold_lock, args=(self.store.root, ready, release))
        proc.start()
        try:
            self.assertTrue(ready.wait(10))
            start = time.monotonic()
            with self.assertRaises(st.StoreError) as caught:
                with self.store.lock(timeout=0.3):
                    pass
            self.assertEqual(caught.exception.code, "locked")
            self.assertGreaterEqual(time.monotonic() - start, 0.3)
        finally:
            release.set()
            proc.join(10)
        with self.store.lock(timeout=1):  # free again
            pass

    def test_lock_excludes_other_instances_in_the_same_process(self):
        other = st.Store(self.store.root)
        with self.store.lock():
            with self.assertRaises(st.StoreError):
                with other.lock(timeout=0.1):
                    pass

    def test_iter_paths_skips_invalid_files(self):
        self.put("global/topics/nix.md", doc("nix", "Nix"))
        self.put("global/topics/Nope.md", "x")
        self.put("global/random.txt", "x")
        self.assertEqual(list(self.store.iter_paths()), ["global/topics/nix.md"])

    def test_iter_skipped_reports_what_iter_paths_passes_over(self):
        # The four files from the issue, plus an attachment and a nested project path.
        self.put("global/topics/nix.md", doc("nix", "Nix"))
        self.put("global/topics/My Note.md", "x")
        self.put("global/topics/nix/flakes.md", "x")
        self.put("global/topics/ex_luna.md", "x")
        self.put("global/notes.md", "x")
        self.put("Proj/topics/y.md", "x")
        self.put("proj-1/topics/diagram.png", "x")
        # Hidden files and directories (Obsidian's config, our own temp files) aren't reported.
        self.put(".obsidian/app.json", "{}")
        self.put("global/topics/.nix.md.tmp.1.2", "x")
        skipped = dict(self.store.iter_skipped())
        self.assertEqual(sorted(skipped), ["Proj/topics/y.md", "global/notes.md", "global/topics/My Note.md",
                                           "global/topics/ex_luna.md", "global/topics/nix/flakes.md",
                                           "proj-1/topics/diagram.png"])
        self.assertEqual(set(skipped.values()), {"path"})
        # Nothing is both read and reported.
        self.assertEqual(list(self.store.iter_paths()), ["global/topics/nix.md"])

    def test_iter_skipped_reports_symlinks_without_following_them(self):
        outside = self.home / "outside"
        outside.mkdir()
        (outside / "Loose.md").write_text("x", encoding="utf-8")
        self.put("global/topics/real.md", doc("real", "Real"))
        symlink_or_skip(self, self.store.memories / "global/topics/real.md", self.store.memories / "global/topics/sym.md")
        symlink_or_skip(self, outside, self.store.memories / "global/linked")
        self.assertEqual(list(self.store.iter_skipped()),
                         [("global/linked", "symlink"), ("global/topics/sym.md", "symlink")])

    def test_iter_skipped_with_no_store_yet(self):
        self.store.memories.rmdir()
        self.assertEqual(list(self.store.iter_skipped()), [])

    def test_projects_lists_buckets_on_disk(self):
        self.assertEqual(self.store.projects(), [])
        self.put("proj-2/topics/x.md", doc("x", "X"))
        self.put("proj-1/profile.md", doc("profile", "P"))
        self.put("global/topics/nix.md", doc("nix", "Nix"))
        (self.store.memories / "not-a-bucket.txt").write_text("x", encoding="utf-8")
        (self.store.memories / "Bad_Slug").mkdir()
        self.assertEqual(self.store.projects(), ["proj-1", "proj-2"])

    def test_projects_ignores_symlinked_bucket_directories(self):
        outside = self.home / "outside-proj"
        outside.mkdir()
        symlink_or_skip(self, outside, self.store.memories / "sym-proj")
        self.put("proj-1/profile.md", doc("profile", "P"))
        self.assertEqual(self.store.projects(), ["proj-1"])

    def test_delete_removes_empty_parent_and_project_directories(self):
        v1 = self.store.write("proj-1/topics/nix.md", doc("nix", "Nix"), "new")["version"]
        v2 = self.store.write("proj-1/topics/rust.md", doc("rust", "Rust"), "new")["version"]
        v3 = self.store.write("proj-2/profile.md", doc("profile", "Proj 2"), "new")["version"]

        self.store.delete("proj-1/topics/nix.md", v1)
        self.assertTrue((self.store.memories / "proj-1/topics").is_dir())
        self.assertEqual(self.store.projects(), ["proj-1", "proj-2"])

        self.store.delete("proj-1/topics/rust.md", v2)
        self.assertFalse((self.store.memories / "proj-1/topics").exists())
        self.assertFalse((self.store.memories / "proj-1").exists())
        self.assertEqual(self.store.projects(), ["proj-2"])

        self.store.delete("proj-2/profile.md", v3)
        self.assertFalse((self.store.memories / "proj-2").exists())
        self.assertEqual(self.store.projects(), [])
        self.assertTrue(self.store.memories.is_dir())

    def test_project_index_is_not_a_memory_path(self):
        # The project index is generated into the prompt; no file holds it.
        with self.assertRaises(st.StoreError) as caught:
            self.store.write("proj-1/index.md", doc("index", "proj-1"), "new")
        self.assertEqual(caught.exception.code, "invalid_path")
        self.put("proj-1/index.md", doc("index", "left over from an older version"))
        self.assertEqual(list(self.store.iter_paths()), [])

    def test_project_id_rules(self):
        self.assertTrue(st.is_project_id("proj-1"))
        self.assertFalse(st.is_project_id("global"))
        self.assertFalse(st.is_project_id("Proj"))

    def test_trailing_newline_is_rejected(self):
        # $ also matches before a final newline in Python, so a plain .match
        # with $ accepted "global/topics/x.md\n" and created a file named
        # "x.md\n" on disk; anchors must be absolute.
        self.assertFalse(st.is_project_id("proj-1\n"))
        for path in ("global/topics/x.md\n", "global/inbox.md\n", "proj-1/topics/x.md\n"):
            with self.assertRaises(st.StoreError) as caught:
                st.validate_path(path)
            self.assertEqual(caught.exception.code, "invalid_path")
            with self.assertRaises(st.StoreError):
                self.store.write(path, doc("x", "x"), "new")

    # -- Windows: files held open by other processes, junctions --------------------

    @unittest.skipUnless(sys.platform == "win32", "only Windows refuses to replace or delete an open file")
    def test_writes_and_deletes_wait_out_a_brief_reader(self):
        path = "global/topics/nix.md"
        v1 = self.store.write(path, doc("nix", "Nix"), "new")["version"]
        reader = open(self.store.memories / path, "rb")
        threading.Timer(0.2, reader.close).start()
        v2 = self.store.write(path, doc("nix", "Nix", "- flakes\n"), v1)["version"]
        self.assertEqual(self.store.read(path)["version"], v2)
        reader = open(self.store.memories / path, "rb")
        threading.Timer(0.2, reader.close).start()
        self.store.delete(path, v2)
        self.assertIsNone(self.store.read_bytes(path))

    @unittest.skipUnless(sys.platform == "win32", "only Windows refuses to replace or delete an open file")
    def test_a_reader_that_never_lets_go_is_reported_as_locked(self):
        path = "global/topics/nix.md"
        v1 = self.store.write(path, doc("nix", "Nix"), "new")["version"]
        with open(self.store.memories / path, "rb"), mock.patch.object(st, "SHARING_RETRY_S", 0.1):
            for attempt in (lambda: self.store.write(path, doc("nix", "Nix", "- flakes\n"), v1),
                            lambda: self.store.delete(path, v1)):
                with self.assertRaises(st.StoreError) as caught:
                    attempt()
                self.assertEqual(caught.exception.code, "locked")
                self.assertIn("nix.md is open in another program", caught.exception.message)
        self.assertEqual(self.store.read(path)["version"], v1, "unchanged")
        leftovers = [n for _, _, names in os.walk(self.store.root) for n in names if ".tmp." in n]
        self.assertEqual(leftovers, [])

    @unittest.skipUnless(sys.platform == "win32", "junctions are Windows-only")
    def test_junctions_are_treated_like_symlinks(self):
        import _winapi
        outside = self.home / "outside"
        (outside / "topics").mkdir(parents=True)
        (outside / "topics" / "nix.md").write_text(doc("nix", "Nix"), encoding="utf-8")
        try:
            _winapi.CreateJunction(str(outside), str(self.store.memories / "proj-1"))
        except OSError as err:
            self.skipTest(f"can't create a junction here: {err}")
        self.assertTrue(st.is_link(self.store.memories / "proj-1"))
        self.assertEqual(self.store.projects(), [])
        self.assertEqual(list(self.store.iter_paths()), [])
        with self.assertRaises(st.StoreError) as caught:
            self.store.read("proj-1/topics/nix.md")
        self.assertEqual(caught.exception.code, "invalid_path")


if __name__ == "__main__":
    unittest.main()
