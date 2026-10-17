"""Read-only investigation tools.

Tools see a LedgerView for one run. Every result is small (capped row counts) so the model only ever
sees the exception under investigation plus the slices it asked for, never a full ledger.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from tieout.generator import is_digit_swap
from tieout.models import BankTransaction, BreakType, GLEntry, Side
from tieout.money import MoneyError, money_to_cents, to_money
from tieout.similarity import similarity

MAX_RESULTS = 10


class ToolError(ValueError):
    """Bad tool arguments. Returned to the model as an error payload, never raised past the loop."""


@dataclass
class LedgerView:
    bank: dict[str, BankTransaction]
    gl: dict[str, GLEntry]
    matched: dict[str, str]  # item_id -> match_id
    match_members: dict[str, list[str]]  # match_id -> all item ids in that match
    period_start: date
    period_end: date
    window_days: int
    resolved: dict[str, str] = field(default_factory=dict)  # item_id -> exception_id

    def exists(self, item_id: str) -> bool:
        return item_id in self.bank or item_id in self.gl

    def item(self, item_id: str) -> BankTransaction | GLEntry:
        if item_id in self.bank:
            return self.bank[item_id]
        if item_id in self.gl:
            return self.gl[item_id]
        raise ToolError(f"unknown transaction id: {item_id}")

    def status(self, item_id: str) -> str:
        if item_id in self.matched:
            return "MATCHED"
        if item_id in self.resolved:
            return "RESOLVED"
        return "UNMATCHED"

    def describe(self, item: BankTransaction | GLEntry, **extra: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": item.item_id,
            "ledger": item.side.value,
            "date": item.item_date.isoformat(),
            "description": item.description,
            "amount": format(item.amount, "f"),
            "check_number": item.check_number,
            "reference": item.reference,
            "status": self.status(item.item_id),
        }
        if isinstance(item, GLEntry):
            row["journal"] = item.journal
            row["memo"] = item.memo
        if item.item_id in self.matched:
            mid = self.matched[item.item_id]
            row["match_id"] = mid
            row["matched_with"] = [i for i in self.match_members[mid] if i != item.item_id]
        row.update(extra)
        return row


def _parse_amount(value: Any, name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return abs(to_money(str(value)))
    except MoneyError as exc:
        raise ToolError(f"{name} is not a valid amount: {value!r}") from exc


def _parse_date(value: Any, name: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ToolError(f"{name} must be YYYY-MM-DD, got {value!r}") from exc


def _ledger_arg(value: Any, default: str = "BOTH") -> str:
    v = str(value or default).upper()
    if v not in ("BANK", "GL", "BOTH"):
        raise ToolError("ledger must be BANK, GL, or BOTH")
    return v


# Bank transaction codes a careful reviewer would recognize.
BANK_CODES: list[tuple[re.Pattern[str], str, str, BreakType | None]] = [
    (
        re.compile(r"\b(RETURNED ITEM|RTN ITEM|RETURN ITEM)\b.*\bNSF\b|\bNSF\b"),
        "NSF_RETURN",
        "Customer item returned unpaid for non-sufficient funds; the bank charged it back.",
        BreakType.NSF_RETURN_UNRECORDED,
    ),
    (
        re.compile(
            r"\b(SERVICE CHARGE|SVC CHG|ANALYSIS FEE|WIRE TRANSFER FEE|STOP PAYMENT FEE|"
            r"ORIGINATION FEE|MAINTENANCE FEE|FEE)\b"
        ),
        "BANK_FEE",
        "Fee assessed by the bank.",
        BreakType.BANK_FEE_UNRECORDED,
    ),
    (
        re.compile(r"\bINTEREST\b|\bINT (CREDIT|PAID)\b"),
        "INTEREST",
        "Interest credited by the bank.",
        BreakType.INTEREST_UNRECORDED,
    ),
    (
        re.compile(r"\bACH DEBIT\b"),
        "ACH_DEBIT",
        "Electronic payment initiated by the account holder.",
        None,
    ),
    (re.compile(r"\bACH CREDIT\b"), "ACH_CREDIT", "Electronic receipt from a payer.", None),
    (re.compile(r"\bWIRE IN\b"), "WIRE_IN", "Incoming wire transfer.", None),
    (re.compile(r"\b(CHECK|CHK)\b"), "CHECK_PAID", "Check presented and paid by the bank.", None),
    (re.compile(r"\bDEPOSIT\b"), "DEPOSIT", "Deposit credited to the account.", None),
    (re.compile(r"\bMERCHANT SETTLEMENT\b"), "CARD_SETTLEMENT", "Card processor settlement.", None),
    (re.compile(r"\bPAYROLL\b"), "PAYROLL", "Payroll provider debit.", None),
]
ADJUSTMENT_NOTICE = re.compile(r"^ADJN\d+$")


class ToolBox:
    def __init__(self, view: LedgerView) -> None:
        self.view = view
        self._tools: dict[str, Callable[..., Any]] = {
            "search_ledger": self.search_ledger,
            "get_transaction": self.get_transaction,
            "check_transposition": self.check_transposition,
            "find_duplicates": self.find_duplicates,
            "describe_bank_codes": self.describe_bank_codes,
        }

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run a tool. Errors come back as {"error": ...} so the model can correct itself."""
        fn = self._tools.get(name)
        if fn is None:
            return {"error": f"unknown tool {name!r}; available: {', '.join(self.names)}"}
        try:
            result: dict[str, Any] = fn(**args)
            return result
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}
        except ToolError as exc:
            return {"error": str(exc)}

    # ----- tools -------------------------------------------------------------------------------
    def search_ledger(
        self,
        ledger: str | None = None,
        min_amount: str | None = None,
        max_amount: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        text: str | None = None,
        limit: int = MAX_RESULTS,
    ) -> dict[str, Any]:
        """Filter one or both ledgers by absolute amount range, date range, and description text."""
        which = _ledger_arg(ledger)
        lo, hi = _parse_amount(min_amount, "min_amount"), _parse_amount(max_amount, "max_amount")
        d0, d1 = _parse_date(start_date, "start_date"), _parse_date(end_date, "end_date")
        if lo is None and hi is None and d0 is None and d1 is None and not text:
            raise ToolError("provide at least one filter (amount, date, or text)")
        pool: list[BankTransaction | GLEntry] = []
        if which in ("BANK", "BOTH"):
            pool.extend(self.view.bank.values())
        if which in ("GL", "BOTH"):
            pool.extend(self.view.gl.values())
        hits: list[tuple[float, BankTransaction | GLEntry]] = []
        for item in pool:
            amt = abs(item.amount)
            if (lo is not None and amt < lo) or (hi is not None and amt > hi):
                continue
            if (d0 is not None and item.item_date < d0) or (d1 is not None and item.item_date > d1):
                continue
            score = 1.0
            if text:
                haystack = item.description + " " + getattr(item, "memo", "")
                score = similarity(text, haystack)
                if score < 0.6 and text.lower() not in haystack.lower():
                    continue
            hits.append((score, item))
        hits.sort(key=lambda h: (-h[0], h[1].item_date, h[1].item_id))
        cap = max(1, min(int(limit), MAX_RESULTS))
        results = [self.view.describe(i, text_score=round(s, 3)) for s, i in hits[:cap]]
        return {"count": len(hits), "returned": len(results), "results": results}

    def get_transaction(self, id: str) -> dict[str, Any]:
        """Full detail for one bank or GL line, including match status."""
        return self.view.describe(self.view.item(id))

    def check_transposition(self, amount: str, source_ledger: str = "BANK") -> dict[str, Any]:
        """Unmatched opposite-ledger items whose amount is a two-digit swap of `amount`."""
        src = _ledger_arg(source_ledger, "BANK")
        if src == "BOTH":
            raise ToolError("source_ledger must be BANK or GL")
        try:
            target = to_money(str(amount))
        except MoneyError as exc:
            raise ToolError(f"amount is not valid: {amount!r}") from exc
        cents = money_to_cents(target)
        opposite = self.view.gl if src == "BANK" else self.view.bank
        out = []
        for item in opposite.values():
            if item.item_id in self.view.matched:
                continue
            other = money_to_cents(item.amount)
            if (other < 0) != (cents < 0) or not is_digit_swap(cents, other):
                continue
            diff = abs(cents - other)
            out.append(
                self.view.describe(
                    item,
                    difference=format(to_money(Decimal(diff) / 100), "f"),
                    difference_divisible_by_9=(diff % 9 == 0),
                )
            )
        out.sort(key=lambda r: (r["date"], r["id"]))
        return {
            "amount": format(target, "f"),
            "searched_ledger": "GL" if src == "BANK" else "BANK",
            "candidates": out[:MAX_RESULTS],
        }

    def find_duplicates(self, id: str) -> dict[str, Any]:
        """Other entries in the same ledger with the same amount and same reference or payee."""
        item = self.view.item(id)
        same_ledger: dict[str, Any] = self.view.gl if item.side == Side.GL else self.view.bank
        out = []
        for other in same_ledger.values():
            if other.item_id == item.item_id or other.amount != item.amount:
                continue
            same_ref = bool(item.reference and item.reference == other.reference)
            sim = similarity(item.description, other.description)
            if not same_ref and sim < 0.9:
                continue
            out.append(
                self.view.describe(
                    other,
                    same_reference=same_ref,
                    description_similarity=round(sim, 3),
                    days_apart=abs((other.item_date - item.item_date).days),
                )
            )
        out.sort(key=lambda r: (r["date"], r["id"]))
        return {"id": item.item_id, "duplicates": out[:MAX_RESULTS]}

    def describe_bank_codes(self, description: str, reference: str | None = None) -> dict[str, Any]:
        """Decode bank transaction descriptions and reference prefixes."""
        text = description.upper()
        codes = []
        for pattern, code, meaning, suggests in BANK_CODES:
            if pattern.search(text):
                codes.append(
                    {
                        "code": code,
                        "meaning": meaning,
                        "suggests": suggests.value if suggests else None,
                    }
                )
        ref = (reference or "").strip().upper()
        if ADJUSTMENT_NOTICE.match(ref):
            codes.insert(
                0,
                {
                    "code": "ADJUSTMENT_NOTICE",
                    "meaning": (
                        f"Reference {ref} is a bank adjustment notice: the bank acknowledged "
                        "this item was posted to the account in error and will reverse it."
                    ),
                    "suggests": BreakType.BANK_ERROR.value,
                },
            )
        return {"description": description, "reference": reference, "codes": codes}


