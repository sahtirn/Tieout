from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from tieout.agent.investigator import Investigator, _parse_text, load_prompt
from tieout.agent.policy import ExceptionContext
from tieout.agent.providers import MockProvider, make_provider
from tieout.agent.providers.base import (
    LLMProvider,
    Message,
    ProviderError,
    ProviderResponse,
    ToolCall,
)
from tieout.agent.tools import LedgerView, ToolBox
from tieout.agent.validation import FindingValidationError, validate_finding
from tieout.config import Settings, load_pricing
from tieout.io import load_bank_csv, load_gl_csv
from tieout.matching import MatchingEngine, exceptions_from_unmatched
from tieout.models import BreakType, FindingSource, ReconException, ReviewStatus
from tieout.pipeline import reconcile
from tieout.warehouse import DuckDBWarehouse


class Env:
    def __init__(self, dataset_dir: Path) -> None:
        self.wh = DuckDBWarehouse()
        bank = load_bank_csv(dataset_dir / "bank_statement.csv").transactions
        gl = load_gl_csv(dataset_dir / "gl_cash.csv").entries
        self.wh.load_bank("r1", bank)
        self.wh.load_gl("r1", gl)
        matches, _ = MatchingEngine(self.wh, Settings()).run("r1")
        self.exceptions = exceptions_from_unmatched(
            self.wh.unmatched_bank("r1"), self.wh.unmatched_gl("r1")
        )
        self.wh.save_exceptions("r1", self.exceptions)
        matched: dict[str, str] = {}
        members: dict[str, list[str]] = {}
        for m in matches:
            members[m.match_id] = [*m.bank_ids, *m.gl_ids]
            for i in members[m.match_id]:
                matched[i] = m.match_id
        self.view = LedgerView(
            bank={t.txn_id: t for t in bank},
            gl={e.entry_id: e for e in gl},
            matched=matched,
            match_members=members,
            period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 30),
            window_days=3,
        )
        self.labels = json.loads((dataset_dir / "labels.json").read_text())
        self.truth = {
            i: br["break_type"]
            for br in self.labels["breaks"]
            for i in br["bank_ids"] + br["gl_ids"]
        }

    def exc(self, bt: BreakType, side: str | None = None) -> ReconException:
        for e in self.exceptions:
            if self.truth.get(e.item_id) == bt.value and (side is None or e.side.value == side):
                return e
        raise LookupError(bt)

    def investigator(self, provider: LLMProvider, **settings: Any) -> Investigator:
        return Investigator(
            self.wh, Settings(**settings), provider, self.view, load_pricing(), "r1"
        )

    def ctx(self, e: ReconException) -> ExceptionContext:
        return self.investigator(MockProvider()).context_for(e)


@pytest.fixture()
def env(dataset_dir: Path) -> Env:
    return Env(dataset_dir)


