"""Maker-checker review: human approval or rejection of NEEDS_REVIEW items and proposed JEs.

Review actions are append-only. Current state is derived by replaying them, so the audit trail
always shows who decided what and when, including changed minds.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from tieout.models import JEStatus, JournalEntryProposal, ResolvedFinding, ReviewStatus
from tieout.warehouse.base import ReviewAction, Warehouse

DECISIONS = ("APPROVE", "REJECT")


class ReviewError(ValueError):
    """Raised when a review action violates a control."""


def apply_reviews(
    findings: list[ResolvedFinding],
    journal_entries: list[JournalEntryProposal],
    actions: list[ReviewAction],
) -> tuple[list[ResolvedFinding], list[JournalEntryProposal]]:
    latest: dict[tuple[str, str], ReviewAction] = {}
    for a in actions:
        latest[(a.target_type, a.target_id)] = a
    new_findings = []
    for f in findings:
        fa = latest.get(("EXCEPTION", f.exception_id))
        if fa is None:
            new_findings.append(f)
            continue
        fs = ReviewStatus.APPROVED if fa.decision == "APPROVE" else ReviewStatus.REJECTED
        new_findings.append(f.model_copy(update={"review_status": fs}))
    rejected = {f.exception_id for f in new_findings if f.review_status == ReviewStatus.REJECTED}
    new_jes = []
    for je in journal_entries:
        ja = latest.get(("JE", je.je_id))
        js: JEStatus
        if ja is not None:
            js = JEStatus.APPROVED if ja.decision == "APPROVE" else JEStatus.REJECTED
        elif je.exception_id in rejected:
            js = JEStatus.REJECTED
        else:
            js = JEStatus.PENDING_APPROVAL
        new_jes.append(je.model_copy(update={"status": js}))
    return new_findings, new_jes


def record_review(
    wh: Warehouse,
    run_id: str,
    target_type: str,
    target_id: str,
    reviewer: str,
    decision: str,
    note: str,
) -> ReviewAction:
    run = wh.get_run(run_id)
    if run is None:
        raise ReviewError(f"run {run_id} not found")
    decision = decision.upper()
    if decision not in DECISIONS:
        raise ReviewError(f"decision must be one of {DECISIONS}")
    reviewer = reviewer.strip()
    if not reviewer:
        raise ReviewError("reviewer name is required")
    preparer = str(run.metadata.get("preparer", ""))
    if reviewer.lower() == preparer.lower():
        raise ReviewError(
            f"maker-checker violation: {reviewer} prepared this run and cannot review it"
        )
    target_type = target_type.upper()
    if target_type == "EXCEPTION":
        findings = {f.exception_id: f for f in wh.get_findings(run_id)}
        f = findings.get(target_id)
        if f is None:
            raise ReviewError(f"exception {target_id} has no finding in run {run_id}")
        if decision == "APPROVE" and f.finding.break_type.value == "UNIDENTIFIED":
            raise ReviewError(
                "an UNIDENTIFIED item cannot be approved; reject it and resolve it manually"
            )
    elif target_type == "JE":
        if target_id not in {je.je_id for je in wh.get_journal_entries(run_id)}:
            raise ReviewError(f"journal entry {target_id} not found in run {run_id}")
    else:
        raise ReviewError("target type must be EXCEPTION or JE")
    if decision == "REJECT" and not note.strip():
        raise ReviewError("a rejection requires a note explaining why")
    action = ReviewAction(
        action_id=str(uuid.uuid4()),
        run_id=run_id,
        target_type=target_type,
        target_id=target_id,
        reviewer=reviewer,
        decision=decision,
        note=note.strip(),
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    wh.add_review_action(action)
    return action


def pending_items(
    findings: list[ResolvedFinding], journal_entries: list[JournalEntryProposal]
) -> tuple[list[ResolvedFinding], list[JournalEntryProposal]]:
    return (
        [f for f in findings if f.review_status == ReviewStatus.NEEDS_REVIEW],
        [je for je in journal_entries if je.status == JEStatus.PENDING_APPROVAL],
    )
