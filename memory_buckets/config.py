"""Plugin config (MEM-1 Plan §4, §5).

Inside Hermes, the config is ``plugins.memory-buckets`` in ``config.yaml``, read with
``hermes_cli.config.load_config_readonly`` (imported lazily). Outside Hermes
(tests) there's no YAML parser in stdlib, so
the defaults apply, overridden by ``MEMORY_BUCKETS_*`` environment variables.
The embeddings API key always comes from the environment (``api_key_env``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Optional

logger = logging.getLogger("memory_buckets")

CONFIG_KEY = "memory-buckets"
DEFAULT_API_KEY_ENV = "MEMORY_BUCKETS_EMBEDDINGS_API_KEY"


@dataclass
class EmbeddingsConfig:
    backend: str = "auto"  # auto (http if base_url is set, else built-in) | local | http | off
    auto_download: bool = True  # fetch the built-in model in the background if it isn't bundled
    local_model_dir: str = ""  # use a built-in model from here instead
    base_url: str = ""  # an OpenAI-compatible endpoint, e.g. http://homelab:11434/v1
    model: str = ""
    api_key_env: str = DEFAULT_API_KEY_ENV
    timeout_s: float = 2.0
    batch: int = 32
    query_prefix: str = ""  # e.g. "search_query: " for nomic-embed-text
    document_prefix: str = ""  # e.g. "search_document: "; changing it re-embeds everything

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""


@dataclass
class Config:
    embeddings: EmbeddingsConfig = field(default_factory=EmbeddingsConfig)
    store_path: str = ""  # default: $HERMES_HOME/memory-buckets
    prefetch_min_similarity: float = -1.0  # < 0: the backend's measured default (0.27 built-in, 0.45 http)
    prefetch_max_hints: int = 4
    snapshot_max_chars: int = 12000  # the rules alone are ~4k; the rest is profile, preferences and the file list
    nudge_interval: int = 10  # user turns between filing reminders; 0 = off (ADR-0011)
    write_policy: str = "shared"  # or "confined": project sessions can't write global files
    cron_writes: bool = False
    readonly: bool = False
    project_boost: float = 1.3
    search_limit: int = 8

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Config":
        cfg = cls()
        if not isinstance(data, dict):
            return cfg
        for f in fields(cls):
            if f.name == "embeddings" or f.name not in data:
                continue
            _assign(cfg, f.name, data[f.name], f.type)
        emb = data.get("embeddings")
        if isinstance(emb, dict):
            for f in fields(EmbeddingsConfig):
                if f.name in emb:
                    _assign(cfg.embeddings, f.name, emb[f.name], f.type)
        if cfg.write_policy not in ("shared", "confined"):
            logger.warning("memory-buckets: unknown write_policy %r, using 'shared'", cfg.write_policy)
            cfg.write_policy = "shared"
        return cfg


def _assign(obj: Any, name: str, value: Any, type_name: Any) -> None:
    kind = type_name if isinstance(type_name, str) else getattr(type_name, "__name__", "str")
    try:
        if kind == "bool":
            value = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
        elif kind == "int":
            value = int(value)
        elif kind == "float":
            value = float(value)
        else:
            value = "" if value is None else str(value)
    except (TypeError, ValueError):
        logger.warning("memory-buckets: ignoring bad config value %s=%r", name, value)
        return
    setattr(obj, name, value)


_ENV = {
    "MEMORY_BUCKETS_EMBEDDINGS_BASE_URL": ("embeddings", "base_url"),
    "MEMORY_BUCKETS_EMBEDDINGS_MODEL": ("embeddings", "model"),
    "MEMORY_BUCKETS_EMBEDDINGS_BACKEND": ("embeddings", "backend"),
    "MEMORY_BUCKETS_EMBEDDINGS_AUTO_DOWNLOAD": ("embeddings", "auto_download"),
    "MEMORY_BUCKETS_MODEL_DIR": ("embeddings", "local_model_dir"),
    "MEMORY_BUCKETS_STORE": (None, "store_path"),
}


def _from_hermes() -> Optional[Dict[str, Any]]:
    try:
        from hermes_cli.config import cfg_get, load_config_readonly
    except ImportError:
        return None
    try:
        return cfg_get(load_config_readonly(), "plugins", CONFIG_KEY, default={}) or {}
    except Exception:  # config errors must never stop the provider loading
        logger.warning("memory-buckets: couldn't read plugins.%s from Hermes config", CONFIG_KEY, exc_info=True)
        return {}


def load(data: Optional[Dict[str, Any]] = None) -> Config:
    """Config from ``data`` if given, else Hermes's config.yaml, else defaults.
    ``MEMORY_BUCKETS_*`` environment variables override either."""
    raw = dict(data if data is not None else (_from_hermes() or {}))
    raw["embeddings"] = dict(raw.get("embeddings") or {})
    for env, (section, key) in _ENV.items():
        if os.environ.get(env):
            (raw[section] if section else raw)[key] = os.environ[env]
    return Config.from_dict(raw)


# `hermes memory setup` / dashboard fields (MemoryProvider.get_config_schema).
SETUP_SCHEMA: List[Dict[str, Any]] = [
    {"key": "embeddings_base_url", "description": "OpenAI-compatible embeddings endpoint, e.g. http://homelab:11434/v1. Empty = keyword search only.", "required": False, "default": "", "type": "text"},
    {"key": "embeddings_model", "description": "Embedding model name at that endpoint", "required": False, "default": "", "type": "text"},
    {"key": "embeddings_api_key", "description": "API key for the endpoint, if it needs one", "secret": True, "required": False, "env_var": DEFAULT_API_KEY_ENV},
]


def save(values: Dict[str, Any]) -> None:
    """Write non-secret setup values under ``plugins.memory-buckets`` (Hermes only)."""
    from hermes_cli.config import save_config

    embeddings = {}
    if "embeddings_base_url" in values:
        embeddings["base_url"] = values["embeddings_base_url"]
    if "embeddings_model" in values:
        embeddings["model"] = values["embeddings_model"]
    if embeddings:
        save_config({"plugins": {CONFIG_KEY: {"embeddings": embeddings}}}, merge_existing=True)
