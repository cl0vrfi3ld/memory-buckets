"""Synthetic retrieval and prefetch hint benchmark for memory-buckets.

Run with:
    MEMORY_BUCKETS_MODEL_DIR=<path-to-potion-retrieval-32M> uv run python -m tests.bench_retrieval

Three splits, reported separately, over two stores:

- exact: queries that reuse the target file's own words. A keyword match is enough, so
  this split saturates at 1.0 and only catches a retrieval path that is broken outright.
- paraphrase: queries that share no content word with their target. Only the embedding
  can link them, so this is where the model's limits show.
- negative: greetings, acknowledgements and off-topic requests. Any prefetch hint is a
  false positive.

The small store is the corpus below. The large store adds 400 filler files, enough that
the prompt's file listing truncates; only queries whose target the prompt does not show
are scored there, so search is the only way to the answer. A sweep of
prefetch_min_similarity against hint recall and false-positive rate follows each store.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from memory_buckets import scopes, snapshot
from memory_buckets.config import Config
from memory_buckets.embeddings import StaticEmbedder
from memory_buckets.index import _STOPWORDS, Index
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


# The splits below were added after the exact-match set above saturated at 1.0. The
# files here join CORPUS in one store, so the exact set now also competes with them.
EXTRA_CORPUS: Dict[str, Tuple[str, List[str], str]] = {
    "global/areas/sourdough.md": (
        "Read when helping with sourdough, starters or proofing schedules.",
        [],
        (
            "- Rye-and-white starter fed at 1:5:5 every morning; it lives on the counter in summer.\n"
            "- The standard loaf is 78% hydration with 20% wholemeal, proofed overnight in the fridge.\n"
            "- Bakes in a preheated cast-iron pot at 250C: 20 minutes lid on, then 20 minutes off.\n"
        ),
    ),
    "global/areas/spanish.md": (
        "Read when helping Sam keep up their Spanish.",
        [],
        (
            "- Conversational level from growing up with it at home; rusty on the subjunctive.\n"
            "- Reads the headlines of a Madrid newspaper on the train.\n"
            "- Wants corrections flagged gently, with the rule behind each one.\n"
        ),
    ),
    "global/areas/saltwater-tank.md": (
        "Read when troubleshooting the saltwater tank, its chemistry or livestock.",
        [],
        (
            "- 200 litre display; salinity held at 1.025 and temperature at 25C.\n"
            "- Weekly 10% exchange of seawater; alkalinity dosed with a two-part solution.\n"
            "- Stocking: a cleaner wrasse, two cardinalfish and a torch coral.\n"
        ),
    ),
    "global/areas/ocaml-learning.md": (
        "Read when helping Sam learn OCaml on weekends.",
        [],
        (
            "- Working through Real World OCaml, currently on variants and pattern matching.\n"
            "- Projects use dune, with tests in alcotest.\n"
            "- Wants idiomatic answers, not Haskell translated word for word.\n"
        ),
    ),
    "global/areas/commuting.md": (
        "Read when planning the commute or servicing the folding bicycle.",
        [],
        (
            "- 9 km each way along the Avon cycle path, 35 minutes door to door.\n"
            "- The folding bicycle has a hub gear and goes to the local co-op every six months.\n"
            "- Rain gear lives in the pannier; the lights are USB rechargeable.\n"
        ),
    ),
    "global/topics/nixpkgs-contributions.md": (
        "Read when packaging software for nixpkgs or chasing a review.",
        [],
        (
            "- Opened a pull request adding the daemon they maintain to nixpkgs; waiting on a committer.\n"
            "- Runs nixpkgs-review against the pull request before pinging anyone.\n"
            "- ofborg labels show how many rebuilds a change causes.\n"
        ),
    ),
    "global/topics/personal-site.md": (
        "Read when working on Sam's personal website.",
        [],
        (
            "- Static site generated with Zola, templates in Tera, deployed with rsync to a small VPS.\n"
            "- Pages are hand-written Markdown; the colour scheme is a dark Solarized variant.\n"
            "- Aims for a Lighthouse score above 95 and no JavaScript on content pages.\n"
        ),
    ),
    "global/topics/sea-trials.md": (
        "Read when planning vessel deployments for the marine robotics lab.",
        [],
        (
            "- Trials run from the research launch out of Plymouth, weather window permitting.\n"
            "- The autonomous submarine's battery limits a dive to about six hours.\n"
            "- Calibrate hydrophones on the quay before loading; the kit list lives in the lab wiki.\n"
        ),
    ),
    "global/topics/home-server.md": (
        "Read when working on Sam's home server, DNS filtering or backups.",
        [],
        (
            "- Raspberry Pi 5 running Pi-hole and Syncthing behind a WireGuard tunnel.\n"
            "- Restic snapshots go to a Backblaze B2 bucket every evening.\n"
            "- A restore drill is due each January.\n"
        ),
    ),
    "global/people/maya-okafor.md": (
        "Read when Maya Okafor comes up: a colleague at the lab.",
        [],
        (
            "- Leads firmware at the lab and prefers short async updates over meetings.\n"
            "- Birthday is in March; vegetarian.\n"
        ),
    ),
    "atlas-db/topics/retention.md": (
        "Read when changing how atlas-db expires aged segments.",
        [],
        (
            "- Segments older than 90 days are downsampled to one-minute averages.\n"
            "- Tombstones are only reclaimed on the next compaction pass.\n"
        ),
    ),
}

# Queries that share no content word with their target file, so only the embedding can
# link them (the keyword half of the search has nothing to match). tests/
# test_bench_retrieval.py enforces that. Each reads as a request a user might type, not
# as a description of the file. Some targets have a plausible rival (the profile, a
# neighbouring topic); that is deliberate, and part of what the split measures.
PARAPHRASE_QUERIES: List[Tuple[str, str, Optional[str]]] = [
    ("the crumb is dense and gummy, what did I get wrong", "global/areas/sourdough.md", None),
    ("when do I use the mood for wishes and doubts", "global/areas/spanish.md", None),
    ("everything has gone murky and the inhabitants are hiding", "global/areas/saltwater-tank.md", None),
    ("explain how to model a value that is either a circle or a rectangle", "global/areas/ocaml-learning.md", None),
    ("my chain keeps slipping when I pedal uphill", "global/areas/commuting.md", None),
    ("how do I get my derivation merged upstream", "global/topics/nixpkgs-contributions.md", None),
    ("add an RSS feed to my blog", "global/topics/personal-site.md", None),
    ("scheduling a boat trip to test the underwater vehicle", "global/topics/sea-trials.md", None),
    ("make sure my photos are copied somewhere safe in case the house burns down", "global/topics/home-server.md", None),
    ("who should I ask about the code that runs on the microcontroller", "global/people/maya-okafor.md", None),
    ("how long do we hold on to historical samples before aggregating them", "atlas-db/topics/retention.md", "atlas-db"),
    ("what format do I save the microphone correction data in", "global/topics/kelp-sonar.md", None),
    ("get my pamphlet text laid out so it folds correctly when printed double-sided", "global/topics/lantern-press.md", None),
    ("how many radios can share the airwaves before messages start clashing", "global/topics/tinymesh.md", None),
    ("the sound card crackles whenever I record vocals", "global/topics/silverblue-audio.md", None),
    ("count the ways of splitting a number into smaller numbers", "global/topics/combinatorics-lib.md", None),
]

# Turns that should surface nothing: greetings, acknowledgements and off-topic requests.
# The two HINT_QUERIES entries with no expected file join them in the negative split.
NEGATIVE_QUERIES: List[str] = [
    "hello",
    "good morning, how are you today?",
    "hey there!",
    "hi again",
    "thanks, that worked",
    "ok sounds good",
    "great, thank you",
    "yes please go ahead",
    "what is the capital of Australia",
    "write a haiku about autumn rain",
    "translate 'good evening' into French",
    "what's 17 times 23",
    "tell me a joke",
]

# Filler for the large store: fifty subjects Sam has no file about, crossed with eight
# facets, give 400 files whose listing cannot fit in the prompt.
FILLER_SUBJECTS = [
    "woodworking", "gardening", "film photography", "fountain pens", "board games", "rock climbing",
    "birdwatching", "pottery", "chess", "knitting", "home brewing", "3d printing", "astronomy",
    "letterpress", "beekeeping", "calligraphy", "orienteering", "origami", "mushroom foraging",
    "kite flying", "model railways", "tea tasting", "vinyl records", "bookbinding", "lock picking",
    "puzzle boxes", "street food", "jazz piano", "cider pressing", "dry stone walling", "stamp collecting",
    "cross stitch", "ukulele", "pinball", "cheese making", "scuba diving", "tabletop painting",
    "paper marbling", "trail running", "wood turning", "glassblowing", "heraldry", "fell walking",
    "ceramic glazes", "bonsai", "dowsing", "escape rooms", "sketchbooks", "rope work", "hedge laying",
]
FILLER_FACETS = [
    ("kit", "Read when choosing or maintaining equipment for {s}.",
     ["- Current kit for {s} is a mid-range starter set bought second-hand.",
      "- Replacement parts for {s} come from one specialist supplier.", "- Budget for {s} is capped at {n} pounds a year."]),
    ("history", "Read when {s} history or provenance comes up.",
     ["- Interest in {s} started after a museum visit.", "- A reference book on {s} sits on the second shelf.",
      "- Notable early practitioners of {s} are listed in the notes."]),
    ("goals", "Read when planning the next milestone in {s}.",
     ["- Goal for {s} this year: finish {n} projects.", "- Progress on {s} is reviewed on the first Sunday of the month.",
      "- Stretch goal for {s} is entering a local show."]),
    ("people", "Read when someone from the {s} circle is mentioned.",
     ["- The {s} group meets every other Wednesday.", "- A mentor for {s} lives two towns away.",
      "- Newsletter for {s} arrives on the fifteenth."]),
    ("venues", "Read when travelling for {s} or looking for a place to do it.",
     ["- Favourite venue for {s} is a converted warehouse.", "- Day trips for {s} are kept under {n} miles.",
      "- Membership for {s} renews in September."]),
    ("tips", "Read when giving advice about {s}.",
     ["- First lesson learned in {s}: slow down.", "- Common beginner mistake in {s}: buying too much too soon.",
      "- Checklist for {s} is pinned in the notes."]),
    ("reading", "Read when recommending books or videos about {s}.",
     ["- Best introduction to {s} is a short illustrated guide.", "- A podcast about {s} is saved for long journeys.",
      "- Forum threads on {s} are bookmarked by topic."]),
    ("storage", "Read when organising materials or space for {s}.",
     ["- Materials for {s} are kept in labelled crates.", "- Space for {s} is half of the spare room.",
      "- Inventory of {s} is counted every spring."]),
]
FILLER_FILES = 400

# Files the session prompt inlines, so prefetch never hints them.
INLINED = {"global/profile.md", "global/preferences.md", "atlas-db/profile.md"}

# Thresholds for the prefetch_min_similarity sweep; the model's own default is added to the list.
SWEEP_THRESHOLDS = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45]


def _format_doc(path: str, desc: str, aliases: List[str], body: str) -> str:
    name = path.rsplit("/", 1)[-1].removesuffix(".md")
    alias_line = f"aliases: [{', '.join(aliases)}]\n" if aliases else ""
    return f"---\nname: {name}\ndescription: {desc}\n{alias_line}---\n{body}"


def filler_corpus(count: int = FILLER_FILES) -> Dict[str, Tuple[str, List[str], str]]:
    """Deterministic distractor files: every subject crossed with every facet, so the same
    store (and the same numbers) come out of every run. ``count`` is at most 400."""
    out: Dict[str, Tuple[str, List[str], str]] = {}
    for i in range(count):
        subject = FILLER_SUBJECTS[i % len(FILLER_SUBJECTS)]
        facet, desc, bullets = FILLER_FACETS[(i // len(FILLER_SUBJECTS)) % len(FILLER_FACETS)]
        number = 10 + (i * 7) % 90
        path = f"global/topics/{subject.replace(' ', '-')}-{facet}.md"
        body = "".join(b.format(s=subject, n=number) + "\n" for b in bullets)
        out[path] = (desc.format(s=subject), [], body)
    return out


def _words(text: str) -> set:
    """Content words, lower-cased, with a plural 's' removed. Stopwords go, as the index drops them.
    Not ``_active_tokens``: that stops at 32 tokens, which would leave most of a file unchecked."""
    words = (w for w in re.findall(r"\w+", text.lower()) if w not in _STOPWORDS and (len(w) > 1 or not w.isdigit()))
    return {w[:-1] if w.endswith("s") and len(w) > 3 else w for w in words}


def shared_words(query: str, doc_text: str) -> List[str]:
    """Content words of ``query`` that ``doc_text`` also has: the same word, or (for words of
    six letters or more) the same first five letters, which catches licence/license and
    calibrate/calibration. Empty means only a semantic match can link the two."""
    have = _words(doc_text)
    hits = []
    for word in sorted(_words(query)):
        if word in have or (len(word) >= 6 and any(len(h) >= 6 and h[:5] == word[:5] for h in have)):
            hits.append(word)
    return hits


def doc_text(path: str, corpus: Dict[str, Tuple[str, List[str], str]]) -> str:
    """Everything the keyword search can see of a file: its stem, description, aliases and body."""
    desc, aliases, body = corpus[path]
    stem = path.rsplit("/", 1)[-1].removesuffix(".md").replace("-", " ")
    return f"{stem} {desc} {' '.join(aliases)} {body}"


def full_corpus() -> Dict[str, Tuple[str, List[str], str]]:
    return {**CORPUS, **EXTRA_CORPUS}


def _write_store(root: Path, corpus: Dict[str, Tuple[str, List[str], str]]) -> Store:
    store = Store(root)
    for rel, (desc, aliases, body) in corpus.items():
        target = store.memories / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_format_doc(rel, desc, aliases, body), encoding="utf-8")
    return store


def prompt_visible(store: Store, index: Index, project: Optional[str]) -> set:
    """Paths a session in ``project`` sees without searching: the files inlined into the
    prompt and the ones its truncated file listing still names."""
    cfg = Config()
    snap = snapshot.build(store, index, scopes.resolve(cfg, project, "primary"), cfg)
    visible = set(snap.paths)
    if "### Other files\n" in snap.text:
        section = snap.text.split("### Other files\n", 1)[1].split("\n\n", 1)[0]
        visible.update(line[2:].split(":", 1)[0] for line in section.splitlines() if line.startswith("- "))
    return visible


Query = Tuple[str, str, Optional[str]]


def _ratio(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def search_metrics(index: Index, queries: List[Query]) -> Dict[str, float]:
    top1 = top3 = 0
    mrr = 0.0
    started = time.perf_counter()
    for query, expected, proj in queries:
        res = index.search(query, lambda _: True, project=proj)["results"]
        paths = [r["path"] for r in res]  # type: ignore[index]
        top1 += bool(paths) and paths[0] == expected
        top3 += expected in paths[:3]
        if expected in paths:
            mrr += 1.0 / (paths.index(expected) + 1)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return {
        "n": float(len(queries)),
        "search_top1": _ratio(top1, len(queries)),
        "search_top3": _ratio(top3, len(queries)),
        "search_mrr": round(mrr / len(queries), 4) if queries else 0.0,
        "search_ms": round(elapsed_ms, 1),
    }


def _hints(index: Index, query: str, proj: Optional[str], threshold: float) -> List[Tuple[str, str, float]]:
    return index.hints(query, lambda _: True, exclude=INLINED, min_similarity=threshold, project=proj)


def hint_metrics(index: Index, queries: List[Query], threshold: float) -> Dict[str, float]:
    """``hint_top1``: the first hint is the target. ``hint_recall``: the target is among the hints."""
    first = found = 0
    for query, expected, proj in queries:
        paths = [p for p, _, _ in _hints(index, query, proj, threshold)]
        first += bool(paths) and paths[0] == expected
        found += expected in paths
    return {"hint_top1": _ratio(first, len(queries)), "hint_recall": _ratio(found, len(queries))}


def negative_metrics(index: Index, queries: List[str], threshold: float, *, headroom: bool = False) -> Dict[str, float]:
    """``hint_fp_rate``: turns that got any hint. With ``headroom``, also ``max_sim``: the
    highest similarity any file reached for these turns, which is how far the threshold
    could still drop before the next false positive."""
    hinted = 0
    highest = 0.0
    for query in queries:
        hinted += bool(_hints(index, query, None, threshold))
        if headroom:
            top = _hints(index, query, None, 0.0)[:1]
            highest = max(highest, top[0][2] if top else 0.0)
    out = {"n": float(len(queries)), "hint_fp_rate": _ratio(hinted, len(queries))}
    if headroom:
        out["max_sim"] = round(highest, 3)
    return out


def evaluate(index: Index, store: Store, threshold: float, *, only_searchable: bool) -> Dict[str, object]:
    """Every split against one store. With ``only_searchable``, a query counts only when its
    target is neither inlined nor named in the prompt's file listing, so search is the only
    way to it."""
    exact_hints = [q for q in HINT_QUERIES if q[1] is not None]
    negatives = [q for q, expected, _ in HINT_QUERIES if expected is None] + NEGATIVE_QUERIES
    exact_search: List[Query] = list(SEARCH_QUERIES)
    paraphrase: List[Query] = list(PARAPHRASE_QUERIES)
    total = len(exact_search) + len(paraphrase)
    if only_searchable:
        visible: Dict[Optional[str], set] = {}

        def hidden(q: Query) -> bool:
            if q[2] not in visible:
                visible[q[2]] = prompt_visible(store, index, q[2])
            return q[1] not in visible[q[2]]

        exact_search = [q for q in exact_search if hidden(q)]
        exact_hints = [q for q in exact_hints if hidden(q)]  # type: ignore[arg-type]
        paraphrase = [q for q in paraphrase if hidden(q)]
    splits = {
        "exact": {**search_metrics(index, exact_search), **hint_metrics(index, exact_hints, threshold)},  # type: ignore[arg-type]
        "paraphrase": {**search_metrics(index, paraphrase), **hint_metrics(index, paraphrase, threshold)},
        "negative": negative_metrics(index, negatives, threshold, headroom=True),
    }
    sweep = []
    for t in SWEEP_THRESHOLDS + ([threshold] if threshold not in SWEEP_THRESHOLDS else []):
        sweep.append({
            "threshold": t,
            "exact": hint_metrics(index, exact_hints, t)["hint_recall"],  # type: ignore[arg-type]
            "paraphrase": hint_metrics(index, paraphrase, t)["hint_recall"],
            "negative": negative_metrics(index, negatives, t)["hint_fp_rate"],
        })
    sweep.sort(key=lambda row: row["threshold"])  # type: ignore[arg-type,return-value]
    return {
        "splits": splits,
        "sweep": sweep,
        "queries": len(exact_search) + len(paraphrase),
        "queries_total": total,
    }


def run_benchmark(
    model_dir: str = "", *, embedder: Optional[object] = None, filler: int = FILLER_FILES
) -> Dict[str, Dict[str, object]]:
    """Run both stores and return ``{"small": ..., "large": ...}``. ``embedder`` stands in for
    the real model (the unit tests pass a fake); without it ``model_dir`` is loaded. ``filler``
    is how many distractor files the large store adds."""
    stores = {"small": full_corpus(), "large": {**full_corpus(), **filler_corpus(filler)}}
    results: Dict[str, Dict[str, object]] = {}
    for label, corpus in stores.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "store"
            store = _write_store(root, corpus)
            emb = embedder or StaticEmbedder(root, model_dir=model_dir, auto_download=False)
            index = Index(store, emb)  # type: ignore[arg-type]
            try:
                t0 = time.perf_counter()
                index.reconcile()
                index.embed_backlog(max_batches=None)
                embed_ms = (time.perf_counter() - t0) * 1000.0
                threshold = emb.default_min_similarity  # type: ignore[attr-defined]
                report = evaluate(index, store, threshold, only_searchable=label == "large")
                report.update(
                    files=len(corpus),
                    chunks=int(index.stats()["chunks"]),  # type: ignore[arg-type]
                    embed_ms=round(embed_ms, 1),
                    threshold=threshold,
                )
                results[label] = report
            finally:
                index.close()
    return results


COLUMNS = ["n", "search_top1", "search_top3", "search_mrr", "hint_top1", "hint_recall", "hint_fp_rate", "max_sim"]


def format_report(results: Dict[str, Dict[str, object]]) -> str:
    lines: List[str] = []
    for label, report in results.items():
        lines.append(
            f"{label} store: {report['files']} files, {report['chunks']} chunks, "
            f"embedded in {report['embed_ms']} ms, prefetch_min_similarity {report['threshold']}"
        )
        if label == "large":
            lines.append(
                f"  {report['queries']} of {report['queries_total']} search queries have a target the prompt "
                "does not show; only those are scored"
            )
        lines.append("  " + "split".ljust(11) + "".join(c.rjust(13) for c in COLUMNS))
        for name, row in report["splits"].items():  # type: ignore[union-attr]
            cells = "".join((f"{row[c]:g}" if c in row else "-").rjust(13) for c in COLUMNS)
            lines.append("  " + name.ljust(11) + cells)
        lines.append("  hint recall and false positives by prefetch_min_similarity")
        lines.append("  " + "threshold".ljust(11) + "".join(c.rjust(13) for c in ("exact", "paraphrase", "negative")))
        for row in report["sweep"]:  # type: ignore[union-attr]
            mark = "  <- default" if row["threshold"] == report["threshold"] else ""
            lines.append(
                "  " + f"{row['threshold']:g}".ljust(11)
                + "".join(f"{row[c]:g}".rjust(13) for c in ("exact", "paraphrase", "negative")) + mark
            )
        lines.append("")
    lines.append("exact and paraphrase columns under the sweep are hint recall; negative is the false-positive rate.")
    return "\n".join(lines)


if __name__ == "__main__":
    mdir = os.environ.get("MEMORY_BUCKETS_MODEL_DIR", "")
    if not mdir:
        raise SystemExit("Set MEMORY_BUCKETS_MODEL_DIR to run bench_retrieval")
    print(format_report(run_benchmark(mdir)))
