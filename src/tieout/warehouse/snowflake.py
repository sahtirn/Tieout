"""Snowflake port of the Warehouse interface (stub).

How the port would work:

* Connection: ``snowflake-connector-python`` (or Snowpark) using key-pair auth from a secrets
  manager. Snowflake is intentionally not a dependency of this project.
* Schema: the DDL in ``duckdb_warehouse.SCHEMA`` ports almost verbatim. ``DECIMAL(18,2)`` becomes
  ``NUMBER(18,2)``, ``VARCHAR`` JSON payloads become ``VARIANT`` populated with ``PARSE_JSON``, and
  primary keys become informational, so the ``match_items`` uniqueness guarantee must be enforced
  with a ``MERGE`` or a post-load duplicate check inside the same transaction.
* Candidate queries: ``ABS(b.txn_date - g.entry_date)`` becomes ``ABS(DATEDIFF('day', g.entry_date,
  b.txn_date))``. Everything else is ANSI joins with ``NOT EXISTS`` anti-joins.
* Bulk loads: stage CSVs to an internal stage and ``COPY INTO`` rather than row inserts.
* LLM: Snowflake Cortex (``SNOWFLAKE.CORTEX.COMPLETE``) can host the investigator so ledger data
  never leaves the account boundary; the provider interface stays the same.
"""

from __future__ import annotations

from typing import Any, NoReturn

from tieout.warehouse.base import Warehouse

_MSG = "SnowflakeWarehouse is a documented stub; see the module docstring for the port plan."


class SnowflakeWarehouse(Warehouse):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(_MSG)

    def _stub(self, *args: Any, **kwargs: Any) -> NoReturn:  # pragma: no cover
        raise NotImplementedError(_MSG)

    init_schema = close = create_run = finish_run = update_run_result = get_run = _stub
    list_runs = load_bank = load_gl = get_bank = get_gl = exact_candidates = _stub
    near_candidates = unmatched_bank = unmatched_gl = save_matches = get_matches = _stub
    save_exceptions = get_exceptions = save_finding = get_findings = log_agent_step = _stub
    get_agent_steps = log_llm_call = get_llm_calls = save_journal_entries = _stub
    get_journal_entries = add_review_action = get_review_actions = _stub
