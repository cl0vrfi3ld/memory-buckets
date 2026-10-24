"""A private store: created 0o700/0o600, flagged by ``status`` when loose, fixed by ``migrate``.

POSIX only. On Windows nothing here changes, which the last class pins down.
"""

import contextlib
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from .helpers import doc, run_hermes_cli, symlink_or_skip
from memory_buckets import index, migrate, provider, static_model, store

POSIX = sys.platform != "win32"
PROFILE = doc("profile", "Who the user is", "- Sam\n")


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


class PrivateCase(unittest.TestCase):
    """A temp directory and umask 022, the usual default and the one the issue measured.
    The store root is a child of the temp directory: mkdtemp makes the directory itself
    0o700, which would hide a root created with the umask's modes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)  # first registered, so last to run, after indexes close
        self.base = Path(self._tmp.name)
        self.root = self.base / "memory-buckets"
        self.store = store.Store(self.root)
        if POSIX:
            previous = os.umask(0o022)
            self.addCleanup(os.umask, previous)

    def loosen(self, root=None):
        """Put the store back as an older version left it: umask 022, nobody's secret."""
        root = self.root if root is None else Path(root)
        for dirpath, dirnames, filenames in os.walk(root):
            os.chmod(dirpath, 0o755)
            for name in filenames:
                os.chmod(os.path.join(dirpath, name), 0o644)

    def run_cli(self, *argv):
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORY_BUCKETS_")}
        env.update(MEMORY_BUCKETS_EMBEDDINGS_BACKEND="off", HERMES_HOME=str(self.base))
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out):
            code = run_hermes_cli(*argv)
        return code, out.getvalue()

    def start_provider(self):
        with mock.patch.dict(os.environ, {"MEMORY_BUCKETS_EMBEDDINGS_BACKEND": "off"}):
            p = provider.MemoryBucketsProvider()
            p.initialize("s1", hermes_home=str(self.base), platform="cli")
        self.addCleanup(p.shutdown)
        return p


