"""Hatchling build hook: bundles the built-in embedding model into the wheel.

A wheel built from a checkout or the sdist (``uv build``, ``pip install .``) gets
``memory_buckets/model/``, where the plugin looks first, so it never downloads a
model at runtime. The files are the ones ``static_model.py`` pins: fetched from
Hugging Face with every sha256 checked, then converted to float16, the same as
``nix/model.nix``. The converted copy is cached in ``$XDG_CACHE_HOME/memory-buckets``
(default ``~/.cache``), so each machine fetches it once.

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


def _static_model(root: Path):
    # Loaded by path: the build environment can't import the package, and this module is stdlib only.
    spec = importlib.util.spec_from_file_location("_memory_buckets_static_model", root / "memory_buckets" / "static_model.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cache_dir(sm) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "memory-buckets" / f"{sm.MODEL_NAME}-{sm.MODEL_REVISION[:8]}-f16"


def _build_model(sm, dest: Path, say) -> None:
    """Fetch, verify and convert into ``dest``, which appears whole or not at all."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{dest.name}.", dir=dest.parent))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            raw = sm.fetch(Path(tmp), progress=say, timeout=120.0)
            say("converting the model to float16")
            sm.convert_to_f16(raw / "model.safetensors", staging / "model.safetensors")
            shutil.copyfile(raw / "vocab.txt", staging / "vocab.txt")
        (staging / "NOTICE").write_text(sm.notice())
        try:
            staging.rename(dest)
        except OSError:
            if not sm.is_complete(dest):  # not just another build winning the race
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
        sm = _static_model(root)
        model = root / "memory_buckets" / "model"
        if not sm.is_complete(model):
            model = _cache_dir(sm)
            if not sm.is_complete(model):
                self.app.display_info(f"memory-buckets: fetching the embedding model {sm.MODEL_NAME} into {model}")
                _build_model(sm, model, lambda msg: self.app.display_info(f"memory-buckets: {msg}"))
        for path in sorted(model.iterdir()):
            if path.is_file() and not path.name.startswith("."):
                build_data["force_include"][str(path)] = f"memory_buckets/model/{path.name}"
