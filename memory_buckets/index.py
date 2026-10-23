"""Search index at ``<store>/.index/memory.sqlite``: a cache, never the source of truth.

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
import weakref
from array import array
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Tuple

from . import frontmatter
from .embeddings import EmbeddingError, StaticEmbedder, dot
from .store import GLOBAL, Store, create_private_file, scope_of, stem, version

logger = logging.getLogger("memory_buckets")

SCHEMA_VERSION = "4"
CHUNK_CHARS = 300
RRF_K = 60
CANDIDATES = 200
SEARCH_MIN_SIMILARITY = 0.20

_GENERIC_STEMS = frozenset({"profile", "preferences", "inbox"})

_STOPWORDS = frozenset({
    "a", "about", "also", "an", "and", "are", "as", "at", "be", "by", "can",
    "could", "did", "do", "does", "for", "from", "get", "got", "had", "has",
    "have", "help", "helping", "how", "i", "in", "into", "is", "it", "its",
    "just", "know", "like", "make", "may", "me", "might", "more", "most",
    "much", "must", "my", "need", "now", "of", "on", "only", "or", "other",
    "our", "out", "over", "read", "reading", "should", "some", "such", "tell",
    "than", "that", "the", "their", "them", "then", "there", "these", "they",
    "thin", "think", "this", "those", "through", "to", "too", "under", "up",
    "use", "using", "very", "want", "was", "we", "what", "when", "where",
    "which", "who", "whom", "why", "will", "with", "work", "working", "would",
    "you", "your",
})

_DESC_PREFIX_RE = re.compile(
    r"^(?:read\s+(?:when\s+(?:working\s+on|helping\s+(?:\S+\s+)?with|touching)|for)\s+)",
    re.IGNORECASE,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, version TEXT, mtime_ns INTEGER, size INTEGER,
                                 description TEXT, aliases TEXT);
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, ord INTEGER,
                                  heading TEXT, text TEXT, vec BLOB);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, heading, tokenize='porter unicode61');
"""

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


# -- chunking ------------------------------------------------------------------

def _blocks(body: str) -> List[Tuple[str, str]]:
    """(heading, block) pairs: a block is one bullet (with its indented
    continuation lines) or one paragraph."""
    heading = ""
    stack: List[Tuple[int, str]] = []
    out: List[Tuple[str, str]] = []
    current: List[str] = []
    fence: Optional[str] = None

    def flush():
        if current:
            out.append((heading, "\n".join(current).strip()))
            current.clear()

    for line in body.split("\n"):
        fm = _FENCE_RE.match(line)
        if fence is not None:
            current.append(line)
            if fm and fm.group(1).startswith(fence):
                fence = None
            continue
        if fm:
            fence = fm.group(1)
            current.append(line)
            continue
        m = _HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2)))
            heading = " > ".join(h for _, h in stack)
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


def _fts_identity(rel: str, description: str, aliases: List[str]) -> str:
    bucket = scope_of(rel)
    clean_desc = _DESC_PREFIX_RE.sub("", description).strip()
    return "\n".join(filter(None, [
        bucket if bucket != GLOBAL else "",
        stem(rel),
        clean_desc,
        ", ".join(aliases),
    ]))


def _embed_text(rel: str, ord_: int, heading: str, text: str, description: str, aliases: List[str]) -> str:
    if ord_ == 0:
        return _fts_identity(rel, description, aliases)
    if heading:
        return f"{heading}\n{text}"
    if stem(rel) not in _GENERIC_STEMS:
        return f"{stem(rel).replace('-', ' ')}\n{text}"
    return text


def _active_tokens(query: str) -> List[str]:
    """Query tokens worth matching on: head and tail sampling first, so a key
    term at the end of a long paste survives, then stopwords dropped, bare
    single-digit numbers dropped from anything multi-word, order-preserving
    dedupe."""
    all_tokens = re.findall(r"\w+", query.lower())
    # Head/tail sampling mirrors the embedder's own query pre-truncation in
    # static_model.py, so the FTS leg sees the same beginning and end of a
    # long message the vector leg does. Order-preserving dedupe collapses the
    # head/tail overlap; for short queries the sampled list is the whole
    # query, so behaviour is unchanged.
    tokens = list(dict.fromkeys(all_tokens[:32] + all_tokens[-32:]))
    if not tokens:
        return []
    content_tokens = [t for t in tokens if t not in _STOPWORDS]
    active = [t for t in content_tokens if len(t) > 1 or not t.isdigit()] or content_tokens or tokens
    return list(dict.fromkeys(active))


# -- the index -----------------------------------------------------------------

