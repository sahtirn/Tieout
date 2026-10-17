from __future__ import annotations

from pathlib import Path

import pytest

from tieout.io import InputError, load_bank_csv, load_gl_csv, sha256_file


def test_gl_sign_normalization(dataset_dir: Path) -> None:
    gl = load_gl_csv(dataset_dir / "gl_cash.csv")
    receipts = [e for e in gl.entries if e.journal == "CR"]
    payments = [e for e in gl.entries if e.journal in ("AP", "CD", "PR")]
    assert all(e.amount > 0 for e in receipts)
    assert all(e.amount < 0 for e in payments)


def test_running_balance_break_detected(tmp_path: Path, dataset_dir: Path) -> None:
    text = (dataset_dir / "bank_statement.csv").read_text().splitlines()
    parts = text[5].split(",")
    parts[-1] = "1.00"
    text[5] = ",".join(parts)
    bad = tmp_path / "bad.csv"
    bad.write_text("\n".join(text) + "\n")
    with pytest.raises(InputError, match="running balance"):
        load_bank_csv(bad)


def test_missing_columns_and_empty(tmp_path: Path) -> None:
    p = tmp_path / "x.csv"
    p.write_text("txn_id,date\n")
    with pytest.raises(InputError, match="missing columns"):
        load_bank_csv(p)
    p.write_text("txn_id,date,description,amount,check_number,reference,balance\n")
    with pytest.raises(InputError, match="no data rows"):
        load_bank_csv(p)


def test_duplicate_ids_and_double_sided_lines(tmp_path: Path) -> None:
    p = tmp_path / "gl.csv"
    header = "entry_id,date,journal,description,memo,reference,check_number,debit,credit,balance\n"
    p.write_text(header + "GL-1,2026-09-01,GJ,x,,,,5.00,5.00,0.00\n")
    with pytest.raises(InputError, match="both debit and credit"):
        load_gl_csv(p)
    p.write_text(
        header + "GL-1,2026-09-01,GJ,x,,,,5.00,,5.00\nGL-1,2026-09-01,GJ,x,,,,5.00,,10.00\n"
    )
    with pytest.raises(InputError, match="duplicate entry_id"):
        load_gl_csv(p)
    b = tmp_path / "b.csv"
    b.write_text(
        "txn_id,date,description,amount,check_number,reference,balance\n"
        "B1,2026-09-01,x,5.00,,,5.00\nB1,2026-09-01,x,5.00,,,10.00\n"
    )
    with pytest.raises(InputError, match="duplicate txn_id"):
        load_bank_csv(b)


def test_hash_is_stable(dataset_dir: Path) -> None:
    p = dataset_dir / "labels.json"
    assert sha256_file(p) == sha256_file(p)
    assert len(sha256_file(p)) == 64
