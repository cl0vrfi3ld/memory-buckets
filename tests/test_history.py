"""Recoverable history: every destructive write leaves a restorable copy in
``.history/``, retention limits hold, and a broken ``.history`` never breaks a
write. Appends and creations record nothing: they add, never destroy."""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

from .helpers import StoreCase, doc, run_hermes_cli
from memory_buckets import cli, config, index, inbox, scopes, store, tools
from memory_buckets.store import StoreError, version

PATH = "global/topics/nix.md"


class HistoryTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.v1 = self.store.write(PATH, doc("nix", "Nix", "- fact\n"), "new")["version"]

    def test_overwrite_records_the_previous_bytes(self):
        self.store.write(PATH, doc("nix", "Nix", "- changed\n"), self.v1)
        got = self.store.restore(PATH, self.v1)
        self.assertFalse(got["unchanged"])
        self.assertFalse(got["recreated"])  # the file existed; its bytes were replaced and recorded
        self.assertEqual(self.store.read(PATH)["version"], self.v1)
        self.assertEqual(version(doc("nix", "Nix", "- fact\n").encode()), self.v1)

    def test_str_replace_records(self):
        self.store.update(PATH, lambda cur: cur.replace(b"fact", b"flakes"), self.v1)
        entries = self.store.history(PATH)
        self.assertEqual([e["version"] for e in entries], [self.v1])
        self.assertEqual(entries[0]["bytes"], len(doc("nix", "Nix", "- fact\n").encode()))
        self.store.restore(PATH)
        self.assertEqual(version(self.store.read_bytes(PATH)), self.v1)

    def test_delete_records_and_restore_recreates(self):
        self.store.delete(PATH, self.v1)
        self.assertIsNone(self.store.read_bytes(PATH))
        self.assertEqual([e["version"] for e in self.store.history(PATH)], [self.v1])
        got = self.store.restore(PATH)
        self.assertTrue(got["recreated"])  # nothing was replaced, so nothing new was recorded
        self.assertEqual(got["version"], self.v1)
        self.assertEqual(self.store.read(PATH)["version"], self.v1)
        self.assertIn("- fact\n", self.store.read(PATH)["content"])

    def test_restore_replaces_the_current_content(self):
        self.store.update(PATH, lambda cur: cur + b"- extra\n")
        self.store.restore(PATH)  # newest recorded version is v1's content
        content = self.store.read(PATH)["content"]
        self.assertIn("- fact\n", content)
        self.assertNotIn("- extra\n", content)

    def test_restore_is_itself_recorded(self):
        self.store.update(PATH, lambda cur: cur + b"- extra\n")
        self.store.restore(PATH)
        # The pre-restore content (with "- extra") is now the newest recorded version.
        replaced = version(self.store.read_bytes(PATH))  # what restore wrote back
        newest = self.store.history(PATH)[0]
        self.assertNotEqual(newest["version"], replaced)
        self.assertEqual(version(self._entry(newest).read_bytes()), newest["version"])
        self.store.restore(PATH, newest["version"])
        self.assertIn("- extra\n", self.store.read(PATH)["content"])
        self.assertIn("- fact\n", self.store.read(PATH)["content"])

    def _entry(self, row):
        return self.store._history_dir(PATH) / row["file"]

    def test_create_records_nothing(self):
        self.assertEqual(self.store.history("global/people/sam.md"), [])
        self.store.write("global/people/sam.md", doc("sam", "Sam"), "new")
        self.assertEqual(self.store.history("global/people/sam.md"), [])

    def test_same_content_rewrite_records_nothing(self):
        self.store.write(PATH, doc("nix", "Nix", "- fact\n"), self.v1)
        self.assertEqual(self.store.history(PATH), [])

    def test_keeping_zero_versions_turns_history_off(self):
        store.Store(self.store.root, history_keep=0).update(
            PATH, lambda cur: cur + b"- x\n", version(self.store.read_bytes(PATH)))
        self.assertEqual(self.store.history(PATH), [])

    def test_history_of_a_never_valid_path_is_refused(self):
        with self.assertRaises(StoreError) as caught:
            self.store.history("global/evil/../x.md")
        self.assertEqual(caught.exception.code, "invalid_path")

    def test_restore_refuses_an_invalid_path(self):
        with self.assertRaises(StoreError) as caught:
            self.store.restore("../escape.md")
        self.assertEqual(caught.exception.code, "invalid_path")

    def test_restore_without_any_history(self):
        self.store.write("global/people/sam.md", doc("sam", "Sam"), "new")
        with self.assertRaises(StoreError) as caught:
            self.store.restore("global/people/sam.md")
        self.assertEqual(caught.exception.code, "not_found")

    def test_restore_with_an_unknown_version(self):
        with self.assertRaises(StoreError) as caught:
            self.store.restore(PATH, "000000000000")
        self.assertEqual(caught.exception.code, "not_found")

    def test_restore_with_an_ambiguous_prefix(self):
        # Two entries, and a one-character prefix matching both.
        self.store.update(PATH, lambda cur: cur + b"- a\n")
        self.store.update(PATH, lambda cur: cur + b"- b\n")
        entries = self.store.history(PATH)
        prefix = os.path.commonprefix([e["version"] for e in entries])[:1]
        with self.assertRaises(StoreError) as caught:
            self.store.restore(PATH, prefix)
        self.assertEqual(caught.exception.code, "ambiguous_version")

    @unittest.skipIf(sys.platform == "win32", "chmod can't make a directory unwritable there")
    def test_unwritable_history_does_not_break_the_write(self):
        target = self.store.root / ".history"
        target.mkdir()
        os.chmod(target, 0o500)
        self.addCleanup(os.chmod, target, 0o700)
        with self.assertLogs("memory_buckets.store", level="WARNING"):
            got = self.store.update(PATH, lambda cur: cur + b"- x\n", self.v1)
        self.assertEqual(self.store.read(PATH)["version"], got["version"])
        self.assertEqual(self.store.history(PATH), [])

    def test_history_is_invisible_to_the_index_and_lint(self):
        planted = self.store.root / ".history" / "global" / "topics" / "nix.md" / "junk.md"
        planted.parent.mkdir(parents=True)
        planted.write_text("planted", encoding="utf-8")
        self.assertEqual(list(self.store.iter_paths()), [PATH])
        self.assertEqual(cli.lint(self.store), [])
        idx = index.Index(self.store)
        try:
            idx.reconcile()
            self.assertEqual([f[0] for f in idx.files()], [PATH], "the planted .history file isn't indexed")
        finally:
            idx.close()