class IndexInUse(sqlite3.OperationalError):
    """``rebuild()`` couldn't delete the cache because another process has it open.
    Only Windows refuses to delete an open file. A ``sqlite3.Error``, so callers that
    already handle a broken index report this one the same way."""


class Index:
    def __init__(self, store: Store, embedder: Optional[StaticEmbedder] = None) -> None:
        self.store = store
        self.embedder = embedder
        self.path = store.root / ".index" / "memory.sqlite"
        self._local = threading.local()
        self._conns: List[Tuple[weakref.ReferenceType[threading.Thread], sqlite3.Connection]] = []
        self._conns_lock = threading.Lock()
        self._schema_ready: bool = False
        self._vec_cache: Tuple[Optional[str], List[Tuple[int, str, array]]] = (None, [])
        self._vec_lock = threading.Lock()

    # -- connections -----------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            self.store.ensure_dir(self.path.parent)
            # Pre-created so the database, and the -wal and -shm files SQLite makes from its
            # mode, are private from the start. The index holds every memory's text.
            create_private_file(self.path)
            con = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
            try:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("PRAGMA synchronous=NORMAL")
                if not self._schema_ready:
                    self._ensure_schema(con)
                    self._schema_ready = True
            except BaseException:
                # A corrupt file fails here. close() can't reach a connection it never saw,
                # and on Windows an open one stops rebuild() from deleting the file.
                con.close()
                raise
            self._local.con = con
            with self._conns_lock:
                alive: List[Tuple[weakref.ReferenceType[threading.Thread], sqlite3.Connection]] = []
                for ref, old in self._conns:
                    thread = ref()
                    if thread is not None and thread.is_alive():
                        alive.append((ref, old))
                    else:
                        try:
                            old.close()
                        except sqlite3.Error:
                            pass
                alive.append((weakref.ref(threading.current_thread()), con))
                self._conns = alive
        return con

    def _ensure_schema(self, con: sqlite3.Connection) -> None:
        has_meta = con.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
        if has_meta:
            current = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if current and current[0] == SCHEMA_VERSION:
                if self.embedder is None:
                    return
                row = con.execute("SELECT value FROM meta WHERE key='embed_identity'").fetchone()
                if row is not None and row[0] == self.embedder.identity:
                    return
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
            for _, con in self._conns:
                try:
                    con.close()
                except sqlite3.Error:
                    pass
            self._conns.clear()
            self._schema_ready = False
        self._local = threading.local()

    def rebuild(self) -> None:
        """Delete the cache; the next call rebuilds it from the files."""
        self.close()
        # WAL and shm first: if another process holds the database, they're held too, so
        # nothing is deleted. Deleting the database alone would leave a stale WAL behind.
        for suffix in ("-wal", "-shm", ""):
            try:
                os.unlink(f"{self.path}{suffix}")
            except FileNotFoundError:
                pass
            except PermissionError as err:
                raise IndexInUse(f"the search index ({self.path}) is open in another process, probably a gateway "
                                 "or another Hermes session; close it and try again") from err
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
        raw_desc = fm.get("description")
        description = raw_desc if isinstance(raw_desc, str) else ""
        raw_aliases = fm.get("aliases")
        aliases = [a for a in raw_aliases if isinstance(a, str)] if isinstance(raw_aliases, list) else []
        self._drop(con, rel)
        for ord_, (heading, chunk_text) in enumerate(chunk(rel, description, aliases, body)):
            cur = con.execute("INSERT INTO chunks(path, ord, heading, text, vec) VALUES(?, ?, ?, ?, NULL)",
                              (rel, ord_, heading, chunk_text))
            fts_text = _fts_identity(rel, description, aliases) if ord_ == 0 else chunk_text
            con.execute("INSERT INTO chunks_fts(rowid, text, heading) VALUES(?, ?, ?)",
                        (cur.lastrowid, fts_text, heading))
        con.execute("INSERT INTO files VALUES(?, ?, ?, ?, ?, ?)",
                    (rel, version(data), mtime, size, description, ", ".join(aliases)))

    # -- embeddings ------------------------------------------------------------

    def backlog(self) -> int:
        return self._conn().execute("SELECT count(*) FROM chunks WHERE vec IS NULL").fetchone()[0]

    def embed_backlog(self, max_batches: Optional[int] = 1) -> int:
        """Embed up to ``max_batches`` batches of un-embedded chunks (None = all).
        Returns how many got vectors. Never raises when the model isn't available."""
        if self.embedder is None or self.embedder.down_reason():
            return 0
        con, done, batches = self._conn(), 0, 0
        while max_batches is None or batches < max_batches:
            rows = con.execute(
                "SELECT c.id, c.path, c.ord, c.heading, c.text, "
                "COALESCE(f.description, ''), COALESCE(f.aliases, '') "
                "FROM chunks c LEFT JOIN files f ON f.path = c.path "
                "WHERE c.vec IS NULL ORDER BY c.id LIMIT ?",
                (self.embedder.batch,),
            ).fetchall()
            if not rows:
                break
            try:
                vectors = self.embedder.embed_documents([
                    _embed_text(path, ord_, heading, text, desc, aliases.split(", ") if aliases else [])
                    for _, path, ord_, heading, text, desc, aliases in rows
                ])
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
                                [(vec.tobytes(), cid, text) for (cid, _, _, _, text, _, _), vec in zip(rows, vectors)])
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
        active = _active_tokens(query)
        if not active:
            return []
        match = " OR ".join(f'"{t}"' for t in active)
        out: List[int] = []
        cursor = self._conn().execute(
            "SELECT chunks.id, chunks.path FROM chunks_fts JOIN chunks ON chunks.id = chunks_fts.rowid "
            "WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts, 1.0, 2.0)", (match,))
        for cid, path in cursor:
            if read_ok(path):
                out.append(cid)
                if len(out) >= CANDIDATES:
                    break
        cursor.close()
        return out

    def _cosine(self, qvec: array, read_ok: Callable[[str], bool],
                min_similarity: float = SEARCH_MIN_SIMILARITY) -> List[Tuple[int, str, float]]:
        if not any(qvec):  # zero vector: unknown tokens, punctuation or whitespace only
            return []
        scored = [(cid, path, sim) for cid, path, vec in self._vectors()
                  if read_ok(path) and (sim := dot(qvec, vec)) >= min_similarity]
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

        chunk_scores: Dict[int, float] = defaultdict(float)
        for rank, cid in enumerate(fts_ids):
            chunk_scores[cid] += 1.0 / (RRF_K + rank + 1)
        for rank, (cid, _, _) in enumerate(vec_hits):
            chunk_scores[cid] += 1.0 / (RRF_K + rank + 1)
        if not chunk_scores:
            return {"mode": mode, "note": note, "results": []}

        con = self._conn()
        ids = list(chunk_scores)
        info = {}
        for start in range(0, len(ids), 500):
            part = ids[start:start + 500]
            marks = ",".join("?" * len(part))
            for cid, path, ord_, heading, text in con.execute(
                    f"SELECT id, path, ord, heading, text FROM chunks WHERE id IN ({marks})", part):
                info[cid] = (path, ord_, heading, text)

        active = _active_tokens(query)
        # File-rank deduplication: RRF ranks count distinct files, not raw chunk
        # positions, so a file split into many chunks cannot push another file
        # several ranks down. Each path takes the rank of its first appearance.
        best_fts_rrf: Dict[str, float] = {}
        fts_matched: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
        fts_file_rank = 0
        for cid in fts_ids:
            if cid not in info:
                continue
            path, _, heading, text = info[cid]
            if path not in best_fts_rrf:
                best_fts_rrf[path] = 1.0 / (RRF_K + fts_file_rank + 1)
                fts_file_rank += 1
            fts_matched[path].append((heading, text, path))
        best_vec_rrf: Dict[str, float] = {}
        best_sim: Dict[str, float] = defaultdict(float)
        vec_file_rank = 0
        for cid, path, sim in vec_hits:
            if cid not in info:
                continue
            if sim > best_sim[path]:
                best_sim[path] = sim
            if path not in best_vec_rrf:
                best_vec_rrf[path] = 1.0 / (RRF_K + vec_file_rank + 1)
                vec_file_rank += 1
        # Lexical coverage: a file whose matched chunks cover more of the query's
        # active tokens keeps more of its FTS score.
        descriptions = self.descriptions(list(fts_matched))
        for path, matched_chunks in fts_matched.items():
            if not active:
                coverage = 1.0
            else:
                haystack = " ".join(f"{h} {t} {p}" for h, t, p in matched_chunks)
                htokens = set(re.findall(r"\w+", f"{haystack} {descriptions.get(path, '')}".lower()))
                matched = sum(
                    1 for token in active
                    if token in htokens or (len(token) >= 4 and any(ht.startswith(token[:4]) for ht in htokens))
                )
                coverage = max(1, matched) / len(active)
            best_fts_rrf[path] *= 0.5 + 0.5 * coverage
        # Similarity calibration: a strong semantic match keeps more of its vector
        # score than a match scraping the floor at an adjacent rank.
        for path in best_vec_rrf:
            best_vec_rrf[path] *= 0.75 + 0.5 * best_sim[path]
        best_chunk_for_path: Dict[str, Tuple[float, int]] = {}
        best_body_for_path: Dict[str, Tuple[float, int]] = {}
        for cid, cscore in chunk_scores.items():
            if cid not in info:
                continue
            path, ord_, _, _ = info[cid]
            if path not in best_chunk_for_path or cscore > best_chunk_for_path[path][0]:
                best_chunk_for_path[path] = (cscore, cid)
            if ord_ > 0 and (path not in best_body_for_path or cscore > best_body_for_path[path][0]):
                best_body_for_path[path] = (cscore, cid)
        best_for_path: Dict[str, Tuple[float, int]] = {}
        for path, (_, cid) in best_chunk_for_path.items():
            file_score = (best_fts_rrf.get(path, 0.0) + best_vec_rrf.get(path, 0.0)) * (
                boost if project and path.startswith(f"{project}/") else 1.0
            )
            best_for_path[path] = (file_score, cid)
        ranked = sorted(best_for_path.items(), key=lambda kv: kv[1][0], reverse=True)[:limit]
        descriptions = self.descriptions([p for p, _ in ranked])
        results = []
        for path, (score, cid) in ranked:
            _, ord_, heading, text = info[cid]
            if ord_ == 0:
                # Chunk 0 is the identity chunk (path and description): prefer a body
                # chunk's heading and text so the snippet is not a repeat of the metadata.
                body = best_body_for_path.get(path)
                if body is not None:
                    _, heading, text = info[body[1]][1:]
                else:
                    row = con.execute("SELECT heading, text FROM chunks WHERE path = ? AND ord > 0 "
                                      "ORDER BY ord LIMIT 1", (path,)).fetchone()
                    if row is not None:
                        heading, text = row
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
              min_similarity: float = 0.45, max_hints: int = 4, project: Optional[str] = None,
              boost: float = 1.3, embed_backlog: bool = True) -> List[Tuple[str, str, float]]:
        """(path, description, similarity) for files worth reading, by vectors
        only. Empty when embeddings are off or down. Embeds one batch of
        backlog by default. ``project`` files get ``boost``; a file whose exact
        stem or alias is named in the query is rescued above the threshold."""
        if self.embedder is None or self.embedder.down_reason():
            return []
        self.reconcile()
        if embed_backlog:
            self.embed_backlog(max_batches=1)
        qvec, _ = self._query_vector(query)
        if qvec is None:
            return []
        exclude = exclude or set()
        best: Dict[str, float] = {}
        for _, path, sim in self._cosine(qvec, lambda p: read_ok(p) and p not in exclude, min_similarity):
            if sim >= min_similarity:
                project_boost = boost if (project and path.startswith(f"{project}/")) else 1.0
                score = sim * project_boost
                if score > best.get(path, -1.0):
                    best[path] = score
        # Exact stem or alias match: a topic named in a noisy turn should surface
        # even when the weak embedding misses it.
        lowered = query.lower()
        for path, _description, aliases in self._conn().execute("SELECT path, description, aliases FROM files"):
            if path in exclude or not read_ok(path):
                continue
            stem_ = stem(path)
            terms: List[str] = []
            if stem_ not in _GENERIC_STEMS and stem_ not in _STOPWORDS and len(stem_) >= 3:
                terms.append(stem_.replace("-", " "))
                terms.append(stem_)
            terms.extend(
                a for a in (t.strip() for t in (aliases or "").split(","))
                if len(a) >= 3 and a.lower() not in _STOPWORDS
            )
            if not any(re.search(r"(?<!\w)" + re.escape(t.lower()) + r"(?!\w)", lowered) for t in terms):
                continue
            project_boost = boost if (project and path.startswith(f"{project}/")) else 1.0
            best[path] = max(best.get(path, 0.0), min_similarity) * project_boost + 0.15
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

    def mtimes(self) -> Dict[str, float]:
        """Path -> mtime in seconds for every indexed file. Already in the index, so no ``stat``."""
        return {path: ns / 1e9 for path, ns in self._conn().execute("SELECT path, mtime_ns FROM files")}

    def stats(self) -> Dict[str, object]:
        con = self._conn()
        files = con.execute("SELECT count(*) FROM files").fetchone()[0]
        chunks, embedded = con.execute("SELECT count(*), count(vec) FROM chunks").fetchone()
        dims = con.execute("SELECT value FROM meta WHERE key='embed_dims'").fetchone()
        return {"files": files, "chunks": chunks, "embedded": embedded, "backlog": chunks - embedded,
                "embed_dims": int(dims[0]) if dims else None,
                "embed_identity": self.embedder.identity if self.embedder else None}
