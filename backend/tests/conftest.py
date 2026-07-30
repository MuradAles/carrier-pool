"""Make ``import app.*`` work from a plain ``pytest`` run.

The backend is not installed as a package in the test environment, so put its
root (the directory holding ``app/``) on ``sys.path``. This is the only setup
the unit suite needs: no database, no network, no fixtures with I/O.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent

if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
