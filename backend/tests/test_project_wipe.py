"""The wipe must delete every table that FKs to `agents`, or Postgres rolls the whole thing back."""
from tracely.infrastructure.db import models


def test_every_table_referencing_agents_is_wiped_before_it():
    """A new table with an `agents` FK breaks the Delete-all-data button the day it ships: the
    DELETE raises ForeignKeyViolation, the transaction rolls back, and the UI shows 'internal
    server error' having deleted nothing from Postgres — but ClickHouse went first, so the traces
    ARE gone. That is how `scenarios` broke it."""
    import inspect as pyinspect
    from tracely.infrastructure.db import repositories

    src = pyinspect.getsource(repositories.project_data_delete)
    wiped = {line for line in src.splitlines()}

    referencing = set()
    for mapper in models.Base.registry.mappers:
        table = mapper.local_table
        if table is None:
            continue
        for fk in table.foreign_keys:
            if fk.column.table.name == "agents":
                referencing.add(mapper.class_.__name__)

    missing = [name for name in referencing if not any(f"delete({name})" in ln for ln in wiped)]
    assert not missing, (
        f"these FK `agents` but are not deleted before it in project_data_delete: {missing}"
    )


def test_every_table_referencing_projects_is_deleted_with_it():
    """The same trap one level up: `project_delete` ends with `DELETE FROM projects`, and any
    table that FKs to `projects` and was not emptied first turns "delete workspace" into a
    ForeignKeyViolation. A row may be wiped by either half — the data wipe (derived rows) or the
    workspace delete (configuration) — so both sources are searched."""
    import inspect as pyinspect

    from tracely.infrastructure.db import repositories

    src = pyinspect.getsource(repositories.project_data_delete) + pyinspect.getsource(
        repositories.project_delete
    )
    referencing = set()
    for mapper in models.Base.registry.mappers:
        table = mapper.local_table
        if table is None or table.name == "projects":
            continue
        if any(fk.column.table.name == "projects" for fk in table.foreign_keys):
            referencing.add(mapper.class_.__name__)

    missing = sorted(name for name in referencing if f"delete({name})" not in src)
    assert not missing, f"these FK `projects` but neither wipe deletes them: {missing}"
