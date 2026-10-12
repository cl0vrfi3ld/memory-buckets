import contextlib
import io
import os
import unittest
from unittest import mock

from .helpers import FakeEmbedder, StoreCase, doc, fake_hermes, run_hermes_cli
from memory_buckets import cli, inbox


class CliTest(StoreCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORY_BUCKETS_")}
        env.update(MEMORY_BUCKETS_EMBEDDINGS_BACKEND="off", HERMES_HOME=str(self.home))
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out):
            code = run_hermes_cli(*argv)
        return code, out.getvalue()

    def test_status_outside_hermes(self):
        self.put("global/profile.md", doc("profile", "Who"))
        self.put("proj-1/profile.md", doc("profile", "proj-1"))
        code, out = self.run_cli("status")
        self.assertEqual(code, 0, out)
        self.assertIn("global 1, proj-1 1", out)
        self.assertIn("embeddings  off", out)
        self.assertIn("2 files", out)

    def test_status_with_the_built_in_model_missing_isnt_a_failure(self):
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORY_BUCKETS_")}
        env.update(MEMORY_BUCKETS_EMBEDDINGS_AUTO_DOWNLOAD="0", MEMORY_BUCKETS_MODEL_DIR=str(self.home / "nowhere"),
                   HERMES_HOME=str(self.home))
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("memory_buckets.static_model.BUNDLED_DIR", self.home / "none"), \
                contextlib.redirect_stdout(out):
            code = run_hermes_cli("status")
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("not ready", out.getvalue())
        self.assertIn("fetch-model", out.getvalue())

    def test_import_builtin_into_inbox(self):
        (self.home / "memories").mkdir()
        (self.home / "memories/USER.md").write_text("Name is Ivy\n§\nUses they/them\n§\n")
        (self.home / "memories/MEMORY.md").write_text("Host runs NixOS\nwith flakes")
        code, out = self.run_cli("import", "--dry-run")
        self.assertIn("would import 2 entries", out)
        self.assertFalse((self.store.memories / "global/inbox.md").exists())
        code, out = self.run_cli("import")
        self.assertEqual(code, 0, out)
        text = self.store.read("global/inbox.md")["content"]
        self.assertIn(f"description: {inbox.DESCRIPTION}\n", text)
        self.assertIn("- Name is Ivy\n- Uses they/them\n", text)
        self.assertIn("- Host runs NixOS with flakes\n", text)
        self.assertIn("  - cli:import", text)
        self.assertEqual(text.count("## imported"), 2)
        self.assertIn("----- prompt -----", out)
        self.assertIn("global/inbox.md", out.split("----- prompt -----")[1])
        self.assertIn("cp -a", out)
        code, again = self.run_cli("import-prompt")
        self.assertEqual(code, 0)
        self.assertEqual(again, out[out.index("\nNext:"):])

    def test_import_recreates_a_deleted_inbox(self):
        (self.home / "memories").mkdir()
        (self.home / "memories/USER.md").write_text("Name is Ivy\n")
        self.put("global/inbox.md", doc("inbox", "Old description", "## imported\n- a\n"))
        (self.store.memories / "global/inbox.md").unlink()
        code, out = self.run_cli("import")
        self.assertEqual(code, 0, out)
        text = self.store.read("global/inbox.md")["content"]
        self.assertIn(f"description: {inbox.DESCRIPTION}\n", text)
        self.assertIn("- Name is Ivy\n", text)
        self.assertNotIn("- a\n", text)

    def test_import_prompt_needs_an_inbox(self):
        code, out = self.run_cli("import-prompt")
        self.assertEqual(code, 1)
        self.assertIn("has nothing to sort", out)

    def test_import_prompt_prints_the_skill_body(self):
        self.put("global/inbox.md", doc("inbox", inbox.DESCRIPTION, "## imported\n- a\n"))
        code, out = self.run_cli("import-prompt")
        self.assertEqual(code, 0, out)
        self.assertEqual(cli.SKILL_FILE.parent.name, "sort-imports")
        self.assertIn(cli.import_prompt(), out)
        self.assertIn("hermes memory-buckets import-prompt", out, "tells the user how to print it again")
        self.assertTrue(cli.import_prompt().startswith("# Sort the imported memory"))

    def test_removed_commands_have_no_alias(self):
        # D9: sort-prompt became import-prompt; proposals are gone with their commands.
        for command in ("sort-prompt", "pending", "apply", "reject"):
            with self.subTest(command), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    run_hermes_cli(command)
                self.assertEqual(caught.exception.code, 2)

    def test_the_skill_has_no_proposal_instructions(self):
        text = cli.SKILL_FILE.read_text().lower()
        self.assertIn("name: sort-imports\n", text)
        for gone in ("propos", "/memory-apply", "/memory-pending", "/memory-reject", "sort-inbox"):
            self.assertNotIn(gone, text)

    def test_diagnose_checks_sort_imports(self):
        import tempfile
        from pathlib import Path
        lines = []
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as out:
            cli._diagnose_skill(Path(tmp), lambda ok, msg: lines.append((ok, msg)))
            self.assertEqual(len(lines), 1)
            self.assertFalse(lines[0][0])
            self.assertIn(f"no sort-imports/SKILL.md in {tmp}", lines[0][1])
            (Path(tmp) / "sort-imports").mkdir()
            (Path(tmp) / "sort-imports/SKILL.md").write_text("---\nname: sort-imports\n---\nx\n")
            lines.clear()
            cli._diagnose_skill(Path(tmp), lambda ok, msg: lines.append((ok, msg)))
            self.assertEqual(lines, [(True, f"skill file {Path(tmp) / 'sort-imports/SKILL.md'}")])
        self.assertIn("/sort-imports", out.getvalue(), "outside Hermes it says what it can't check")

    def test_the_skill_keeps_sentences_short(self):
        # House style for model-facing text: sentences of 15 words or fewer.
        import re
        for line in cli.import_prompt().splitlines():
            line = re.sub(r"^\s*(?:[-*]|\d+\.)?\s*(?:\*\*[^*]+\*\*)?\s*", "", line)
            if not line or line.startswith("#"):
                continue
            for sentence in re.split(r"(?<=[.?!])\s+", line):
                self.assertLessEqual(len(sentence.split()), 15, sentence)
        text = cli.import_prompt()
        self.assertIn("could not place them. Give a one-line reason for each.", text)
        self.assertIn('If the file does not exist, use memory_write with if_version "new".', text)
        self.assertIn("Drop an entry only if it is stale, already saved, or not worth keeping.", text)

    def test_import_prompt_only_names_real_tools(self):
        import re
        from memory_buckets import tools
        named = set(re.findall(r"memory_[a-z_]+", cli.import_prompt()))
        self.assertTrue(named)
        self.assertLessEqual(named, set(tools.TOOL_NAMES))

    def test_import_nothing(self):
        code, out = self.run_cli("import", "--from", str(self.home / "nope"))
        self.assertEqual(code, 1)
        self.assertIn("nothing to import", out)

    def test_status_matches_buckets_to_hermes_projects(self):
        self.put("proj-1/profile.md", doc("profile", "proj-1"))
        self.put("old-proj/profile.md", doc("profile", "old-proj"))
        with fake_hermes({"proj-1": "/src/proj-1", "fresh": "/src/fresh"}):
            code, out = self.run_cli("status")
        self.assertIn("2 Hermes project(s); 1 with a bucket (proj-1)", out)
        self.assertIn("no Hermes project (no session is scoped to them): old-proj", out)
        self.assertIn("--slug <bucket>", out)

    def test_lint(self):
        self.put("global/topics/nix.md", doc("nix", "Nix", "- see [[sam]] and [[nope]] and [[proj-1/profile]]\n"))
        self.put("global/people/sam.md", doc("sam", "Sam"))
        self.put("global/areas/sam.md", doc("sam", "Also sam"))
        self.put("global/topics/bad.md", "no frontmatter\n")
        self.put("global/Stray.md", "x")
        self.put("proj-1/profile.md", doc("profile", "proj-1"))
        (self.store.memories / ".obsidian").mkdir()
        (self.store.memories / ".obsidian/app.json").write_text("{}")
        self.put("proj-1/index.md", doc("index", "left over from an older version"))
        code, out = self.run_cli("lint")
        self.assertEqual(code, 1)
        self.assertIn("proj-1/index.md: left over from an older version; run `hermes memory-buckets migrate`", out)
        self.assertIn("broken link [[nope]]", out)
        self.assertNotIn("[[sam]]\n", out.replace("is ambiguous", ""))
        self.assertIn("name 'sam' is used by", out)
        self.assertIn("global/topics/bad.md: bad frontmatter", out)
        self.assertIn("global/Stray.md: not a memory path", out)
        self.assertNotIn(".obsidian", out)
        self.assertNotIn("[[proj-1/profile]]", out)

    def test_lint_only_sends_real_leftovers_to_migrate(self):
        # migrate ignores global/ and directories that aren't project ids.
        self.put("global/index.md", doc("index", "x"))
        code, out = self.run_cli("lint")
        self.assertIn("global/index.md: not a memory path", out)
        self.assertNotIn("migrate", out)

    def test_inbox_messages_count_entries(self):
        self.put("global/inbox.md", doc("inbox", inbox.DESCRIPTION, "## imported\n"))
        self.assertNotIn("inbox ", self.run_cli("status")[1])
        self.assertNotIn("waiting to be sorted", self.run_cli("lint")[1])
        code, out = self.run_cli("import-prompt")
        self.assertEqual(code, 1)
        self.assertIn("has nothing to sort", out)
        self.put("global/inbox.md", doc("inbox", inbox.DESCRIPTION,
                                        "## imported\n- a\n- b\n```\n- not an entry\n```\n"))
        self.assertIn("inbox       global/inbox.md: 2 entries waiting to be sorted", self.run_cli("status")[1])
        self.assertIn("note: global/inbox.md has 2 entries waiting to be sorted", self.run_cli("lint")[1])
        self.assertEqual(self.run_cli("import-prompt")[0], 0)

    def test_old_inbox_description_is_reported(self):
        self.put("global/inbox.md", doc("inbox", "Imported memory; sort it, then delete it", "- a\n"))
        self.assertIn("migration   global/inbox.md has an outdated description", self.run_cli("status")[1])
        code, out = self.run_cli("lint")
        self.assertEqual(code, 1)
        self.assertIn("global/inbox.md: outdated description", out)
        code, out = self.run_cli("migrate")
        self.assertEqual(code, 0, out)
        self.assertIn("global/inbox.md: description set to the current one", out)
        self.assertEqual(self.run_cli("lint")[0], 0)

    def test_migrate_command(self):
        self.put("proj-1/index.md", doc("index", "proj-1 overview", "- a web app\n"))
        code, out = self.run_cli("status")
        self.assertIn("migration   index.md left over in: proj-1", out)
        code, out = self.run_cli("migrate", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("would: proj-1/index.md becomes proj-1/profile.md", out)
        self.assertTrue((self.store.memories / "proj-1/index.md").exists())
        code, out = self.run_cli("migrate")
        self.assertEqual(code, 0, out)
        self.assertIn("originals kept in", out)
        self.assertFalse((self.store.memories / "proj-1/index.md").exists())
        self.assertEqual(self.run_cli("migrate")[1].strip(), "nothing to migrate")

    def test_leftover_pending_is_reported_until_migrate_runs(self):
        pending = self.store.root / "_pending"
        pending.mkdir()
        for pid in ("a1b2c3", "d4e5f6"):
            (pending / f"{pid}.json").write_text(f'{{"id": "{pid}", "project": "proj-1"}}\n')
        self.put("global/inbox.md", doc("inbox", inbox.DESCRIPTION, "## added in conversation 2026-10-02\n- a\n"))
        code, out = self.run_cli("status")
        self.assertIn("migration   _pending/ left over from 0.1.0 (2 proposals); run `hermes memory-buckets migrate`", out)
        code, out = self.run_cli("lint")
        self.assertEqual(code, 1)
        self.assertIn(f"{pending}/: left over from 0.1.0 (2 proposals); run `hermes memory-buckets migrate`", out)
        code, out = self.run_cli("migrate", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines(), [
            "would: _pending/a1b2c3.json (a proposal for proj-1) kept in _backup/0.2.0/_pending/ and removed",
            "would: _pending/d4e5f6.json (a proposal for proj-1) kept in _backup/0.2.0/_pending/ and removed",
            "would: _pending/ removed: its proposals weren't applied. Their lines are still in global/inbox.md; "
            "/sort-imports files them",
        ])
        self.assertTrue(pending.is_dir())
        code, out = self.run_cli("migrate")
        self.assertEqual(code, 0, out)
        self.assertIn(f"originals kept in {self.store.root / '_backup'}\n", out)
        self.assertFalse(pending.exists())
        self.assertNotIn("_pending", self.run_cli("status")[1])
        code, out = self.run_cli("lint")
        self.assertEqual(code, 0, out)
        self.assertNotIn("_pending", out)
        self.assertEqual(self.run_cli("migrate")[1].strip(), "nothing to migrate")

    def test_an_empty_leftover_pending_is_still_reported(self):
        (self.store.root / "_pending").mkdir()
        self.assertIn("_pending/ left over from 0.1.0 (0 proposals)", self.run_cli("status")[1])
        code, out = self.run_cli("migrate")
        self.assertEqual((code, out.splitlines()[0]), (0, "_pending/ removed: it was empty"))

    def test_reindex_and_search(self):
        self.put("global/topics/nix.md", doc("nix", "Nix", "- flakes pin inputs\n"))
        code, out = self.run_cli("reindex")
        self.assertIn("indexed 1 file", out)
        code, out = self.run_cli("search", "flakes")
        self.assertIn("global/topics/nix.md: Nix", out)
        code, out = self.run_cli("search", "flakes", "--json")
        self.assertIn('"mode": "fts"', out)

    def test_hints_needs_embeddings(self):
        code, out = self.run_cli("hints", "anything")
        self.assertEqual(code, 1)
        self.assertIn("embeddings are off", out)

    def test_hints_shows_similarities_and_the_cut(self):
        self.put("global/people/sam.md", doc("sam", "Who Sam is", "- Sam likes climbing\n"))
        self.put("global/topics/nix.md", doc("nix", "Nix", "- flakes pin inputs\n"))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)}), contextlib.redirect_stdout(out), \
                mock.patch.object(cli, "make_embedder", lambda *a, **k: FakeEmbedder()):
            code = run_hermes_cli("hints", "Sam likes climbing", "--threshold", "0.5")
        lines = out.getvalue().splitlines()
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].endswith("global/people/sam.md: Who Sam is"))
        self.assertTrue(any(l.startswith("------  prefetch_min_similarity = 0.5") for l in lines))

    def test_no_command(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_cli()[0], 2)


if __name__ == "__main__":
    unittest.main()
