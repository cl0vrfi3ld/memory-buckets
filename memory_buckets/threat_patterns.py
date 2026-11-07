"""Shared threat-pattern library (prompt injection / promptware / exfiltration).

Vendored from Hermes tools/threat_patterns.py. Threat patterns are loaded from
the accompanying `threat_patterns.json` file. Each pattern has a pattern string (regex),
pattern_id, and scope. Scope is cumulative: "all" everywhere, "context" adds promptware /
C2 / role hijack for context files, memory and tool results (warn-level: that content is
not user-authored), "strict" adds aggressive checks only for user-mediated writes (memory,
skill installs) where a block is resolvable. New patterns must anchor on C2 vocabulary
or unambiguous attack behaviour, not bossy English ("you must" is common in legitimate
documentation).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import unicodedata
from typing import Optional

# Hard cap on scanned text: scanners are advisory, so bound worst-case runtime.
MAX_SCAN_CHARS = 65_536

THREAT_PATTERNS_FILE = Path(__file__).resolve().parent / "threat_patterns.json"


def load_threat_patterns(path: Path | str | None = None) -> list[tuple[str, str, str]]:
    """Load threat patterns from a JSON file. Returns list of (pattern, pattern_id, scope) tuples."""
    target = Path(path) if path is not None else THREAT_PATTERNS_FILE
    with open(target, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"threat_patterns: expected list, got {type(data).__name__}")
    patterns: list[tuple[str, str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError(f"threat_patterns: expected dict entry, got {type(item).__name__}")
        pattern = item.get("pattern")
        pid = item.get("pattern_id")
        scope = item.get("scope")
        if not (
            isinstance(pattern, str)
            and bool(pattern)
            and isinstance(pid, str)
            and bool(pid)
            and isinstance(scope, str)
            and bool(scope)
        ):
            raise ValueError(f"threat_patterns: invalid pattern entry {item!r}")
        patterns.append((pattern, pid, scope))
    return patterns


_PATTERNS: list[tuple[str, str, str]] = load_threat_patterns()

# Invisible / bidirectional unicode used in injection attacks:
# zero-width space/non-joiner/joiner, word joiner, invisible times/separator/
# plus, BOM, LTR/RTL embedding + pop + overrides, LTR/RTL/first-strong isolates + pop.
INVISIBLE_CHARS = frozenset(
    "\u200b\u200c\u200d\u2060\u2062\u2063\u2064\ufeff"
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)

# Compiled per scope at import; inclusion is cumulative (all in context in strict).
_SCOPE_SETS = {"all": ("all", "context", "strict"), "context": ("context", "strict"), "strict": ("strict",)}


def _compile() -> dict[str, list[tuple[re.Pattern, str]]]:
    compiled: dict[str, list[tuple[re.Pattern, str]]] = {"all": [], "context": [], "strict": []}
    for pattern, pid, scope in _PATTERNS:
        if scope not in _SCOPE_SETS:
            raise ValueError(f"threat_patterns: unknown scope {scope!r} for pattern {pid!r}")
        for s in _SCOPE_SETS[scope]:
            compiled[s].append((re.compile(pattern, re.IGNORECASE), pid))
    return compiled


_COMPILED = _compile()


def scan_for_threats(content: str, scope: str = "context") -> list[str]:
    """Matched pattern IDs in `content` for `scope`; invisible codepoints are
    reported as `"invisible_unicode_U+XXXX"`. Raises ValueError on an unknown scope."""
    if not content:
        return []
    if (patterns := _COMPILED.get(scope)) is None:
        raise ValueError(f"scan_for_threats: unknown scope {scope!r}")
    content = content[:MAX_SCAN_CHARS]
    # Invisible unicode is checked on the RAW content: NFKC below can strip these codepoints.
    findings: list[str] = [f"invisible_unicode_U+{ord(ch):04X}" for ch in set(content) & INVISIBLE_CHARS]
    # NFKC folds full-width / compatibility variants (e.g. full-width cat -> cat) against homograph bypass.
    # It does NOT fold cross-script confusables (Cyrillic a) - that needs a TR#39 database.
    normalised = unicodedata.normalize("NFKC", content)
    findings.extend(pid for compiled, pid in patterns if compiled.search(normalised))
    return findings


def first_threat_message(content: str, scope: str = "strict") -> Optional[str]:
    """User-facing error for the first threat found, or None (block-on-first-hit paths)."""
    findings = scan_for_threats(content, scope=scope)
    if not findings:
        return None
    pid = findings[0]
    if pid.startswith("invisible_unicode_"):
        codepoint = pid.replace("invisible_unicode_", "")
        return f"Blocked: content contains invisible unicode character {codepoint} (possible injection)."
    return (
        f"Blocked: content matches threat pattern '{pid}'. "
        f"Content is injected into the system prompt and must not contain "
        f"injection or exfiltration payloads."
    )


__all__ = [
    "INVISIBLE_CHARS",
    "MAX_SCAN_CHARS",
    "THREAT_PATTERNS_FILE",
    "first_threat_message",
    "load_threat_patterns",
    "scan_for_threats",
]