@unittest.skipUnless(POSIX, "POSIX modes")
class CreatedPrivateTest(PrivateCase):
    def test_a_new_store_is_private(self):
        self.store.write("global/profile.md", PROFILE, "new")
        for rel in ("", "memories", "memories/global"):
            self.assertEqual(mode(self.root / rel), 0o700, rel or "(root)")
        for rel in ("memories/global/profile.md", ".lock"):
            self.assertEqual(mode(self.root / rel), 0o600, rel)

    def test_every_directory_made_below_the_root_is_private(self):
        # Path.mkdir(mode, parents=True) applies the mode to the leaf only.
        self.store.write("proj-1/topics/nix.md", doc("nix", "Nix notes"), "new")
        for rel in ("memories/proj-1", "memories/proj-1/topics"):
            self.assertEqual(mode(self.root / rel), 0o700, rel)

    def test_directories_above_the_root_are_left_to_the_umask(self):
        # Not the plugin's to restrict: only the store itself.
        self.store = store.Store(self.base / "a" / "b" / "store")
        self.store.write("global/profile.md", PROFILE, "new")
        self.assertEqual(mode(self.base / "a"), 0o755)
        self.assertEqual(mode(self.base / "a" / "b"), 0o755)
        self.assertEqual(mode(self.base / "a" / "b" / "store"), 0o700)

    def test_a_rewritten_file_comes_out_private(self):
        self.store.write("global/profile.md", PROFILE, "new")
        target = self.root / "memories/global/profile.md"
        os.chmod(target, 0o644)
        self.store.write("global/profile.md", PROFILE.replace("Sam", "Alex"), self.store.read("global/profile.md")["version"])
        self.assertEqual(mode(target), 0o600)

    def test_the_search_index_is_private_with_its_journal_files(self):
        self.store.write("global/profile.md", PROFILE, "new")
        idx = index.Index(self.store)
        self.addCleanup(idx.close)
        idx.reconcile()
        self.assertEqual(mode(self.root / ".index"), 0o700)
        files = sorted(p.name for p in (self.root / ".index").iterdir())
        self.assertIn("memory.sqlite", files)
        for name in files:
            self.assertEqual(mode(self.root / ".index" / name), 0o600, name)

    def test_a_rebuilt_index_is_private_too(self):
        self.store.write("global/profile.md", PROFILE, "new")
        idx = index.Index(self.store)
        self.addCleanup(idx.close)
        idx.reconcile()
        idx.rebuild()
        idx.reconcile()
        for p in (self.root / ".index").iterdir():
            self.assertEqual(mode(p), 0o600, p.name)

    def test_backups_a_migration_keeps_are_private(self):
        self.store._atomic_write(self.root / migrate.BACKUP_DIR / "0.1.0" / "proj-1" / "index.md", b"old")
        for rel in (migrate.BACKUP_DIR, f"{migrate.BACKUP_DIR}/0.1.0", f"{migrate.BACKUP_DIR}/0.1.0/proj-1"):
            self.assertEqual(mode(self.root / rel), 0o700, rel)
        self.assertEqual(mode(self.root / migrate.BACKUP_DIR / "0.1.0" / "proj-1" / "index.md"), 0o600)

    def test_the_provider_creates_the_store_private(self):
        self.start_provider()
        self.assertEqual(mode(self.root), 0o700)
        self.assertEqual(mode(self.root / "memories"), 0o700)
        self.assertEqual(mode(self.root / "memories" / "global"), 0o700)

    def test_fetch_model_creates_the_store_root_private(self):
        # static_model.fetch mkdirs <root>/.models with parents, and nothing else in
        # that path goes through ensure_dir: on a fresh machine the root would take
        # the umask's mode. The model itself stays public, shielded by the root.
        def offline(store_root, **_):
            dest = static_model.download_dir(store_root)  # what the real fetch mkdirs first
            dest.mkdir(parents=True, exist_ok=True)
            raise OSError("no network")

        # Point BUNDLED_DIR at a path that cannot exist: the Nix build ships a
        # complete model in memory_buckets/model/, and cmd_fetch_model short-circuits
        # to "already available" before the mocked fetch runs when locate() finds one.
        with mock.patch.object(static_model, "fetch", offline), \
                mock.patch.object(static_model, "BUNDLED_DIR", self.base / "none"):
            code, out = self.run_cli("fetch-model")
        self.assertEqual(code, 1, out)
        self.assertEqual(mode(self.root), 0o700)
        self.assertEqual(mode(self.root / store.MODELS_DIR), 0o755)
        self.assertEqual(self.store.loose_modes(), [])

    def test_every_write_path_leaves_the_store_private(self):
        # One run of every creation path, at the umask loose (0o022): a path that
        # skips ensure_dir shows up as a loose mode instead of hiding behind the
        # 0o700 assertions on the chosen paths above. history (from the overwrite,
        # the delete and the restore), the recreated directory and the index all
        # have to come out private.
        version = self.store.write("global/profile.md", PROFILE, "new")["version"]
        self.store.write("global/profile.md", PROFILE.replace("Sam", "Alex"), version)
        nix = doc("nix", "Nix notes")
        self.store.write("proj-1/topics/nix.md", nix, "new")
        self.store.delete("proj-1/topics/nix.md", self.store.read("proj-1/topics/nix.md")["version"])
        self.store.restore("proj-1/topics/nix.md")
        self.assertTrue((self.root / store.HISTORY_DIR / "global").is_dir())
        idx = index.Index(self.store)
        self.addCleanup(idx.close)
        idx.reconcile()
        # Before migrate, so the assertion sees what the write paths made, not
        # what tighten fixed: this is the one that catches a skipped ensure_dir.
        self.assertEqual(self.store.loose_modes(), [])
        migrate.run(self.store)
        # And after: on a private store migrate has nothing to tighten.
        self.assertEqual(self.store.loose_modes(), [])

    def test_a_path_that_is_a_file_still_raises(self):
        (self.base / "taken").write_text("x", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            store.Store(self.base / "taken" / "store").ensure_dir(self.base / "taken")


@unittest.skipUnless(POSIX, "POSIX modes")
class LooseStoreTest(PrivateCase):
    def setUp(self):
        super().setUp()
        self.store.write("global/profile.md", PROFILE, "new")
        self.store.write("proj-1/topics/nix.md", doc("nix", "Nix notes"), "new")
        self.cli = self.run_cli

    def test_a_private_store_has_nothing_loose(self):
        self.assertEqual(self.store.loose_modes(), [])
        code, out = self.cli("status")
        self.assertEqual(code, 0, out)
        self.assertNotIn("permissions", out)

    def test_status_flags_a_loose_store_and_says_how_to_fix_it(self):
        self.loosen()
        code, out = self.cli("status")
        self.assertEqual(code, 0, out)  # a warning, like the migration lines
        line = next(ln for ln in out.splitlines() if ln.startswith("permissions"))
        self.assertIn("can be read by group or other", line)
        self.assertIn("hermes memory-buckets migrate", line)
        self.assertIn("0755", line)  # the first offender is named with its mode

    def test_status_flags_a_single_loose_file_in_a_private_store(self):
        os.chmod(self.root / "memories/global/profile.md", 0o640)
        _, out = self.cli("status")
        self.assertIn("1 path(s)", out)
        self.assertIn("memories/global/profile.md", out)
        self.assertIn("0640", out)

    def test_the_downloaded_model_is_not_a_secret_and_is_not_flagged(self):
        # store.MODELS_DIR repeats the literal static_model uses; this keeps the two together.
        self.assertEqual(static_model.download_dir(self.root).parent.name, store.MODELS_DIR)
        models = self.root / store.MODELS_DIR
        models.mkdir()
        (models / "model.safetensors").write_bytes(b"weights")
        os.chmod(models, 0o755)
        os.chmod(models / "model.safetensors", 0o644)
        self.assertEqual(self.store.loose_modes(), [])

    def test_a_symlink_is_neither_followed_nor_listed(self):
        outside = self.base / "outside.md"
        outside.write_text("x", encoding="utf-8")
        os.chmod(outside, 0o644)
        symlink_or_skip(self, outside, self.root / "memories/global/link.md")
        self.assertEqual(self.store.loose_modes(), [])
        migrate.run(self.store)
        self.assertEqual(mode(outside), 0o644, "the target of a link is not ours to chmod")

    def test_migrate_dry_run_reports_and_changes_nothing(self):
        self.loosen()
        code, out = self.cli("migrate", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("would:", out)
        self.assertIn("group and other access", out)
        self.assertEqual(mode(self.root), 0o755)
        self.assertEqual(mode(self.root / "memories/global/profile.md"), 0o644)

    def test_migrate_tightens_everything_and_a_second_run_has_nothing_to_do(self):
        self.loosen()
        code, out = self.cli("migrate")
        self.assertEqual(code, 0, out)
        self.assertIn("group and other access", out)
        self.assertNotIn("originals kept", out, "no file was rewritten, so there is no backup")
        for dirpath, dirnames, filenames in os.walk(self.root):
            self.assertEqual(mode(dirpath), 0o700, dirpath)
            for name in filenames:
                self.assertEqual(mode(os.path.join(dirpath, name)), 0o600, name)
        self.assertEqual(self.cli("migrate"), (0, "nothing to migrate\n"))
        self.assertNotIn("permissions", self.cli("status")[1])

    def test_migrate_only_removes_access_it_never_adds_any(self):
        self.loosen()
        target = self.root / "memories/global/profile.md"
        os.chmod(target, 0o444)  # read-only for everyone
        migrate.run(self.store)
        self.assertEqual(mode(target), 0o400, "read-only stays read-only; the owner gains nothing")
        os.chmod(target, 0o664)
        migrate.run(self.store)
        self.assertEqual(mode(target), 0o600)

    def test_a_path_that_cannot_be_changed_is_reported_and_the_rest_still_are(self):
        self.loosen()
        real_chmod = os.chmod

        def chmod(path, m, *a, **k):
            if str(path).endswith("profile.md"):
                raise PermissionError(1, "Operation not permitted", str(path))
            return real_chmod(path, m, *a, **k)

        with mock.patch.object(os, "chmod", chmod):
            code, out = self.cli("migrate")
        self.assertEqual(code, 1, out)
        self.assertIn("memories/global/profile.md", out)
        self.assertIn("left as it is", out)
        self.assertEqual(mode(self.root / "memories/proj-1/topics/nix.md"), 0o600)
        self.assertEqual(mode(self.root), 0o700)

    def test_starting_the_provider_tightens_a_loose_store(self):
        self.loosen()
        with self.assertLogs("memory_buckets", "INFO") as logs:
            self.start_provider()
        self.assertEqual(self.store.loose_modes(), [])
        self.assertIn("group and other access", " ".join(r.getMessage() for r in logs.records))


@unittest.skipUnless(POSIX, "POSIX modes")
class SymlinkedRootTest(PrivateCase):
    """A store reached through a symlinked root (a legacy store behind a dotfile
    symlink, ``cfg.store_path`` pointing at one) is still the user's store: the
    permission walk resolves the root instead of skipping it."""

    def test_a_loose_store_behind_a_symlinked_root_is_reported_and_fixed(self):
        real = self.base / "vault"
        vault = store.Store(real)
        vault.write("global/profile.md", PROFILE, "new")
        vault.write("proj-1/topics/nix.md", doc("nix", "Nix notes"), "new")
        self.loosen(real)
        self.assertEqual(mode(real), 0o755)
        symlink_or_skip(self, real, self.root)  # self.base/memory-buckets -> vault
        self.assertIn((".", 0o755), self.store.loose_modes())
        self.assertIn(("memories/global/profile.md", 0o644), self.store.loose_modes())
        code, out = self.run_cli("status")
        self.assertEqual(code, 0, out)
        line = next(ln for ln in out.splitlines() if ln.startswith("permissions"))
        self.assertIn("can be read by group or other", line)
        self.assertIn("0755", line)
        code, out = self.run_cli("migrate")
        self.assertEqual(code, 0, out)
        self.assertIn("group and other access", out)
        self.assertEqual(mode(real), 0o700)
        self.assertEqual(mode(real / "memories/global/profile.md"), 0o600)
        self.assertEqual(mode(real / "memories/proj-1/topics/nix.md"), 0o600)
        self.assertEqual(self.store.loose_modes(), [])
        self.assertNotIn("permissions", self.run_cli("status")[1])


class WindowsNoOpTest(PrivateCase):
    """Windows keeps the old behaviour: the modes are the platform's, and nothing is
    flagged or changed. Faked where this runs on POSIX, real where it runs on Windows."""

    def fake_windows(self):
        patcher = mock.patch.object(store, "private_modes", lambda: False, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def use(self):
        """Create a store, run a migration and read status: all of it must leave modes alone."""
        self.store.write("global/profile.md", PROFILE, "new")
        idx = index.Index(self.store)
        self.addCleanup(idx.close)
        idx.reconcile()
        return self.run_cli("status")

    def check(self):
        with mock.patch.object(os, "chmod") as chmod:
            code, out = self.use()
            self.assertEqual(migrate.run(self.store), [])
        chmod.assert_not_called()
        self.assertEqual(code, 0, out)
        self.assertNotIn("permissions", out)
        self.assertEqual(self.store.loose_modes(), [])

    @unittest.skipUnless(POSIX, "fakes Windows on a POSIX host")
    def test_create_flag_and_migrate_do_nothing_about_modes(self):
        self.fake_windows()
        self.check()
        # The umask still decides, as before: 022 leaves directories at 0o755 and files at 0o644.
        self.assertEqual(mode(self.root), 0o755)
        self.assertEqual(mode(self.root / "memories"), 0o755)
        self.assertEqual(mode(self.root / ".lock"), 0o644)
        self.assertEqual(mode(self.root / "memories/global/profile.md"), 0o644)
        self.assertEqual(mode(self.root / ".index"), 0o755)
        self.assertEqual(mode(self.root / ".index" / "memory.sqlite"), 0o644)

    @unittest.skipUnless(POSIX, "fakes Windows on a POSIX host")
    def test_a_loose_store_is_not_flagged_on_windows(self):
        self.store.write("global/profile.md", PROFILE, "new")
        self.loosen()
        self.fake_windows()
        self.assertEqual(self.store.loose_modes(), [])
        self.assertEqual(migrate.run(self.store), [])
        self.assertNotIn("permissions", self.run_cli("status")[1])

    @unittest.skipUnless(sys.platform == "win32", "the real thing")
    def test_on_windows_itself(self):
        self.check()
