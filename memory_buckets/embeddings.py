"""Embeddings: the built-in ``embeddinggemma-2-ONNX`` model, run in-process on CPU
through ``onnxruntime`` and tokenised with Hugging Face ``tokenizers`` on the
vendored ``tokenizer.json``. No server, no runtime network calls unless
downloading the pinned model files on first use.

``OnnxEmbedder`` exposes ``identity``, ``describe()``, ``down_reason()``,
``embed_documents()``, ``embed_query()`` and ``default_min_similarity``, which is
all the index and the provider use. Vectors are 256-dimensional Matryoshka slices,
L2-normalised in float32, so cosine similarity is a dot product.

Where the model comes from, first match wins:

1. ``model/`` next to this file: bundled into wheels by ``hatch_build.py`` and into
   the Nix package by ``nix/model.nix``; gitignored in a checkout.
2. ``<store>/.models/<name>/``: downloaded, pinned and hash-checked. The download
   happens in the background on first use, or up front with
   ``hermes memory-buckets fetch-model``.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import ssl
import struct
import threading
import urllib.request
from array import array
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("memory_buckets")

_sumprod = getattr(math, "sumprod", None)

MODEL_NAME = "embeddinggemma-2-ONNX"
MODEL_REPO = "onnx-community/embeddinggemma-2-ONNX"
MODEL_REVISION = "daa72c51243991dfcaf9f9137d2c573d8f7790c0"
MODEL_FILES = {  # local filename -> sha256 of the upstream file at MODEL_REVISION
    "model_q4.onnx": "f9eeba97acddf139b8ee2ddf04bc30dceafa88de93fadf74d7644e0d61a477a9",
    "model_q4.onnx_data": "c3975f2d1ab7a1878ae31a7d7a9b7804a827aff3800b60dfceafce21cac3df49",
    "tokenizer.json": "4d777ef5bdc1aa36227abdfb77c3e49e7b9c892d16e1b6bda41c393504828be4",
}
MODEL_URLS = {  # local filename -> path inside MODEL_REPO
    "model_q4.onnx": "onnx/model_q4.onnx",
    "model_q4.onnx_data": "onnx/model_q4.onnx_data",
    "tokenizer.json": "tokenizer.json",
}
DOWNLOAD_BASE = "https://huggingface.co"
BUNDLED_DIR = Path(__file__).resolve().parent / "model"

DIMS = 256
MAX_TOKENS = 512
QUERY_HEAD_TOKENS = 352
QUERY_TAIL_TOKENS = MAX_TOKENS - QUERY_HEAD_TOKENS
QUERY_PREFIX = "task: search result | query: "
PAD_ID = 0
_QUERY_CACHE_MAX = 64


class EmbeddingError(Exception):
    pass


class ModelUnavailable(Exception):
    pass


def dot(a: array, b: array) -> float:
    if len(a) != len(b):
        return 0.0
    return _sumprod(a, b) if _sumprod is not None else sum(map(float.__mul__, a, b))


def _onnx_dir(directory: Path) -> Path:
    d = Path(directory)
    if (d / "onnx" / "model_q4.onnx").is_file() and not (d / "model_q4.onnx").is_file():
        return d / "onnx"
    return d


class OnnxModel:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        if not is_complete(self.directory):
            raise ModelUnavailable(f"incomplete model directory {self.directory}")
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as err:
            raise ModelUnavailable(f"missing runtime dependency ({err})") from None
        self._np = np
        try:
            self.tokenizer = Tokenizer.from_file(str(self.directory / "tokenizer.json"))
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = min(4, os.cpu_count() or 1)
            opts.inter_op_num_threads = 1
            opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
            onnx_path = _onnx_dir(self.directory) / "model_q4.onnx"
            self.session = ort.InferenceSession(
                str(onnx_path),
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
        except Exception as err:
            raise ModelUnavailable(str(err)) from None
        self.dims = DIMS
        self._empty_features = np.empty((0, 512), dtype=np.float32)
        self._query_cache: "OrderedDict[str, array]" = OrderedDict()
        self._query_lock = threading.Lock()

    def _token_ids(self, text: str, *, query: bool = False) -> List[int]:
        if query:
            text = QUERY_PREFIX + text
        ids = list(self.tokenizer.encode(text, add_special_tokens=True).ids)
        if len(ids) > MAX_TOKENS + 2:
            bos, body, eos = ids[0], ids[1:-1], ids[-1]
            if query:
                body = body[:QUERY_HEAD_TOKENS] + body[-QUERY_TAIL_TOKENS:]
            else:
                body = body[:MAX_TOKENS]
            ids = [bos, *body, eos]
        return ids

    def encode_batch(self, texts: List[str], *, query: bool = False) -> List[array]:
        if not texts:
            return []
        np = self._np
        try:
            seqs = [self._token_ids(t, query=query) for t in texts]
            max_len = max((len(s) for s in seqs), default=0)
            if max_len == 0:
                return [array("f", [0.0] * self.dims) for _ in texts]
            batch_size = len(seqs)
            input_ids = np.full((batch_size, max_len), PAD_ID, dtype=np.int64)
            attention_mask = np.zeros((batch_size, max_len), dtype=np.int64)
            for i, seq in enumerate(seqs):
                n = len(seq)
                if n:
                    input_ids[i, :n] = seq
                    attention_mask[i, :n] = 1
            feeds = {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "image_features": self._empty_features,
                "video_features": self._empty_features,
                "audio_features": self._empty_features,
            }
            (raw,) = self.session.run(["sentence_embedding"], feeds)
        except Exception as err:
            raise ModelUnavailable(str(err)) from None
        sliced = np.asarray(raw[:, : self.dims], dtype=np.float32)
        norms = np.linalg.norm(sliced, axis=1, keepdims=True)
        valid = np.isfinite(norms) & (norms > 0.0)
        safe_norms = np.where(valid, norms, np.float32(1.0))
        normed = np.where(valid, sliced / safe_norms, np.float32(0.0)).astype(np.float32, copy=False)
        return [array("f", row.tolist()) for row in normed]

    def encode(self, text: str, *, query: bool = False) -> array:
        if query:
            with self._query_lock:
                cached = self._query_cache.get(text)
                if cached is not None:
                    self._query_cache.move_to_end(text)
                    return array("f", cached)
        vec = self.encode_batch([text], query=query)[0]
        if query:
            with self._query_lock:
                self._query_cache[text] = array("f", vec)
                self._query_cache.move_to_end(text)
                while len(self._query_cache) > _QUERY_CACHE_MAX:
                    self._query_cache.popitem(last=False)
        return vec


# -- locating, caching and fetching -----------------------------------------------------------------

_cache: Dict[str, OnnxModel] = {}
_cache_lock = threading.Lock()
_download_lock = threading.Lock()
_downloading: Dict[str, str] = {}  # destination -> status


def is_complete(directory: Path) -> bool:
    d = Path(directory)
    onnx_d = _onnx_dir(d)
    for name in MODEL_FILES:
        base = onnx_d if name.startswith("model_q4.onnx") else d
        if not (base / name).is_file():
            return False
    return True


def download_dir(store_root: Path) -> Path:
    return Path(store_root) / ".models" / f"{MODEL_NAME}-{MODEL_REVISION[:8]}"


def locate(store_root: Optional[Path], override: str = "") -> Optional[Path]:
    candidates = [Path(override).expanduser()] if override else []
    candidates.append(BUNDLED_DIR)
    if store_root is not None:
        candidates.append(download_dir(store_root))
    return next((c for c in candidates if is_complete(c)), None)


def load(directory: Path) -> OnnxModel:
    key = str(Path(directory).resolve())
    with _cache_lock:
        model = _cache.get(key)
        if model is None:
            model = _cache[key] = OnnxModel(Path(directory))
        return model


def download_status(store_root: Path) -> Optional[str]:
    with _download_lock:
        return _downloading.get(str(download_dir(store_root)))


# Where distributions keep their CA bundle. A Python built elsewhere (uv's standalone
# builds, for one) can look in a directory this system doesn't have and trust nothing.
CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/pki/tls/cacert.pem",
    "/etc/ssl/cert.pem",
)


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats()["x509_ca"]:
        return ctx
    for bundle in (os.environ.get("NIX_SSL_CERT_FILE"), *CA_BUNDLES):
        if bundle and os.path.isfile(bundle):
            try:
                ctx.load_verify_locations(cafile=bundle)
                return ctx
            except (OSError, ssl.SSLError):
                continue
    return ctx  # nothing found: the download fails with the usual verification error


def fetch(
    store_root: Path,
    *,
    dest: Optional[Path] = None,
    progress: Optional[Callable[[str], None]] = None,
    timeout: float = 120.0,
) -> Path:
    """Download the pinned model into ``<store>/.models`` (or ``dest``), verifying
    every file's sha256. Safe to call from several processes; files land by atomic
    rename, with ``model_q4.onnx`` and ``model_q4.onnx_data`` side by side."""
    target_dir = Path(dest) if dest is not None else download_dir(store_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, sha in MODEL_FILES.items():
        target = target_dir / name
        if target.is_file():
            # A pre-existing file is only as good as its hash: a partial download that
            # landed here, or a tampered .models dir, must not be accepted as complete.
            digest = hashlib.sha256()
            with open(target, "rb") as existing:
                for block in iter(lambda: existing.read(1 << 20), b""):
                    digest.update(block)
            if digest.hexdigest() == sha:
                continue
            logger.warning("memory-buckets: %s fails its sha256 check; downloading it again", target)
            target.unlink()
        rel_url = MODEL_URLS.get(name, name)
        url = f"{DOWNLOAD_BASE}/{MODEL_REPO}/resolve/{MODEL_REVISION}/{rel_url}"
        tmp = target_dir / f".{name}.part.{os.getpid()}.{threading.get_ident()}"
        digest = hashlib.sha256()
        if progress:
            progress(f"downloading {name}")
        try:
            with urllib.request.urlopen(url, timeout=timeout, context=_ssl_context()) as response, open(tmp, "wb") as out:
                while True:
                    block = response.read(1 << 20)
                    if not block:
                        break
                    digest.update(block)
                    out.write(block)
            if digest.hexdigest() != sha:
                raise ModelUnavailable(f"{name}: sha256 mismatch (got {digest.hexdigest()[:12]}..., want {sha[:12]}...)")
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink()
    return target_dir


def fetch_in_background(store_root: Path, spawn: Callable[..., threading.Thread]) -> None:
    """Start one background download per destination per process."""
    key = str(download_dir(store_root))
    with _download_lock:
        if key in _downloading:
            return
        _downloading[key] = "downloading the built-in embedding model"

    def run():
        try:
            fetch(store_root)
            logger.info("memory-buckets: built-in embedding model ready at %s", key)
            with _download_lock:
                _downloading.pop(key, None)
        except Exception as err:  # noqa: BLE001 - reported through status, retried next process
            logger.warning("memory-buckets: built-in embedding model download failed: %s", err)
            with _download_lock:
                _downloading[key] = f"model download failed ({err}); keyword search only until it succeeds"

    spawn(run, name="memory-buckets-model-fetch").start()


def notice() -> str:
    """Attribution shipped beside a bundled copy of the model (NOTICE)."""
    return (
        f"{MODEL_NAME}\n"
        f"Google EmbeddingGemma 2 (https://huggingface.co/google/embeddinggemma-2),\n"
        f"ONNX q4 export by onnx-community: https://huggingface.co/{MODEL_REPO}\n"
        f"revision {MODEL_REVISION}, Apache-2.0 licence.\n"
    )


# -- embedder ---------------------------------------------------------------------------------------

class OnnxEmbedder:
    """The built-in ONNX model. ``down_reason`` is None once the model is loaded;
    before that it says why (downloading, missing) and, with ``auto_download``,
    starts the background download."""

    default_min_similarity = 0.68  # measured on small store: relevant median 0.69, negative max 0.675 (p95 0.674)
    batch = 16

    def __init__(
        self,
        store_root: Path,
        *,
        model_dir: str = "",
        auto_download: bool = True,
        spawn: Optional[Callable[..., threading.Thread]] = None,
    ) -> None:
        self.store_root = Path(store_root)
        self.model_dir = model_dir
        self.auto_download = auto_download
        self.spawn = spawn or (lambda target, name: threading.Thread(target=target, name=name, daemon=True))
        self._model: Optional[OnnxModel] = None
        self._lock = threading.Lock()

    @property
    def identity(self) -> str:
        return f"onnx:{MODEL_NAME}@{MODEL_REVISION[:8]}:d{DIMS}"

    def describe(self) -> str:
        where = self._model.directory if self._model else "not loaded"
        return f"built-in {MODEL_NAME} ({where})"

    def _load(self) -> OnnxModel:
        with self._lock:
            if self._model is not None:
                return self._model
            found = locate(self.store_root, self.model_dir)
            if found is None:
                status = download_status(self.store_root)
                if status is None and self.auto_download:
                    fetch_in_background(self.store_root, self.spawn)
                    status = download_status(self.store_root)
                raise ModelUnavailable(
                    status or "built-in embedding model not present; run `hermes memory-buckets fetch-model`"
                )
            try:
                self._model = load(found)
            except (OSError, ValueError, KeyError, struct.error, ModelUnavailable) as err:
                raise ModelUnavailable(f"built-in embedding model at {found} is unusable: {err}") from None
            return self._model

    def down_reason(self) -> Optional[str]:
        try:
            self._load()
            return None
        except ModelUnavailable as err:
            return str(err)

    def embed(self, texts: List[str]) -> List[array]:
        try:
            model = self._load()
            return model.encode_batch(texts, query=False)
        except ModelUnavailable as err:
            raise EmbeddingError(str(err)) from None

    def embed_documents(self, texts: List[str]) -> List[array]:
        return self.embed(texts)

    def embed_query(self, text: str) -> array:
        try:
            model = self._load()
            return model.encode(text, query=True)
        except ModelUnavailable as err:
            raise EmbeddingError(str(err)) from None


StaticEmbedder = OnnxEmbedder


def make_embedder(cfg, store_root: Path, *, spawn: Optional[Callable[..., threading.Thread]] = None):
    """The built-in model, or None when ``embeddings.backend`` is ``off``."""
    backend = (cfg.embeddings.backend or "local").strip().lower()
    if backend == "off" or backend in ("0", "false", "no"):
        # `hermes config set ... embeddings.backend off` writes a YAML boolean (Hermes coerces
        # the word first, and a hand-edited unquoted `off` parses as one too), which config.py
        # stringifies to "False". The user asked for keyword search, not a silent fallback to
        # the built-in model, so every spelling of false means off.
        return None
    if backend not in ("local", "auto"):  # "auto" was the default while an HTTP backend existed
        logger.warning(
            "memory-buckets: embeddings.backend %r isn't supported (local or off); using the built-in model",
            backend,
        )
    emb = cfg.embeddings
    return OnnxEmbedder(store_root, model_dir=emb.local_model_dir, auto_download=emb.auto_download, spawn=spawn)


def min_similarity(cfg, embedder) -> float:
    """Configured prefetch threshold, or the model's measured default when unset (< 0).

    With embeddings off there is no model to take a default from, and ``index.hints``
    returns [] before any similarity is compared, so the value is never used: return
    a threshold nothing can meet rather than crash on the missing embedder.
    """
    configured = cfg.prefetch_min_similarity
    if configured is not None and configured >= 0:
        return configured
    if embedder is None:
        return float("inf")
    return embedder.default_min_similarity
