"""Seed the default project + a dev ingest key + the recommended online evaluators.

In prod (`TRACELY_ENV=prod`) the dev ingest key is NEVER seeded — `tracely_dev_key` is published
in the docs and would be a world-pwnable credential if it survived into prod. The default project
still gets created (operators add their own real keys via the UI / `IngestKey` API)."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select

from tracely.config import settings
from tracely.domain.evaluation.evaluators import TEMPLATES
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.db.models import Evaluator, IngestKey, Project

DEFAULT_PROJECT_SLUG = "default"
DEFAULT_KEY = "tracely_dev_key"


def _seed_evaluators(s, project_id: str) -> int:
    """Install the recommended evaluator catalog as editable records so online evaluation runs out
    of the box. Existing rows are left untouched (preserves user edits).

    ONLY safe to call on a project this process just created — see `main`. "Idempotent by
    score_name" is not the same as idempotent: a column the user deleted is, by definition, a
    score_name that is no longer present, so re-running this re-creates it."""
    existing = set(
        s.execute(select(Evaluator.score_name).where(Evaluator.project_id == project_id)).scalars()
    )
    added = 0
    for t in TEMPLATES:
        if not t.get("recommended") or t["score_name"] in existing:
            continue
        s.add(Evaluator(
            id=str(uuid4()), project_id=project_id, name=t["name"], description=t.get("description", ""),
            kind=t["kind"], score_name=t["score_name"], level=t["level"], config=t.get("config") or {},
        ))
        added += 1
    if added:
        s.commit()
    return added


def main() -> None:
    with SyncSessionLocal() as s:
        project = s.execute(
            select(Project).where(Project.slug == DEFAULT_PROJECT_SLUG)
        ).scalar_one_or_none()
        created = project is None
        if created:
            project = Project(id=str(uuid4()), slug=DEFAULT_PROJECT_SLUG, name="Default")
            s.add(project)
            s.commit()

        seeded_key: str
        if settings.is_prod:
            seeded_key = "(skipped: TRACELY_ENV=prod)"
        else:
            key = s.execute(select(IngestKey).where(IngestKey.key == DEFAULT_KEY)).scalar_one_or_none()
            if not key:
                s.add(IngestKey(id=str(uuid4()), project_id=project.id, key=DEFAULT_KEY))
                s.commit()
            seeded_key = DEFAULT_KEY

        # Evaluators are seeded ONCE, when this process creates the project — never on an existing
        # one. This runs as the pre-deploy step on every deploy, and the evaluators a workspace has
        # are user state: deleting a column is a decision, so re-seeding an existing project
        # resurrects every recommended column its owner deliberately removed.
        evaluators = _seed_evaluators(s, project.id) if created else "skipped (project exists)"
        print(f"project_id={project.id}  slug={project.slug}  ingest_key={seeded_key}  evaluators+={evaluators}")


if __name__ == "__main__":
    main()
