"""Hatchling build hook: bundles the built-in embedding model into the wheel.

A wheel built from a checkout or the sdist (``uv build``, ``pip install .``) gets
``memory_buckets/model/``, where the plugin looks first, so it never downloads a
model at runtime. The files are the ones ``embeddings.py`` pins: fetched from
Hugging Face with every sha256 checked, the same as ``nix/model.nix``. The
verified copy is cached in ``$XDG_CACHE_HOME/memory-buckets`` (default
``~/.cache``), so each machine fetches it once.

- A complete model already at ``memory_buckets/model/`` is used as is. The Nix
  build puts one there, because its sandbox has no network.
- ``MEMORY_BUCKETS_BUNDLE_MODEL=0`` builds without it; the plugin then downloads
  it on first use.
- Editable installs (``uv sync``) never bundle it.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

OFF = ("0", "false", "no", "off")


def _embeddings(root: Path):
    # Loaded by path: the build environment can't import the package, and top-level imports are stdlib only.
    spec = importlib.util.spec_from_file_location("_memory_buckets_embeddings", root / "memory_buckets" / "embeddings.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cache_dir(emb) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "memory-buckets" / f"{emb.MODEL_NAME}-{emb.MODEL_REVISION[:8]}"


def _build_model(emb, dest: Path, say) -> None:
    """Fetch and verify into ``dest``, which appears whole or not at all."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{dest.name}.", dir=dest.parent))
    try:
        emb.fetch(staging, dest=staging, progress=say, timeout=120.0)
        (staging / "NOTICE").write_text(emb.notice(), encoding="utf-8")
        try:
            staging.rename(dest)
        except OSError:
            if not emb.is_complete(dest):  # not just another build winning the race
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


class ModelBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel" or version == "editable":
            return
        if os.environ.get("MEMORY_BUCKETS_BUNDLE_MODEL", "").strip().lower() in OFF:
            self.app.display_info("memory-buckets: not bundling the embedding model (MEMORY_BUCKETS_BUNDLE_MODEL=0)")
            return
        root = Path(self.root)
        emb = _embeddings(root)
        model = root / "memory_buckets" / "model"
        if not emb.is_complete(model):
            model = _cache_dir(emb)
            if not emb.is_complete(model):
                self.app.display_info(f"memory-buckets: fetching the embedding model {emb.MODEL_NAME} into {model}")
                _build_model(emb, model, lambda msg: self.app.display_info(f"memory-buckets: {msg}"))
        for path in sorted(model.iterdir()):
            if path.is_file() and not path.name.startswith("."):
                build_data["force_include"][str(path)] = f"memory_buckets/model/{path.name}"
