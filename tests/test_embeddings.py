"""The built-in ONNX embedding model (EmbeddingGemma 2).

Unit tests build a tiny synthetic ONNX graph (with external data in
``model_q4.onnx_data``) and a BPE/WordLevel ``tokenizer.json`` on the fly.
Real-model tests run when ``MEMORY_BUCKETS_MODEL_DIR`` points at
``embeddinggemma-2-ONNX`` (the Nix check and CI do this).
"""

import gc
import hashlib
import importlib.util
import math
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import numpy as np
from tokenizers import Tokenizer, models, normalizers, pre_tokenizers, processors

from . import memory_buckets  # noqa: F401
from memory_buckets import config, embeddings

VOCAB_TOKENS = [
    "<pad>", "<eos>", "<bos>", "<unk>",
    "task", ":", "search", "result", "|", "query", "title", "none", "text",
    "tea", "coffee", "drink", "drinks", "morning", "climb", "climbing", "sam",
]
VOCAB = {tok: i for i, tok in enumerate(VOCAB_TOKENS)}


# -- minimal ONNX protobuf wire builders (IR v8, opset 18) -------------------------

def _varint(v: int) -> bytes:
    out = bytearray()
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)


def _field_varint(tag: int, val: int) -> bytes:
    return _varint((tag << 3) | 0) + _varint(val)


def _field_bytes(tag: int, data: bytes) -> bytes:
    return _varint((tag << 3) | 2) + _varint(len(data)) + data


def _field_str(tag: int, s: str) -> bytes:
    return _field_bytes(tag, s.encode("utf-8"))


def _value_info(name: str, elem_type: int, dims: list) -> bytes:
    shape_bytes = b""
    for d in dims:
        dim_msg = _field_varint(1, d) if isinstance(d, int) else _field_str(2, str(d))
        shape_bytes += _field_bytes(1, dim_msg)
    tensor_msg = _field_varint(1, elem_type) + _field_bytes(2, shape_bytes)
    return _field_str(1, name) + _field_bytes(2, _field_bytes(1, tensor_msg))


def _attr_int(name: str, val: int) -> bytes:
    return _field_str(1, name) + _field_varint(3, val) + _field_varint(20, 2)


def _node(op_type: str, inputs: list, outputs: list, attrs: tuple = ()) -> bytes:
    body = b""
    for i in inputs:
        body += _field_str(1, i)
    for o in outputs:
        body += _field_str(2, o)
    body += _field_str(4, op_type)
    for a in attrs:
        body += _field_bytes(5, a)
    return body


def _external_tensor(name: str, elem_type: int, dims: list, location: str, offset: int, length: int) -> bytes:
    body = b""
    for d in dims:
        body += _field_varint(1, d)
    body += _field_varint(2, elem_type)
    body += _field_str(8, name)
    for k, v in (("location", location), ("offset", str(offset)), ("length", str(length))):
        body += _field_bytes(13, _field_str(1, k) + _field_str(2, v))
    body += _field_varint(14, 1)  # EXTERNAL = 1
    return body


def _inline_int64_tensor(name: str, vals: list) -> bytes:
    raw = struct.pack(f"<{len(vals)}q", *vals)
    return _field_varint(1, len(vals)) + _field_varint(2, 7) + _field_str(8, name) + _field_bytes(9, raw)


