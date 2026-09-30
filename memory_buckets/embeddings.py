"""Embedding backends (ADR-0010, ADR-0015; MEM-1 Plan §4).

- ``StaticEmbedder``: the built-in ``potion-retrieval-32M`` static model
  (``static_model.py``). No server; the default.
- ``EmbeddingClient``: any OpenAI-compatible ``/v1/embeddings`` endpoint over
  urllib. Used when ``embeddings.base_url`` is set.

Both expose ``identity``, ``describe()``, ``down_reason()``, ``embed_documents()``,
``embed_query()`` and ``default_min_similarity``. ``make_embedder`` picks one.

The HTTP client:

Any failure (HTTP error, timeout, malformed reply, wrong dimensions) puts the
endpoint into a 60 s backoff, shared by every client for that endpoint in the
process, since a gateway runs one provider instance per chat. While backed off,
callers fall back to keyword search. Vectors come back unit-normalised float32,
so cosine similarity is a dot product.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request
from array import array
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import static_model

logger = logging.getLogger("memory_buckets")

BACKOFF_S = 60.0

_backoff_lock = threading.Lock()
_backoff: Dict[str, Tuple[float, str]] = {}  # base_url -> (until, reason)


class EmbeddingError(Exception):
    pass


def _normalise(values: Sequence[float]) -> array:
    vec = array("f", values)
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        for i in range(len(vec)):
            vec[i] /= norm
    return vec


def dot(a: array, b: array) -> float:
    return sum(map(float.__mul__, a, b)) if len(a) == len(b) else 0.0


def reset_backoff() -> None:
    """For tests."""
    with _backoff_lock:
        _backoff.clear()


class EmbeddingClient:
    default_min_similarity = 0.45  # a guess for transformer models; tune with `hermes memory-buckets hints`

    def __init__(self, base_url: str, model: str, *, api_key: str = "", timeout_s: float = 2.0, batch: int = 32,
                 query_prefix: str = "", document_prefix: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.batch = max(1, batch)
        # Task prefixes some models expect, e.g. nomic-embed-text's "search_query: " / "search_document: ".
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix

    @property
    def identity(self) -> str:
        """Changes when stored vectors would: kept in the index to trigger a re-embed.
        The query prefix isn't part of it, since queries are embedded fresh each time."""
        return f"{self.base_url}|{self.model}|{self.document_prefix}"

    def describe(self) -> str:
        return f"{self.base_url} model {self.model}"

    def embed_documents(self, texts: List[str]) -> List[array]:
        return self.embed([self.document_prefix + t for t in texts])

    def embed_query(self, text: str) -> array:
        return self.embed([self.query_prefix + text])[0]

    def down_reason(self) -> Optional[str]:
        """Why the endpoint is backed off, or None if it's usable."""
        with _backoff_lock:
            until, reason = _backoff.get(self.base_url, (0.0, ""))
        return reason if time.monotonic() < until else None

    def _fail(self, reason: str) -> EmbeddingError:
        with _backoff_lock:
            already = self.base_url in _backoff and time.monotonic() < _backoff[self.base_url][0]
            _backoff[self.base_url] = (time.monotonic() + BACKOFF_S, reason)
        if not already:
            logger.warning("memory-buckets: embeddings endpoint %s unavailable (%s); keyword search only for %ds",
                           self.base_url, reason, BACKOFF_S)
        return EmbeddingError(reason)

    def embed(self, texts: List[str]) -> List[array]:
        """Embed ``texts`` (in batches). Raises EmbeddingError, and backs off, on any failure."""
        reason = self.down_reason()
        if reason:
            raise EmbeddingError(f"backed off: {reason}")
        out: List[array] = []
        for start in range(0, len(texts), self.batch):
            out.extend(self._embed_batch(texts[start:start + self.batch]))
        dims = {len(v) for v in out}
        if len(dims) > 1:
            raise self._fail(f"inconsistent dimensions {sorted(dims)}")
        return out

    def _embed_batch(self, texts: List[str]) -> List[array]:
        body = json.dumps({"model": self.model, "input": texts}).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(f"{self.base_url}/embeddings", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as err:
            err.close()
            raise self._fail(f"HTTP {err.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise self._fail(type(err).__name__ if not str(err) else str(err)[:120]) from None
        except ValueError:
            raise self._fail("reply isn't JSON") from None
        try:
            data = sorted(payload["data"], key=lambda d: d.get("index", 0))
            vectors = [_normalise(d["embedding"]) for d in data]
        except (KeyError, TypeError, ValueError):
            raise self._fail("malformed reply") from None
        if len(vectors) != len(texts) or any(len(v) == 0 for v in vectors):
            raise self._fail(f"expected {len(texts)} vectors, got {len(vectors)}")
        return vectors


class StaticEmbedder:
    """The built-in model. ``down_reason`` is None once the model is loaded; before
    that it says why (downloading, missing) and, with ``auto_download``, starts the
    background download."""

    default_min_similarity = 0.27  # measured: relevant median 0.37, unrelated p95 0.15
    batch = 256

    def __init__(self, store_root: Path, *, model_dir: str = "", auto_download: bool = True,
                 spawn: Optional[Callable[..., threading.Thread]] = None) -> None:
        self.store_root = Path(store_root)
        self.model_dir = model_dir
        self.auto_download = auto_download
        self.spawn = spawn or (lambda target, name: threading.Thread(target=target, name=name, daemon=True))
        self._model: Optional[static_model.StaticModel] = None
        self._lock = threading.Lock()

    @property
    def identity(self) -> str:
        return f"static:{static_model.MODEL_NAME}@{static_model.MODEL_REVISION[:8]}"

    def describe(self) -> str:
        where = self._model.directory if self._model else "not loaded"
        return f"built-in {static_model.MODEL_NAME} ({where})"

    def _load(self) -> static_model.StaticModel:
        with self._lock:
            if self._model is not None:
                return self._model
            found = static_model.locate(self.store_root, self.model_dir)
            if found is None:
                status = static_model.download_status(self.store_root)
                if status is None and self.auto_download:
                    static_model.fetch_in_background(self.store_root, self.spawn)
                    status = static_model.download_status(self.store_root)
                raise static_model.ModelUnavailable(
                    status or "built-in embedding model not present; run `hermes memory-buckets fetch-model`")
            try:
                self._model = static_model.load(found)
            except (OSError, ValueError, KeyError, static_model.ModelUnavailable) as err:
                raise static_model.ModelUnavailable(f"built-in embedding model at {found} is unusable: {err}") from None
            return self._model

    def down_reason(self) -> Optional[str]:
        try:
            self._load()
            return None
        except static_model.ModelUnavailable as err:
            return str(err)

    def embed(self, texts: List[str]) -> List[array]:
        try:
            model = self._load()
        except static_model.ModelUnavailable as err:
            raise EmbeddingError(str(err)) from None
        return [model.encode(t) for t in texts]

    def embed_documents(self, texts: List[str]) -> List[array]:
        return self.embed(texts)

    def embed_query(self, text: str) -> array:
        return self.embed([text])[0]


def make_embedder(cfg, store_root: Path, *, spawn: Optional[Callable[..., threading.Thread]] = None,
                  timeout_s: Optional[float] = None):
    """The configured backend, or None when embeddings are off.
    ``backend: auto`` means the HTTP endpoint when ``base_url`` is set, else the built-in model."""
    emb = cfg.embeddings
    backend = (emb.backend or "auto").strip().lower()
    if backend == "off":
        return None
    if backend == "http" or (backend == "auto" and emb.base_url):
        if not emb.enabled:
            logger.warning("memory-buckets: embeddings.backend is http but base_url/model aren't set; embeddings off")
            return None
        return EmbeddingClient(emb.base_url, emb.model, api_key=emb.api_key,
                               timeout_s=emb.timeout_s if timeout_s is None else timeout_s, batch=emb.batch,
                               query_prefix=emb.query_prefix, document_prefix=emb.document_prefix)
    if backend not in ("auto", "local"):
        logger.warning("memory-buckets: unknown embeddings.backend %r; using the built-in model", backend)
    return StaticEmbedder(store_root, model_dir=emb.local_model_dir, auto_download=emb.auto_download, spawn=spawn)


def min_similarity(cfg, embedder) -> float:
    """Configured prefetch threshold, or the backend's measured default when unset (< 0)."""
    configured = cfg.prefetch_min_similarity
    if configured is not None and configured >= 0:
        return configured
    return getattr(embedder, "default_min_similarity", 0.45)
