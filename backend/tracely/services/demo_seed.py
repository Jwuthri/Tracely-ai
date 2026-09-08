"""Launch the demo seeder for a workspace.

`scripts/seed_demo.py` drives the product through its own HTTP API — it pushes traces, then waits
for them to be ingested, clustered and evaluated. That wait is why it runs as a DETACHED CHILD of
the API process rather than a Celery task: under the default `--pool=solo --concurrency=1` a seed
task would hold the worker's only slot while waiting on work queued behind itself, and deadlock.

Everything here is best-effort. A workspace whose demo data failed to seed is a slightly emptier
workspace, never a failed signup — so no caller has to handle an error.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import structlog

from tracely.config import settings

log = structlog.get_logger()


def script_path() -> Path:
    return Path(__file__).resolve().parents[3] / "scripts" / "seed_demo.py"


def available() -> bool:
    """False on a trimmed image that ships without `scripts/`."""
    return script_path().exists()


def launch(ingest_key: str) -> bool:
    """Start the seeder for the workspace behind `ingest_key`. Returns whether it started.

    The seeder emits synthetic spans only — no LLM calls — so it costs nothing and works even
    with `REQUIRE_PROJECT_LLM_KEY` on (the clustering phase self-skips without keys). Its traces
    are ordinary customer traces and do count toward the workspace's quota; the demo set is a few
    dozen, against a monthly cap in the thousands.
    """
    if not ingest_key or not available():
        return False
    try:
        subprocess.Popen(  # noqa: S603
            [sys.executable, str(script_path())],
            env={
                **os.environ,
                "TRACELY_API": settings.internal_api_url,
                "TRACELY_KEY": ingest_key,
                # Every span the seeder emits is stamped `tracely.sample=true`, so sample data
                # never counts as the customer's own activation (services/milestones.py).
                "TRACELY_SAMPLE": "1",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception as exc:  # a failed spawn must never surface to the caller
        log.warning("demo_seed_launch_failed", error=str(exc))
        return False