def write_model(directory: Path, *, onnx_subdir: bool = False) -> Path:
    """Write a synthetic EmbeddingGemma 2 ONNX model (768d output, external data in
    ``model_q4.onnx_data``, and ``tokenizer.json``) into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    tok = Tokenizer(models.WordLevel(vocab=VOCAB, unk_token="<unk>"))
    tok.normalizer = normalizers.Lowercase()
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.post_processor = processors.TemplateProcessing(
        single="<bos> $A <eos>",
        special_tokens=[("<bos>", VOCAB["<bos>"]), ("<eos>", VOCAB["<eos>"])],
    )
    tok.save(str(directory / "tokenizer.json"))

    rows, cols = len(VOCAB_TOKENS), 768
    weights = np.zeros((rows, cols), dtype=np.float32)
    # Non-zero weight on <pad> (id 0): an unmasked pad token would corrupt the output.
    weights[VOCAB["<pad>"], 10] = 50.0
    # Distinct small weights on <bos>, <eos>, and "query" so tests can verify wrapping and prefixing.
    weights[VOCAB["<bos>"], 6] = 0.05
    weights[VOCAB["<eos>"], 7] = 0.05
    weights[VOCAB["query"], 5] = 0.1
    for tok_str, idx in VOCAB.items():
        if tok_str in ("tea", "coffee", "drink", "drinks", "morning"):
            weights[idx, 0] = 1.0
            weights[idx, 1] = 0.2
            weights[idx, 300] = 4.0  # beyond 256d: tests Matryoshka truncation + re-normalisation
        elif tok_str in ("sam", "climb", "climbing"):
            weights[idx, 2] = 1.0
            weights[idx, 3] = 0.2
            weights[idx, 300] = 4.0
    raw_weights = weights.tobytes()

    onnx_dir = directory / "onnx" if onnx_subdir else directory
    onnx_dir.mkdir(parents=True, exist_ok=True)
    (onnx_dir / "model_q4.onnx_data").write_bytes(raw_weights)

    inputs = [
        _value_info("input_ids", 7, ["batch_size", "sequence_length"]),
        _value_info("attention_mask", 7, ["batch_size", "sequence_length"]),
        _value_info("image_features", 1, ["num_image_tokens", 512]),
        _value_info("video_features", 1, ["num_video_tokens", 512]),
        _value_info("audio_features", 1, ["num_audio_tokens", 512]),
    ]
    outputs = [
        _value_info("last_hidden_state", 1, ["batch_size", "sequence_length", cols]),
        _value_info("sentence_embedding", 1, ["batch_size", cols]),
    ]
    initializers = [
        _external_tensor("embed_table", 1, [rows, cols], "model_q4.onnx_data", 0, len(raw_weights)),
        _inline_int64_tensor("unsq_axes", [2]),
        _inline_int64_tensor("reduce_axes", [1]),
    ]
    nodes = [
        _node("Gather", ["embed_table", "input_ids"], ["last_hidden_state"], (_attr_int("axis", 0),)),
        _node("Cast", ["attention_mask"], ["mask_f32"], (_attr_int("to", 1),)),
        _node("Unsqueeze", ["mask_f32", "unsq_axes"], ["mask_3d"]),
        _node("Mul", ["last_hidden_state", "mask_3d"], ["masked_hidden"]),
        _node("ReduceSum", ["masked_hidden", "reduce_axes"], ["sentence_embedding"], (_attr_int("keepdims", 0),)),
    ]
    graph = b"".join(_field_bytes(1, n) for n in nodes) + _field_str(2, "synthetic_embeddinggemma")
    graph += b"".join(_field_bytes(5, i) for i in initializers)
    graph += b"".join(_field_bytes(11, i) for i in inputs)
    graph += b"".join(_field_bytes(12, o) for o in outputs)
    opset = _field_str(1, "") + _field_varint(2, 18)
    model_proto = _field_varint(1, 8) + _field_bytes(7, graph) + _field_bytes(8, opset)
    (onnx_dir / "model_q4.onnx").write_bytes(model_proto)
    return directory


def forget_loaded_models():
    """Drop models that load() cached for the process. Each one keeps its files open,
    and Windows can't delete an open file, so a test's temp dir couldn't be cleaned up."""
    embeddings._cache.clear()
    gc.collect()


class OnnxModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(forget_loaded_models)  # runs first: cleanups are last in, first out
        self.root = Path(self.tmp.name)

    def test_encode_truncates_to_256d_and_renormalises(self):
        model = embeddings.OnnxModel(write_model(self.root / "m"))
        vec = model.encode("tea zzz")
        self.assertEqual(len(vec), 256)
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in vec)), 1.0, places=5)
        self.assertEqual(list(vec), list(model.encode("tea")), "<unk> contributes nothing")
        drink, coffee, sam = model.encode("drink"), model.encode("coffee"), model.encode("sam")
        self.assertGreater(embeddings.dot(drink, coffee), embeddings.dot(drink, sam))
        self.assertEqual(embeddings.dot(vec, embeddings.array("f", [1.0])), 0.0)

    def test_batch_padding_masks_pad_tokens_and_matches_single_encode(self):
        model = embeddings.OnnxModel(write_model(self.root / "m"))
        texts = ["tea", "tea coffee drinks morning", "sam climbing"]
        batched = model.encode_batch(texts)
        singles = [model.encode(t) for t in texts]
        for got, want in zip(batched, singles):
            self.assertEqual(len(got), 256)
            self.assertEqual(got[10], 0.0, "<pad> has weight at dim 10; attention_mask must zero it out")
            for a, b in zip(got, want):
                self.assertAlmostEqual(a, b, places=6)

    def test_query_prefix_and_id_level_windowing_preserve_tail_and_special_tokens(self):
        model_dir = write_model(self.root / "m")
        model = embeddings.OnnxModel(model_dir)
        self.assertEqual(embeddings.QUERY_HEAD_TOKENS, 352)
        self.assertEqual(embeddings.QUERY_TAIL_TOKENS, embeddings.MAX_TOKENS - embeddings.QUERY_HEAD_TOKENS)
        long_text = ("tea " * 600) + ("sam " * 80)
        doc_ids = model._token_ids(long_text, query=False)
        query_ids = model._token_ids(long_text, query=True)
        self.assertEqual(len(doc_ids), embeddings.MAX_TOKENS + 2)
        self.assertEqual(len(query_ids), embeddings.MAX_TOKENS + 2)
        self.assertEqual((doc_ids[0], doc_ids[-1]), (VOCAB["<bos>"], VOCAB["<eos>"]))
        self.assertEqual((query_ids[0], query_ids[-1]), (VOCAB["<bos>"], VOCAB["<eos>"]))
        self.assertNotIn(VOCAB["sam"], doc_ids)
        self.assertIn(VOCAB["sam"], query_ids)

        sam_vec = model.encode("sam", query=False)
        doc_vec = model.encode(long_text, query=False)
        query_vec = model.encode(long_text, query=True)
        self.assertGreater(query_vec[5], 0.0, "embed_query prepends the task prefix containing 'query'")
        self.assertEqual(doc_vec[5], 0.0, "document encoding does not prepend the query prefix")
        self.assertGreater(query_vec[6], 0.0, "<bos> preserved after windowing")
        self.assertGreater(query_vec[7], 0.0, "<eos> preserved after windowing")
        self.assertLess(embeddings.dot(doc_vec, sam_vec), 0.05)
        self.assertGreater(embeddings.dot(query_vec, sam_vec), 0.15)

        emb = embeddings.OnnxEmbedder(self.root / "store", model_dir=str(model_dir), auto_download=False)
        self.assertEqual(list(emb.embed_query(long_text)), list(query_vec))
        self.assertEqual(list(emb.embed_documents([long_text])[0]), list(doc_vec))

    def test_session_requests_only_sentence_embedding_and_sets_thread_caps(self):
        model = embeddings.OnnxModel(write_model(self.root / "m"))
        opts = model.session.get_session_options()
        self.assertEqual(opts.intra_op_num_threads, min(4, os.cpu_count() or 1))
        self.assertEqual(opts.inter_op_num_threads, 1)
        with mock.patch.object(model.session, "run", wraps=model.session.run) as run_spy:
            model.encode("tea")
        run_spy.assert_called_once()
        output_names, feeds = run_spy.call_args.args
        self.assertEqual(output_names, ["sentence_embedding"])
        self.assertEqual(
            set(feeds),
            {"input_ids", "attention_mask", "image_features", "video_features", "audio_features"},
        )
        for media in ("image_features", "video_features", "audio_features"):
            self.assertEqual(feeds[media].shape, (0, 512))
            self.assertEqual(feeds[media].dtype, np.float32)

    def test_missing_external_data_is_refused(self):
        d = write_model(self.root / "m")
        (d / "model_q4.onnx_data").unlink()
        self.assertFalse(embeddings.is_complete(d))
        with self.assertRaises(embeddings.ModelUnavailable):
            embeddings.OnnxModel(d)

    def test_locate_prefers_override_then_bundled_then_download_and_accepts_onnx_subdir(self):
        store = self.root / "store"
        if not embeddings.is_complete(embeddings.BUNDLED_DIR):
            self.assertIsNone(embeddings.locate(store))
        downloaded = write_model(embeddings.download_dir(store))
        override = write_model(self.root / "override", onnx_subdir=True)
        self.assertTrue(embeddings.is_complete(override))
        self.assertEqual(embeddings.locate(store, str(override)), override)
        model = embeddings.OnnxModel(override)
        self.assertEqual(len(model.encode("tea")), 256)
        if not embeddings.is_complete(embeddings.BUNDLED_DIR):
            self.assertEqual(embeddings.locate(store), downloaded)

    def test_standalone_load_and_notice_need_only_stdlib(self):
        # hatch_build.py and nix/model.nix load embeddings.py by file path in a build
        # environment without onnxruntime or tokenizers installed.
        path = Path(embeddings.__file__).resolve()
        spec = importlib.util.spec_from_file_location("_standalone_embeddings", path)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"onnxruntime": None, "tokenizers": None, "numpy": None}):
            spec.loader.exec_module(module)
        text = module.notice()
        self.assertIn(embeddings.MODEL_NAME, text)
        self.assertIn(embeddings.MODEL_REVISION, text)
        self.assertIn("Apache-2.0", text)


