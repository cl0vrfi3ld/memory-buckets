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

    def test_no_frontmatter(self):
        fm, body = fmm.parse("just text\n")
        self.assertEqual(fm.entries, [])
        self.assertEqual(body, "just text\n")

    def test_errors(self):
        for bad in ["---\nname: a\n", "---\nname: a\nname: b\n---\n", "---\nnot a key\n---\n"]:
            with self.assertRaises(fmm.FrontmatterError, msg=bad):
                fmm.parse(bad)

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
