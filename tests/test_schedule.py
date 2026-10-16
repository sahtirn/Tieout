from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.helpers import finding, findings_from_labels
from tieout.config import Settings, load_accounts
from tieout.io import load_bank_csv, load_gl_csv
from tieout.journal import propose_journal_entries
from tieout.matching import MatchingEngine, exceptions_from_unmatched
from tieout.models import (
    BreakType,
    JELine,
    JournalEntryProposal,
    ReconStatus,
    ReviewStatus,
)
from tieout.schedule import ScheduleError, build_tieout
from tieout.warehouse import DuckDBWarehouse


@pytest.fixture()
def recon(loaded_warehouse: DuckDBWarehouse, dataset_dir: Path):  # type: ignore[no-untyped-def]
    MatchingEngine(loaded_warehouse, Settings()).run("r1")
    exceptions = exceptions_from_unmatched(
        loaded_warehouse.unmatched_bank("r1"), loaded_warehouse.unmatched_gl("r1")
    )
    bank = load_bank_csv(dataset_dir / "bank_statement.csv")
    gl = load_gl_csv(dataset_dir / "gl_cash.csv")
    amounts = {t.txn_id: t.amount for t in bank.transactions}
    amounts.update({e.entry_id: e.amount for e in gl.entries})
    labels = json.loads((dataset_dir / "labels.json").read_text())
    return {"bank": bank, "gl": gl, "exceptions": exceptions, "amounts": amounts, "labels": labels}


def test_ground_truth_findings_tie_to_zero(recon) -> None:  # type: ignore[no-untyped-def]
    findings = findings_from_labels(recon["labels"], recon["exceptions"])
    t = build_tieout(
        "2026-09",
        recon["bank"].ending_balance,
        recon["gl"].ending_balance,
        recon["exceptions"],
        findings,
        recon["amounts"],
    )
    assert t.status == ReconStatus.TIED
    assert t.difference == Decimal("0.00")
    assert t.adjusted_bank_balance == Decimal(recon["labels"]["expected_adjusted_balance"])
    assert t.unexplained_items == []
    assert len(t.outstanding_checks) == 4 and all(ln.amount < 0 for ln in t.outstanding_checks)
    assert t.total(t.deposits_in_transit) > 0


def test_unreconciled_path_shows_difference(recon) -> None:  # type: ignore[no-untyped-def]
    findings = findings_from_labels(recon["labels"], recon["exceptions"])
    dropped = next(f for f in findings if f.finding.break_type == BreakType.BANK_FEE_UNRECORDED)
    kept = [f for f in findings if f is not dropped]
    t = build_tieout(
        "2026-09",
        recon["bank"].ending_balance,
        recon["gl"].ending_balance,
        recon["exceptions"],
        kept,
        recon["amounts"],
    )
    assert t.status == ReconStatus.UNRECONCILED
    # The fee was not deducted from the book side, so book is higher by the fee amount.
    assert t.difference == recon["amounts"][dropped.item_id]
    assert [e.item_id for e in t.unexplained_items] == [dropped.item_id]


def test_needs_review_items_are_not_counted(recon) -> None:  # type: ignore[no-untyped-def]
    findings = findings_from_labels(recon["labels"], recon["exceptions"])
    target = next(f for f in findings if f.finding.break_type == BreakType.DEPOSIT_IN_TRANSIT)
    exc = next(e for e in recon["exceptions"] if e.item_id == target.item_id)
    swapped = [f for f in findings if f is not target]
    swapped.append(finding(exc, BreakType.DEPOSIT_IN_TRANSIT, status=ReviewStatus.NEEDS_REVIEW))
    t = build_tieout(
        "2026-09",
        recon["bank"].ending_balance,
        recon["gl"].ending_balance,
        recon["exceptions"],
        swapped,
        recon["amounts"],
    )
    assert t.status == ReconStatus.UNRECONCILED
    assert t.difference == -recon["amounts"][target.item_id]
    swapped[-1] = finding(exc, BreakType.DEPOSIT_IN_TRANSIT, status=ReviewStatus.APPROVED)
    t = build_tieout(
        "2026-09",
        recon["bank"].ending_balance,
        recon["gl"].ending_balance,
        recon["exceptions"],
        swapped,
        recon["amounts"],
    )
    assert t.status == ReconStatus.TIED


