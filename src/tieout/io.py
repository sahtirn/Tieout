"""CSV loading, sign normalization, and input integrity checks."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from tieout.models import BankTransaction, GLEntry
from tieout.money import ZERO, to_money

BANK_COLUMNS = ["txn_id", "date", "description", "amount", "check_number", "reference", "balance"]
GL_COLUMNS = [
    "entry_id",
    "date",
    "journal",
    "description",
    "memo",
    "reference",
    "check_number",
    "debit",
    "credit",
    "balance",
]


class InputError(ValueError):
    """Raised when an input file is malformed or internally inconsistent."""


@dataclass(frozen=True)
class BankStatement:
    transactions: list[BankTransaction]
    opening_balance: Decimal
    ending_balance: Decimal


@dataclass(frozen=True)
class GLDetail:
    entries: list[GLEntry]
    opening_balance: Decimal
    ending_balance: Decimal


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _rows(path: Path, expected: list[str]) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in expected if c not in (reader.fieldnames or [])]
        if missing:
            raise InputError(f"{path.name}: missing columns {missing}")
        rows = list(reader)
    if not rows:
        raise InputError(f"{path.name}: no data rows")
    return rows


def _opt(value: str | None) -> str | None:
    v = (value or "").strip()
    return v or None


def _check_running_balance(name: str, amounts: list[Decimal], balances: list[Decimal]) -> Decimal:
    """Derive the opening balance and verify every running balance foots."""
    opening = to_money(balances[0] - amounts[0])
    running = opening
    for i, (amt, bal) in enumerate(zip(amounts, balances, strict=True), start=2):
        running = to_money(running + amt)
        if running != bal:
            raise InputError(
                f"{name}: running balance breaks at line {i}: expected {running}, file says {bal}"
            )
    return opening


def load_bank_csv(path: Path) -> BankStatement:
    rows = _rows(path, BANK_COLUMNS)
    txns: list[BankTransaction] = []
    balances: list[Decimal] = []
    seen: set[str] = set()
    for r in rows:
        txn = BankTransaction(
            txn_id=r["txn_id"].strip(),
            txn_date=date.fromisoformat(r["date"].strip()),
            description=r["description"].strip(),
            amount=r["amount"],
            check_number=_opt(r["check_number"]),
            reference=_opt(r["reference"]),
        )
        if txn.txn_id in seen:
            raise InputError(f"{path.name}: duplicate txn_id {txn.txn_id}")
        seen.add(txn.txn_id)
        txns.append(txn)
        balances.append(to_money(r["balance"]))
    opening = _check_running_balance(path.name, [t.amount for t in txns], balances)
    return BankStatement(txns, opening, balances[-1])


def load_gl_csv(path: Path) -> GLDetail:
    rows = _rows(path, GL_COLUMNS)
    entries: list[GLEntry] = []
    balances: list[Decimal] = []
    seen: set[str] = set()
    for r in rows:
        debit = to_money(r["debit"]) if _opt(r["debit"]) else ZERO
        credit = to_money(r["credit"]) if _opt(r["credit"]) else ZERO
        if debit and credit:
            raise InputError(f"{path.name}: {r['entry_id']} has both debit and credit")
        entry = GLEntry(
            entry_id=r["entry_id"].strip(),
            entry_date=date.fromisoformat(r["date"].strip()),
            journal=r["journal"].strip(),
            description=r["description"].strip(),
            memo=(r["memo"] or "").strip(),
            amount=to_money(debit - credit),  # sign normalization: debit to cash is positive
            check_number=_opt(r["check_number"]),
            reference=_opt(r["reference"]),
        )
        if entry.entry_id in seen:
            raise InputError(f"{path.name}: duplicate entry_id {entry.entry_id}")
        seen.add(entry.entry_id)
        entries.append(entry)
        balances.append(to_money(r["balance"]))
    opening = _check_running_balance(path.name, [e.amount for e in entries], balances)
    return GLDetail(entries, opening, balances[-1])
