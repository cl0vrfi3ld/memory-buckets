import os
import time
import unittest

from .helpers import FakeEmbedder, StoreCase, doc
from memory_buckets import index as idx

ALL = lambda path: True  # noqa: E731


class ChunkTest(unittest.TestCase):
    def test_identity_chunk_first(self):
        chunks = idx.chunk("global/topics/nix.md", "Read for Nix", ["nixos"], "- a\n")
        self.assertEqual(chunks[0], ("", "global/topics/nix.md\nRead for Nix\naliases: nixos"))

    def test_packs_bullets_under_headings(self):
        body = "# One\n- a\n- b\n\n# Two\n- c\n"
        self.assertEqual(idx.chunk("p.md", "", [], body)[1:], [("One", "- a\n- b"), ("Two", "- c")])

    def test_respects_the_size_limit(self):
        body = "".join(f"- fact number {i} is here.\n" for i in range(200))
        chunks = idx.chunk("p.md", "", [], body, limit=100)
        self.assertTrue(all(len(t) <= 100 for _, t in chunks[1:]))
        self.assertEqual(sum(t.count("fact number") for _, t in chunks[1:]), 200)

    def test_splits_one_long_bullet_at_sentences(self):
        body = "- " + " ".join(f"Sentence {i} is quite long indeed." for i in range(40)) + "\n"
        chunks = idx.chunk("p.md", "", [], body, limit=120)[1:]
        self.assertGreater(len(chunks), 5)
        self.assertTrue(all(len(t) <= 120 for _, t in chunks))


class IndexTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.put("global/topics/nix.md", doc("nix", "Read for Nix and NixOS", "- flakes pin inputs\n- nixpkgs maintainer\n"))
        self.put("global/people/sam.md", doc("sam", "Who Sam is", "- Sam likes climbing\n"))
        self.put("proj-1/topics/deploy.md", doc("deploy", "How proj-1 deploys", "- deploys with colmena\n"))

    def make(self, embedder=None):
        index = idx.Index(self.store, embedder)
        self.addCleanup(index.close)
        return index

    def test_reconcile_picks_up_hand_edits_and_deletes(self):
        index = self.make()
        self.assertEqual(index.reconcile(), {"changed": 3, "removed": 0})
        self.assertEqual(index.reconcile(), {"changed": 0, "removed": 0})
        target = self.put("global/people/sam.md", doc("sam", "Who Sam is", "- Sam likes bouldering now\n"))
        os.utime(target, ns=(time.time_ns(), time.time_ns() + 10**9))
        (self.store.memories / "global/topics/nix.md").unlink()
        self.assertEqual(index.reconcile(), {"changed": 1, "removed": 1})
        hits = index.search("bouldering", ALL)["results"]
        self.assertEqual([h["path"] for h in hits], ["global/people/sam.md"])
        self.assertEqual(index.search("flakes", ALL)["results"], [])

    def test_fts_only_without_embeddings(self):
        found = self.make().search("colmena deploys", ALL)
        self.assertEqual(found["mode"], "fts")
        self.assertIsNone(found["note"])
        self.assertEqual(found["results"][0]["path"], "proj-1/topics/deploy.md")
        self.assertIn("colmena", found["results"][0]["snippet"])

    def test_scope_filter_and_project_boost(self):
        index = self.make()
        only_global = index.search("deploys colmena nix", lambda p: p.startswith("global/"))["results"]
        self.assertTrue(all(r["path"].startswith("global/") for r in only_global))
        boosted = index.search("nix deploys", ALL, project="proj-1", boost=100.0)["results"]
        self.assertEqual(boosted[0]["path"], "proj-1/topics/deploy.md")

    def test_fts_query_syntax_is_inert(self):
        self.assertIsInstance(self.make().search('nix" OR "* NEAR( -', ALL)["results"], list)

    def test_deleting_the_index_loses_nothing(self):
        index = self.make()
        index.reconcile()
        index.rebuild()
        self.assertEqual(index.search("climbing", ALL)["results"][0]["path"], "global/people/sam.md")


class EmbeddingIndexTest(IndexTest):
    def test_hybrid_search_and_backlog(self):
        index = self.make(FakeEmbedder())
        found = index.search("Sam likes climbing", ALL)
        self.assertEqual(found["mode"], "hybrid")
        self.assertEqual(found["results"][0]["path"], "global/people/sam.md")
        self.assertEqual(index.stats()["backlog"], 0)  # small store: one batch embeds everything

    def test_hints_threshold_and_exclusion(self):
        index = self.make(FakeEmbedder())
        index.reconcile()
        index.embed_backlog(max_batches=None)
        hints = index.hints("Sam likes climbing", ALL, min_similarity=0.3)
        self.assertEqual(hints[0][0], "global/people/sam.md")
        self.assertEqual(hints[0][1], "Who Sam is")
        excluded = index.hints("Sam likes climbing", ALL, exclude={"global/people/sam.md"}, min_similarity=0.3)
        self.assertNotIn("global/people/sam.md", [h[0] for h in excluded])
        self.assertEqual(index.hints("quantum chromodynamics lecture", ALL, min_similarity=0.9), [])

    def test_model_unavailable_falls_back_to_fts(self):
        embedder = FakeEmbedder()
        embedder.down = "built-in embedding model not present"
        index = self.make(embedder)
        found = index.search("colmena", ALL)
        self.assertEqual(found["mode"], "fts")
        self.assertIn("embeddings unavailable", found["note"])
        self.assertEqual(found["results"][0]["path"], "proj-1/topics/deploy.md")
        self.assertEqual(index.hints("colmena", ALL), [])
        self.assertEqual(embedder.calls, [])

    def test_model_change_reembeds(self):
        index = self.make(FakeEmbedder())
        index.reconcile()
        index.embed_backlog(max_batches=None)
        index.close()
        index2 = self.make(FakeEmbedder(name="other"))
        self.assertEqual(index2.stats()["embedded"], 0)
        index2.embed_backlog(max_batches=None)
        self.assertEqual(index2.stats()["backlog"], 0)



if __name__ == "__main__":
    unittest.main()
