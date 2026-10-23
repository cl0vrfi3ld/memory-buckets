import contextlib
import io
import os
import unittest
from unittest import mock

from .helpers import FakeEmbedder, StoreCase, doc, fake_hermes, run_hermes_cli, symlink_or_skip
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

    def test_status_does_not_spawn_download_or_create_models_dir_when_auto_download_enabled(self):
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORY_BUCKETS_")}
        env.update(MEMORY_BUCKETS_MODEL_DIR=str(self.home / "nowhere"), HERMES_HOME=str(self.home))
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("memory_buckets.static_model.BUNDLED_DIR", self.home / "none"), \
                mock.patch("memory_buckets.static_model.fetch_in_background") as bg_fetch, \
                contextlib.redirect_stdout(out):
            code = run_hermes_cli("status")
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("not ready", out.getvalue())
        bg_fetch.assert_not_called()
        self.assertFalse((self.store.root / ".models").exists())

    def test_import_builtin_into_inbox(self):
        (self.home / "memories").mkdir()
        (self.home / "memories/USER.md").write_text("Name is Sam\n§\nUses they/them\n§\n", encoding="utf-8")
        (self.home / "memories/MEMORY.md").write_text("Host runs NixOS\nwith flakes", encoding="utf-8")
        code, out = self.run_cli("import", "--dry-run")
        self.assertIn("would import 2 entries", out)
        self.assertFalse((self.store.memories / "global/inbox.md").exists())
        code, out = self.run_cli("import")
        self.assertEqual(code, 0, out)
        text = self.store.read("global/inbox.md")["content"]
        self.assertIn(f"description: {inbox.DESCRIPTION}\n", text)
        self.assertIn("- Name is Sam\n- Uses they/them\n", text)
        self.assertIn("- Host runs NixOS with flakes\n", text)
        self.assertIn("  - cli:import", text)
        self.assertEqual(text.count("## imported"), 2)
        self.assertIn("----- prompt -----", out)
        self.assertIn("global/inbox.md", out.split("----- prompt -----")[1])
        self.assertIn(cli._backup_commands(self.store)[0], out)
        code, again = self.run_cli("import-prompt")
        self.assertEqual(code, 0)
        self.assertEqual(again, out[out.index("\nNext:"):])

    def test_backup_and_review_commands_suit_the_shell(self):
        root = str(self.store.root)
        with mock.patch.object(cli.sys, "platform", "linux"):
            self.assertEqual(cli._backup_commands(self.store),
                             (f"cp -a {root} {root}.before-sort", f"diff -ru {root}.before-sort/memories {self.store.memories}"))
        with mock.patch.object(cli.sys, "platform", "win32"):  # PowerShell: no cp -a, no diff -ru
            backup, review = cli._backup_commands(self.store)
        self.assertEqual(backup, f'Copy-Item -Recurse "{root}" "{root}.before-sort"')
        self.assertTrue(review.startswith(f'git diff --no-index "{root}.before-sort'), review)

    def test_import_recreates_a_deleted_inbox(self):
        (self.home / "memories").mkdir()
        (self.home / "memories/USER.md").write_text("Name is Sam\n", encoding="utf-8")
        self.put("global/inbox.md", doc("inbox", "Old description", "## imported\n- a\n"))
        (self.store.memories / "global/inbox.md").unlink()
        code, out = self.run_cli("import")
        self.assertEqual(code, 0, out)
        text = self.store.read("global/inbox.md")["content"]
        self.assertIn(f"description: {inbox.DESCRIPTION}\n", text)
        self.assertIn("- Name is Sam\n", text)
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
        text = cli.SKILL_FILE.read_text(encoding="utf-8").lower()
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
            (Path(tmp) / "sort-imports/SKILL.md").write_text("---\nname: sort-imports\n---\nx\n", encoding="utf-8")
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
        self.assertIn("Set old to the bullet line, including its trailing newline (\\n).", text)

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

    def test_status_reports_files_the_index_skips(self):
        self.put("global/topics/nix.md", doc("nix", "Nix"))
        self.put("global/topics/My Note.md", "x")
        self.put("global/topics/nix/flakes.md", "x")
        self.put("global/topics/ex_luna.md", "x")
        self.put("global/notes.md", "x")
        self.put(".obsidian/app.json", "{}")
        code, out = self.run_cli("status")
        self.assertEqual(code, 0, out)  # a notice, not a fault
        self.assertIn("ignored     4 files outside the path rules, so the agent can't see them; "
                      "run `hermes memory-buckets lint`", out)
        self.assertNotIn("frontmatter ", out)
        self.assertIn("1 files", out)  # the index still has only the valid one

    def test_status_says_nothing_about_a_clean_store(self):
        self.put("global/topics/nix.md", doc("nix", "Nix"))
        code, out = self.run_cli("status")
        self.assertNotIn("ignored", out)
        self.assertNotIn("frontmatter ", out)

    def test_status_singular_wording(self):
        self.put("global/notes.md", "x")
        self.put("global/topics/bad.md", "no frontmatter\n")
        out = self.run_cli("status")[1]
        self.assertIn("ignored     1 file outside the path rules, so the agent can't see it;", out)
        self.assertIn("frontmatter 1 file with invalid frontmatter, so tools won't edit it until it's fixed;", out)

    def test_status_reports_unparseable_frontmatter(self):
        self.put("global/topics/nix.md", doc("nix", "Nix"))
        self.put("global/topics/bad.md", "no frontmatter\n")
        self.put("global/topics/dupe.md", "---\nname: dupe\nname: dupe\ndescription: x\n---\n")
        self.put("global/topics/wrong.md", doc("other", "name does not match the file"))
        code, out = self.run_cli("status")
        self.assertEqual(code, 0, out)
        self.assertIn("frontmatter 3 files with invalid frontmatter, so tools won't edit them until it's fixed; "
                      "run `hermes memory-buckets lint`", out)
        self.assertNotIn("ignored", out)  # all three are on valid paths

    def test_status_counts_symlinks_apart_and_leaves_leftover_indexes_to_migration(self):
        self.put("global/topics/real.md", doc("real", "Real"))
        symlink_or_skip(self, self.store.memories / "global/topics/real.md", self.store.memories / "global/topics/sym.md")
        self.put("proj-1/index.md", doc("index", "left over from an older version"))
        out = self.run_cli("status")[1]
        self.assertIn("ignored     1 symlink (never followed); run `hermes memory-buckets lint`", out)
        self.assertNotIn("outside the path rules", out)  # proj-1/index.md has its migration line
        self.assertIn("index.md left over in: proj-1", out)

    def test_lint(self):
        self.put("global/topics/nix.md", doc("nix", "Nix", "- see [[sam]] and [[nope]] and [[proj-1/profile]]\n"))
        self.put("global/people/sam.md", doc("sam", "Sam"))
        self.put("global/areas/sam.md", doc("sam", "Also sam"))
        self.put("global/topics/bad.md", "no frontmatter\n")
        self.put("global/Stray.md", "x")
        self.put("proj-1/profile.md", doc("profile", "proj-1"))
        (self.store.memories / ".obsidian").mkdir()
        (self.store.memories / ".obsidian/app.json").write_text("{}", encoding="utf-8")
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

    def test_lint_rejects_slash_links_to_symlinks_invalid_paths_and_traversals(self):
        (self.store.root / "outside.md").write_text(doc("outside", "Outside"), encoding="utf-8")
        self.put("global/Stray.md", doc("Stray", "Invalid path"))
        self.put("global/topics/real.md", doc("real", "Real"))
        symlink_or_skip(self, self.store.memories / "global/topics/real.md", self.store.memories / "global/topics/sym.md")
        self.put(
            "global/topics/nix.md",
            doc("nix", "Nix", "- see [[../outside]] and [[global/Stray]] and [[global/topics/sym]]\n"),
        )
        problems = cli.lint(self.store)
        self.assertIn("global/topics/nix.md: broken link [[../outside]]", problems)
        self.assertIn("global/topics/nix.md: broken link [[global/Stray]]", problems)
        self.assertIn("global/topics/nix.md: broken link [[global/topics/sym]]", problems)

    def test_lint_ignores_wikilinks_in_code_fences_and_inline_code(self):
        body = (
            "```bash\n"
            "if [[ -f /etc/NIXOS ]]; then echo nix; fi\n"
            "```\n"
            "~~~\n"
            "if [[ -f /etc/NIXOS ]]; then echo tilde; fi\n"
            "~~~\n"
            "- inline `[[inline]]` and broken [[nope]]\n"
        )
        self.put("global/topics/nix.md", doc("nix", "Nix", body))
        self.assertEqual(cli.lint(self.store), ["global/topics/nix.md: broken link [[nope]]"])

    def test_lint_only_sends_real_leftovers_to_migrate(self):
        # migrate ignores global/ and directories that aren't project ids.
        self.put("global/index.md", doc("index", "x"))
        code, out = self.run_cli("lint")
        self.assertIn("global/index.md: not a memory path", out)
        self.assertNotIn("migrate", out)

    def test_a_frontmatter_comment_is_not_a_problem(self):
        self.put("global/topics/nix.md", doc("nix", "Nix", extra="# written in Obsidian\n"))
        self.assertEqual(self.run_cli("lint")[1], "0 problem(s)\n")
        self.assertNotIn("frontmatter ", self.run_cli("status")[1])

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

    def test_empty_headings_are_quiet_and_cleared_by_the_next_session(self):
        # status and lint don't report them; a migrate --dry-run lists them, and
        # a headings-only run mentions no backup because it keeps none.
        self.put("global/inbox.md", doc("inbox", inbox.DESCRIPTION, "## a\n- x\n\n## empty\n\n## b\n- y\n\n## end\n"))
        status = self.run_cli("status")[1]
        self.assertNotIn("migration", status, "no migration line at all")
        self.assertNotIn("outdated description", status)
        self.assertEqual(self.run_cli("lint")[0], 0)
        code, out = self.run_cli("migrate", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("would: global/inbox.md: removed ## empty; ## end", out)
        self.assertIn("## a\n- x\n", self.store.read("global/inbox.md")["content"], "a dry run changes nothing")
        code, out = self.run_cli("migrate")
        self.assertEqual(code, 0, out)
        self.assertIn("global/inbox.md: removed ## empty; ## end", out)
        self.assertNotIn("originals kept", out)
        self.assertEqual(self.run_cli("migrate")[1].strip(), "nothing to migrate")

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
            (pending / f"{pid}.json").write_text(f'{{"id": "{pid}", "project": "proj-1"}}\n', encoding="utf-8")
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
        self.assertEqual((code, out.splitlines()), (0, ["_pending/ removed: it was empty"]))
        self.assertNotIn("originals kept", out)

    def test_skill_guidance_depends_on_plugin_dir_or_external_dirs(self):
        import sys
        import tempfile
        import types
        from pathlib import Path

        self.put("global/inbox.md", doc("inbox", inbox.DESCRIPTION, "## imported\n- a\n"))
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            (skills_dir / "sort-imports").mkdir(parents=True)
            (skills_dir / "sort-imports/SKILL.md").write_text("---\nname: sort-imports\n---\n# Sort\n", encoding="utf-8")

            sc_mod = types.SimpleNamespace(scan_skill_commands=lambda: {})
            su_mod = types.SimpleNamespace(
                get_external_skills_dirs=lambda: [],
                get_skills_dir=lambda: Path(tmp) / "local-skills",
            )
            pm_mod = types.SimpleNamespace(
                find_provider_dir=lambda _name: Path(tmp) / "site-packages" / "memory_buckets",
                find_provider_entry_point=lambda _name: types.SimpleNamespace(value="memory_buckets:register"),
                load_memory_provider=lambda *a, **k: None,
            )
            hc_mod = types.SimpleNamespace(
                load_config_readonly=lambda: {},
                cfg_get=lambda _cfg, *keys: "memory-buckets" if keys == ("memory", "provider") else None,
            )
            mm_mod = types.SimpleNamespace(memory_provider_tools_enabled=lambda *a, **k: True)

            fakes = {
                "agent.memory_manager": mm_mod,
                "agent.skill_commands": sc_mod,
                "agent.skill_utils": su_mod,
                "hermes_cli.config": hc_mod,
                "plugins.memory": pm_mod,
            }
            with mock.patch.dict(sys.modules, fakes), mock.patch.object(cli, "SKILLS_DIR", skills_dir):
                self.assertFalse(cli._is_plugin_dir_install())

                diag_out = io.StringIO()
                with contextlib.redirect_stdout(diag_out):
                    cli._diagnose_skill(skills_dir, lambda ok, msg: None)
                self.assertIn("hermes memory-buckets import-prompt", diag_out.getvalue())
                self.assertIn("external_dirs", diag_out.getvalue())
                self.assertNotIn("Asking the agent to sort your imported memory still works", diag_out.getvalue())

                _, full_diag_out = self.run_cli("diagnose")
                self.assertIn("found by entry point memory_buckets:register", full_diag_out)
                self.assertNotIn("found at ", full_diag_out)

                _, prompt_out = self.run_cli("import-prompt")
                self.assertIn("hermes memory-buckets import-prompt", prompt_out)
                self.assertIn("external_dirs", prompt_out)
                self.assertNotIn("(it uses memory-buckets's sort-imports skill)", prompt_out)

                pm_mod.find_provider_dir = lambda _name: Path(tmp) / "plugins" / "memory-buckets"
                self.assertTrue(cli._is_plugin_dir_install())
                diag_out = io.StringIO()
                with contextlib.redirect_stdout(diag_out):
                    cli._diagnose_skill(skills_dir, lambda ok, msg: None)
                self.assertIn("Asking the agent to sort your imported memory still works", diag_out.getvalue())

                _, prompt_out = self.run_cli("import-prompt")
                self.assertIn("(it uses memory-buckets's sort-imports skill)", prompt_out)

                pm_mod.find_provider_dir = lambda _name: Path(tmp) / "site-packages" / "memory_buckets"
                su_mod.get_external_skills_dirs = lambda: [skills_dir]
                _, prompt_out = self.run_cli("import-prompt")
                self.assertIn("(it uses memory-buckets's sort-imports skill)", prompt_out)

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
        self.assertTrue(any(ln.startswith("------  prefetch_min_similarity = 0.5") for ln in lines))

    def test_no_command(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_cli()[0], 2)


if __name__ == "__main__":
    unittest.main()
