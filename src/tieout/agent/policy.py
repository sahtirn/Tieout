"""Deterministic investigation policy.

One rule set, used two ways:
* MockProvider replays it inside the real tool-calling loop (same messages, tools, and schema),
  so CI and evals exercise the full agent path without an API key.
* The fallback classifier runs it directly against the tools when an LLM answer fails validation.

The policy decides the next tool call from what it has observed so far, then a final finding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from tieout.models import BreakType
from tieout.money import to_money

NEAR_SIMILARITY = 0.78


@dataclass(frozen=True)
class ExceptionContext:
    """Everything the investigator is told up front. Deliberately minimal (data minimization)."""

    exception_id: str
    ledger: str  # BANK or GL
    item_id: str
    item_date: date
    amount: Decimal
    description: str
    period_start: date
    period_end: date
    window_days: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "exception_id": self.exception_id,
            "ledger": self.ledger,
            "item_id": self.item_id,
            "date": self.item_date.isoformat(),
            "amount": format(self.amount, "f"),
            "description": self.description,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "date_window_days": self.window_days,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExceptionContext:
        return cls(
            exception_id=d["exception_id"],
            ledger=d["ledger"],
            item_id=d["item_id"],
            item_date=date.fromisoformat(d["date"]),
            amount=to_money(d["amount"]),
            description=d["description"],
            period_start=date.fromisoformat(d["period_start"]),
            period_end=date.fromisoformat(d["period_end"]),
            window_days=d["date_window_days"],
        )


@dataclass(frozen=True)
class Observation:
    tool: str
    args: dict[str, Any]
    result: dict[str, Any]


@dataclass(frozen=True)
class NextCall:
    tool: str
    args: dict[str, Any]


Decision = NextCall | dict[str, Any]  # dict = submit_finding arguments


def _seen(obs: list[Observation], tool: str) -> Observation | None:
    for o in obs:
        if o.tool == tool:
            return o
    return None


def _final(
    bt: BreakType,
    explanation: str,
    evidence: list[str],
    action: str,
    confidence: float,
    counterpart: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "break_type": bt.value,
        "explanation": explanation,
        "evidence_ids": evidence,
        "proposed_action": action,
        "confidence": confidence,
    }
    if counterpart:
        out["counterpart_id"] = counterpart
    return out


def _money(s: str) -> str:
    return f"{abs(to_money(s)):,.2f}"


def decide(ctx: ExceptionContext, obs: list[Observation]) -> Decision:
    return _decide_bank(ctx, obs) if ctx.ledger == "BANK" else _decide_gl(ctx, obs)


def _window(ctx: ExceptionContext) -> tuple[str, str]:
    w = timedelta(days=ctx.window_days)
    return (ctx.item_date - w).isoformat(), (ctx.item_date + w).isoformat()


def _decide_bank(ctx: ExceptionContext, obs: list[Observation]) -> Decision:
    detail = _seen(obs, "get_transaction")
    if detail is None:
        return NextCall("get_transaction", {"id": ctx.item_id})
    item = detail.result
    codes_obs = _seen(obs, "describe_bank_codes")
    if codes_obs is None:
        return NextCall(
            "describe_bank_codes",
            {"description": item["description"], "reference": item.get("reference") or ""},
        )
    suggestions = [c["suggests"] for c in codes_obs.result.get("codes", []) if c.get("suggests")]
    amt = _money(item["amount"])
    iid = ctx.item_id
    if suggestions:
        bt = BreakType(suggestions[0])
        if bt == BreakType.BANK_ERROR:
            ref = item.get("reference")
            return _final(
                bt,
                f"Bank line {iid} for {amt} carries bank adjustment notice {ref}. The notice "
                "confirms the bank posted this item to our account in error. No GL entry is "
                "needed.",
                [iid],
                "Show as a bank-side reconciling item until the bank reverses it next "
                "cycle; obtain a copy of the adjustment notice for the file.",
                0.93,
            )
        if bt == BreakType.BANK_FEE_UNRECORDED:
            return _final(
                bt,
                f"Bank line {iid} is a bank fee of {amt} ({item['description']}). Fees are "
                "charged by the bank and are not yet recorded in the GL.",
                [iid],
                "Record the fee to bank service charge expense with a credit to cash.",
                0.95,
            )
        if bt == BreakType.INTEREST_UNRECORDED:
            return _final(
                bt,
                f"Bank line {iid} is interest of {amt} credited by the bank. Interest is "
                "known only from the statement, so it is not yet in the GL.",
                [iid],
                "Record interest income with a debit to cash.",
                0.95,
            )
        # NSF: confirm against the original deposit in the GL.
        search = _seen(obs, "search_ledger")
        if search is None:
            return NextCall(
                "search_ledger",
                {
                    "ledger": "GL",
                    "min_amount": item["amount"],
                    "max_amount": item["amount"],
                    "end_date": ctx.item_date.isoformat(),
                },
            )
        originals = [
            r
            for r in search.result.get("results", [])
            if to_money(r["amount"]) == -to_money(item["amount"])
        ]
        evidence = [iid] + [r["id"] for r in originals[:1]]
        orig_note = (
            f" It reverses customer receipt {originals[0]['id']} deposited on "
            f"{originals[0]['date']}."
            if originals
            else ""
        )
        return _final(
            bt,
            f"Bank line {iid} is a returned item for non-sufficient funds of {amt}.{orig_note} "
            "The GL still shows the receipt as collected.",
            evidence,
            "Reinstate the customer receivable with a credit to cash and contact the customer.",
            0.95 if originals else 0.82,
        )

    trans = _seen(obs, "check_transposition")
    if trans is None:
        return NextCall("check_transposition", {"amount": item["amount"], "source_ledger": "BANK"})
    cands = _transposition_candidates(item, trans.result.get("candidates", []))
    if len(cands) == 1:
        c = cands[0]
        return _final(
            BreakType.GL_TRANSPOSITION,
            f"Bank cleared {iid} for {amt}, but the GL recorded {c['id']} for "
            f"{_money(c['amount'])}. "
            f"The amounts differ by two swapped digits (difference {c['difference']}, divisible by "
            "9) and the payee and identifiers agree.",
            [iid, c["id"]],
            "Correct the GL entry to the bank-cleared amount.",
            0.92,
            counterpart=c["id"],
        )

    search = _seen(obs, "search_ledger")
    if search is None:
        start, end = _window(ctx)
        return NextCall(
            "search_ledger",
            {"ledger": "GL", "start_date": start, "end_date": end, "text": item["description"]},
        )
    sign_neg = to_money(item["amount"]) < 0
    wrong = [
        r
        for r in search.result.get("results", [])
        if r["status"] == "UNMATCHED"
        and (to_money(r["amount"]) < 0) == sign_neg
        and f"${amt}" in (r.get("memo") or "")
    ]
    if len(wrong) == 1:
        w = wrong[0]
        return _final(
            BreakType.GL_WRONG_AMOUNT,
            f"Bank cleared {iid} for {amt}, and GL entry {w['id']} for the same payee shows an "
            f"invoice amount of {amt} in its memo but was booked at {_money(w['amount'])}. The "
            "GL amount was keyed incorrectly.",
            [iid, w["id"]],
            "Correct the GL entry to the invoice and bank-cleared amount.",
            0.9,
            counterpart=w["id"],
        )
    return _final(
        BreakType.UNIDENTIFIED,
        f"Bank line {iid} for {amt} has no GL counterpart and no recognizable bank code. The "
        "cause could not be determined from the available records.",
        [iid],
        "Escalate to a reviewer and request supporting documents from the bank.",
        0.0,
    )


def _transposition_candidates(
    item: dict[str, Any], cands: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep digit-swap candidates that share an identifier or payee with the item."""
    from tieout.similarity import similarity

    keep = []
    for c in cands:
        if c.get("status") != "UNMATCHED":
            continue
        same_check = bool(
            item.get("check_number") and item["check_number"] == c.get("check_number")
        )
        same_ref = bool(item.get("reference") and item["reference"] == c.get("reference"))
        same_payee = similarity(item["description"], c["description"]) >= NEAR_SIMILARITY
        if same_check or same_ref or same_payee:
            keep.append(c)
    return keep


