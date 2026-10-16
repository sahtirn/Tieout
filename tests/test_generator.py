from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tieout.generator import (
    DEFAULT_BREAKS,
    generate,
    is_digit_swap,
    parse_break_overrides,
    write_dataset,
)
from tieout.io import load_bank_csv, load_gl_csv
from tieout.models import BreakType
from tieout.money import ZERO, money_to_cents, to_money


def test_same_seed_is_byte_identical(tmp_path: Path) -> None:
    a = write_dataset(generate(7, "2026-09"), tmp_path / "a")
    b = write_dataset(generate(7, "2026-09"), tmp_path / "b")
    for key in ("bank", "gl", "labels"):
        assert a[key].read_bytes() == b[key].read_bytes()


def test_different_seeds_differ(tmp_path: Path) -> None:
    a = write_dataset(generate(1, "2026-09"), tmp_path / "a")
    b = write_dataset(generate(2, "2026-09"), tmp_path / "b")
    assert a["bank"].read_bytes() != b["bank"].read_bytes()


def test_volumes_and_break_counts(dataset_dir: Path) -> None:
    bank = load_bank_csv(dataset_dir / "bank_statement.csv")
    gl = load_gl_csv(dataset_dir / "gl_cash.csv")
    assert 370 <= len(bank.transactions) <= 430
    assert 390 <= len(gl.entries) <= 460
    labels = json.loads((dataset_dir / "labels.json").read_text())
    counts: dict[str, int] = {}
    for br in labels["breaks"]:
        counts[br["break_type"]] = counts.get(br["break_type"], 0) + 1
    assert counts == {bt.value: n for bt, n in DEFAULT_BREAKS.items() if n}


@pytest.mark.parametrize("seed", [42, 3, 11, 19])
def test_ground_truth_ties_to_zero(tmp_path: Path, seed: int) -> None:
    paths = write_dataset(generate(seed, "2026-09"), tmp_path)
    bank = load_bank_csv(paths["bank"])
    gl = load_gl_csv(paths["gl"])
    labels = json.loads(paths["labels"].read_text())
    assert bank.opening_balance == gl.opening_balance == Decimal(labels["opening_balance"])
    bank_adj = sum(
        (Decimal(b["adjustment"]) for b in labels["breaks"] if b["adjusts"] == "BANK"), ZERO
    )
    book_adj = sum(
        (Decimal(b["adjustment"]) for b in labels["breaks"] if b["adjusts"] == "BOOK"), ZERO
    )
    adjusted_bank = to_money(bank.ending_balance + bank_adj)
    adjusted_book = to_money(gl.ending_balance + book_adj)
    assert adjusted_bank - adjusted_book == Decimal("0.00")
    assert adjusted_bank == Decimal(labels["expected_adjusted_balance"])


def test_planted_breaks_carry_evidence(dataset_dir: Path) -> None:
    bank = {t.txn_id: t for t in load_bank_csv(dataset_dir / "bank_statement.csv").transactions}
    gl = {e.entry_id: e for e in load_gl_csv(dataset_dir / "gl_cash.csv").entries}
    labels = json.loads((dataset_dir / "labels.json").read_text())
    for br in labels["breaks"]:
        bt = BreakType(br["break_type"])
        b = [bank[i] for i in br["bank_ids"]]
        g = [gl[i] for i in br["gl_ids"]]
        if bt == BreakType.GL_TRANSPOSITION:
            assert is_digit_swap(money_to_cents(b[0].amount), money_to_cents(g[0].amount))
        elif bt == BreakType.GL_WRONG_AMOUNT:
            assert f"${abs(b[0].amount):,.2f}" in g[0].memo
            assert not is_digit_swap(money_to_cents(b[0].amount), money_to_cents(g[0].amount))
        elif bt == BreakType.BANK_ERROR:
            assert (b[0].reference or "").startswith("ADJN")
        elif bt == BreakType.NSF_RETURN_UNRECORDED:
            chk = b[0].description.split("CHK ")[1].split()[0]
            originals = [e for e in gl.values() if e.amount == -b[0].amount]
            assert len(originals) == 1 and f"check {chk}" in originals[0].description
        elif bt == BreakType.GL_DUPLICATE:
            twins = [
                e for e in gl.values() if e.amount == g[0].amount and e.reference == g[0].reference
            ]
            assert len(twins) == 2
        elif bt == BreakType.OUTSTANDING_CHECK:
            assert g[0].check_number and g[0].amount < 0
        elif bt == BreakType.DEPOSIT_IN_TRANSIT:
            assert g[0].amount > 0


def test_break_overrides() -> None:
    counts = parse_break_overrides("outstanding_check=6, BANK_ERROR=0")
    assert counts[BreakType.OUTSTANDING_CHECK] == 6
    assert counts[BreakType.BANK_ERROR] == 0
    assert counts[BreakType.GL_DUPLICATE] == DEFAULT_BREAKS[BreakType.GL_DUPLICATE]
    with pytest.raises(ValueError):
        parse_break_overrides("GL_DUPLICATE=99")
    ds = generate(5, "2026-09", counts)
    assert sum(1 for b in ds.labels["breaks"] if b["break_type"] == "OUTSTANDING_CHECK") == 6


def test_digit_swap_helper() -> None:
    assert is_digit_swap(125300, 123500)
    assert not is_digit_swap(125300, 125300)
    assert not is_digit_swap(125300, 125311)
