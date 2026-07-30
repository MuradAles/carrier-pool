#!/usr/bin/env python3
"""Validate the generated TMS sync fixtures without rewriting them.

Exits non-zero on any failure. Thin wrapper around the checks that live in
``generate_data.py`` so there is exactly one definition of "valid".

    python3 backend/scripts/validate_data.py [--out data]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_data import main  # noqa: E402

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "--validate-only", *sys.argv[1:]]
    raise SystemExit(main())