def window_dates(view: LedgerView, d: date) -> tuple[str, str]:
    w = timedelta(days=view.window_days)
    return (d - w).isoformat(), (d + w).isoformat()


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "search_ledger",
        "description": "Search the bank and/or GL ledger by absolute amount range, date range, and "
        "description text. Returns at most 10 rows with match status.",
        "parameters": {
            "type": "object",
            "properties": {
                "ledger": {"type": "string", "enum": ["BANK", "GL", "BOTH"]},
                "min_amount": {"type": "string", "description": 'Absolute amount, e.g. "125.00"'},
                "max_amount": {"type": "string", "description": "Absolute amount"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "text": {"type": "string", "description": "Payee or description words"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_transaction",
        "description": "Get full detail for one bank or GL transaction id, including match status.",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "check_transposition",
        "description": "Find unmatched items in the opposite ledger whose amount equals this "
        "amount "
        "with two digits swapped (a transposition error).",
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {"type": "string", "description": 'Signed amount, e.g. "-1253.00"'},
                "source_ledger": {"type": "string", "enum": ["BANK", "GL"]},
            },
            "required": ["amount", "source_ledger"],
        },
    },
    {
        "name": "find_duplicates",
        "description": "Find other entries in the same ledger with the same amount and the same "
        "reference or payee description.",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "describe_bank_codes",
        "description": "Decode a bank description and reference into standard transaction codes "
        "(fees, interest, NSF returns, adjustment notices).",
        "parameters": {
            "type": "object",
            "properties": {"description": {"type": "string"}, "reference": {"type": "string"}},
            "required": ["description"],
        },
    },
    {
        "name": "submit_finding",
        "description": "Submit the final classification. Call exactly once when done.",
        "parameters": {
            "type": "object",
            "properties": {
                "break_type": {"type": "string", "enum": [b.value for b in BreakType]},
                "explanation": {
                    "type": "string",
                    "description": "Plain English, 2 to 4 sentences.",
                },
                "evidence_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Transaction ids that support the finding.",
                },
                "proposed_action": {"type": "string"},
                "confidence": {"type": "number", "description": "0 to 1"},
                "counterpart_id": {
                    "type": "string",
                    "description": "Required for GL_TRANSPOSITION and GL_WRONG_AMOUNT: the id of "
                    "the same transaction in the opposite ledger. Omit otherwise.",
                },
            },
            "required": [
                "break_type",
                "explanation",
                "evidence_ids",
                "proposed_action",
                "confidence",
            ],
        },
    },
]