class RetentionTest(StoreCase):
    def test_keep_limit(self):
        small = store.Store(self.store.root, history_keep=3)
        versions = [small.write(PATH, doc("nix", "Nix"), "new")["version"]]
        for i in range(6):
            versions.append(small.update(PATH, lambda cur, i=i: cur + f"- {i}\n".encode(),
                                         versions[-1])["version"])
        # Each update records the version it replaced: versions[0..5]. Three survive.
        entries = small.history(PATH)
        self.assertEqual([e["version"] for e in entries], versions[5:2:-1])
        self.assertEqual(entries[0]["bytes"], len(small.read_bytes(PATH)) - len(b"- 5\n"))

    def test_days_limit(self):
        keep = store.Store(self.store.root, history_days=90)
        v = keep.write(PATH, doc("nix", "Nix"), "new")["version"]
        v = keep.update(PATH, lambda cur: cur + b"- one\n", v)["version"]
        entry_dir = keep._history_dir(PATH)
        old_name = min(os.listdir(entry_dir))
        os.rename(entry_dir / old_name, entry_dir / f"20200101T000000.000000Z-{version(doc('nix', 'Nix').encode())}.md")
        v = keep.update(PATH, lambda cur: cur + b"- two\n", v)["version"]
        self.assertEqual(len(keep.history(PATH)), 1, "the 2020 entry is gone, the fresh one stays")

    def test_unparseable_entries_are_not_pruned(self):
        keep = store.Store(self.store.root, history_days=90)
        v = keep.write(PATH, doc("nix", "Nix"), "new")["version"]
        v = keep.update(PATH, lambda cur: cur + b"- one\n", v)["version"]
        entry_dir = keep._history_dir(PATH)
        (entry_dir / "stray.md").write_text("x", encoding="utf-8")
        keep.update(PATH, lambda cur: cur + b"- two\n", v)["version"]
        self.assertTrue((entry_dir / "stray.md").exists())