class ScriptedProvider(LLMProvider):
    """Returns pre-scripted responses; a callable entry receives the messages."""

    name = "scripted"
    model = "scripted-1"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.seen: list[list[Message]] = []

    def complete(
        self, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> ProviderResponse:
        self.seen.append(list(messages))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(messages)
        if isinstance(item, ProviderResponse):
            return item
        return ProviderResponse(
            tool_call=ToolCall(*item), text=None, input_tokens=100, output_tokens=20
        )


def submit(**kw: Any) -> tuple[str, dict[str, Any]]:
    base = {
        "explanation": "This is a test explanation. It has two sentences.",
        "proposed_action": "Record the fee.",
        "confidence": 0.95,
    }
    base.update(kw)
    return ("submit_finding", base)


# ----- tools ---------------------------------------------------------------------------------
def test_tools_basic(env: Env) -> None:
    tb = ToolBox(env.view)
    fee = env.exc(BreakType.BANK_FEE_UNRECORDED)
    got = tb.execute("get_transaction", {"id": fee.item_id})
    assert got["status"] == "UNMATCHED" and got["ledger"] == "BANK"
    assert tb.execute("get_transaction", {"id": "NOPE-1"})["error"].startswith("unknown")
    assert "unknown tool" in tb.execute("drop_tables", {})["error"]
    assert "bad arguments" in tb.execute("get_transaction", {"wrong": 1})["error"]
    codes = tb.execute("describe_bank_codes", {"description": fee.description})
    assert codes["codes"][0]["suggests"] == "BANK_FEE_UNRECORDED"


def test_search_ledger_filters_and_caps(env: Env) -> None:
    tb = ToolBox(env.view)
    res = tb.execute(
        "search_ledger", {"ledger": "GL", "start_date": "2026-09-01", "end_date": "2026-09-30"}
    )
    assert res["returned"] == 10 and res["count"] > 10  # data minimization cap
    assert "error" in tb.execute("search_ledger", {})
    assert "error" in tb.execute("search_ledger", {"min_amount": "abc"})
    assert "error" in tb.execute("search_ledger", {"start_date": "09/01/2026"})
    assert "error" in tb.execute("search_ledger", {"ledger": "ALL", "text": "x"})
    one = next(iter(env.view.gl.values()))
    hit = tb.execute(
        "search_ledger", {"min_amount": str(abs(one.amount)), "max_amount": str(abs(one.amount))}
    )
    assert one.entry_id in [r["id"] for r in hit["results"]]


def test_transposition_and_duplicate_tools(env: Env) -> None:
    tb = ToolBox(env.view)
    tr = env.exc(BreakType.GL_TRANSPOSITION, "BANK")
    res = tb.execute("check_transposition", {"amount": str(tr.amount), "source_ledger": "BANK"})
    assert res["candidates"] and res["candidates"][0]["difference_divisible_by_9"]
    assert "error" in tb.execute("check_transposition", {"amount": "x", "source_ledger": "BANK"})
    assert "error" in tb.execute("check_transposition", {"amount": "1", "source_ledger": "BOTH"})
    dup = env.exc(BreakType.GL_DUPLICATE)
    twins = tb.execute("find_duplicates", {"id": dup.item_id})["duplicates"]
    assert twins and twins[0]["status"] == "MATCHED" and twins[0]["same_reference"]
    err = env.exc(BreakType.BANK_ERROR)
    ref = env.view.bank[err.item_id].reference
    codes = tb.execute("describe_bank_codes", {"description": err.description, "reference": ref})
    assert codes["codes"][0]["code"] == "ADJUSTMENT_NOTICE"


# ----- validation ----------------------------------------------------------------------------
def test_hallucinated_evidence_id_rejected(env: Env) -> None:
    fee = env.exc(BreakType.BANK_FEE_UNRECORDED)
    payload = submit(break_type="BANK_FEE_UNRECORDED", evidence_ids=[fee.item_id, "BNK-99999"])[1]
    with pytest.raises(FindingValidationError, match="BNK-99999"):
        validate_finding(payload, env.ctx(fee), env.view)
    payload = submit(
        break_type="BANK_FEE_UNRECORDED",
        evidence_ids=[fee.item_id],
        explanation="The fee matches GL-88888 exactly. It is a bank fee.",
    )[1]
    with pytest.raises(FindingValidationError, match="GL-88888"):
        validate_finding(payload, env.ctx(fee), env.view)


@pytest.mark.parametrize(
    ("truth", "side", "claimed", "extra", "match"),
    [
        (BreakType.BANK_FEE_UNRECORDED, None, "DEPOSIT_IN_TRANSIT", {}, "applies to GL"),
        (BreakType.BANK_FEE_UNRECORDED, None, "INTEREST_UNRECORDED", {}, "inflow"),
        (BreakType.DEPOSIT_IN_TRANSIT, None, "OUTSTANDING_CHECK", {}, "outflow"),
        (BreakType.GL_TRANSPOSITION, "BANK", "GL_TRANSPOSITION", {}, "requires counterpart"),
        (
            BreakType.GL_WRONG_AMOUNT,
            "BANK",
            "GL_TRANSPOSITION",
            {"wrong_cp": True},
            "not a two-digit",
        ),
        (
            BreakType.BANK_FEE_UNRECORDED,
            None,
            "BANK_FEE_UNRECORDED",
            {"cp_self": True},
            "only allowed",
        ),
        (BreakType.DEPOSIT_IN_TRANSIT, None, "GL_DUPLICATE", {}, "no GL entry duplicates"),
    ],
)
def test_deterministic_controls(
    env: Env, truth: BreakType, side: str | None, claimed: str, extra: dict[str, bool], match: str
) -> None:
    e = env.exc(truth, side)
    kw: dict[str, Any] = {"break_type": claimed, "evidence_ids": [e.item_id]}
    if extra.get("wrong_cp"):
        br = next(b for b in env.labels["breaks"] if e.item_id in b["bank_ids"])
        kw["counterpart_id"] = br["gl_ids"][0]
    if extra.get("cp_self"):
        kw["counterpart_id"] = e.item_id
    with pytest.raises(FindingValidationError, match=match):
        validate_finding(submit(**kw)[1], env.ctx(e), env.view)


def test_counterpart_must_be_opposite_and_unmatched(env: Env) -> None:
    e = env.exc(BreakType.GL_TRANSPOSITION, "BANK")
    matched_bank = next(i for i in env.view.matched if i.startswith("BNK"))
    payload = submit(
        break_type="GL_TRANSPOSITION", evidence_ids=[e.item_id], counterpart_id=matched_bank
    )[1]
    with pytest.raises(FindingValidationError, match="opposite ledger"):
        validate_finding(payload, env.ctx(e), env.view)


def test_outstanding_check_requires_check_number(env: Env) -> None:
    dup = env.exc(BreakType.GL_DUPLICATE)  # ACH payment, no check number
    with pytest.raises(FindingValidationError, match="no check number"):
        validate_finding(
            submit(break_type="OUTSTANDING_CHECK", evidence_ids=[dup.item_id])[1],
            env.ctx(dup),
            env.view,
        )


def test_schema_errors_are_readable(env: Env) -> None:
    fee = env.exc(BreakType.BANK_FEE_UNRECORDED)
    with pytest.raises(FindingValidationError, match="2 to 4 sentences"):
        validate_finding(
            submit(
                break_type="BANK_FEE_UNRECORDED",
                evidence_ids=[fee.item_id],
                explanation="Only one sentence here for the reviewer.",
            )[1],
            env.ctx(fee),
            env.view,
        )
    with pytest.raises(FindingValidationError, match="break_type"):
        validate_finding({"break_type": "MADE_UP"}, env.ctx(fee), env.view)


# ----- investigator loop ---------------------------------------------------------------------
def test_retry_then_success(env: Env) -> None:
    fee = env.exc(BreakType.BANK_FEE_UNRECORDED)
    provider = ScriptedProvider(
        [
            submit(break_type="BANK_FEE_UNRECORDED", evidence_ids=[fee.item_id, "BNK-99999"]),
            submit(break_type="BANK_FEE_UNRECORDED", evidence_ids=[fee.item_id]),
        ]
    )
    rf = env.investigator(provider).investigate(fee)
    assert rf.source == FindingSource.AGENT
    assert rf.review_status == ReviewStatus.AUTO_ACCEPTED
    retry_msg = provider.seen[1][-1]
    assert retry_msg.role == "tool" and "BNK-99999" in retry_msg.tool_result["error"]  # type: ignore[index]
    calls = env.wh.get_llm_calls("r1")
    assert [c.retry_count for c in calls] == [0, 1]
    assert all(c.prompt_version == "investigator_v1" for c in calls)
    steps = env.wh.get_agent_steps("r1")
    assert [s.step_type for s in steps] == ["invalid_finding", "final"]


def test_two_invalid_answers_fall_back_to_deterministic_classifier(env: Env) -> None:
    fee = env.exc(BreakType.BANK_FEE_UNRECORDED)
    bad = submit(break_type="INTEREST_UNRECORDED", evidence_ids=["BNK-99999"])
    rf = env.investigator(ScriptedProvider([bad, bad])).investigate(fee)
    assert rf.source == FindingSource.FALLBACK
    assert rf.review_status == ReviewStatus.NEEDS_REVIEW
    assert rf.finding.break_type == BreakType.BANK_FEE_UNRECORDED
    assert "after one retry" in rf.review_reasons[0]
    kinds = [s.step_type for s in env.wh.get_agent_steps("r1")]
    assert kinds.count("invalid_finding") == 2 and kinds[-1] == "fallback_final"


def test_step_limit_triggers_fallback(env: Env) -> None:
    dit = env.exc(BreakType.DEPOSIT_IN_TRANSIT)
    loop = ScriptedProvider([("get_transaction", {"id": dit.item_id})])
    rf = env.investigator(loop, max_agent_steps=3).investigate(dit)
    assert rf.source == FindingSource.FALLBACK
    assert rf.review_status == ReviewStatus.NEEDS_REVIEW
    assert "step limit of 3" in rf.review_reasons[0]
    assert len(env.wh.get_llm_calls("r1")) == 3
    assert rf.finding.break_type == BreakType.DEPOSIT_IN_TRANSIT


def test_provider_error_falls_back(env: Env) -> None:
    os_check = env.exc(BreakType.OUTSTANDING_CHECK)
    rf = env.investigator(ScriptedProvider([ProviderError("boom", 2)])).investigate(os_check)
    assert rf.source == FindingSource.FALLBACK
    assert rf.finding.break_type == BreakType.OUTSTANDING_CHECK
    call = env.wh.get_llm_calls("r1")[0]
    assert not call.success and call.error == "boom" and call.retry_count == 2


def test_low_confidence_needs_review_and_text_answers_parse(env: Env) -> None:
    fee = env.exc(BreakType.BANK_FEE_UNRECORDED)
    body = submit(break_type="BANK_FEE_UNRECORDED", evidence_ids=[fee.item_id], confidence=0.5)[1]
    text_answer = ProviderResponse(
        tool_call=None, text="Here you go:\n" + json.dumps(body), input_tokens=10, output_tokens=10
    )
    rf = env.investigator(ScriptedProvider([text_answer])).investigate(fee)
    assert rf.review_status == ReviewStatus.NEEDS_REVIEW
    assert "below threshold" in rf.review_reasons[0]
    assert _parse_text(None) == {} and _parse_text("no json") == {}
    assert _parse_text("{broken") == {} and _parse_text("[1]") == {}


def test_unidentified_routes_to_review(env: Env) -> None:
    fee = env.exc(BreakType.BANK_FEE_UNRECORDED)
    rf = env.investigator(
        ScriptedProvider(
            [submit(break_type="UNIDENTIFIED", evidence_ids=[fee.item_id], confidence=0.9)]
        )
    ).investigate(fee)
    assert rf.review_status == ReviewStatus.NEEDS_REVIEW
    assert "could not identify" in rf.review_reasons[0]


def test_tool_results_flow_back_to_provider(env: Env) -> None:
    fee = env.exc(BreakType.BANK_FEE_UNRECORDED)

    def final(messages: list[Message]) -> tuple[str, dict[str, Any]]:
        assert messages[-1].role == "tool" and messages[-1].tool_result["id"] == fee.item_id  # type: ignore[index]
        return submit(break_type="BANK_FEE_UNRECORDED", evidence_ids=[fee.item_id])

    script: list[Any] = [("get_transaction", {"id": fee.item_id}), final]
    rf = env.investigator(ScriptedProvider(script)).investigate(fee)
    assert rf.steps_used == 2 and rf.source == FindingSource.AGENT


def test_mock_policy_classifies_every_planted_break(env: Env) -> None:
    findings = env.investigator(MockProvider()).investigate_all(env.exceptions)
    predicted: dict[str, str] = {}
    for f in findings:
        predicted[f.item_id] = f.finding.break_type.value
        if f.finding.counterpart_id:
            predicted[f.finding.counterpart_id] = f.finding.break_type.value
    assert predicted == env.truth
    assert all(f.review_status == ReviewStatus.AUTO_ACCEPTED for f in findings)
    assert all(f.source == FindingSource.AGENT for f in findings)


def test_unknown_items_become_unidentified(env: Env) -> None:
    odd = ReconException(
        exception_id="EXC-X",
        side="BANK",
        item_id="BNK-00001",
        item_date=date(2026, 9, 1),
        amount="-1.00",
        description="MYSTERY",
    )
    env.view.matched.pop("BNK-00001", None)
    rf = env.investigator(MockProvider()).investigate(odd)
    assert rf.finding.break_type in (
        BreakType.UNIDENTIFIED,
        BreakType.GL_WRONG_AMOUNT,
        BreakType.GL_TRANSPOSITION,
    )


def test_full_mock_end_to_end(dataset_dir: Path) -> None:
    wh = DuckDBWarehouse()
    result = reconcile(
        dataset_dir / "bank_statement.csv", dataset_dir / "gl_cash.csv", Settings(), wh
    )
    labels = json.loads((dataset_dir / "labels.json").read_text())
    assert result["status"] == "TIED"
    assert result["tieout"]["difference"] == "0.00"
    assert result["tieout"]["adjusted_bank_balance"] == labels["expected_adjusted_balance"]
    assert result["summary"]["findings_by_status"]["NEEDS_REVIEW"] == 0
    run_id = result["run_id"]
    assert wh.get_llm_calls(run_id) and wh.get_agent_steps(run_id)
    assert result["metadata"]["prompt_version"] == "investigator_v1"
    assert len(result["metadata"]["inputs"][0]["sha256"]) == 64
    assert "gemini_api_key" not in result["metadata"]["config_snapshot"]


def test_prompt_loading_and_provider_factory() -> None:
    assert "submit_finding" in load_prompt(Settings().prompts_dir, "investigator_v1")
    with pytest.raises(FileNotFoundError):
        load_prompt(Settings().prompts_dir, "investigator_v999")
    assert isinstance(make_provider(Settings()), MockProvider)
    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        make_provider(Settings(llm_provider="gemini", gemini_api_key=""))


def test_mock_requires_context() -> None:
    with pytest.raises(ProviderError):
        MockProvider().complete("s", [Message(role="user", content="hi")], [])


# ----- gemini adapter (offline) --------------------------------------------------------------
class _FakeModels:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kw: Any) -> Any:
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class _Client:
    def __init__(self, responses: list[Any]) -> None:
        self.models = _FakeModels(responses)


