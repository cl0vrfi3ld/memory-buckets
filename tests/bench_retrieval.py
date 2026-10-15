"""Synthetic retrieval and prefetch hint benchmark for memory-buckets.

Run with:
    MEMORY_BUCKETS_MODEL_DIR=<path-to-potion-retrieval-32M> uv run python -m tests.bench_retrieval
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from memory_buckets.embeddings import StaticEmbedder
from memory_buckets.index import Index
from memory_buckets.store import Store

CORPUS: Dict[str, Tuple[str, List[str], str]] = {
    "global/profile.md": (
        "Who the user is (identity, background, circumstances, what they own and use).",
        [],
        (
            "- Name is Sam Carter; lives in Bristol and commutes by folding bicycle.\n"
            "- Works as a backend acoustic engineer at a marine robotics lab.\n"
            "- Studying Mandarin in an evening class (course code MAND204B, textbook HSK2).\n"
            "- Speaks English and Spanish.\n"
            "- Hobbies: sourdough baking, amateur radio (callsign M0XYZ), and botanical illustration.\n"
            "- Keeps a reef aquarium with clownfish and anemones.\n"
            "- Workstation: ThinkPad T14 running Fedora Silverblue with a Focusrite Scarlett audio interface.\n"
            "- Writes Go and SQL at work; learning OCaml on weekends.\n"
            "- Maintains an open-source Modbus telemetry daemon.\n"
            "- Allergic to shellfish.\n"
        ),
    ),
    "global/preferences.md": (
        "How the user wants the assistant to work (tone, formatting, conventions).",
        [],
        (
            "- Prefer concise prose and plain ASCII diagrams over Mermaid charts.\n"
            "- Show SQL query plans before suggesting schema migrations.\n"
            "- Use British English spelling in documentation and commit messages.\n"
            "- Always include property-based test cases when touching parsers.\n"
            "- Run database migrations inside a transaction with lock_timeout set.\n"
            "- Format shell scripts with shfmt and check them with shellcheck.\n"
        ),
    ),
    "global/topics/kelp-sonar.md": (
        "Read when working on kelp-sonar, the hydrophone array calibration tool.",
        ["hydro-cal"],
        (
            "- Command-line acoustic calibration utility written in Go and CFFI.\n"
            "- Computes impulse responses from swept-sine chirp recordings.\n"
            "- Stores calibration curves in HDF5 files with transducer serial numbers.\n"
        ),
    ),
    "global/topics/lantern-press.md": (
        "Read when working on Lantern Press, the static zine typesetter.",
        ["zine-maker"],
        (
            "- Typesets Markdown booklets into imposition-ready PostScript signatures.\n"
            "- Uses HarfBuzz for glyph shaping and a custom hyphenation dictionary.\n"
            "- Supports risograph two-colour spot separation.\n"
        ),
    ),
    "global/topics/tinymesh.md": (
        "Read when working on TinyMesh, the LoRa packet router and simulator.",
        ["lora-sim", "mesh-net"],
        (
            "- Discrete-event network simulator for 868 MHz LoRa mesh topologies.\n"
            "- Models duty-cycle limits, packet collisions, and multi-hop airtime.\n"
            "- Includes a replay harness for captured field telemetry.\n"
        ),
    ),
    "global/areas/mandarin.md": (
        "Read when helping with MAND204B Mandarin coursework, tone drills, or flashcards.",
        ["MAND204A", "HSK2"],
        (
            "- Enrolled in MAND204B Intermediate Mandarin (portal lists section MAND204A).\n"
            "- Practises tone pairs daily and reviews cloze sentences in Anki.\n"
            "- Focuses on listening comprehension using slow news dialogues.\n"
        ),
    ),
    "global/topics/silverblue-audio.md": (
        "Read when low-latency PipeWire jack routing fails on Fedora Silverblue.",
        ["scarlett-routing", "pipewire-jack"],
        (
            "- Requires pipewire-jack-audio-connection-kit and wireplumber quantum override.\n"
            "- Set PIPEWIRE_LATENCY=128/48000 when launching Ardour.\n"
            "- Check pw-top and wpctl status for xrun counters on the USB bus.\n"
        ),
    ),
    "global/topics/combinatorics-lib.md": (
        "Read when working on the OCaml enumerative combinatorics library.",
        ["enum-comb", "partitions"],
        (
            "- Pure OCaml generators for integer partitions, Young tableaux, and Dyck paths.\n"
            "- Verified with QCheck against closed-form generating functions.\n"
            "- Includes Prüfer sequence bijections for labelled trees.\n"
        ),
    ),
    "atlas-db/profile.md": (
        "What atlas-db is (purpose, technology, architecture).",
        [],
        (
            "- Embedded time-series storage engine for hydrophone sensor buoys.\n"
            "- Uses Gorilla float compression and Roaring bitmap tag indexes.\n"
            "- Modules: wal.go, compaction.go, gorilla.go, query.go, retention.go.\n"
        ),
    ),
    "atlas-db/topics/inspector-ui.md": (
        "Read when working on the atlas-db web inspector dashboard.",
        ["buoy-ui"],
        (
            "- Local HTTP dashboard showing segment compaction and WAL segment lag.\n"
            "- Exposes JSON metrics endpoints under /debug/atlas/segments.\n"
        ),
    ),
}

SEARCH_QUERIES: List[Tuple[str, str, Optional[str]]] = [
    ("what audio interface do I use on my ThinkPad T14", "global/profile.md", None),
    ("what is my amateur radio callsign", "global/profile.md", None),
    ("open-source Modbus telemetry daemon", "global/profile.md", None),
    ("reef aquarium with clownfish and anemones", "global/profile.md", None),
    ("food allergies to avoid when cooking", "global/profile.md", None),
    ("which linter and formatter should we run on shell scripts", "global/preferences.md", None),
    ("swept-sine chirp impulse responses in HDF5 files", "global/topics/kelp-sonar.md", None),
    ("risograph spot separation and PostScript booklet imposition", "global/topics/lantern-press.md", None),
    ("868 MHz LoRa duty-cycle and packet collision simulator", "global/topics/tinymesh.md", None),
    ("Mandarin tone pairs and Anki cloze flashcards", "global/areas/mandarin.md", None),
    ("Ardour xrun counters pw-top PIPEWIRE_LATENCY", "global/topics/silverblue-audio.md", None),
    ("Young tableaux and Dyck paths verified with QCheck", "global/topics/combinatorics-lib.md", None),
    ("JSON metrics endpoints for segment compaction lag", "atlas-db/topics/inspector-ui.md", "atlas-db"),
]

HINT_QUERIES: List[Tuple[str, Optional[str], Optional[str]]] = [
    ("let's fix the transducer serial number bug in kelp-sonar", "global/topics/kelp-sonar.md", None),
    ("can we add a new HarfBuzz ligature rule to lantern-press", "global/topics/lantern-press.md", None),
    ("run the replay harness on lora-sim", "global/topics/tinymesh.md", None),
    ("help me drill vocabulary for MAND204A", "global/areas/mandarin.md", None),
    ("Ardour is getting xruns on my Focusrite USB bus again", "global/topics/silverblue-audio.md", None),
    ("check my Prüfer sequence bijection in the combinatorics library", "global/topics/combinatorics-lib.md", None),
    (
        "Here is a long build log:\n" + ("building package foo-0.1.0\n" * 180)
        + "\nWhy is PipeWire jack routing dropping frames with wireplumber?",
        "global/topics/silverblue-audio.md",
        None,
    ),
    ("working on global topics today", None, None),
    ("read about kubernetes helm charts and terraform", None, None),
]


def _format_doc(path: str, desc: str, aliases: List[str], body: str) -> str:
    name = path.rsplit("/", 1)[-1].removesuffix(".md")
    alias_line = f"aliases: [{', '.join(aliases)}]\n" if aliases else ""
    return f"---\nname: {name}\ndescription: {desc}\n{alias_line}---\n{body}"


def run_benchmark(model_dir: str) -> Dict[str, float]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "store"
        store = Store(root)
        for rel, (desc, aliases, body) in CORPUS.items():
            target = store.memories / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_format_doc(rel, desc, aliases, body), encoding="utf-8")

        embedder = StaticEmbedder(root, model_dir=model_dir, auto_download=False)
        index = Index(store, embedder)
        try:
            t0 = time.perf_counter()
            index.reconcile()
            index.embed_backlog(max_batches=None)
            embed_ms = (time.perf_counter() - t0) * 1000.0

            top1 = 0
            mrr = 0.0
            t_search = time.perf_counter()
            for query, expected, proj in SEARCH_QUERIES:
                res = index.search(query, lambda _: True, project=proj)["results"]
                paths = [r["path"] for r in res]  # type: ignore[index]
                if paths and paths[0] == expected:
                    top1 += 1
                if expected in paths:
                    mrr += 1.0 / (paths.index(expected) + 1)
            search_ms = (time.perf_counter() - t_search) * 1000.0

            inlined = {"global/profile.md", "global/preferences.md", "atlas-db/profile.md"}
            hint_tp = 0
            hint_pos = 0
            hint_fp = 0
            hint_neg = 0
            for query, expected, proj in HINT_QUERIES:
                try:
                    hints = index.hints(
                        query,
                        lambda _: True,
                        exclude=inlined,
                        min_similarity=embedder.default_min_similarity,
                        project=proj,  # type: ignore[call-arg]
                    )
                except TypeError:
                    hints = index.hints(
                        query,
                        lambda _: True,
                        exclude=inlined,
                        min_similarity=embedder.default_min_similarity,
                    )
                paths = [p for p, _, _ in hints]
                if expected is None:
                    hint_neg += 1
                    if paths:
                        hint_fp += 1
                else:
                    hint_pos += 1
                    if paths and paths[0] == expected:
                        hint_tp += 1

            return {
                "chunks": float(index.stats()["chunks"]),  # type: ignore[arg-type]
                "embed_ms": round(embed_ms, 1),
                "search_ms": round(search_ms, 1),
                "search_top1": round(top1 / len(SEARCH_QUERIES), 4),
                "search_mrr": round(mrr / len(SEARCH_QUERIES), 4),
                "hint_top1": round(hint_tp / hint_pos, 4),
                "hint_fp_rate": round(hint_fp / hint_neg, 4),
            }
        finally:
            index.close()


if __name__ == "__main__":
    mdir = os.environ.get("MEMORY_BUCKETS_MODEL_DIR", "")
    if not mdir:
        raise SystemExit("Set MEMORY_BUCKETS_MODEL_DIR to run bench_retrieval")
    metrics = run_benchmark(mdir)
    for k, v in metrics.items():
        print(f"{k:16s}: {v}")
