"""Deterministic matching engine.

Passes run in order and each item is matched at most once:

1. EXACT: same amount, same date, and the same check number or reference.
2. NEAR: same amount, dates within the window, description similarity at or above the threshold
   (an identical check number or reference counts as similarity 1.0).
3. MANY_TO_ONE: one bank deposit equals the sum of 2..N GL receipts dated within the window on or
   before the deposit. Bounded subset-sum search with a time budget. Ambiguous solutions are left
   unmatched rather than guessed.

Candidates come from set-based SQL in the warehouse; assignment is deterministic Python.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from tieout.config import Settings
from tieout.models import GLEntry, Match, MatchRule
from tieout.money import cents_to_money, money_to_cents
from tieout.similarity import pair_similarity
from tieout.warehouse.base import CandidatePair, Warehouse


@dataclass
class MatchStats:
    exact: int = 0
    near: int = 0
    many_to_one: int = 0
    batch_ambiguous: list[str] = field(default_factory=list)
    batch_budget_exhausted: bool = False
    elapsed_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "exact": self.exact,
            "near": self.near,
            "many_to_one": self.many_to_one,
            "batch_ambiguous": self.batch_ambiguous,
            "batch_budget_exhausted": self.batch_budget_exhausted,
            "elapsed_ms": self.elapsed_ms,
        }


class MatchingEngine:
    def __init__(self, warehouse: Warehouse, settings: Settings) -> None:
        self.wh = warehouse
        self.settings = settings
        self._seq = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"M-{self._seq:05d}"

    def run(self, run_id: str) -> tuple[list[Match], MatchStats]:
        start = time.perf_counter()
        stats = MatchStats()
        exact = self._exact(run_id)
        self.wh.save_matches(run_id, exact)
        near = self._near(run_id)
        self.wh.save_matches(run_id, near)
        batch = self._many_to_one(run_id, stats)
        self.wh.save_matches(run_id, batch)
        stats.exact, stats.near, stats.many_to_one = len(exact), len(near), len(batch)
        stats.elapsed_ms = int((time.perf_counter() - start) * 1000)
        return exact + near + batch, stats

    # ----- pass 1 ------------------------------------------------------------------------------
    def _exact(self, run_id: str) -> list[Match]:
        out: list[Match] = []
        used_b: set[str] = set()
        used_g: set[str] = set()
        for c in self.wh.exact_candidates(run_id):
            if c.bank_id in used_b or c.gl_id in used_g:
                continue
            on_check = bool(c.bank_check and c.bank_check == c.gl_check)
            key_field = "check_number" if on_check else "reference"
            key_value = c.bank_check if on_check else c.bank_reference
            used_b.add(c.bank_id)
            used_g.add(c.gl_id)
            out.append(
                Match(
                    match_id=self._next_id(),
                    rule=MatchRule.EXACT,
                    bank_ids=[c.bank_id],
                    gl_ids=[c.gl_id],
                    amount=c.amount,
                    confidence=1.0,
                    evidence={
                        "matched_on": ["amount", "date", key_field],
                        "amount": format(c.amount, "f"),
                        "date": c.bank_date.isoformat(),
                        key_field: key_value,
                    },
                )
            )
        return out

    # ----- pass 2 ------------------------------------------------------------------------------
    def _near(self, run_id: str) -> list[Match]:
        scored: list[tuple[float, int, str, str, CandidatePair, str]] = []
        for c in self.wh.near_candidates(run_id, self.settings.date_window_days):
            score, basis = pair_similarity(
                c.bank_description,
                c.gl_description,
                c.bank_check,
                c.gl_check,
                c.bank_reference,
                c.gl_reference,
            )
            if score >= self.settings.near_similarity_threshold:
                scored.append((-score, c.day_gap, c.bank_id, c.gl_id, c, basis))
        scored.sort(key=lambda t: (t[0], t[1], t[2], t[3]))
        out: list[Match] = []
        used_b: set[str] = set()
        used_g: set[str] = set()
        window = max(self.settings.date_window_days, 1)
        for neg_score, gap, bank_id, gl_id, c, basis in scored:
            if bank_id in used_b or gl_id in used_g:
                continue
            used_b.add(bank_id)
            used_g.add(gl_id)
            score = -neg_score
            confidence = round(max(0.5, min(0.99, 0.75 + 0.2 * score - 0.05 * gap / window)), 4)
            out.append(
                Match(
                    match_id=self._next_id(),
                    rule=MatchRule.NEAR,
                    bank_ids=[bank_id],
                    gl_ids=[gl_id],
                    amount=c.amount,
                    confidence=confidence,
                    evidence={
                        "matched_on": ["amount", "date_window", basis],
                        "amount": format(c.amount, "f"),
                        "bank_date": c.bank_date.isoformat(),
                        "gl_date": c.gl_date.isoformat(),
                        "day_gap": gap,
                        "similarity": score,
                        "bank_description": c.bank_description,
                        "gl_description": c.gl_description,
                    },
                )
            )
        return out

    # ----- pass 3 ------------------------------------------------------------------------------
    def _many_to_one(self, run_id: str, stats: MatchStats) -> list[Match]:
        deadline = time.perf_counter() + self.settings.batch_time_budget_ms / 1000
        window = timedelta(days=self.settings.date_window_days)
        deposits = [b for b in self.wh.unmatched_bank(run_id) if b.amount > 0]
        receipts = [g for g in self.wh.unmatched_gl(run_id) if g.amount > 0]
        used: set[str] = set()
        out: list[Match] = []
        for dep in deposits:
            if time.perf_counter() > deadline:
                stats.batch_budget_exhausted = True
                break
            pool = [
                g
                for g in receipts
                if g.entry_id not in used
                and dep.txn_date - window <= g.entry_date <= dep.txn_date
                and g.amount < dep.amount
            ]
            solutions = subset_sum(
                pool,
                money_to_cents(dep.amount),
                self.settings.batch_max_items,
                deadline,
            )
            if solutions is None:
                stats.batch_budget_exhausted = True
                break
            if not solutions:
                continue
            best = _pick_unique(solutions, dep.txn_date.toordinal())
            if best is None:
                stats.batch_ambiguous.append(dep.txn_id)
                continue
            used.update(g.entry_id for g in best)
            ids = sorted(g.entry_id for g in best)
            out.append(
                Match(
                    match_id=self._next_id(),
                    rule=MatchRule.MANY_TO_ONE,
                    bank_ids=[dep.txn_id],
                    gl_ids=ids,
                    amount=dep.amount,
                    confidence=0.95 if len(solutions) == 1 else 0.85,
                    evidence={
                        "matched_on": ["sum_of_amounts", "date_window"],
                        "bank_amount": format(dep.amount, "f"),
                        "gl_amounts": {g.entry_id: format(g.amount, "f") for g in best},
                        "gl_sum": format(
                            cents_to_money(sum(money_to_cents(g.amount) for g in best)), "f"
                        ),
                        "bank_date": dep.txn_date.isoformat(),
                        "solutions_found": len(solutions),
                        "pool_size": len(pool),
                    },
                )
            )
        return out


def subset_sum(
    pool: list[GLEntry],
    target_cents: int,
    max_items: int,
    deadline: float,
    limit: int = 3,
) -> list[list[GLEntry]] | None:
    """All combinations of 2..max_items entries summing exactly to target (up to `limit`).

    Integer cents only. Returns None if the time budget runs out mid-search.
    """
    items = sorted(pool, key=lambda g: (-money_to_cents(g.amount), g.entry_id))
    cents = [money_to_cents(g.amount) for g in items]
    suffix = [0] * (len(cents) + 1)
    for i in range(len(cents) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + cents[i]
    found: list[list[GLEntry]] = []
    chosen: list[int] = []
    timed_out = False
    nodes = 0

    def dfs(start: int, remaining: int) -> None:
        nonlocal timed_out, nodes
        if timed_out or len(found) >= limit:
            return
        nodes += 1
        if nodes % 2048 == 0 and time.perf_counter() > deadline:
            timed_out = True
            return
        if remaining == 0:
            if len(chosen) >= 2:
                found.append([items[i] for i in chosen])
            return
        if len(chosen) >= max_items or remaining < 0:
            return
        for i in range(start, len(cents)):
            if suffix[i] < remaining:
                return
            if cents[i] > remaining:
                continue
            chosen.append(i)
            dfs(i + 1, remaining - cents[i])
            chosen.pop()

    dfs(0, target_cents)
    if timed_out:
        return None
    return found


def _pick_unique(solutions: list[list[GLEntry]], bank_ordinal: int) -> list[GLEntry] | None:
    """Prefer the solution whose receipts sit closest to the deposit date; refuse true ties."""
    if len(solutions) == 1:
        return solutions[0]

    def spread(sol: list[GLEntry]) -> int:
        return sum(bank_ordinal - g.entry_date.toordinal() for g in sol)

    ranked = sorted(solutions, key=spread)
    if spread(ranked[0]) == spread(ranked[1]):
        return None
    return ranked[0]
