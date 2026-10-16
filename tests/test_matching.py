from __future__ import annotations

import json
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from tieout.config import Settings
from tieout.matching import MatchingEngine, subset_sum
from tieout.models import BankTransaction, GLEntry, Match, MatchRule
from tieout.similarity import normalize, pair_similarity, similarity
from tieout.warehouse import DuckDBWarehouse
from tieout.warehouse.snowflake import SnowflakeWarehouse

D = date(2026, 9, 10)


def bank(
    i: str, amt: str, d: date = D, desc: str = "X", chk: str | None = None, ref: str | None = None
) -> BankTransaction:
    return BankTransaction(
        txn_id=i, txn_date=d, description=desc, amount=amt, check_number=chk, reference=ref
    )


def gl(
    i: str, amt: str, d: date = D, desc: str = "X", chk: str | None = None, ref: str | None = None
) -> GLEntry:
    return GLEntry(
        entry_id=i,
        entry_date=d,
        journal="AP",
        description=desc,
        amount=amt,
        check_number=chk,
        reference=ref,
    )


def run(b: list[BankTransaction], g: list[GLEntry], **kw: object) -> tuple[list[Match], object]:
    wh = DuckDBWarehouse()
    wh.load_bank("r", b)
    wh.load_gl("r", g)
    return MatchingEngine(wh, Settings(**kw)).run("r")  # type: ignore[arg-type]


def test_exact_pass_requires_date_amount_and_identifier() -> None:
    matches, _ = run(
        [
            bank("B1", "-100.00", ref="T1"),
            bank("B2", "-200.00", chk="1001"),
            bank("B3", "-300.00", ref="T3"),
        ],
        [
            gl("G1", "-100.00", ref="T1"),
            gl("G2", "-200.00", chk="1001"),
            gl("G3", "-300.00", ref="OTHER", desc="Totally different"),
        ],
    )
    exact = [m for m in matches if m.rule == MatchRule.EXACT]
    assert {(m.bank_ids[0], m.gl_ids[0]) for m in exact} == {("B1", "G1"), ("B2", "G2")}
    assert exact[0].confidence == 1.0
    assert "date" in exact[0].evidence["matched_on"]


def test_near_pass_uses_window_and_similarity() -> None:
    d2 = date(2026, 9, 12)
    matches, _ = run(
        [
            bank("B1", "-512.33", d2, "ACH DEBIT BRIGHTLINE OFC SPLY"),
            bank("B2", "-77.10", date(2026, 9, 20), "ACH DEBIT BRIGHTLINE OFC SPLY"),
            bank("B3", "-91.00", d2, "ACH DEBIT GRANITE PEAK LOGIST"),
        ],
        [
            gl("G1", "-512.33", D, "ACH Payment - Brightline Office Supply"),
            gl("G2", "-77.10", D, "ACH Payment - Brightline Office Supply"),
            gl("G3", "-91.00", D, "ACH Payment - Meadowbrook Catering"),
        ],
    )
    pairs = {(m.bank_ids[0], m.gl_ids[0]) for m in matches}
    assert pairs == {("B1", "G1")}  # B2 outside window, B3 payee mismatch
    m = matches[0]
    assert m.rule == MatchRule.NEAR and m.evidence["day_gap"] == 2
    assert m.evidence["similarity"] >= 0.78


def test_near_pass_prefers_closest_date_on_ties() -> None:
    matches, _ = run(
        [bank("B1", "-50.00", date(2026, 9, 11), "CHECK 1001", chk="1001")],
        [
            gl("G1", "-50.00", date(2026, 9, 8), "Check 1001 - A", chk="1001"),
            gl("G2", "-50.00", date(2026, 9, 10), "Check 1001 - A", chk="1001"),
        ],
    )
    assert matches[0].gl_ids == ["G2"]


