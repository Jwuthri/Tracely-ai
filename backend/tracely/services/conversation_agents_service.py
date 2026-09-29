"""Read access to the user-declared conversation agent catalog (Postgres `conversation_agents`).

Tiny sync service so the eval path (`@AGENTS`) can fetch a thread's declared agents through one
guarded seam — it never raises, so a lookup failure degrades to the spans-derived agent view.
"""

from __future__ import annotations

import structlog

from tracely.infrastructure.db import repositories
from tracely.infrastructure.db.engine import SyncSessionLocal

log = structlog.get_logger()


class ConversationAgentsService:
    @staticmethod
    def for_thread(project_id: str, thread_id: str) -> list[dict] | None:
        """The declared agent catalog for a thread, or None when none was sent (caller then falls
        back to deriving agents from spans). Guarded — never blocks a grade."""
        if not thread_id:
            return None
        try:
            with SyncSessionLocal() as s:
                row = repositories.conversation_agents_get(s, project_id, thread_id)
            return list(row.agents) if row and row.agents else None
        except Exception as exc:
            log.warning("conversation_agents_lookup_failed", error=str(exc))
            return None
