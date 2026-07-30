"""Shared setup for the data-integrity suite.

``generate_data.py`` lives in ``backend/scripts/``, which is not a package, so
put it on ``sys.path``. Importing it also puts ``backend/`` there for
``app.domain.*``.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _REPO_ROOT / "backend" / "scripts"

if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import generate_data as gd  # noqa: E402

DATA_ROOT = _REPO_ROOT / "data"


@pytest.fixture(scope="session")
def plans() -> list:
    """The seeded plan, built once.

    ``render()`` assigns public ids off ``plan.rng``, so it must run exactly
    once per plan -- a second call would hand out different ids. The validator
    only reads the plan, so one build is safe to share across the whole session.
    """
    built = [gd.build_plan(cfg) for cfg in gd.BROKERS]
    for plan in built:
        gd.render(plan)
    return built


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A throwaway copy of ``data/``. Corruptions never touch the real tree."""
    dst = tmp_path / "data"
    shutil.copytree(DATA_ROOT, dst)
    return dst
