"""Pytest configuration shared by every test in tests/.

The repo has two importable roots:

- ``src/`` for the installed ``tangerine`` package (already on the path
  via ``[tool.pytest.ini_options] pythonpath`` in pyproject.toml).
- the repo root, for ad-hoc helpers that are not part of the shipped
  package but have their own tests — currently ``scripts/`` (the
  Loyverse menu-dump worksheet generator).

Putting the repo root on ``sys.path`` here lets tests import
``scripts.dump_loyverse_items`` without each test re-inserting the path,
and without making ``scripts/`` a packaged module (it isn't — it ships
as standalone scripts and is excluded from the wheel via
``[tool.setuptools.packages.find] where = ["src"]``).
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