class _ApiError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"http {code}")
        self.code = code


def _fake_response(fc: Any = None, text: str | None = None) -> Any:
    from types import SimpleNamespace

    usage = SimpleNamespace(
        prompt_token_count=120, candidates_token_count=30, thoughts_token_count=5
    )
    return SimpleNamespace(
        usage_metadata=usage,
        function_calls=[fc] if fc else [],
        text=text,
        candidates=[SimpleNamespace(content="RAW")],
    )


def test_gemini_adapter_parses_function_calls_and_retries() -> None:
    from types import SimpleNamespace

    from tieout.agent.providers.gemini import GeminiProvider

    sleeps: list[float] = []
    fc = SimpleNamespace(name="get_transaction", args={"id": "BNK-00001"}, id="call-1")
    client = _Client([_ApiError(503), _fake_response(fc)])
    p = GeminiProvider(Settings(llm_provider="gemini"), client=client, sleep=sleeps.append)
    msgs = [
        Message(role="user", content="ctx"),
        Message(role="assistant", tool_call=ToolCall("get_transaction", {"id": "x"}, "c0")),
        Message(
            role="tool",
            tool_call=ToolCall("get_transaction", {"id": "x"}, "c0"),
            tool_result={"ok": True},
        ),
        Message(role="assistant", content="thinking"),
    ]
    resp = p.complete(
        "system",
        msgs,
        [
            {
                "name": "get_transaction",
                "description": "d",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
    )
    assert resp.tool_call == ToolCall("get_transaction", {"id": "BNK-00001"}, "call-1")
    assert (resp.input_tokens, resp.output_tokens, resp.retry_count) == (120, 35, 1)
    assert len(sleeps) == 1 and resp.raw == "RAW"
    sent = client.models.calls[-1]
    assert sent["model"] == "gemini-3.6-flash" and len(sent["contents"]) == 4
    # Gemini 3 should run at the model's default temperature unless explicitly overridden.
    assert sent["config"].temperature is None
    assert sent["config"].tool_config.function_calling_config.mode == "ANY"
    tuned = GeminiProvider(
        Settings(gemini_temperature=0.3), client=_Client([]), sleep=sleeps.append
    )
    assert tuned._config("s", []).temperature == 0.3


def test_gemini_adapter_text_and_non_retryable_errors() -> None:
    from tieout.agent.providers.gemini import GeminiProvider

    p = GeminiProvider(
        Settings(), client=_Client([_fake_response(text="{}")]), sleep=lambda s: None
    )
    assert p.complete("s", [Message(role="user", content="x")], []).text == "{}"
    p = GeminiProvider(Settings(), client=_Client([_ApiError(400)]), sleep=lambda s: None)
    with pytest.raises(ProviderError) as info:
        p.complete("s", [Message(role="user", content="x")], [])
    assert info.value.retry_count == 0
    errs: list[Any] = [_ApiError(429)] * 3
    p = GeminiProvider(Settings(gemini_max_retries=2), client=_Client(errs), sleep=lambda s: None)
    with pytest.raises(ProviderError) as info:
        p.complete("s", [Message(role="user", content="x")], [])
    assert info.value.retry_count == 2
    timeout_cls: Callable[[], Exception] = type("ReadTimeout", (Exception,), {})
    p = GeminiProvider(
        Settings(gemini_max_retries=1),
        client=_Client([timeout_cls(), _fake_response(text="ok")]),
        sleep=lambda s: None,
    )
    assert p.complete("s", [Message(role="user", content="x")], []).retry_count == 1


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="GEMINI_API_KEY not set")
def test_live_gemini_investigates_a_fee(env: Env) -> None:  # pragma: no cover - live only
    from tieout.agent.providers.gemini import GeminiProvider

    settings = Settings(llm_provider="gemini", gemini_api_key=os.environ["GEMINI_API_KEY"])
    fee = env.exc(BreakType.BANK_FEE_UNRECORDED)
    rf = env.investigator(GeminiProvider(settings)).investigate(fee)
    assert rf.finding.break_type == BreakType.BANK_FEE_UNRECORDED
    calls = env.wh.get_llm_calls("r1")
    assert calls and calls[0].input_tokens > 0
