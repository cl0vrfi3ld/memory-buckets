"""The retrieval benchmark's own corpus and plumbing.

The real-model benchmark is a manual tool (``python -m tests.bench_retrieval``); these tests keep
its splits honest. A paraphrase query that quietly shares a word with its target, or a filler store
that fits in the prompt after all, would put the benchmark back to measuring nothing.
"""

import os
import tempfile
import unittest
from pathlib import Path

from . import bench_retrieval as bench
from .helpers import FakeEmbedder
from memory_buckets import index, static_model

REAL = os.environ.get("MEMORY_BUCKETS_MODEL_DIR")


class CorpusTest(unittest.TestCase):
    def test_paraphrases_share_no_content_word_with_their_target(self):
        corpus = bench.full_corpus()
        for query, target, _ in bench.PARAPHRASE_QUERIES:
            with self.subTest(query=query):
                self.assertIn(target, corpus)
                self.assertEqual(bench.shared_words(query, bench.doc_text(target, corpus)), [])

    def test_exact_queries_do_share_words(self):
        # The check above can only pass if it can fail: the exact set is the same shape of
        # query with the words left in, so it must be flagged almost everywhere.
        corpus = bench.full_corpus()
        flagged = [q for q, target, _ in bench.SEARCH_QUERIES if bench.shared_words(q, bench.doc_text(target, corpus))]
        self.assertGreaterEqual(len(flagged), len(bench.SEARCH_QUERIES) - 1)

    def test_shared_words_catches_plurals_and_close_forms(self):
        text = "- Computes calibration curves from recordings."
        self.assertEqual(bench.shared_words("record a curve", text), ["curve", "record"])
        self.assertEqual(bench.shared_words("calibrate the array", text), ["calibrate"])
        self.assertEqual(bench.shared_words("play some music", text), [])

    def test_every_query_target_exists(self):
        corpus = bench.full_corpus()
        for query, target, _ in [*bench.SEARCH_QUERIES, *bench.PARAPHRASE_QUERIES]:
            self.assertIn(target, corpus, query)
        for query, target, _ in bench.HINT_QUERIES:
            if target is not None:
                self.assertIn(target, corpus, query)

    def test_paraphrases_are_not_the_exact_queries(self):
        exact = {q for q, _, _ in [*bench.SEARCH_QUERIES, *bench.HINT_QUERIES]}
        self.assertFalse(exact & {q for q, _, _ in bench.PARAPHRASE_QUERIES})

    def test_negatives_name_no_file(self):
        # Prefetch rescues a file whose stem or alias appears in the turn. A negative that
        # did that would score a false positive the threshold could never fix.
        for query in bench.NEGATIVE_QUERIES:
            for path, (_, aliases, _) in bench.full_corpus().items():
                if path in bench.INLINED:
                    continue
                names = " ".join([path.rsplit("/", 1)[-1].removesuffix(".md").replace("-", " "), *aliases])
                with self.subTest(query=query, path=path):
                    self.assertEqual(bench.shared_words(query, names), [])

    def test_splits_are_not_tiny(self):
        self.assertGreaterEqual(len(bench.PARAPHRASE_QUERIES), 15)
        self.assertGreaterEqual(len(bench.NEGATIVE_QUERIES), 10)

    def test_filler_is_deterministic_and_unique(self):
        first, second = bench.filler_corpus(), bench.filler_corpus()
        self.assertEqual(first, second)
        self.assertEqual(len(first), bench.FILLER_FILES)
        self.assertFalse(set(first) & set(bench.full_corpus()))
        self.assertTrue(all(path.startswith("global/") for path in first))


class LargeStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = bench._write_store(Path(self._tmp.name) / "store", {**bench.full_corpus(), **bench.filler_corpus()})
        self.index = index.Index(self.store)
        self.addCleanup(self.index.close)

    def test_listing_truncates_and_hides_some_targets(self):
        visible = bench.prompt_visible(self.store, self.index, None)
        targets = {t for _, t, p in [*bench.SEARCH_QUERIES, *bench.PARAPHRASE_QUERIES] if p is None}
        self.assertTrue(targets - visible, "every target is still in the prompt: nothing forces a search")
        self.assertTrue(targets & visible, "no target is visible: the sanity check proves nothing")
        self.assertIn("global/profile.md", visible)  # inlined, so counted as visible

    def test_a_small_store_hides_nothing(self):
        # The control: without the filler the same check must find every global target listed.
        with tempfile.TemporaryDirectory() as tmp:
            store = bench._write_store(Path(tmp) / "store", bench.full_corpus())
            idx = index.Index(store)
            try:
                visible = bench.prompt_visible(store, idx, None)
            finally:
                idx.close()
        targets = {t for _, t, p in [*bench.SEARCH_QUERIES, *bench.PARAPHRASE_QUERIES] if p is None}
        self.assertEqual(targets - visible, set())


class RunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = bench.run_benchmark(embedder=FakeEmbedder(), filler=120)  # the unit run stays quick

    def test_reports_both_stores_and_all_splits(self):
        self.assertEqual(set(self.results), {"small", "large"})
        for report in self.results.values():
            self.assertEqual(set(report["splits"]), {"exact", "paraphrase", "negative"})
            for row in report["splits"].values():
                for key, value in row.items():
                    if key not in {"n", "search_ms"}:
                        self.assertTrue(0.0 <= value <= 1.0, (key, value))

    def test_large_store_scores_only_what_search_alone_reaches(self):
        small, large = self.results["small"], self.results["large"]
        self.assertEqual(small["queries"], small["queries_total"])
        self.assertLess(large["queries"], large["queries_total"])
        self.assertGreater(large["queries"], 0)
        self.assertGreater(large["files"], small["files"] + 100)

    def test_paraphrases_score_below_exact_queries(self):
        # With a bag-of-words fake there is no semantic link, so the paraphrase split must
        # fall away from the exact one. If it does not, the split measures nothing.
        for label, report in self.results.items():
            exact, paraphrase = report["splits"]["exact"], report["splits"]["paraphrase"]
            with self.subTest(store=label):
                self.assertGreaterEqual(exact["search_top1"], 0.8)
                self.assertLess(paraphrase["search_top1"], exact["search_top1"])
                self.assertLess(paraphrase["search_top3"], 1.0)

    def test_sweep_is_ordered_and_monotonic(self):
        for report in self.results.values():
            sweep = report["sweep"]
            thresholds = [row["threshold"] for row in sweep]
            self.assertEqual(thresholds, sorted(set(thresholds)))
            self.assertIn(report["threshold"], thresholds)
            for column in ("exact", "paraphrase", "negative"):
                values = [row[column] for row in sweep]
                self.assertEqual(values, sorted(values, reverse=True), f"{column} rose with the threshold")

    def test_report_names_every_split_and_the_default(self):
        text = bench.format_report(self.results)
        for needle in ("small store", "large store", "exact", "paraphrase", "negative", "<- default"):
            self.assertIn(needle, text)


@unittest.skipUnless(REAL and static_model.is_complete(Path(REAL or "/")), "set MEMORY_BUCKETS_MODEL_DIR to the real model")
class RealModelBenchTest(unittest.TestCase):
    """Floors from the first run on potion-retrieval-32M, with slack. They catch a retrieval
    regression; the last test catches the benchmark going soft."""

    @classmethod
    def setUpClass(cls):
        cls.results = bench.run_benchmark(REAL)

    def test_exact_set_holds(self):
        # Measured 0.9231 search top-1 (one sourdough-for-allergy miss), 1.0 top-3 and hint recall.
        for label, report in self.results.items():
            row = report["splits"]["exact"]
            with self.subTest(store=label):
                self.assertGreaterEqual(row["search_top1"], 0.8)
                self.assertGreaterEqual(row["search_top3"], 0.9)
                self.assertGreaterEqual(row["hint_recall"], 0.8)

    def test_paraphrase_floor(self):
        # Measured small store: search top-3 0.5, hint recall 0.1875.
        row = self.results["small"]["splits"]["paraphrase"]
        self.assertGreaterEqual(row["search_top3"], 0.35)
        self.assertGreaterEqual(row["hint_recall"], 0.1)

    def test_negatives_stay_quiet(self):
        # Measured 1 of 15 turns hinted at the 0.27 default.
        for label, report in self.results.items():
            with self.subTest(store=label):
                self.assertLessEqual(report["splits"]["negative"]["hint_fp_rate"], 0.2)

    def test_benchmark_is_not_saturated(self):
        # The point of the paraphrase split. If a better model lifts this to 1.0, the corpus
        # needs harder paraphrases; do not relax the assertion.
        paraphrase = self.results["small"]["splits"]["paraphrase"]
        self.assertLess(paraphrase["search_top1"], 1.0)
        self.assertLess(paraphrase["hint_recall"], 1.0)


if __name__ == "__main__":
    unittest.main()
