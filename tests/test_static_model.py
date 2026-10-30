"""The built-in static embedding model.

Unit tests use a tiny synthetic model written by the test. Real-model tests run
when MEMORY_BUCKETS_MODEL_DIR points at potion-retrieval-32M (the Nix check does
this). The tokenizer parity test also needs HF `tokenizers` and
MEMORY_BUCKETS_REFERENCE_TOKENIZER (tokenizer.json). The float16 test needs
MEMORY_BUCKETS_MODEL_F32 (the upstream float32 file).
"""

import gc
import hashlib
import importlib.util
import json
import math
import os
import struct
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from . import memory_buckets  # noqa: F401
from memory_buckets import config, embeddings, static_model as sm

from .helpers import symlink_or_skip

VOCAB = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "tea", "coffee", "drink", "##s", "morning", "climb", "##ing",
         "sam", ",", "!", "cafe", "日", "本", "##x"]


def write_model(directory: Path, dims=4, dtype="F32"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "vocab.txt").write_text("\n".join(VOCAB) + "\n", encoding="utf-8")
    values = []
    for i in range(len(VOCAB)):  # deterministic rows; tea/coffee/drink point the same way
        base = [1.0, 0.2, 0.0, 0.0] if VOCAB[i] in ("tea", "coffee", "drink", "##s", "morning") else [0.0, 0.0, 1.0, float(i) / 10]
        values.extend(base[:dims])
    data = struct.pack(f"<{len(values)}f", *values)
    header = json.dumps({"embeddings": {"dtype": "F32", "shape": [len(VOCAB), dims], "data_offsets": [0, len(data)]}}).encode()
    f32 = directory / "model.safetensors"
    f32.write_bytes(struct.pack("<Q", len(header)) + header + data)
    if dtype == "F16":
        tmp = directory / "f32.safetensors"
        f32.rename(tmp)
        sm.convert_to_f16(tmp, f32)
        tmp.unlink()
    return directory


def forget_loaded_models():
    """Drop models that load() cached for the process. Each one keeps its file mmapped,
    and Windows can't delete a mapped file, so a test's temp dir couldn't be cleaned up."""
    sm._cache.clear()
    gc.collect()


def rss_kb():
    """Current VmRSS in KB from /proc/self/status; raises OSError where there is no /proc."""
    with open("/proc/self/status", encoding="ascii") as status:
        for line in status:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    raise OSError("no VmRSS line")


class TokenizerTest(unittest.TestCase):
    def setUp(self):
        self.wp = sm.WordPiece({t: i for i, t in enumerate(VOCAB)})

    def test_bert_normalisation_and_wordpiece(self):
        self.assertEqual(self.wp.tokens("Tea, COFFEE drinks!"), ["tea", ",", "coffee", "drink", "##s", "!"])
        self.assertEqual(self.wp.tokens("Café"), ["cafe"], "accents stripped, lowercased")
        self.assertEqual(self.wp.tokens("日本"), ["日", "本"], "CJK ideographs are separate words")
        self.assertEqual(self.wp.tokens("climbing"), ["climb", "##ing"])
        self.assertEqual(self.wp.tokens("zzz"), ["[UNK]"], "unknown word is one [UNK]")
        self.assertEqual(self.wp.tokens("tea[SEP]coffee [unk]"), ["tea", "[SEP]", "coffee", "[UNK]", "[UNK]", "[UNK]"],
                         "special tokens are matched verbatim and case-sensitively: '[unk]' is '[', 'unk', ']'")
        self.assertEqual(self.wp.tokens("x" * 101), ["[UNK]"], "over 100 chars is [UNK]")
        self.assertEqual(self.wp.tokens("tea\x00 coffee\tmorning"), ["tea", "coffee", "morning"])
        self.assertEqual(self.wp.tokens("tea\u200bs"), ["tea", "##s"], "zero-width space is deleted, joining the words")

    def test_lowercasing_is_per_character_so_capital_sigma_never_becomes_final_sigma(self):
        # str.lower() applies the final-sigma rule: "ΑΣ" -> "ας". tokenizers lowercases one char
        # at a time, so Σ is always σ ("ασ"). An already-lowercase ς is left alone.
        self.assertEqual(sm.normalise("ΑΣ"), "ασ")
        self.assertEqual(sm.normalise("[λΠΣ]"), "[λπσ]")
        self.assertEqual(sm.normalise("ΣΑΣ ΑΣ."), "σασ ασ.")
        self.assertEqual(sm.normalise("ας"), "ας")
        self.assertEqual(sm.normalise("İ"), "i", "accents are stripped before lowercasing")


class StaticModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(forget_loaded_models)  # runs first: cleanups are last in, first out
        self.root = Path(self.tmp.name)

    def test_encode_is_normalised_mean_and_drops_unk(self):
        model = sm.StaticModel(write_model(self.root / "m"))
        vec = model.encode("tea zzz")
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in vec)), 1.0, places=5)
        self.assertEqual(list(vec), list(model.encode("tea")), "[UNK] contributes nothing")
        self.assertEqual(list(model.encode("zzz qqq")), [0.0] * 4, "nothing known: zero vector")
        drink, coffee, sam = model.encode("drink"), model.encode("coffee"), model.encode("sam")
        self.assertGreater(sum(a * b for a, b in zip(drink, coffee)), sum(a * b for a, b in zip(drink, sam)))

    def test_word_and_row_caches_populate_and_preserve_vectors(self):
        model = sm.StaticModel(write_model(self.root / "m"))
        self.assertEqual(model.tokenizer._word_cache, {})
        self.assertEqual(model._row_cache, {})
        vec = model.encode("climbing tea climbing sam")
        self.assertEqual(model.tokenizer._word_cache["climbing"], [model.vocab["climb"], model.vocab["##ing"]])
        self.assertEqual(model.tokenizer._word_cache["tea"], [model.vocab["tea"]])
        for tok in ("climb", "##ing", "tea", "sam"):
            tid = model.vocab[tok]
            self.assertIn(tid, model._row_cache)
            self.assertIsInstance(model._row_cache[tid], sm.array)  # issue #32: rows are arrays now, not tuples
            self.assertEqual(len(model._row_cache[tid]), model.dims)
        self.assertEqual(list(model.encode("tea tea tea")), list(model.encode("tea")))
        # Manual un-deduplicated sum for "climbing tea climbing sam" (6 token occurrences).
        ids = [model.vocab[t] for t in ("climb", "##ing", "tea", "climb", "##ing", "sam")]
        acc = [0.0] * model.dims
        for i in ids:
            row = model.weights[i * model.dims:(i + 1) * model.dims]
            for j in range(model.dims):
                acc[j] += row[j]
        norm = math.sqrt(sum(x * x for x in acc))
        expected = sm.array("f", [x / norm for x in acc])
        for got, want in zip(vec, expected):
            self.assertAlmostEqual(got, want, delta=1e-6)
        self.assertAlmostEqual(embeddings.dot(vec, expected), 1.0, places=5)
        self.assertEqual(embeddings.dot(vec, sm.array("f", [1.0])), 0.0)

    def test_row_cache_clears_at_the_cap_and_rows_stay_arrays(self):
        # Issue #32: the row cache must not grow without bound. On main this fails twice:
        # the cache holds all 4,295 rows (> the cap), and the rows are tuples, not arrays.
        n = sm.ROW_CACHE_MAX_ROWS + 204
        vocab = list(sm.SPECIAL_TOKENS) + [f"tk{i}" for i in range(n - len(sm.SPECIAL_TOKENS))]
        d = self.root / "wide"
        d.mkdir()
        (d / "vocab.txt").write_text("\n".join(vocab) + "\n", encoding="utf-8")
        dims = 4
        values = [float((i + j) % 7) for i in range(len(vocab)) for j in range(dims)]
        data = struct.pack(f"<{len(values)}f", *values)
        header = json.dumps({"embeddings": {"dtype": "F32", "shape": [len(vocab), dims],
                                            "data_offsets": [0, len(data)]}}).encode()
        (d / "model.safetensors").write_bytes(struct.pack("<Q", len(header)) + header + data)
        model = sm.StaticModel(d)
        words = vocab[len(sm.SPECIAL_TOKENS):]
        # 300 words per call stay under both the 512-token cap and the pre-truncation
        # (median token length here is 6 chars, so 300 x 7 fits comfortably), so every
        # word's row reaches the cache.
        for k in range(0, len(words), 300):
            model.encode(" ".join(words[k:k + 300]))
        # One clear at the cap leaves exactly len(words) - cap rows: asserting the exact
        # count pins both the eviction and the insertions after it, which a bare
        # assertLessEqual and a vacuous all() over an empty cache would not.
        self.assertEqual(len(model._row_cache), len(words) - sm.ROW_CACHE_MAX_ROWS)
        self.assertTrue(all(isinstance(row, sm.array) for row in model._row_cache.values()))

    def test_query_windowing_preserves_tail_tokens_for_long_prompts(self):
        model_dir = write_model(self.root / "m")
        model = sm.StaticModel(model_dir)
        self.assertEqual(sm.QUERY_HEAD_TOKENS, 352)
        self.assertEqual(sm.QUERY_TAIL_TOKENS, sm.MAX_TOKENS - sm.QUERY_HEAD_TOKENS)
        long_text = ("tea " * 600) + ("sam " * 80)
        self.assertGreater(len(long_text), sm.MAX_TOKENS * model.median_token_length)
        sam_vec = model.encode("sam")
        doc_vec = model.encode(long_text, query=False)
        query_vec = model.encode(long_text, query=True)
        self.assertEqual(list(doc_vec), list(model.encode("tea")))
        self.assertAlmostEqual(embeddings.dot(doc_vec, sam_vec), 0.0, places=5)
        self.assertGreater(embeddings.dot(query_vec, sam_vec), 0.2)
        emb = embeddings.StaticEmbedder(self.root / "store", model_dir=str(model_dir), auto_download=False)
        self.assertEqual(list(emb.embed_query(long_text)), list(query_vec))
        self.assertEqual(list(emb.embed_documents([long_text])[0]), list(doc_vec))

    def test_f16_matches_f32(self):
        f32 = sm.StaticModel(write_model(self.root / "a"))
        f16 = sm.StaticModel(write_model(self.root / "b", dtype="F16"))
        for text in ("tea coffee", "climbing sam", "morning drinks"):
            a, b = f32.encode(text), f16.encode(text)
            self.assertAlmostEqual(sum(x * y for x, y in zip(a, b)), 1.0, places=3)

    def test_f16_loads_where_memoryview_cannot_cast_half(self):
        # Python 3.10 and 3.11: the bundled (float16) model must still load, and encode the same.
        d = write_model(self.root / "m", dtype="F16")
        native = sm.StaticModel(d)
        with mock.patch.object(sm, "_MEMORYVIEW_CASTS_HALF", False):
            fallback = sm.StaticModel(d)
        self.assertIsInstance(fallback.weights, sm._HalfRows)
        for text in ("tea coffee", "climbing sam", "morning drinks", "日本 cafe!"):
            self.assertEqual(list(fallback.encode(text)), list(native.encode(text)))

    def test_shape_mismatch_is_refused(self):
        d = write_model(self.root / "m")
        (d / "vocab.txt").write_text("\n".join(VOCAB[:-1]) + "\n", encoding="utf-8")
        with self.assertRaises(sm.ModelUnavailable):
            sm.StaticModel(d)

    def test_locate_prefers_override_then_bundled_then_download(self):
        store = self.root / "store"
        self.assertIsNone(sm.locate(store) if not sm.is_complete(sm.BUNDLED_DIR) else None)
        downloaded = write_model(sm.download_dir(store))
        override = write_model(self.root / "override")
        self.assertEqual(sm.locate(store, str(override)), override)
        if not sm.is_complete(sm.BUNDLED_DIR):
            self.assertEqual(sm.locate(store), downloaded)


