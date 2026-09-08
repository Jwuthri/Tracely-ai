"""Server-side thread filters (W7): the HAVING fragments the page and its count share."""

from __future__ import annotations

from tracely.infrastructure.clickhouse.async_reader import session_filter_clauses


def test_no_filters_is_a_noop():
    assert session_filter_clauses(None, None, "") == ("", {})
    assert session_filter_clauses(None, None, "   ") == ("", {})


def test_status_and_multi_turn_are_whole_set_predicates():
    sql, params = session_filter_clauses(True, True, "")
    assert sql == " AND failing = 1 AND turns > 1" and params == {}
    assert session_filter_clauses(False, False, "")[0] == " AND failing = 0 AND turns = 1"


def test_text_is_parameterised_case_insensitive_and_bounded():
    sql, params = session_filter_clauses(None, None, "  Refund POLICY  ")
    assert "positionCaseInsensitiveUTF8(" in sql and "{q:String}" in sql
    assert params == {"q": "Refund POLICY"}
    assert "first_input" in sql and "metadata" in sql and "agent_id" in sql  # the documented fields
    assert len(session_filter_clauses(None, None, "x" * 500)[1]["q"]) == 200
    assert "'" not in sql.replace("' '", "")  # nothing user-controlled is spliced into the SQL
