"""Credential shapes that memory must not hold.

Memory is plain Markdown that gets synced (Obsidian Sync, Syncthing, a git repo) and
offered back to the model on later turns, and a pasted credential is the commonest
thing a model will helpfully file. ``scan`` names the credential families in a text so
the write tools can refuse it.

This is a local list on purpose. Hermes's ``tools.threat_patterns`` has one secret
pattern (a quoted value of 20+ characters) and ``agent.redact`` is a redactor with
private lists and no way to ask "is there a secret here". A refusal also costs more
than a mask: a false positive blocks a real memory, so every family is anchored on a
shape that ordinary prose does not have, and where it can't be (an assignment such as
``password: hunter2``) the value has to look like a credential, not a word.

Pure: no I/O, no Hermes imports, and the same answer on every platform. The patterns
avoid nested repeats, so a long run of a repeated character stays linear.
"""

from __future__ import annotations

import re
from typing import List, Tuple

# A credential starts a word. Without this, ``task-management-system`` holds an ``sk-``
# key and ``Arsenal-fc-...`` a ``fc-`` one.
_START = r"(?<![A-Za-z0-9_-])"

# Name -> pattern, in the order names are reported.
_PREFIXED: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    # AKIA (long-lived) and ASIA (temporary) key ids: four capitals and sixteen of A-Z0-9.
    ("aws_access_key", re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])")),
    # ``sk-`` plus 20 or more key characters, at least one a digit. Real keys are random
    # and always have one; a kebab-case name that happens to start ``sk-`` seldom does.
    ("api_key", re.compile(_START + r"sk-(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{20,}")),
    # ghp_ gho_ ghu_ ghs_ ghr_ (36 characters) and the fine-grained github_pat_.
    ("github_token", re.compile(_START + r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("gitlab_token", re.compile(_START + r"glpat-[A-Za-z0-9_-]{20,}")),
    # Bot, app, user and refresh tokens (xox*) and app-level tokens (xapp-).
    ("slack_token", re.compile(_START + r"(?:xox[abprs]|xapp)-[A-Za-z0-9-]{20,}")),
    # The header plus something that looks like key material, not the header alone: a note
    # that explains what a PEM header is should be writable. Up to three lines may sit
    # between them (``Proc-Type`` and ``DEK-Info`` on a legacy encrypted key).
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----\s+(?:[^\n]*\n){0,3}?[A-Za-z0-9+/=]{32,}")),
)

# ``password: x``, ``GITHUB_TOKEN=x``, ``"secret": "x"``, ``**Password**: x``. The keyword must
# start a word (so ``GITHUB_TOKEN`` and ``db_password`` count but ``mytoken`` and
# ``tokenizer`` do not) and be followed straight away by ``:`` or ``=``, so "a password
# manager" never matches. Whether the value is a credential is decided in ``_is_credential``.
_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9])(?:password|passwd|secret|token|api[_-]?key)[*\"'`]*[ \t]*[:=][*\"'`]*[ \t]*[\"'`]?([^\s\"'`]+)",
    re.IGNORECASE,
)
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")  # SHOUTY_SNAKE: says where a value lives
_CALL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\(")  # ``lex(src)``: code assigning to a variable called token
_LETTER = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"[0-9]")


def _is_credential(value: str) -> bool:
    """Whether the word after ``password:`` looks like a password and not a note about one.

    One word, six or more characters, with a letter and a digit. That takes ``hunter2`` and
    ``P@ssw0rd!`` and leaves ``password: ask Ivy``, ``token: kept in 1Password`` and any
    path, URL, ``$VAR``, ``<placeholder>``, ``MY_ENV_VAR`` or function call: the notes the
    refusal asks the agent to write instead, and code. The cost is a miss on a letters-only
    or digits-only value; refusing every colon after the word password would block far more
    than it protects."""
    value = value.rstrip(".,;:)]}>*")
    if len(value) < 6 or value[0] in "$<{%*/~.(" or "://" in value:
        return False
    if _ENV_NAME.fullmatch(value) or _CALL.search(value):
        return False
    return bool(_LETTER.search(value) and _DIGIT.search(value))


def scan(text: str) -> List[str]:
    """The credential families ``text`` matches, each once, in a fixed order. Empty when it
    holds none. The names say what kind of thing was found; the text itself is never
    returned, because the caller puts the result in an error that goes back to the model."""
    if not text:
        return []
    found = [name for name, pattern in _PREFIXED if pattern.search(text)]
    if any(_is_credential(m.group(1)) for m in _ASSIGNMENT.finditer(text)):
        found.append("credential_assignment")
    return found
