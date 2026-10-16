import os
import sys
import threading
import time
import unittest
from unittest import mock

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

    def test_fenced_code_blocks_ignore_headings_and_bullets(self):
        body = (
            "# Setup\n"
            "```sh\n"
            "# install packages\n"
            "- not a markdown bullet\n"
            "echo ok\n"
            "```\n\n"
            "~~~python\n"
            "# tilde comment\n"
            "x = 1\n"
            "~~~\n"
        )
        self.assertEqual(
            idx.chunk("p.md", "", [], body, limit=500)[1:],
            [("Setup", "```sh\n# install packages\n- not a markdown bullet\necho ok\n```\n~~~python\n# tilde comment\n"
                       "x = 1\n~~~")],
        )

    def test_nested_headings_preserve_parent_hierarchy(self):
        body = (
            "# Infrastructure\n"
            "## Hetzner Storage Box\n"
            "- mounted at /mnt/box\n\n"
            "## Backups\n"
            "- restic nightly\n\n"
            "# Personal\n"
            "- notes\n"
        )
        self.assertEqual(
            idx.chunk("p.md", "", [], body)[1:],
            [
                ("Infrastructure > Hetzner Storage Box", "- mounted at /mnt/box"),
                ("Infrastructure > Backups", "- restic nightly"),
                ("Personal", "- notes"),
            ],
        )


class IndexTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.put("global/topics/nix.md", doc("nix", "Read for Nix and NixOS", "- flakes pin inputs\n- deploys with colmena\n"))
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

    def test_a_connection_that_fails_to_open_is_closed(self):
        # Nothing else can reach it; on Windows it would keep rebuild() from deleting the file.
        index = self.make()
        index.path.parent.mkdir(parents=True)
        index.path.write_bytes(b"not a sqlite file")
        opened = []
        real_connect = idx.sqlite3.connect

        def tracking_connect(*args, **kwargs):
            opened.append(real_connect(*args, **kwargs))
            return opened[-1]

        with mock.patch.object(idx.sqlite3, "connect", tracking_connect), self.assertRaises(idx.sqlite3.DatabaseError):
            index.reconcile()
        with self.assertRaises(idx.sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")  # closed
        index.rebuild()
        self.assertEqual(index.reconcile(), {"changed": 3, "removed": 0})

    @unittest.skipUnless(sys.platform == "win32", "only Windows refuses to delete an open file")
    def test_rebuild_while_another_process_has_the_index_open(self):
        index = self.make()
        index.reconcile()
        other = idx.sqlite3.connect(index.path)  # stands in for a gateway's connection
        self.addCleanup(other.close)
        other.execute("SELECT count(*) FROM files").fetchone()
        with self.assertRaises(idx.IndexInUse) as caught:
            index.rebuild()
        self.assertIn("open in another process", str(caught.exception))
        self.assertTrue(index.path.exists(), "nothing deleted")
        self.assertEqual(index.search("climbing", ALL)["results"][0]["path"], "global/people/sam.md")

    def test_one_shot_worker_threads_do_not_leak_connections(self):
        index = self.make()
        for _ in range(8):
            worker = threading.Thread(target=lambda: index.search("nix", ALL))
            worker.start()
            worker.join()
        index.stats()
        self.assertTrue(getattr(index, "_schema_ready"))
        self.assertEqual(len(index._conns), 1)
        index.close()
        self.assertFalse(getattr(index, "_schema_ready"))
        con = index._conn()
        statements = []
        con.set_trace_callback(statements.append)
        index._ensure_schema(con)
        self.assertFalse(any("BEGIN IMMEDIATE" in s for s in statements))

    def test_parent_heading_without_immediate_body_is_searchable_via_fts(self):
        self.put(
            "global/topics/infra.md",
            doc("infra", "Server notes", "# Infrastructure\n## Hetzner Storage Box\n- mounted at /mnt/box\n"),
        )
        index = self.make()
        hits = list(index.search("Infrastructure", ALL)["results"])  # type: ignore[arg-type]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["path"], "global/topics/infra.md")
        self.assertEqual(hits[0]["heading"], "Infrastructure > Hetzner Storage Box")

    def test_stopword_heavy_natural_language_query_prefers_the_keyword_match(self):
        self.put("global/topics/aurora.md", doc("aurora", "Northern lights", "- green aurora\n"))
        self.put(
            "global/topics/noise.md",
            doc("noise", "What do I do",
                "- what do you do\n- what will you do\n- I do what you do\n- what do I do in the\n"),
        )
        hits = list(self.make().search("what do I do in the aurora", ALL)["results"])  # type: ignore[arg-type]
        self.assertEqual(hits[0]["path"], "global/topics/aurora.md")
        self.assertNotIn("global/topics/noise.md", [h["path"] for h in hits])

    def test_scope_filter_reaches_fts_matches_beyond_the_unscoped_rank_cutoff(self):
        # global/ holds more than CANDIDATES * 2 chunks that outrank the scoped file,
        # so a scope filter applied after a fixed LIMIT would never see proj-1.
        for i in range(250):
            self.put(f"global/topics/filler-{i}.md", doc(f"filler-{i}", "zebra herd", "- zebra\n"))
        long_body = "- zebra\n" + "".join(f"- padding line {j}\n" for j in range(40))
        self.put("proj-1/topics/plan.md", doc("plan", "Project plan", long_body))
        index = self.make()
        hits = list(index.search("zebra", lambda p: p.startswith("proj-1/"))["results"])  # type: ignore[arg-type]
        self.assertEqual([h["path"] for h in hits], ["proj-1/topics/plan.md"])

    def test_identity_chunk_winner_returns_a_body_snippet(self):
        self.put("global/topics/tea.md", doc("tea", "Ceremony ritual", "# Steeping\n- steep for three minutes\n"))
        hits = list(self.make().search("ceremony ritual", ALL)["results"])  # type: ignore[arg-type]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["path"], "global/topics/tea.md")
        self.assertEqual(hits[0]["heading"], "Steeping")
        self.assertEqual(hits[0]["snippet"], "- steep for three minutes")

    def test_fts_ignores_chunk_zero_path_and_description_boilerplate(self):
        self.put("global/topics/nix.md", doc("nix", "Read when working on Nix and NixOS", "- flakes pin inputs\n"))
        self.put("global/topics/tea.md", doc("tea", "Read when helping Sam with oolong", "- steep leaves\n"))
        index = self.make()
        for query in ("working on kubernetes", "read about terraform", "global topics"):
            with self.subTest(query=query):
                self.assertEqual(index.search(query, ALL)["results"], [])
        proj_hits = list(index.search("proj-1", ALL)["results"])  # type: ignore[arg-type]
        self.assertEqual(proj_hits[0]["path"], "proj-1/topics/deploy.md")
        tea_hits = list(index.search("oolong", ALL)["results"])  # type: ignore[arg-type]
        self.assertEqual(tea_hits[0]["path"], "global/topics/tea.md")


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

    def test_hints_embeds_cold_and_reconciled_backlog(self):
        index = self.make(FakeEmbedder())
        hints = index.hints("Sam likes climbing", ALL, min_similarity=0.3)
        self.assertEqual(hints[0][0], "global/people/sam.md")
        target = self.put("global/people/sam.md", doc("sam", "Who Sam is", "- Sam practices archery\n"))
        os.utime(target, ns=(time.time_ns(), time.time_ns() + 10**9))
        updated = index.hints("Sam practices archery", ALL, min_similarity=0.3)
        self.assertEqual(updated[0][0], "global/people/sam.md")

    def test_file_level_rrf_fuses_identity_fts_with_body_vector_hit(self):
        for i in range(3):
            self.put(
                f"global/topics/filler-{i}.md",
                doc(f"filler-{i}", "Unrelated notes", f"- kestrel alpha beta gamma delta epsilon zeta{i}\n"),
            )
        self.put("global/topics/target.md", doc("target", "Read for kestrel", "# Birds\n- kite buzzard sky\n"))
        index = self.make(FakeEmbedder())
        hits = list(index.search("kestrel raptor avian", ALL)["results"])  # type: ignore[arg-type]
        self.assertEqual(hits[0]["path"], "global/topics/target.md")
        self.assertEqual(hits[0]["heading"], "Birds")
        self.assertEqual(hits[0]["snippet"], "- kite buzzard sky")

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

    def test_embed_backlog_includes_section_headings(self):
        self.put(
            "global/topics/infra.md",
            doc("infra", "Server notes", "# Infrastructure\n## Hetzner Storage Box\n- mounted at /mnt/box\n"),
        )
        embedder = FakeEmbedder()
        index = self.make(embedder)
        index.reconcile()
        embedder.calls.clear()
        index.embed_backlog(max_batches=None)
        embedded_texts = [text for batch in embedder.calls for text in batch]
        self.assertIn("Infrastructure > Hetzner Storage Box\n- mounted at /mnt/box", embedded_texts)
        hints = index.hints("Infrastructure", ALL, min_similarity=0.2)
        self.assertEqual(hints[0][0], "global/topics/infra.md")

    def test_unrelated_and_zero_vector_queries_return_no_results(self):
        index = self.make(FakeEmbedder())
        self.assertEqual(index.search("chromodynamics astrobiology glacier", ALL)["results"], [])
        self.assertEqual(index.search("!!!", ALL)["results"], [])

    def test_single_relevant_hit_is_not_padded_with_unrelated_files(self):
        index = self.make(FakeEmbedder())
        hits = list(index.search("climbing", ALL)["results"])  # type: ignore[arg-type]
        self.assertEqual([h["path"] for h in hits], ["global/people/sam.md"])



class CleanEmbeddingTest(StoreCase):
    """embed_backlog feeds the model a clean identity for chunk 0 and a subject
    stem for headingless body chunks, instead of the raw path and boilerplate."""

    def setUp(self):
        super().setUp()
        self.put("global/topics/nix.md",
                 doc("nix", "Read when working on Nix and NixOS", "- flakes pin inputs\n- deploys with colmena\n"))
        self.put("global/topics/tailscale.md", doc("tailscale", "Mesh VPN", "- tailnet lock is on\n"))
        self.put("global/topics/infra.md", doc("infra", "Server notes", "# Infrastructure\n- mounted at /mnt/box\n"))
        self.put("global/profile.md", doc("profile", "Who the user is", "- Sam\n"))

    def embedded(self):
        embedder = FakeEmbedder()
        index = idx.Index(self.store, embedder)
        self.addCleanup(index.close)
        index.reconcile()
        embedder.calls.clear()
        index.embed_backlog(max_batches=None)
        return [text for batch in embedder.calls for text in batch]

    def test_chunk_zero_embeds_the_clean_identity_not_the_raw_path(self):
        texts = self.embedded()
        self.assertIn("nix\nNix and NixOS", texts)
        self.assertTrue(all("global/topics/" not in t for t in texts))
        self.assertTrue(all(".md" not in t for t in texts))
        self.assertTrue(all("Read when working on" not in t for t in texts))

    def test_headingless_body_chunks_carry_the_stem_but_generic_stems_do_not(self):
        texts = self.embedded()
        self.assertIn("tailscale\n- tailnet lock is on", texts)
        self.assertIn("- Sam", texts)
        self.assertNotIn("profile\n- Sam", texts)

    def test_headed_body_chunk_keeps_its_heading_and_the_identity_stays_clean(self):
        texts = self.embedded()
        self.assertIn("Infrastructure\n- mounted at /mnt/box", texts)
        self.assertIn("infra\nServer notes", texts)


class FileRankAndCoverageTest(StoreCase):
    """search() ranks files, not raw chunks, and keeps FTS score proportional to
    how much of the query a file actually covers."""

    def make(self, embedder=None):
        index = idx.Index(self.store, embedder)
        self.addCleanup(index.close)
        return index

    def test_file_rank_deduplication_uses_the_file_rank_not_the_chunk_rank(self):
        # spread.md splits into five chunks that all outrank rival.md, so a raw
        # chunk rank would leave rival.md at rank 5 (1 / (RRF_K + 6)); the file
        # rank must stay 1 (1 / (RRF_K + 2)).
        self.put("global/topics/spread.md", doc("spread", "Spreading notes",
                 "".join(f"## alpha {i}\n- point {i}\n\n" for i in range(5))))
        self.put("global/topics/rival.md", doc("rival", "Rival notes", "- alpha note\n"))
        results = list(self.make().search("alpha", ALL)["results"])  # type: ignore[arg-type]
        self.assertEqual([r["path"] for r in results],
                         ["global/topics/spread.md", "global/topics/rival.md"])
        self.assertEqual(results[1]["score"], round(1.0 / (idx.RRF_K + 2), 5))

    def test_low_coverage_multichunk_file_does_not_outrank_full_coverage_file(self):
        self.put("global/topics/spread.md", doc("spread", "Spreading notes",
                 "".join(f"## Section {i}\n- alpha alpha alpha point {i}\n\n" for i in range(5))))
        self.put("global/topics/full.md", doc("full", "Full coverage notes", "- alpha beta gamma note\n"))
        for i in range(20):
            self.put(f"global/topics/filler-{i}.md", doc(f"filler-{i}", "Filler", "- beta gamma stuff\n"))
        paths = [r["path"] for r in self.make().search("alpha beta gamma", ALL, limit=40)["results"]]  # type: ignore[arg-type]
        self.assertIn("global/topics/full.md", paths)
        self.assertIn("global/topics/spread.md", paths)
        self.assertLess(paths.index("global/topics/full.md"), paths.index("global/topics/spread.md"))


class HintsProjectAndRescueTest(StoreCase):
    """hints() boosts the session project and rescues files named exactly by a
    stem or alias even when their embedding similarity is weak."""

    def make(self):
        index = idx.Index(self.store, FakeEmbedder())
        self.addCleanup(index.close)
        index.reconcile()
        index.embed_backlog(max_batches=None)
        return index

    def test_alias_named_in_a_noisy_query_rescues_the_hint(self):
        self.put("global/topics/storage.md", doc("storage", "Backup conventions",
                 "- restic runs nightly\n", extra="aliases:\n  - hetznerbox\n"))
        hints = self.make().hints("can you check the hetznerbox thing", ALL, min_similarity=0.45)
        self.assertIn("global/topics/storage.md", [h[0] for h in hints])

    def test_hyphenated_stem_named_in_a_noisy_query_rescues_the_hint(self):
        self.put("global/topics/hetzner-box.md", doc("hetzner-box", "Storage server", "- restic runs nightly\n"))
        hints = self.make().hints("please look at the hetzner box setup", ALL, min_similarity=0.45)
        self.assertEqual(hints[0][0], "global/topics/hetzner-box.md")

    def test_project_boost_ranks_the_project_file_over_an_equal_similarity_global(self):
        body = "- identical duplicated line entry\n"
        self.put("global/topics/shared.md", doc("shared", "identical duplicated line", body))
        self.put("proj-1/topics/shared.md", doc("shared", "identical duplicated line", body))
        hints = self.make().hints("identical duplicated line", ALL,
                                  min_similarity=0.1, project="proj-1", boost=1.3)
        self.assertEqual([h[0] for h in hints][:2],
                         ["proj-1/topics/shared.md", "global/topics/shared.md"])


if __name__ == "__main__":
    unittest.main()
