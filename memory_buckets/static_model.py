"""Built-in embeddings: a Model2Vec static model in pure stdlib (ADR-0015).

``potion-retrieval-32M`` (MinishLab, MIT) is a transformer distilled into a
lookup table, so embedding text is:

1. BERT WordPiece tokenisation;
2. the mean of those tokens' rows;
3. L2 normalisation.

There's no neural network at runtime, no dependency and no server. This mirrors
``model2vec`` 0.9.0's ``StaticModel.encode``: no special tokens, ``[UNK]``
dropped, at most 512 tokens (after pre-truncating the text to 512 × the median
token length), and a zero vector when nothing is left.

Weights are memory-mapped (float32 or float16 safetensors), so loading is
instant and every process shares one copy in the page cache.

Where the model comes from, first match wins:

1. ``model/`` next to this file: bundled into wheels by ``hatch_build.py`` and into the
   Nix package by ``nix/model.nix``; gitignored in a checkout.
2. ``<store>/.models/<name>/``: downloaded, pinned and hash-checked. The download
   happens in the background on first use, or up front with
   ``hermes memory-buckets fetch-model``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import mmap
import os
import re
import ssl
import statistics
import struct
import sys
import threading
import unicodedata
import urllib.request
from array import array
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("memory_buckets")

MODEL_NAME = "potion-retrieval-32M"
MODEL_REPO = "minishlab/potion-retrieval-32M"
MODEL_REVISION = "6fc8051fab2a1e0ee76689cf08c853792ac285e7"
MODEL_FILES = {  # name -> sha256 of the upstream file at MODEL_REVISION
    "model.safetensors": "07609e5bd33aad37900b3fd62f4ec96f6daec88ca4d46b9d8b928bfababf6ea0",
    "vocab.txt": "4b3452e69455f96c6cfc1cdb212d3b7b1a3e9d2505ab6f61a50022f61467a6a3",
}
DOWNLOAD_BASE = "https://huggingface.co"
BUNDLED_DIR = Path(__file__).resolve().parent / "model"
MAX_TOKENS = 512
UNK = "[UNK]"
MAX_WORD_CHARS = 100


class ModelUnavailable(Exception):
    pass


# -- tokenizer: BERT WordPiece, as tokenizers' BertNormalizer(clean_text, handle_chinese_chars,
#    lowercase, strip_accents=lowercase) + BertPreTokenizer + WordPiece(prefix "##") ----------------

def _is_control(ch: str) -> bool:
    if ch in "\t\n\r":
        return False
    return unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs")


def _is_chinese(cp: int) -> bool:
    return (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0x20000 <= cp <= 0x2A6DF
            or 0x2A700 <= cp <= 0x2B73F or 0x2B740 <= cp <= 0x2B81F or 0x2B820 <= cp <= 0x2CEAF
            or 0xF900 <= cp <= 0xFAFF or 0x2F800 <= cp <= 0x2FA1F)


def _is_punctuation(ch: str) -> bool:
    cp = ord(ch)
    if 33 <= cp <= 47 or 58 <= cp <= 64 or 91 <= cp <= 96 or 123 <= cp <= 126:
        return True
    return unicodedata.category(ch).startswith("P")


def normalise(text: str) -> str:
    out = []
    for ch in text:
        cp = ord(ch)
        if cp == 0 or cp == 0xFFFD or _is_control(ch):
            continue
        if ch.isspace():
            out.append(" ")
        elif _is_chinese(cp):
            out.append(f" {ch} ")
        else:
            out.append(ch)
    text = unicodedata.normalize("NFD", "".join(out))
    return "".join(c for c in text if unicodedata.category(c) != "Mn").lower()


def pretokenise(text: str) -> List[str]:
    words: List[str] = []
    for chunk in text.split():
        buf = ""
        for ch in chunk:
            if _is_punctuation(ch):
                if buf:
                    words.append(buf)
                    buf = ""
                words.append(ch)
            else:
                buf += ch
        if buf:
            words.append(buf)
    return words


# BERT's special tokens are "added tokens" in tokenizers: matched verbatim in the raw
# text (case-sensitive, before normalisation) and never split.
SPECIAL_TOKENS = ("[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]")
_SPECIAL_RE = re.compile("(" + "|".join(re.escape(t) for t in SPECIAL_TOKENS) + ")")


class WordPiece:
    def __init__(self, vocab: Dict[str, int]) -> None:
        self.vocab = vocab
        self.unk_id = vocab.get(UNK)
        self.special = {t: vocab[t] for t in SPECIAL_TOKENS if t in vocab}

    def word_ids(self, word: str) -> List[int]:
        if len(word) > MAX_WORD_CHARS:
            return [self.unk_id] if self.unk_id is not None else []
        ids, start = [], 0
        while start < len(word):
            end, found = len(word), None
            while start < end:
                piece = word[start:end] if start == 0 else "##" + word[start:end]
                found = self.vocab.get(piece)
                if found is not None:
                    break
                end -= 1
            if found is None:
                return [self.unk_id] if self.unk_id is not None else []
            ids.append(found)
            start = end
        return ids

    def tokens(self, text: str) -> List[str]:
        """Token strings, for comparing with the reference tokenizer."""
        inverse = {i: t for t, i in self.vocab.items()}
        return [inverse[i] for i in self.ids(text)]

    def ids(self, text: str) -> List[int]:
        out: List[int] = []
        for segment in _SPECIAL_RE.split(text) if self.special else [text]:
            if segment in self.special:
                out.append(self.special[segment])
            elif segment:
                out.extend(i for w in pretokenise(normalise(segment)) for i in self.word_ids(w))
        return out


# -- the model ---------------------------------------------------------------------------------

class StaticModel:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        vocab_lines = (self.directory / "vocab.txt").read_text(encoding="utf-8").split("\n")
        if vocab_lines and vocab_lines[-1] == "":
            vocab_lines.pop()
        self.vocab = {token: i for i, token in enumerate(vocab_lines)}
        self.tokenizer = WordPiece(self.vocab)
        self.median_token_length = int(statistics.median(len(t) for t in vocab_lines))
        with open(self.directory / "model.safetensors", "rb") as f:  # mmap keeps its own descriptor
            header_len = struct.unpack("<Q", f.read(8))[0]
            meta = json.loads(f.read(header_len))["embeddings"]
            self._mmap = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        rows, self.dims = meta["shape"]
        fmt = {"F32": "f", "F16": "e"}.get(meta["dtype"])
        if fmt is None:
            raise ModelUnavailable(f"unsupported dtype {meta['dtype']}")
        start, end = meta["data_offsets"]
        data = memoryview(self._mmap)[8 + header_len + start: 8 + header_len + end]
        if sys.byteorder != "little":  # safetensors is little-endian; copy and swap once
            swapped = array("f" if fmt == "f" else "H", bytes(data))
            swapped.byteswap()
            data = memoryview(swapped.tobytes())
        self.weights = data.cast(fmt)
        self.rows = rows
        if len(self.weights) != rows * self.dims or rows != len(vocab_lines):
            raise ModelUnavailable(f"model shape {rows}x{self.dims} doesn't match vocab of {len(vocab_lines)}")

    def encode(self, text: str) -> array:
        text = text[: MAX_TOKENS * self.median_token_length]
        unk = self.tokenizer.unk_id
        ids = [i for i in self.tokenizer.ids(text) if i != unk][:MAX_TOKENS]
        dims, w = self.dims, self.weights
        acc = [0.0] * dims
        for i in ids:
            row = w[i * dims:(i + 1) * dims]
            for j in range(dims):
                acc[j] += row[j]
        vec = array("f", acc)
        norm = math.sqrt(sum(x * x for x in acc))
        if norm > 0:
            for j in range(dims):
                vec[j] /= norm
        return vec


# -- locating, caching and fetching -----------------------------------------------------------------

_cache: Dict[str, StaticModel] = {}
_cache_lock = threading.Lock()
_download_lock = threading.Lock()
_downloading: Dict[str, str] = {}  # destination -> status


def is_complete(directory: Path) -> bool:
    return all((Path(directory) / name).is_file() for name in MODEL_FILES)


def download_dir(store_root: Path) -> Path:
    return Path(store_root) / ".models" / f"{MODEL_NAME}-{MODEL_REVISION[:8]}"


def locate(store_root: Optional[Path], override: str = "") -> Optional[Path]:
    candidates = [Path(override).expanduser()] if override else []
    candidates.append(BUNDLED_DIR)
    if store_root is not None:
        candidates.append(download_dir(store_root))
    return next((c for c in candidates if is_complete(c)), None)


def load(directory: Path) -> StaticModel:
    key = str(Path(directory).resolve())
    with _cache_lock:
        model = _cache.get(key)
        if model is None:
            model = _cache[key] = StaticModel(Path(directory))
        return model


def download_status(store_root: Path) -> Optional[str]:
    with _download_lock:
        return _downloading.get(str(download_dir(store_root)))


# Where distributions keep their CA bundle. A Python built elsewhere (uv's standalone
# builds, for one) can look in a directory this system doesn't have and trust nothing.
CA_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/ca-bundle.pem",
              "/etc/pki/tls/cacert.pem", "/etc/ssl/cert.pem")


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


def fetch(store_root: Path, *, progress: Optional[Callable[[str], None]] = None, timeout: float = 60.0) -> Path:
    """Download the pinned model into ``<store>/.models``, verifying every file's
    sha256. Safe to call from several processes; files land by atomic rename."""
    dest = download_dir(store_root)
    dest.mkdir(parents=True, exist_ok=True)
    for name, sha in MODEL_FILES.items():
        target = dest / name
        if target.is_file():
            continue
        url = f"{DOWNLOAD_BASE}/{MODEL_REPO}/resolve/{MODEL_REVISION}/{name}"
        tmp = dest / f".{name}.part.{os.getpid()}.{threading.get_ident()}"
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
                raise ModelUnavailable(f"{name}: sha256 mismatch (got {digest.hexdigest()[:12]}…, want {sha[:12]}…)")
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink()
    return dest


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
    return (f"{MODEL_NAME}\nby MinishLab: https://huggingface.co/{MODEL_REPO}\n"
            f"revision {MODEL_REVISION}, MIT licence.\n"
            "Vocabulary from BAAI/bge-base-en-v1.5 (MIT). Weights converted from float32 to float16.\n")


def convert_to_f16(src: Path, dst: Path) -> None:
    """Rewrite a float32 safetensors as float16 (for the bundled copy: half the size,
    cosine differences around 2e-5). Stdlib only, so the Nix build and the wheel's
    build hook (hatch_build.py) can run it."""
    with open(src, "rb") as f:
        header_len = struct.unpack("<Q", f.read(8))[0]
        header = json.loads(f.read(header_len))
        meta = header["embeddings"]
        if meta["dtype"] != "F32":
            raise ValueError("expected F32 embeddings")
        start, end = meta["data_offsets"]
        f.seek(8 + header_len + start)
        values = array("f")
        values.frombytes(f.read(end - start))
    if sys.byteorder != "little":
        values.byteswap()
    halves = memoryview(bytearray(len(values) * 2)).cast("e")
    for i, v in enumerate(values):
        halves[i] = v
    payload = halves.cast("B").tobytes()
    new_header = {**{k: v for k, v in header.items() if k != "embeddings"},
                  "embeddings": {**meta, "dtype": "F16", "data_offsets": [0, len(payload)]}}
    blob = json.dumps(new_header, separators=(",", ":")).encode()
    blob += b" " * (-len(blob) % 8)
    with open(dst, "wb") as out:
        out.write(struct.pack("<Q", len(blob)))
        out.write(blob)
        out.write(payload)
