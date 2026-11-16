"""⌘K global search across conversations, issues, cases, and gates.

Pure HTTP shaping — ClickHouse search lives in `infrastructure.clickhouse.async_reader`,
registry search in `infrastructure.db.repositories`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from tracely.api.auth import get_project_id
from tracely.infrastructure.spans import async_reader
from tracely.infrastructure.db import repositories as repo
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.text import message_text

router = APIRouter(prefix="/api")


@router.get("/search")
async def search(q: str = "", project_id: str = Depends(get_project_id)) -> list[dict]:
    """Match any turn whose user message contains the query, then report the whole THREAD: its
    first message as the label, its TOTAL turn count, and its latest trace — so a multi-turn
    conversation links to /sessions (not a single matched turn) with the right turn count."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    threads = await async_reader.search_threads(project_id, q)
    out = [
        {
            "type": "trace",
            "label": message_text(t["first_input"]) or t["thread"],
            "sub": f"{t['turns']} turn(s)",
            "href": f"/sessions/{t['thread']}" if t["turns"] > 1 else f"/traces/{t['last_trace']}",
        }
        for t in threads
    ]

    def registry_rows():
        with SyncSessionLocal() as s:
            return repo.search_registry(s, project_id, q)

    return out + await run_in_threadpool(registry_rows)
