"""Search index at ``<store>/.index/memory.sqlite``: a cache, never the source of truth (ADR-0010; MEM-1 Plan §4).

Every search and prefetch reconciles first: one ``stat`` per file, and a
re-read only when size or mtime changed. So hand edits from Obsidian or the
explorer are picked up, and deleting ``.index/`` loses nothing. Changed files
are re-chunked straight into FTS. Their vectors are NULLed into the embedding
backlog, which ``embed_backlog`` works through off the hot path.

Search is hybrid: FTS5 bm25 and cosine similarity, each ranked within the
caller's read scope, fused with reciprocal rank fusion (k = 60), grouped by
file, with the session's project boosted.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from array import array
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Tuple

from . import frontmatter
from .embeddings import EmbeddingClient, EmbeddingError, dot
from .store import Store, version

logger = logging.getLogger("memory_buckets")

SCHEMA_VERSION = "1"
CHUNK_CHARS = 700
RRF_K = 60
CANDIDATES = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, version TEXT, mtime_ns INTEGER, size INTEGER,
                                 description TEXT, aliases TEXT);
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, ord INTEGER,
                                  heading TEXT, text TEXT, vec BLOB);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, heading, tokenize='porter unicode61');
"""

_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


# -- chunking ------------------------------------------------------------------

def _blocks(body: str) -> List[Tuple[str, str]]:
    """(heading, block) pairs: a block is one bullet (with its indented
    continuation lines) or one paragraph."""
    heading, out, current = "", [], []

    def flush():
        if current:
            out.append((heading, "\n".join(current).strip()))
            current.clear()

    for line in body.split("\n"):
        m = _HEADING_RE.match(line)
        if m:
            flush()
            heading = m.group(1)
        elif not line.strip():
            flush()
        elif _BULLET_RE.match(line) and not line[:1].isspace():
            flush()
            current.append(line)
        else:
            current.append(line)
    flush()
    return [(h, b) for h, b in out if b]


def _split_long(text: str, limit: int) -> List[str]:
    if len(text) <= limit:
        return [text]
    pieces, buf = [], ""
    for sentence in _SENTENCE_RE.split(text):
        while len(sentence) > limit:  # no sentence break: hard split
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        if buf and len(buf) + 1 + len(sentence) > limit:
            pieces.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}" if buf else sentence
    if buf:
        pieces.append(buf)
    return pieces


def chunk(path: str, description: str, aliases: List[str], body: str, limit: int = CHUNK_CHARS) -> List[Tuple[str, str]]:
    """(heading, text) chunks. Chunk 0 is the file's identity (path, description,
    aliases); then heading-aware body chunks packed from whole bullets or
    paragraphs up to ``limit`` characters."""
    identity = "\n".join(filter(None, [path, description, ("aliases: " + ", ".join(aliases)) if aliases else ""]))
    chunks = [("", identity)]
    heading, buf = None, ""
    for block_heading, block in _blocks(body):
        for piece in _split_long(block, limit):
            if buf and (block_heading != heading or len(buf) + 1 + len(piece) > limit):
                chunks.append((heading or "", buf))
                buf = ""
            heading = block_heading
            buf = f"{buf}\n{piece}" if buf else piece
    if buf:
        chunks.append((heading or "", buf))
    return chunks


# -- the index -----------------------------------------------------------------