class FetchTest(unittest.TestCase):
    """fetch() against a local HTTP server standing in for Hugging Face."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(forget_loaded_models)
        root = Path(self.tmp.name)
        src = write_model(root / "srv" / sm.MODEL_REPO / "resolve" / sm.MODEL_REVISION)
        self.hashes = {n: hashlib.sha256((src / n).read_bytes()).hexdigest() for n in sm.MODEL_FILES}
        handler = lambda *a, **kw: SimpleHTTPRequestHandler(*a, directory=str(root / "srv"), **kw)  # noqa: E731
        SimpleHTTPRequestHandler.log_message = lambda *a: None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.store = root / "store"
        base = f"http://127.0.0.1:{self.server.server_port}"
        patches = [mock.patch.object(sm, "DOWNLOAD_BASE", base), mock.patch.dict(sm.MODEL_FILES, self.hashes),
                   mock.patch.object(sm, "BUNDLED_DIR", root / "no-bundle")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        sm._downloading.clear()

    def test_fetch_verifies_and_installs(self):
        dest = sm.fetch(self.store)
        self.assertTrue(sm.is_complete(dest))
        self.assertEqual([p.name for p in dest.iterdir() if ".part." in p.name], [])

    def test_hash_mismatch_is_refused(self):
        with mock.patch.dict(sm.MODEL_FILES, {"vocab.txt": "0" * 64}):
            with self.assertRaises(sm.ModelUnavailable):
                sm.fetch(self.store)
        self.assertFalse((sm.download_dir(self.store) / "vocab.txt").exists())

    def test_preexisting_file_is_hash_checked_and_redownloaded(self):
        # A corrupt file already at the destination must not be accepted as complete.
        dest = sm.download_dir(self.store)
        dest.mkdir(parents=True)
        (dest / "vocab.txt").write_text("garbage", encoding="utf-8")
        (dest / "model.safetensors").write_bytes(b"garbage")
        sm.fetch(self.store)
        self.assertTrue(sm.is_complete(dest))
        self.assertEqual(hashlib.sha256((dest / "vocab.txt").read_bytes()).hexdigest(), self.hashes["vocab.txt"])

    def test_preexisting_good_file_is_not_redownloaded(self):
        first = sm.fetch(self.store)
        # Verified local files are reused: any network access now is a failure.
        with mock.patch.object(sm.urllib.request, "urlopen", side_effect=AssertionError("re-downloaded")):
            self.assertEqual(sm.fetch(self.store), first)

    def test_static_embedder_downloads_in_the_background_then_works(self):
        emb = embeddings.StaticEmbedder(self.store, auto_download=True)
        self.assertIn("downloading", emb.down_reason())
        for _ in range(200):
            if not sm.download_status(self.store):
                break
            threading.Event().wait(0.02)
        self.assertIsNone(emb.down_reason())
        self.assertEqual(len(emb.embed_query("tea")), 4)

    def test_no_auto_download_says_how_to_get_it(self):
        emb = embeddings.StaticEmbedder(self.store, auto_download=False)
        self.assertIn("fetch-model", emb.down_reason())
        with self.assertRaises(embeddings.EmbeddingError):
            emb.embed_query("tea")

    def test_corrupt_model_is_down_not_a_crash(self):
        # A truncated model.safetensors raises struct.error inside StaticModel.__init__;
        # the embedder must report the model down, not let it escape (review finding).
        dest = sm.download_dir(self.store)
        dest.mkdir(parents=True)
        (dest / "vocab.txt").write_text("[UNK]\ntea\n", encoding="utf-8")
        (dest / "model.safetensors").write_bytes(b"\x00\x01")  # truncated header
        sm._cache.pop(str(dest.resolve()), None)
        emb = embeddings.StaticEmbedder(self.store, auto_download=False)
        reason = emb.down_reason()
        self.assertIsNotNone(reason)
        self.assertIn("unusable", reason or "")
        with self.assertRaises(embeddings.EmbeddingError):
            emb.embed_query("tea")


SYSTEM_BUNDLE = next((b for b in sm.CA_BUNDLES if os.path.isfile(b)), None)


class SslContextTest(unittest.TestCase):
    @unittest.skipUnless(SYSTEM_BUNDLE, "no system CA bundle")
    def test_falls_back_to_the_system_bundle_when_python_trusts_nothing(self):
        import ssl
        empty = lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # noqa: E731  (like a Python built for another distro)
        with mock.patch.object(sm.ssl, "create_default_context", empty), \
                mock.patch.dict(os.environ, {"NIX_SSL_CERT_FILE": ""}):
            self.assertGreater(sm._ssl_context().cert_store_stats()["x509_ca"], 0)


class FactoryTest(unittest.TestCase):
    def pick(self, **emb):
        return embeddings.make_embedder(config.Config.from_dict({"embeddings": emb}), Path("/nonexistent"))

    def test_backend_selection(self):
        self.assertIsInstance(self.pick(), embeddings.StaticEmbedder)
        self.assertIsInstance(self.pick(backend="auto"), embeddings.StaticEmbedder)  # the old default
        self.assertIsNone(self.pick(backend="off"))
        # `hermes config set ... embeddings.backend off` stores a YAML boolean (Hermes coerces
        # the word before writing, and a hand-edited unquoted `off` parses as one too), which
        # config.py stringifies to "False": that also means off, not a fallback to the model.
        self.assertIsNone(self.pick(backend=False))
        self.assertIsNone(self.pick(backend="False"))
        with self.assertLogs("memory_buckets", "WARNING"):
            self.assertIsInstance(self.pick(backend="http"), embeddings.StaticEmbedder)

    def test_thresholds(self):
        cfg = config.Config()
        self.assertEqual(embeddings.min_similarity(cfg, self.pick()), 0.27)
        cfg.prefetch_min_similarity = 0.6
        self.assertEqual(embeddings.min_similarity(cfg, self.pick()), 0.6)


REAL = os.environ.get("MEMORY_BUCKETS_MODEL_DIR")


@unittest.skipUnless(REAL and sm.is_complete(Path(REAL or "/")), "set MEMORY_BUCKETS_MODEL_DIR to the real model")
class RealModelTest(unittest.TestCase):
    DOCS = {
        "tea": "My favourite tea is lapsang souchong; I drink it every morning.",
        "nix": "The homelab runs NixOS with flakes and deploys using colmena.",
        "sam": "Sam is my climbing partner; we boulder on Thursdays.",
        "uni": "I'm studying computer science and my dissertation is on type systems.",
        "car": "My car is a 2014 Honda Jazz that needs its MOT in March.",
        "cat": "We have a black cat called Pixel who hates the vacuum cleaner.",
        "job": "I work part-time as a teaching assistant for the first-year programming module.",
        "diet": "I'm vegetarian and allergic to peanuts.",
    }
    QUERIES = {"what hot drink do I like": "tea", "beverages in the morning": "tea", "how are my servers configured": "nix",
               "who do I go climbing with": "sam", "rock climbing buddy": "sam", "what am I writing my thesis about": "uni",
               "when is my vehicle inspection due": "car", "tell me about our pet": "cat", "where do I work": "job",
               "what food should I avoid cooking for them": "diet", "any dietary restrictions?": "diet",
               "what's my research area": "uni"}

    @classmethod
    def setUpClass(cls):
        cls.model = sm.StaticModel(Path(REAL))

    def test_shape(self):
        self.assertEqual((self.model.rows, self.model.dims), (63091, 512))

    def test_retrieval_quality(self):
        docs = {k: self.model.encode(v) for k, v in self.DOCS.items()}
        cos = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731
        hits = 0
        for query, want in self.QUERIES.items():
            q = self.model.encode(query)
            hits += max(docs, key=lambda k: cos(q, docs[k])) == want
        self.assertGreaterEqual(hits, 10, "measured 11/12 when the model was adopted")

    def test_rss_stays_bounded_while_encoding_many_distinct_tokens(self):
        # Issue #32's acceptance probe as an assertion: encoding a corpus of 20,000
        # distinct tokens through the real model must not park hundreds of MB. On main
        # the delta was 455,984 KB (tuple rows, unbounded). With array rows capped at
        # ROW_CACHE_MAX_ROWS the delta is dominated by the model's own mmap pages,
        # which stay resident; 200 MB of slack separates the two states. Skips where
        # there is no /proc (Windows legs included).
        try:
            before = rss_kb()
        except OSError:
            self.skipTest("no /proc/self/status")
            return
        words = [t for t in self.model.vocab if t.isascii() and t.isalpha() and not t.startswith("##")]
        words = words[:20_000]
        for k in range(0, len(words), 300):
            self.model.encode(" ".join(words[k:k + 300]))
        delta = rss_kb() - before
        self.assertLess(delta, 200_000, f"{delta} KB parked for {len(words)} distinct tokens")

    @unittest.skipUnless(os.environ.get("MEMORY_BUCKETS_MODEL_F32"), "set MEMORY_BUCKETS_MODEL_F32 to the upstream file")
    def test_bundled_precision(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref_dir = Path(tmp)
            (ref_dir / "vocab.txt").write_bytes((Path(REAL) / "vocab.txt").read_bytes())
            symlink_or_skip(self, os.environ["MEMORY_BUCKETS_MODEL_F32"], ref_dir / "model.safetensors")
            ref = sm.StaticModel(ref_dir)
            for text in list(self.DOCS.values())[:4]:
                a, b = ref.encode(text), self.model.encode(text)
                self.assertGreater(sum(x * y for x, y in zip(a, b)), 0.9999)
            del ref  # unmap before the directory goes: Windows can't delete a mapped file

    @unittest.skipUnless(importlib.util.find_spec("tokenizers") and os.environ.get("MEMORY_BUCKETS_REFERENCE_TOKENIZER"),
                         "needs HF tokenizers and MEMORY_BUCKETS_REFERENCE_TOKENIZER")
    def test_tokenizer_matches_reference(self):
        from tokenizers import Tokenizer
        ref = Tokenizer.from_file(os.environ["MEMORY_BUCKETS_REFERENCE_TOKENIZER"])
        corpus = [line for p in Path(__file__).resolve().parents[1].rglob("*.py") for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        corpus += list(self.DOCS.values()) + list(self.QUERIES) + [
            "Café naïve résumé Ångström", "日本語のテキストと中文", "emoji 🎉🚀 ok", "ﬁ ligature ＦＵＬＬＷＩＤＴＨ",
            "tab\there\u00a0nbsp\u200bzwsp", "x" * 150, "don't-stop... (really?!) [[wiki|links]] `code_ident`",
            "İstanbul ß straße", "control\x07char\x00null", "Ⅻ ² ½ № ™",
            "ΟΔΟΣ ΑΣ. ΣΑΣ (?![λΠΣ])[α-ωΑ-Ω]"]  # capital sigma stays σ, never final-sigma ς
        mismatches = [t for t in corpus if ref.encode(t, add_special_tokens=False).tokens != self.model.tokenizer.tokens(t)]
        self.assertEqual(mismatches[:5], [], f"{len(mismatches)}/{len(corpus)} lines tokenise differently")


if __name__ == "__main__":
    unittest.main()