def test_side_swapped_misclassification_still_ties_but_misstates_balance(recon) -> None:  # type: ignore[no-untyped-def]
    """A tie proves arithmetic, not classification.

    Calling a deposit in transit a GL duplicate moves +x off the bank side and -x onto the book
    side. The difference is unchanged, so the rec still ties, but both adjusted balances are wrong
    by x. This is why evals score adjusted-balance accuracy against ground truth separately.
    """
    findings = findings_from_labels(recon["labels"], recon["exceptions"])
    target = next(f for f in findings if f.finding.break_type == BreakType.DEPOSIT_IN_TRANSIT)
    exc = next(e for e in recon["exceptions"] if e.item_id == target.item_id)
    wrong = [f for f in findings if f is not target] + [finding(exc, BreakType.GL_DUPLICATE)]
    t = build_tieout(
        "2026-09",
        recon["bank"].ending_balance,
        recon["gl"].ending_balance,
        recon["exceptions"],
        wrong,
        recon["amounts"],
    )
    assert t.status == ReconStatus.TIED
    expected = Decimal(recon["labels"]["expected_adjusted_balance"])
    assert expected - t.adjusted_bank_balance == recon["amounts"][target.item_id]


def test_paired_break_without_counterpart_is_rejected(recon) -> None:  # type: ignore[no-untyped-def]
    exc = recon["exceptions"][0]
    with pytest.raises(ScheduleError):
        build_tieout(
            "2026-09",
            Decimal("0"),
            Decimal("0"),
            [exc],
            [finding(exc, BreakType.GL_TRANSPOSITION)],
            recon["amounts"],
        )


def test_journal_entries_balance_and_cover_book_items(recon) -> None:  # type: ignore[no-untyped-def]
    findings = findings_from_labels(recon["labels"], recon["exceptions"])
    gl_by_id = {e.entry_id: e for e in recon["gl"].entries}
    jes = propose_journal_entries(findings, recon["amounts"], gl_by_id, load_accounts())
    book_types = {
        BreakType.BANK_FEE_UNRECORDED,
        BreakType.INTEREST_UNRECORDED,
        BreakType.NSF_RETURN_UNRECORDED,
        BreakType.GL_DUPLICATE,
        BreakType.GL_TRANSPOSITION,
        BreakType.GL_WRONG_AMOUNT,
    }
    assert {je.break_type for je in jes} == book_types
    assert len(jes) == sum(1 for f in findings if f.finding.break_type in book_types)
    for je in jes:
        assert sum(ln.debit for ln in je.lines) == sum(ln.credit for ln in je.lines)
        assert any(ln.account == "1010" for ln in je.lines)
    fee = next(je for je in jes if je.break_type == BreakType.BANK_FEE_UNRECORDED)
    assert fee.lines[0].account == "6110" and fee.lines[0].debit > 0
    interest = next(je for je in jes if je.break_type == BreakType.INTEREST_UNRECORDED)
    assert interest.lines[0].account == "1010" and interest.lines[0].debit > 0
    dup = next(je for je in jes if je.break_type == BreakType.GL_DUPLICATE)
    assert dup.lines[0].account == "1010"  # reversing a duplicate payment puts cash back
    assert {ln.account for ln in dup.lines} == {"1010", "2000"}


def test_unbalanced_je_rejected() -> None:
    with pytest.raises(ValidationError, match="unbalanced"):
        JournalEntryProposal(
            je_id="AJE-1",
            break_type=BreakType.BANK_FEE_UNRECORDED,
            memo="x",
            source_item_ids=["BNK-1"],
            exception_id="EXC-1",
            lines=[
                JELine(account="6110", account_name="Fees", debit="10.00"),
                JELine(account="1010", account_name="Cash", credit="9.99"),
            ],
        )


def test_je_line_must_be_one_sided() -> None:
    with pytest.raises(ValidationError):
        JELine(account="1", account_name="x", debit="1.00", credit="1.00")
    with pytest.raises(ValidationError):
        JELine(account="1", account_name="x")
    with pytest.raises(ValidationError):
        JELine(account="1", account_name="x", debit="-1.00")