class Index:
    def __init__(self, store: Store, embedder: Optional[EmbeddingClient] = None) -> None:
        self.store = store
        self.embedder = embedder
        self.path = store.root / ".index" / "memory.sqlite"
        self._local = threading.local()
        self._conns: List[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._vec_cache: Tuple[Optional[str], List[Tuple[int, str, array]]] = (None, [])
        self._vec_lock = threading.Lock()

    # -- connections -----------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            self._ensure_schema(con)
            self._local.con = con
            with self._conns_lock:
                self._conns.append(con)
        return con

    def _ensure_schema(self, con: sqlite3.Connection) -> None:
        con.execute("BEGIN IMMEDIATE")
        try:
            has_meta = con.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
            current = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() if has_meta else None
            if current and current[0] != SCHEMA_VERSION:
                for table in ("meta", "files", "chunks", "chunks_fts"):
                    con.execute(f"DROP TABLE IF EXISTS {table}")
            for statement in filter(str.strip, _SCHEMA.split(";")):
                con.execute(statement)
            con.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (SCHEMA_VERSION,))
            if self.embedder is not None:
                row = con.execute("SELECT value FROM meta WHERE key='embed_identity'").fetchone()
                if row is None or row[0] != self.embedder.identity:
                    con.execute("UPDATE chunks SET vec = NULL")
                    con.execute("INSERT OR REPLACE INTO meta VALUES('embed_identity', ?)", (self.embedder.identity,))
                    con.execute("DELETE FROM meta WHERE key='embed_dims'")
                    self._bump(con)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise

    @staticmethod
    def _bump(con: sqlite3.Connection) -> None:
        con.execute("INSERT INTO meta VALUES('generation', '1') "
                    "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")

    def close(self) -> None:
        with self._conns_lock:
            for con in self._conns:
                try:
                    con.close()
                except sqlite3.Error:
                    pass
            self._conns.clear()
        self._local = threading.local()

    def rebuild(self) -> None:
        """Delete the cache; the next call rebuilds it from the files."""
        self.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(f"{self.path}{suffix}")
            except FileNotFoundError:
                pass
        self._vec_cache = (None, [])

    # -- reconcile -------------------------------------------------------------

    def reconcile(self) -> Dict[str, int]:
        """Bring the index in line with the files. Cheap when nothing changed."""
        con = self._conn()
        on_disk: Dict[str, Tuple[int, int]] = {}
        for rel in self.store.iter_paths():
            try:
                st = os.stat(self.store.memories / rel)
            except FileNotFoundError:
                continue
            on_disk[rel] = (st.st_mtime_ns, st.st_size)
        indexed = {row[0]: row[1:] for row in con.execute("SELECT path, version, mtime_ns, size FROM files")}
        stale = [p for p in on_disk if indexed.get(p, (None, None, None))[1:] != on_disk[p]]
        gone = [p for p in indexed if p not in on_disk]
        counts = {"changed": 0, "removed": 0}
        if not stale and not gone:
            return counts
        con.execute("BEGIN IMMEDIATE")
        try:
            for rel in gone:
                self._drop(con, rel)
                counts["removed"] += 1
            for rel in stale:
                try:
                    data = (self.store.memories / rel).read_bytes()
                except FileNotFoundError:
                    self._drop(con, rel)
                    continue
                mtime, size = on_disk[rel]
                ver = version(data)
                row = con.execute("SELECT version FROM files WHERE path = ?", (rel,)).fetchone()
                if row and row[0] == ver:
                    con.execute("UPDATE files SET mtime_ns = ?, size = ? WHERE path = ?", (mtime, size, rel))
                    continue
                self._index_file(con, rel, data, mtime, size)
                counts["changed"] += 1
            if counts["changed"] or counts["removed"]:
                self._bump(con)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        return counts

    def _drop(self, con: sqlite3.Connection, rel: str) -> None:
        ids = [r[0] for r in con.execute("SELECT id FROM chunks WHERE path = ?", (rel,))]
        con.executemany("DELETE FROM chunks_fts WHERE rowid = ?", [(i,) for i in ids])
        con.execute("DELETE FROM chunks WHERE path = ?", (rel,))
        con.execute("DELETE FROM files WHERE path = ?", (rel,))

    def _index_file(self, con: sqlite3.Connection, rel: str, data: bytes, mtime: int, size: int) -> None:
        text = data.decode("utf-8", errors="replace")
        try:
            fm, body = frontmatter.parse(text)
        except frontmatter.FrontmatterError:
            fm, body = frontmatter.Frontmatter(), text
        description = fm.get("description") if isinstance(fm.get("description"), str) else ""
        aliases = [a for a in (fm.get("aliases") or []) if isinstance(a, str)] if isinstance(fm.get("aliases"), list) else []
        self._drop(con, rel)
        for ord_, (heading, chunk_text) in enumerate(chunk(rel, description, aliases, body)):
            cur = con.execute("INSERT INTO chunks(path, ord, heading, text, vec) VALUES(?, ?, ?, ?, NULL)",
                              (rel, ord_, heading, chunk_text))
            con.execute("INSERT INTO chunks_fts(rowid, text, heading) VALUES(?, ?, ?)", (cur.lastrowid, chunk_text, heading))
        con.execute("INSERT INTO files VALUES(?, ?, ?, ?, ?, ?)",
                    (rel, version(data), mtime, size, description, ", ".join(aliases)))

    # -- embeddings ------------------------------------------------------------

    def backlog(self) -> int:
        return self._conn().execute("SELECT count(*) FROM chunks WHERE vec IS NULL").fetchone()[0]

    def embed_backlog(self, max_batches: Optional[int] = 1) -> int:
        """Embed up to ``max_batches`` batches of un-embedded chunks (None = all).
        Returns how many got vectors. Never raises for endpoint trouble."""
        if self.embedder is None or self.embedder.down_reason():
            return 0
        con, done, batches = self._conn(), 0, 0
        while max_batches is None or batches < max_batches:
            rows = con.execute("SELECT id, text FROM chunks WHERE vec IS NULL ORDER BY id LIMIT ?",
                               (self.embedder.batch,)).fetchall()
            if not rows:
                break
            try:
                vectors = self.embedder.embed_documents([text for _, text in rows])
            except EmbeddingError:
                break
            con.execute("BEGIN IMMEDIATE")
            try:
                dims = con.execute("SELECT value FROM meta WHERE key='embed_dims'").fetchone()
                if dims and int(dims[0]) != len(vectors[0]):  # model changed under us: start over
                    con.execute("UPDATE chunks SET vec = NULL")
                con.execute("INSERT OR REPLACE INTO meta VALUES('embed_dims', ?)", (str(len(vectors[0])),))
                # Only fill chunks whose text is unchanged; ids are never reused (AUTOINCREMENT).
                con.executemany("UPDATE chunks SET vec = ? WHERE id = ? AND text = ?",
                                [(vec.tobytes(), cid, text) for (cid, text), vec in zip(rows, vectors)])
                self._bump(con)
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
            done += len(rows)
            batches += 1
        return done

    def _vectors(self) -> List[Tuple[int, str, array]]:
        con = self._conn()
        row = con.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
        generation = row[0] if row else "0"
        with self._vec_lock:
            if self._vec_cache[0] == generation:
                return self._vec_cache[1]
        vectors = []
        for cid, path, blob in con.execute("SELECT id, path, vec FROM chunks WHERE vec IS NOT NULL"):
            vec = array("f")
            vec.frombytes(blob)
            vectors.append((cid, path, vec))
        with self._vec_lock:
            self._vec_cache = (generation, vectors)
        return vectors

    def _query_vector(self, query: str) -> Tuple[Optional[array], Optional[str]]:
        """(vector, None) or (None, reason it's unavailable)."""
        if self.embedder is None:
            return None, None
        reason = self.embedder.down_reason()
        if reason:
            return None, reason
        try:
            return self.embedder.embed_query(query), None
        except EmbeddingError as err:
            return None, str(err)

    # -- search ----------------------------------------------------------------

    def _fts(self, query: str, read_ok: Callable[[str], bool]) -> List[int]:
        tokens = re.findall(r"\w+", query.lower())[:32]
        if not tokens:
            return []
        match = " OR ".join(f'"{t}"' for t in tokens)
        rows = self._conn().execute(
            "SELECT chunks.id, chunks.path FROM chunks_fts JOIN chunks ON chunks.id = chunks_fts.rowid "
            "WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?", (match, CANDIDATES * 2)).fetchall()
        return [cid for cid, path in rows if read_ok(path)][:CANDIDATES]

    def _cosine(self, qvec: array, read_ok: Callable[[str], bool]) -> List[Tuple[int, str, float]]:
        scored = [(cid, path, dot(qvec, vec)) for cid, path, vec in self._vectors() if read_ok(path)]
        scored.sort(key=lambda t: t[2], reverse=True)
        return scored[:CANDIDATES]

    def search(self, query: str, read_ok: Callable[[str], bool], *, project: Optional[str] = None,
               limit: int = 8, boost: float = 1.3, embed_backlog: bool = True) -> Dict[str, object]:
        self.reconcile()
        if embed_backlog:
            self.embed_backlog(max_batches=1)
        note = None
        fts_ids = self._fts(query, read_ok)
        qvec, down = self._query_vector(query)
        vec_hits = self._cosine(qvec, read_ok) if qvec is not None else []
        mode = "hybrid" if qvec is not None else "fts"
        if down:
            note = f"embeddings unavailable ({down}); keyword results only"
        elif mode == "hybrid":
            pending = self.backlog()
            if pending:
                note = f"{pending} chunk(s) not embedded yet; those match by keyword only"

        scores: Dict[int, float] = defaultdict(float)
        for rank, cid in enumerate(fts_ids):
            scores[cid] += 1.0 / (RRF_K + rank + 1)
        for rank, (cid, _, _) in enumerate(vec_hits):
            scores[cid] += 1.0 / (RRF_K + rank + 1)
        if not scores:
            return {"mode": mode, "note": note, "results": []}

        con = self._conn()
        ids = list(scores)
        info = {}
        for start in range(0, len(ids), 500):
            part = ids[start:start + 500]
            marks = ",".join("?" * len(part))
            for cid, path, heading, text in con.execute(
                    f"SELECT id, path, heading, text FROM chunks WHERE id IN ({marks})", part):
                info[cid] = (path, heading, text)
        best: Dict[str, Tuple[float, int]] = {}
        for cid, score in scores.items():
            if cid not in info:
                continue
            path = info[cid][0]
            if project and path.startswith(f"{project}/"):
                score *= boost
            if path not in best or score > best[path][0]:
                best[path] = (score, cid)
        ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)[:limit]
        descriptions = self.descriptions([p for p, _ in ranked])
        results = []
        for path, (score, cid) in ranked:
            _, heading, text = info[cid]
            snippet = " ".join(text.split())
            results.append({
                "path": path,
                "description": descriptions.get(path, ""),
                "heading": heading,
                "snippet": snippet if len(snippet) <= 240 else snippet[:239] + "…",
                "score": round(score, 5),
            })
        return {"mode": mode, "note": note, "results": results}

    def hints(self, query: str, read_ok: Callable[[str], bool], *, exclude: Optional[set] = None,
              min_similarity: float = 0.45, max_hints: int = 4) -> List[Tuple[str, str, float]]:
        """(path, description, similarity) for files worth reading, by vectors
        only. Empty when embeddings are off or down. Never embeds backlog."""
        if self.embedder is None or self.embedder.down_reason():
            return []
        self.reconcile()
        qvec, _ = self._query_vector(query)
        if qvec is None:
            return []
        exclude = exclude or set()
        best: Dict[str, float] = {}
        for _, path, sim in self._cosine(qvec, lambda p: read_ok(p) and p not in exclude):
            if sim >= min_similarity and sim > best.get(path, -1.0):
                best[path] = sim
        ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:max_hints]
        descriptions = self.descriptions([p for p, _ in ranked])
        return [(p, descriptions.get(p, ""), s) for p, s in ranked]

    # -- metadata ---------------------------------------------------------------

    def descriptions(self, paths: Optional[List[str]] = None) -> Dict[str, str]:
        con = self._conn()
        if paths is None:
            return dict(con.execute("SELECT path, description FROM files"))
        out = {}
        for start in range(0, len(paths), 500):
            part = paths[start:start + 500]
            marks = ",".join("?" * len(part))
            out.update(con.execute(f"SELECT path, description FROM files WHERE path IN ({marks})", part))
        return out

    def files(self) -> List[Tuple[str, str, str, int]]:
        """(path, version, description, size) for every indexed file, sorted."""
        return self._conn().execute("SELECT path, version, description, size FROM files ORDER BY path").fetchall()

    def stats(self) -> Dict[str, object]:
        con = self._conn()
        files = con.execute("SELECT count(*) FROM files").fetchone()[0]
        chunks, embedded = con.execute("SELECT count(*), count(vec) FROM chunks").fetchone()
        dims = con.execute("SELECT value FROM meta WHERE key='embed_dims'").fetchone()
        return {"files": files, "chunks": chunks, "embedded": embedded, "backlog": chunks - embedded,
                "embed_dims": int(dims[0]) if dims else None,
                "embed_identity": self.embedder.identity if self.embedder else None}
