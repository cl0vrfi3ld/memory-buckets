"""Files that ship beside the code. Its own module because Hermes imports ``cli.py``
without running the package's ``__init__.py``."""

from pathlib import Path

_HERE = Path(__file__).resolve().parent
# Inside the package in a wheel; beside it at the repo root, which is also the plugin directory.
SKILLS_DIR = next((d for d in (_HERE / "skills", _HERE.parent / "skills") if d.is_dir()), _HERE / "skills")
