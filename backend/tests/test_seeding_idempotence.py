"""The pre-deploy seeder must never resurrect a deleted evaluator column.

`python -m tracely.services.seeding_service` is the Railway pre-deploy step, so it runs on EVERY
deploy against a long-lived database. `_seed_evaluators` skips templates whose `score_name` is
already present — which is exactly why re-running it on an existing project is destructive: a
column the user deleted *is* an absent score_name, so every deploy added it back. Disabling the
column ("Run automatically on new traces") appeared to be the only thing that stuck, because a
disabled row still exists and is therefore skipped.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tracely.infrastructure.db import models
from tracely.infrastructure.db.base import Base
from tracely.services import seeding_service

_TABLES = [
    models.Project.__table__,
    models.IngestKey.__table__,
    models.Evaluator.__table__,
]


@pytest.fixture
def seeder_db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path}/seed.db")
    Base.metadata.create_all(eng, tables=_TABLES)
    maker = sessionmaker(eng)
    monkeypatch.setattr(seeding_service, "SyncSessionLocal", maker)
    yield maker
    eng.dispose()


def _score_names(maker) -> set[str]:
    with maker() as s:
        return set(s.execute(select(models.Evaluator.score_name)).scalars())


def _one(s, score_name):
    return s.execute(
        select(models.Evaluator).where(models.Evaluator.score_name == score_name)
    ).scalar_one()


def test_first_run_seeds_the_recommended_columns(seeder_db):
    seeding_service.main()
    assert _score_names(seeder_db), "a brand-new project must get the recommended catalog"


def test_a_deleted_column_stays_deleted_across_deploys(seeder_db):
    seeding_service.main()
    seeded = _score_names(seeder_db)
    victim = sorted(seeded)[0]

    with seeder_db() as s:  # the user deletes a column in the UI
        s.delete(_one(s, victim))
        s.commit()

    seeding_service.main()  # the next deploy runs the pre-deploy seeder again

    assert victim not in _score_names(seeder_db), (
        f"{victim} came back — the seeder re-seeded an existing project"
    )
    assert _score_names(seeder_db) == seeded - {victim}, "no other column may change either"


def test_a_disabled_column_is_left_alone(seeder_db):
    """The workaround must keep working: a disabled row is user state, not a missing one."""
    seeding_service.main()
    victim = sorted(_score_names(seeder_db))[0]
    with seeder_db() as s:
        _one(s, victim).enabled = False
        s.commit()

    seeding_service.main()

    with seeder_db() as s:
        assert _one(s, victim).enabled is False, "the seeder re-enabled a column switched off"