def _decide_gl(ctx: ExceptionContext, obs: list[Observation]) -> Decision:
    detail = _seen(obs, "get_transaction")
    if detail is None:
        return NextCall("get_transaction", {"id": ctx.item_id})
    item = detail.result
    iid = ctx.item_id
    amt = _money(item["amount"])
    dups = _seen(obs, "find_duplicates")
    if dups is None:
        return NextCall("find_duplicates", {"id": iid})
    twins = [
        d
        for d in dups.result.get("duplicates", [])
        if d["status"] == "MATCHED" and d.get("same_reference")
    ]
    if twins:
        t = twins[0]
        cleared = t.get("matched_with", [])
        return _final(
            BreakType.GL_DUPLICATE,
            f"GL entry {iid} for {amt} repeats {t['id']} with the same reference and payee. Only "
            f"one payment cleared the bank ({', '.join(cleared) or 'matched'}), so {iid} is a "
            "duplicate posting.",
            [iid, t["id"], *cleared],
            "Reverse the duplicate GL entry.",
            0.95,
        )

    trans = _seen(obs, "check_transposition")
    if trans is None:
        return NextCall("check_transposition", {"amount": item["amount"], "source_ledger": "GL"})
    cands = _transposition_candidates(item, trans.result.get("candidates", []))
    if len(cands) == 1:
        c = cands[0]
        return _final(
            BreakType.GL_TRANSPOSITION,
            f"GL entry {iid} records {amt}, but bank line {c['id']} cleared {_money(c['amount'])} "
            f"for the same payment. The amounts differ by two swapped digits (difference "
            f"{c['difference']}).",
            [iid, c["id"]],
            "Correct the GL entry to the bank-cleared amount.",
            0.92,
            counterpart=c["id"],
        )

    search = _seen(obs, "search_ledger")
    if search is None:
        start, end = _window(ctx)
        return NextCall(
            "search_ledger",
            {"ledger": "BANK", "start_date": start, "end_date": end, "text": item["description"]},
        )
    sign_neg = to_money(item["amount"]) < 0
    memo = item.get("memo") or ""
    wrong = [
        r
        for r in search.result.get("results", [])
        if r["status"] == "UNMATCHED"
        and (to_money(r["amount"]) < 0) == sign_neg
        and f"${_money(r['amount'])}" in memo
    ]
    if len(wrong) == 1:
        w = wrong[0]
        return _final(
            BreakType.GL_WRONG_AMOUNT,
            f"GL entry {iid} was booked at {amt}, but its memo shows an invoice amount of "
            f"{_money(w['amount'])}. Bank line {w['id']} cleared exactly the invoice amount "
            "for the same payee.",
            [iid, w["id"]],
            "Correct the GL entry to the invoice and bank-cleared amount.",
            0.9,
            counterpart=w["id"],
        )

    late = ctx.item_date >= ctx.period_end - timedelta(days=ctx.window_days)
    value = to_money(item["amount"])
    if value > 0:
        return _final(
            BreakType.DEPOSIT_IN_TRANSIT,
            f"GL receipt {iid} for {amt} is dated {item['date']}, "
            f"{'at' if late else 'before'} period end, and no matching bank deposit exists. "
            "It is a deposit in transit that should clear early next month.",
            [iid],
            "Show as a deposit in transit and confirm it clears on the next statement.",
            0.92 if late else 0.6,
        )
    if item.get("check_number"):
        return _final(
            BreakType.OUTSTANDING_CHECK,
            f"Check {item['check_number']} ({iid}) for {amt} was issued on {item['date']} and has "
            "not cleared the bank. It is an outstanding check.",
            [iid],
            "Show as an outstanding check and follow up if it remains uncleared after 90 days.",
            0.92,
        )
    return _final(
        BreakType.UNIDENTIFIED,
        f"GL entry {iid} for {amt} has no bank counterpart and no duplicate or check evidence. "
        "The cause could not be determined from the available records.",
        [iid],
        "Escalate to a reviewer with the supporting invoice.",
        0.0,
    )
