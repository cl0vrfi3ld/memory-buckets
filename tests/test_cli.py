import contextlib
import io
import os
import unittest
from unittest import mock

from .helpers import FakeEmbedder, StoreCase, doc, fake_hermes, run_hermes_cli
from memory_buckets import cli


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
        self.put("proj-1/index.md", doc("index", "proj-1"))
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
        self.assertIn("- Name is Ivy\n- Uses they/them\n", text)
        self.assertIn("- Host runs NixOS with flakes\n", text)
        self.assertIn("  - cli:import", text)
        self.assertEqual(text.count("## imported"), 2)
        self.assertIn("----- prompt -----", out)
        self.assertIn("global/inbox.md", out.split("----- prompt -----")[1])
        self.assertIn("cp -a", out)
        code, again = self.run_cli("sort-prompt")
        self.assertEqual(code, 0)
        self.assertEqual(again, out[out.index("\nNext:"):])

    def test_sort_prompt_needs_an_inbox(self):
        code, out = self.run_cli("sort-prompt")
        self.assertEqual(code, 1)
        self.assertIn("run import first", out)

    def test_sort_prompt_only_names_real_tools(self):
        import re
        from memory_buckets import tools
        named = set(re.findall(r"memory_[a-z_]+", cli.sort_prompt()))
        self.assertTrue(named)
        self.assertLessEqual(named, set(tools.TOOL_NAMES))

    def test_import_nothing(self):
        code, out = self.run_cli("import", "--from", str(self.home / "nope"))
        self.assertEqual(code, 1)
        self.assertIn("nothing to import", out)

    def test_status_matches_buckets_to_hermes_projects(self):
        self.put("proj-1/index.md", doc("index", "proj-1"))
        self.put("old-proj/index.md", doc("index", "old-proj"))
        with fake_hermes({"proj-1": "/src/proj-1", "fresh": "/src/fresh"}):
            code, out = self.run_cli("status")
        self.assertIn("2 Hermes project(s); 1 with a bucket (proj-1)", out)
        self.assertIn("no Hermes project (no session is scoped to them): old-proj", out)
        self.assertIn("--slug <bucket>", out)

    def test_lint(self):
        self.put("global/topics/nix.md", doc("nix", "Nix", "- see [[sam]] and [[nope]] and [[proj-1/index]]\n"))
        self.put("global/people/sam.md", doc("sam", "Sam"))
        self.put("global/areas/sam.md", doc("sam", "Also sam"))
        self.put("global/topics/bad.md", "no frontmatter\n")
        self.put("global/Stray.md", "x")
        self.put("proj-1/index.md", doc("index", "proj-1"))
        (self.store.memories / ".obsidian").mkdir()
        (self.store.memories / ".obsidian/app.json").write_text("{}")
        code, out = self.run_cli("lint")
        self.assertEqual(code, 1)
        self.assertIn("broken link [[nope]]", out)
        self.assertNotIn("[[sam]]\n", out.replace("is ambiguous", ""))
        self.assertIn("name 'sam' is used by", out)
        self.assertIn("global/topics/bad.md: bad frontmatter", out)
        self.assertIn("global/Stray.md: not a memory path", out)
        self.assertNotIn(".obsidian", out)
        self.assertNotIn("[[proj-1/index]]", out)

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
