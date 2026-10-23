import unittest

from . import memory_buckets  # noqa: F401
from memory_buckets import frontmatter as fmm

SAMPLE = ('---\nname: nix\ndescription: "Read for: Nix"\naliases: [nixos]\nobsidian:\n  cssclass: wide\n'
          'tags:\n  - a\n  - b\n---\n# Heading\n- fact\n')


class FrontmatterTest(unittest.TestCase):
    def test_roundtrip_is_byte_identical_when_untouched(self):
        fm, body = fmm.parse(SAMPLE)
        self.assertEqual(fmm.render(fm, body), SAMPLE)

    def test_unknown_structures_are_kept_raw(self):
        fm, _ = fmm.parse(SAMPLE)
        self.assertIsNone(fm.get("obsidian"))
        fm.set("description", "Nix")
        self.assertIn("obsidian:\n  cssclass: wide\n", fmm.render(fm, ""))

    def test_setting_a_raw_key_replaces_it(self):
        fm, _ = fmm.parse(SAMPLE)
        fm.set("obsidian", "plain")
        out = fmm.render(fm, "")
        self.assertIn("obsidian: plain\n", out)
        self.assertNotIn("cssclass", out)

    def test_quoting_survives_a_roundtrip(self):
        for value in ["a: b", "yes", "", " lead", "#tag", "it's", 'say "hi"', "x #y", "- dash", "[x]", "true"]:
            fm = fmm.Frontmatter()
            fm.set("k", value)
            parsed, _ = fmm.parse(fmm.render(fm, ""))
            self.assertEqual(parsed.get("k"), value, value)

    def test_lists_roundtrip(self):
        fm = fmm.Frontmatter()
        fm.set("aliases", ["a: b", "plain", "yes"])
        parsed, _ = fmm.parse(fmm.render(fm, ""))
        self.assertEqual(parsed.get("aliases"), ["a: b", "plain", "yes"])

    def test_inline_list_handles_escaped_quotes_and_commas(self):
        text = '---\naliases: ["a \\"b, c\\"", "d\\\\", "plain"]\n---\n'
        fm, _ = fmm.parse(text)
        self.assertEqual(fm.get("aliases"), ['a "b, c"', "d\\", "plain"])

    def test_outer_quotes_not_stripped_when_inner_unescaped_quotes_exist(self):
        text = (
            "---\n"
            "name: nix\n"
            'description: "a" and "b"\n'
            "single: 'x' and 'y'\n"
            'escaped: "a \\"b\\" and \\\\c"\n'
            "---\n"
        )
        fm, _ = fmm.parse(text)
        self.assertEqual(fm.get("description"), '"a" and "b"')
        self.assertEqual(fm.get("single"), "'x' and 'y'")
        self.assertEqual(fm.get("escaped"), 'a "b" and \\c')

    def test_no_frontmatter(self):
        fm, body = fmm.parse("just text\n")
        self.assertEqual(fm.entries, [])
        self.assertEqual(body, "just text\n")

    def test_errors(self):
        for bad in ["---\nname: a\n", "---\nname: a\nname: b\n---\n", "---\nnot a key\n---\n"]:
            with self.assertRaises(fmm.FrontmatterError, msg=bad):
                fmm.parse(bad)

    def test_whole_line_comments_are_kept_and_hidden(self):
        text = ("---\n# note to self\nname: nix\n# between keys\n\ndescription: x\ntags:\n  - a\n  - b\n"
                "# after a list\naliases: [nixos]\n# last\n---\nbody\n")
        fm, body = fmm.parse(text)
        self.assertEqual(fm.keys(), ["name", "description", "tags", "aliases"])
        self.assertEqual([k for k, _ in fm.pairs()], fm.keys())
        self.assertEqual(fm.get("tags"), ["a", "b"])
        self.assertEqual(fm.get("aliases"), ["nixos"])
        self.assertEqual(fmm.render(fm, body), text)  # untouched: byte for byte
        fmm.validate(fm, "nix")

    def test_comments_survive_an_edit(self):
        fm, body = fmm.parse("---\n# keep me\nname: nix\ndescription: x\n# and me\n---\nbody\n")
        fm.set("description", "y")
        fm.set("edited_at", "2026-10-17T00:00:00Z")
        self.assertEqual(fmm.render(fm, body),
                         "---\n# keep me\nname: nix\ndescription: y\n# and me\nedited_at: 2026-10-17T00:00:00Z\n---\nbody\n")

    def test_a_comment_does_not_adopt_indented_lines(self):
        # They'd be a list item cut off from its key, so it stays an error.
        with self.assertRaises(fmm.FrontmatterError):
            fmm.parse("---\ntags:\n  - a\n# c\n  - b\n---\n")

    def test_a_comment_only_block_has_no_keys(self):
        fm, _ = fmm.parse("---\n# nothing else\n---\n")
        self.assertEqual(fm.keys(), [])
        with self.assertRaises(fmm.FrontmatterError):
            fmm.validate(fm, "nix")

    def test_validate(self):
        fm, _ = fmm.parse("---\nname: nix\ndescription: x\n---\n")
        fmm.validate(fm, "nix")
        with self.assertRaises(fmm.FrontmatterError):
            fmm.validate(fm, "rust")
        fm.set("aliases", "not a list")
        with self.assertRaises(fmm.FrontmatterError):
            fmm.validate(fm, "nix")


if __name__ == "__main__":
    unittest.main()