def test_many_to_one_batch_deposit() -> None:
    matches, stats = run(
        [bank("B1", "1000.00", desc="REMOTE DEPOSIT")],
        [
            gl("G1", "250.25", date(2026, 9, 9)),
            gl("G2", "349.75", D),
            gl("G3", "400.00", D),
            gl("G4", "123.45", D),
        ],
    )
    assert len(matches) == 1
    m = matches[0]
    assert m.rule == MatchRule.MANY_TO_ONE
    assert m.gl_ids == ["G1", "G2", "G3"]
    assert m.evidence["gl_sum"] == "1000.00"
    assert stats.many_to_one == 1  # type: ignore[attr-defined]


def test_many_to_one_refuses_ambiguous_solutions() -> None:
    _, stats = run(
        [bank("B1", "300.00", desc="REMOTE DEPOSIT")],
        [gl("G1", "100.00"), gl("G2", "200.00"), gl("G3", "150.00"), gl("G4", "150.00")],
    )
    assert stats.batch_ambiguous == ["B1"]  # type: ignore[attr-defined]


def test_many_to_one_respects_window_and_size_cap() -> None:
    matches, _ = run(
        [bank("B1", "600.00")],
        [gl("G1", "100.00", date(2026, 9, 1)), gl("G2", "500.00", D)],
    )
    assert matches == []
    many = [gl(f"G{i}", f"{i}.00") for i in range(1, 8)]
    matches, _ = run([bank("B1", "28.00")], many, batch_max_items=5)
    assert matches == []  # needs all 7 items, above the cap of 5


def test_subset_sum_time_budget() -> None:
    pool = [gl(f"G{i}", f"{i}.01") for i in range(1, 60)]
    assert subset_sum(pool, 10**9, 5, time.perf_counter() - 1) == []  # pruned before searching
    result = subset_sum(pool, 9000, 5, time.perf_counter() - 1)
    assert result is None  # budget already spent


def test_no_double_match_invariant(loaded_warehouse: DuckDBWarehouse) -> None:
    matches, _ = MatchingEngine(loaded_warehouse, Settings()).run("r1")
    bank_ids = [i for m in matches for i in m.bank_ids]
    gl_ids = [i for m in matches for i in m.gl_ids]
    assert len(bank_ids) == len(set(bank_ids))
    assert len(gl_ids) == len(set(gl_ids))
    # The database enforces it as well.
    with pytest.raises(duckdb.ConstraintException):
        loaded_warehouse.save_matches(
            "r1",
            [
                Match(
                    match_id="M-99999",
                    rule=MatchRule.EXACT,
                    bank_ids=[bank_ids[0]],
                    gl_ids=["GL-X"],
                    amount=Decimal("1.00"),
                    confidence=1.0,
                    evidence={},
                )
            ],
        )


def test_full_dataset_matches_ground_truth(
    loaded_warehouse: DuckDBWarehouse, dataset_dir: Path
) -> None:
    matches, stats = MatchingEngine(loaded_warehouse, Settings()).run("r1")
    labels = json.loads((dataset_dir / "labels.json").read_text())
    truth = {(b, g) for m in labels["matches"] for b in m["bank_ids"] for g in m["gl_ids"]}
    pred = {(b, g) for m in matches for b in m.bank_ids for g in m.gl_ids}
    assert pred == truth
    assert stats.exact > 0 and stats.near > 0 and stats.many_to_one == 12
    assert len(loaded_warehouse.get_matches("r1")) == len(matches)


def test_similarity_behaviour() -> None:
    assert normalize("ACH DEBIT 123456789 Foo-Bar") == "foo bar"
    assert similarity("ACH DEBIT BRIGHTLINE OFC", "ACH Payment - Brightline Office Supply") > 0.8
    assert similarity("ACH DEBIT", "x") == 0.0
    assert pair_similarity("a", "b", "1", "1") == (1.0, "check_number")
    assert pair_similarity("a", "b", None, None, "R", "R") == (1.0, "reference")


def test_snowflake_stub_raises() -> None:
    with pytest.raises(NotImplementedError):
        SnowflakeWarehouse()
