import multiprocessing
import os
import time
import unittest
from unittest import mock

from .helpers import StoreCase, doc
from memory_buckets import store as st


def _hold_lock(root, ready, release):
    s = st.Store(root)
    with s.lock():
        ready.set()
        release.wait(10)


class StoreTest(StoreCase):
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
        ctx = multiprocessing.get_context("fork")
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

    def test_projects_lists_buckets_on_disk(self):
        self.assertEqual(self.store.projects(), [])
        self.put("proj-2/topics/x.md", doc("x", "X"))
        self.put("proj-1/profile.md", doc("profile", "P"))
        self.put("global/topics/nix.md", doc("nix", "Nix"))
        (self.store.memories / "not-a-bucket.txt").write_text("x")
        (self.store.memories / "Bad_Slug").mkdir()
        self.assertEqual(self.store.projects(), ["proj-1", "proj-2"])

    def test_projects_ignores_symlinked_bucket_directories(self):
        outside = self.home / "outside-proj"
        outside.mkdir()
        os.symlink(outside, self.store.memories / "sym-proj")
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


if __name__ == "__main__":
    unittest.main()
