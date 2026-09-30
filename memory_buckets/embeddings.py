"""Embeddings (ADR-0010, ADR-0015; MEM-1 Plan §4): the built-in ``potion-retrieval-32M``
static model (``static_model.py``), run in-process. No server.

``StaticEmbedder`` exposes ``identity``, ``describe()``, ``down_reason()``,
``embed_documents()``, ``embed_query()`` and ``default_min_similarity``, which is
all the index and the provider use. Vectors are unit-normalised float32, so
cosine similarity is a dot product.
"""

from __future__ import annotations

import logging
import struct
import threading
from array import array
from pathlib import Path
from typing import Callable, List, Optional

from . import static_model

logger = logging.getLogger("memory_buckets")


class EmbeddingError(Exception):
    pass


def dot(a: array, b: array) -> float:
    return sum(map(float.__mul__, a, b)) if len(a) == len(b) else 0.0


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
            except (OSError, ValueError, KeyError, struct.error, static_model.ModelUnavailable) as err:
                # struct.error: a truncated/corrupt model.safetensors; the model is down, not a crash.
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


def make_embedder(cfg, store_root: Path, *, spawn: Optional[Callable[..., threading.Thread]] = None):
    """The built-in model, or None when ``embeddings.backend`` is ``off``."""
    backend = (cfg.embeddings.backend or "local").strip().lower()
    if backend == "off":
        return None
    if backend not in ("local", "auto"):  # "auto" was the default while an HTTP backend existed
        logger.warning("memory-buckets: embeddings.backend %r isn't supported (local or off); using the built-in model",
                       backend)
    emb = cfg.embeddings
    return StaticEmbedder(store_root, model_dir=emb.local_model_dir, auto_download=emb.auto_download, spawn=spawn)


def min_similarity(cfg, embedder) -> float:
    """Configured prefetch threshold, or the model's measured default when unset (< 0)."""
    configured = cfg.prefetch_min_similarity
    if configured is not None and configured >= 0:
        return configured
    return embedder.default_min_similarity
