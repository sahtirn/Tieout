"""Vercel entrypoint for the hosted TieOut demo.

Vercel's Python runtime discovers the module-level ``app``. The filesystem is read-only except
/tmp and instances are ephemeral, so storage points at /tmp and the public demo pages are
stateless (see src/tieout/demo.py). The demo always uses the mock provider; LLM_PROVIDER defaults
to mock here as well so the persistent /reconcile API cannot spend credits unless explicitly
configured.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("DB_PATH", "/tmp/tieout.duckdb")
os.environ.setdefault("RUNS_DIR", "/tmp/tieout-runs")

from tieout.api import create_app  # noqa: E402

app = create_app()