class FetchTest(unittest.TestCase):
    """fetch() against a local HTTP server standing in for Hugging Face."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(forget_loaded_models)
        root = Path(self.tmp.name)
        rev_dir = root / "srv" / embeddings.MODEL_REPO / "resolve" / embeddings.MODEL_REVISION
        src = write_model(root / "built")
        self.hashes = {}
        for name, rel_url in embeddings.MODEL_URLS.items():
            target = rev_dir / rel_url
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src / name, target)
            self.hashes[name] = hashlib.sha256(target.read_bytes()).hexdigest()
        handler = lambda *a, **kw: SimpleHTTPRequestHandler(*a, directory=str(root / "srv"), **kw)  # noqa: E731
        SimpleHTTPRequestHandler.log_message = lambda *a: None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.store = root / "store"
        base = f"http://127.0.0.1:{self.server.server_port}"
        patches = [
            mock.patch.object(embeddings, "DOWNLOAD_BASE", base),
            mock.patch.dict(embeddings.MODEL_FILES, self.hashes),
            mock.patch.object(embeddings, "BUNDLED_DIR", root / "no-bundle"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        embeddings._downloading.clear()

    def test_fetch_verifies_and_places_onnx_and_data_side_by_side(self):
        dest = embeddings.fetch(self.store)
        self.assertTrue(embeddings.is_complete(dest))
        for name in embeddings.MODEL_FILES:
            self.assertTrue((dest / name).is_file(), name)
        self.assertEqual([p.name for p in dest.iterdir() if ".part." in p.name], [])

    def test_hash_mismatch_is_refused(self):
        with mock.patch.dict(embeddings.MODEL_FILES, {"tokenizer.json": "0" * 64}):
            with self.assertRaises(embeddings.ModelUnavailable):
                embeddings.fetch(self.store)
        self.assertFalse((embeddings.download_dir(self.store) / "tokenizer.json").exists())

    def test_preexisting_file_is_hash_checked_and_redownloaded(self):
        dest = embeddings.download_dir(self.store)
        dest.mkdir(parents=True)
        for name in embeddings.MODEL_FILES:
            (dest / name).write_bytes(b"garbage")
        embeddings.fetch(self.store)
        self.assertTrue(embeddings.is_complete(dest))
        for name, want in self.hashes.items():
            self.assertEqual(hashlib.sha256((dest / name).read_bytes()).hexdigest(), want)

    def test_preexisting_good_file_is_not_redownloaded(self):
        first = embeddings.fetch(self.store)
        with mock.patch.object(embeddings.urllib.request, "urlopen", side_effect=AssertionError("re-downloaded")):
            self.assertEqual(embeddings.fetch(self.store), first)

    def test_onnx_embedder_downloads_in_the_background_then_works(self):
        emb = embeddings.OnnxEmbedder(self.store, auto_download=True)
        self.assertIn("downloading", emb.down_reason())
        for _ in range(200):
            if not embeddings.download_status(self.store):
                break
            threading.Event().wait(0.02)
        self.assertIsNone(emb.down_reason())
        self.assertEqual(len(emb.embed_query("tea")), 256)

    def test_no_auto_download_says_how_to_get_it(self):
        emb = embeddings.OnnxEmbedder(self.store, auto_download=False)
        self.assertIn("fetch-model", emb.down_reason())
        with self.assertRaises(embeddings.EmbeddingError):
            emb.embed_query("tea")

    def test_corrupt_model_is_down_not_a_crash(self):
        dest = embeddings.download_dir(self.store)
        dest.mkdir(parents=True)
        (dest / "tokenizer.json").write_text("{}", encoding="utf-8")
        (dest / "model_q4.onnx").write_bytes(b"\x00\x01")
        (dest / "model_q4.onnx_data").write_bytes(b"\x00")
        embeddings._cache.pop(str(dest.resolve()), None)
        emb = embeddings.OnnxEmbedder(self.store, auto_download=False)
        reason = emb.down_reason()
        self.assertIsNotNone(reason)
        self.assertIn("unusable", reason or "")
        with self.assertRaises(embeddings.EmbeddingError):
            emb.embed_query("tea")

    def test_hatch_build_bundles_model_and_notice(self):
        import hatch_build
        cache_dest = Path(self.tmp.name) / "wheel-cache" / "model"
        hatch_build._build_model(embeddings, cache_dest, lambda _msg: None)
        self.assertTrue(embeddings.is_complete(cache_dest))
        self.assertIn("Apache-2.0", (cache_dest / "NOTICE").read_text(encoding="utf-8"))


SYSTEM_BUNDLE = next((b for b in embeddings.CA_BUNDLES if os.path.isfile(b)), None)


class SslContextTest(unittest.TestCase):
    @unittest.skipUnless(SYSTEM_BUNDLE, "no system CA bundle")
    def test_falls_back_to_the_system_bundle_when_python_trusts_nothing(self):
        import ssl
        empty = lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # noqa: E731
        with mock.patch.object(embeddings.ssl, "create_default_context", empty), \
                mock.patch.dict(os.environ, {"NIX_SSL_CERT_FILE": ""}):
            self.assertGreater(embeddings._ssl_context().cert_store_stats()["x509_ca"], 0)


class FactoryTest(unittest.TestCase):
    def pick(self, **emb):
        return embeddings.make_embedder(config.Config.from_dict({"embeddings": emb}), Path("/nonexistent"))

    def test_backend_selection(self):
        emb = self.pick()
        self.assertIsInstance(emb, embeddings.OnnxEmbedder)
        self.assertEqual(emb.identity, "onnx:embeddinggemma-2-ONNX@daa72c51:d256")
        self.assertEqual(emb.batch, 16)
        self.assertIsInstance(self.pick(backend="auto"), embeddings.OnnxEmbedder)
        self.assertIsNone(self.pick(backend="off"))
        self.assertIsNone(self.pick(backend=False))
        self.assertIsNone(self.pick(backend="False"))
        with self.assertLogs("memory_buckets", "WARNING"):
            self.assertIsInstance(self.pick(backend="http"), embeddings.OnnxEmbedder)

    def test_thresholds(self):
        cfg = config.Config()
        self.assertEqual(embeddings.min_similarity(cfg, self.pick()), 0.68)
        cfg.prefetch_min_similarity = 0.6
        self.assertEqual(embeddings.min_similarity(cfg, self.pick()), 0.6)


REAL = os.environ.get("MEMORY_BUCKETS_MODEL_DIR")


@unittest.skipUnless(REAL and embeddings.is_complete(Path(REAL or "/")), "set MEMORY_BUCKETS_MODEL_DIR to the real model")
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
    QUERIES = {
        "what hot drink do I like": "tea",
        "beverages in the morning": "tea",
        "how are my servers configured": "nix",
        "who do I go climbing with": "sam",
        "rock climbing buddy": "sam",
        "what am I writing my thesis about": "uni",
        "when is my vehicle inspection due": "car",
        "tell me about our pet": "cat",
        "where do I work": "job",
        "what food should I avoid cooking for them": "diet",
        "any dietary restrictions?": "diet",
        "what's my research area": "uni",
    }

    @classmethod
    def setUpClass(cls):
        with mock.patch.object(embeddings.urllib.request, "urlopen", side_effect=AssertionError("network access")):
            cls.model = embeddings.OnnxModel(Path(REAL))

    def test_dims_and_unit_norm(self):
        self.assertEqual(self.model.dims, 256)
        vec = self.model.encode("title: tea | text: My favourite tea is lapsang souchong.")
        self.assertEqual(len(vec), 256)
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in vec)), 1.0, places=5)

    def test_retrieval_quality(self):
        with mock.patch.object(embeddings.urllib.request, "urlopen", side_effect=AssertionError("network access")):
            docs = {k: self.model.encode(f"title: {k} | text: {v}") for k, v in self.DOCS.items()}
            hits = 0
            for query, want in self.QUERIES.items():
                q = self.model.encode(query, query=True)
                hits += max(docs, key=lambda k: embeddings.dot(q, docs[k])) == want
        self.assertGreaterEqual(hits, 11, "measured 12/12 when EmbeddingGemma 2 was adopted")


if __name__ == "__main__":
    unittest.main()