class ToolsHistoryTest(StoreCase):
    """The agent-facing tools record through the same path."""

    def setUp(self):
        super().setUp()
        self.put(PATH, doc("nix", "Nix", "- fact\n"))
        self.index = index.Index(self.store)
        self.addCleanup(self.index.close)
        self.caller = None

    def call(self, name, **args):
        c = config.Config()
        ctx = tools.Context(self.store, self.index, scopes.resolve(c, None, "primary", buckets=[]), c, source="test")
        return tools.handle(name, args, ctx)

    def test_overwrite_leaves_a_restorable_copy(self):
        got = self.call("memory_read", paths=[PATH])["files"][0]
        self.assertTrue(self.call("memory_write", path=PATH, description="Nix", body="- new\n",
                                  if_version=got["version"])["ok"])
        self.store.restore(PATH)
        self.assertIn("- fact\n", self.store.read(PATH)["content"])

    def test_str_replace_leaves_a_restorable_copy(self):
        self.assertTrue(self.call("memory_str_replace", path=PATH, old="- fact", new="- flakes")["ok"])
        self.store.restore(PATH)
        self.assertIn("- fact\n", self.store.read(PATH)["content"])

    def test_delete_leaves_a_restorable_copy(self):
        got = self.call("memory_read", paths=[PATH])["files"][0]
        self.assertTrue(self.call("memory_delete", path=PATH, if_version=got["version"])["ok"])
        self.assertIsNone(self.store.read_bytes(PATH))
        self.store.restore(PATH)
        self.assertIn("- fact\n", self.store.read(PATH)["content"])

    def test_append_records_nothing(self):
        before = self.store.history(PATH)
        self.assertTrue(self.call("memory_append", path=PATH, lines=["- more"])["ok"])
        self.assertEqual(self.store.history(PATH), before)

    def test_create_records_nothing(self):
        self.assertTrue(self.call("memory_write", path="global/people/sam.md", description="Sam",
                                  body="- climbs", if_version="new")["ok"])
        self.assertEqual(self.store.history("global/people/sam.md"), [])

    def test_inbox_append_records_nothing(self):
        inbox.append(self.store, "imported", ["- one"], source="test")
        self.assertEqual(self.store.history(inbox.PATH), [])


class CliHistoryTest(StoreCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORY_BUCKETS_")}
        env.update(MEMORY_BUCKETS_EMBEDDINGS_BACKEND="off", HERMES_HOME=str(self.home))
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out):
            code = run_hermes_cli(*argv)
        return code, out.getvalue()

    def test_history_and_restore(self):
        v1 = self.store.write(PATH, doc("nix", "Nix", "- fact\n"), "new")["version"]
        self.store.update(PATH, lambda cur: cur + b"- extra\n")
        code, out = self.run_cli("history", PATH)
        self.assertEqual(code, 0, out)
        self.assertIn(v1, out)
        self.assertRegex(out, r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
        code, out = self.run_cli("restore", PATH, v1)
        self.assertEqual(code, 0, out)
        self.assertIn("restored", out)
        self.assertIn("- fact\n", self.store.read(PATH)["content"])
        self.assertNotIn("- extra\n", self.store.read(PATH)["content"])

    def test_history_of_a_file_without_history_fails(self):
        self.store.write(PATH, doc("nix", "Nix"), "new")
        code, out = self.run_cli("history", PATH)
        self.assertEqual(code, 1, out)
        self.assertIn("no history", out)

    def test_restore_with_a_bad_version_fails(self):
        self.store.write(PATH, doc("nix", "Nix", "- fact\n"), "new")
        self.store.update(PATH, lambda cur: cur + b"- x\n")
        code, out = self.run_cli("restore", PATH, "000000000000")
        self.assertEqual(code, 1, out)
        self.assertIn("error:", out)


class ConfigHistoryTest(unittest.TestCase):
    def test_defaults(self):
        cfg = config.Config()
        self.assertEqual(cfg.history_keep, 20)
        self.assertEqual(cfg.history_days, 90)

    def test_from_dict_and_env(self):
        cfg = config.load({"history_keep": 5, "history_days": 30})
        self.assertEqual((cfg.history_keep, cfg.history_days), (5, 30))
        with mock.patch.dict(os.environ, {"MEMORY_BUCKETS_HISTORY_KEEP": "3", "MEMORY_BUCKETS_HISTORY_DAYS": "7"}):
            cfg = config.load({"history_keep": 5})
        self.assertEqual((cfg.history_keep, cfg.history_days), (3, 7))


if __name__ == "__main__":
    unittest.main()