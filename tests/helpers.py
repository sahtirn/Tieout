"""Shared test helpers."""

from __future__ import annotations

from typing import Any

from tieout.models import (
    AgentFinding,
    BreakType,
    FindingSource,
    ReconException,
    ResolvedFinding,
    ReviewStatus,
)


def finding(
    exc: ReconException,
    bt: BreakType,
    counterpart: str | None = None,
    status: ReviewStatus = ReviewStatus.AUTO_ACCEPTED,
) -> ResolvedFinding:
    return ResolvedFinding(
        exception_id=exc.exception_id,
        item_id=exc.item_id,
        side=exc.side,
        finding=AgentFinding(
            break_type=bt,
            explanation="Ground truth classification for testing. It is used by the test suite.",
            evidence_ids=[exc.item_id] + ([counterpart] if counterpart else []),
            proposed_action="No action, test fixture.",
            confidence=0.99,
            counterpart_id=counterpart,
        ),
        source=FindingSource.AGENT,
        review_status=status,
        prompt_version="test",
    )


def findings_from_labels(
    labels: dict[str, Any], exceptions: list[ReconException]
) -> list[ResolvedFinding]:
    """Perfect classifier: one finding per labeled break, attached to its first exception."""
    by_item = {e.item_id: e for e in exceptions}
    out = []
    for br in labels["breaks"]:
        bt = BreakType(br["break_type"])
        ids = br["bank_ids"] + br["gl_ids"]
        exc = by_item[ids[0]]
        counterpart = ids[1] if len(ids) > 1 else None
        out.append(finding(exc, bt, counterpart))
    return out
