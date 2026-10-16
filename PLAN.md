# TieOut Build Plan

TieOut is an auditable agent that performs a month-end bank reconciliation for one cash account and
either ties to 0.00 or reports the unexplained difference. Deterministic code owns every number. The
LLM investigates, classifies, and explains exceptions, and every claim it makes is verified by code.

## Architecture

```
CSV inputs ──► io (parse + normalize sign) ──► Warehouse (DuckDB)
                                                  │
                                 matching engine (SQL candidates + deterministic assignment)
                                   exact ─► near ─► many-to-one (bounded subset-sum)
                                                  │ unmatched items
                                                  ▼
                       investigator agent loop (provider ⇄ read-only tools, max N steps)
                         │  validated AgentFinding (schema + evidence + side/sign + pairing checks)
                         │  invalid ─► one retry with error ─► deterministic fallback (NEEDS_REVIEW)
                                                  ▼
                       tie-out schedule (pure Decimal) ─► JE proposals (balanced, accounts.yaml)
                                                  ▼
               reports (HTML/MD/JSON) · audit JSONL · maker-checker review · telemetry · evals · API
```

## Modules (`src/tieout/`)

| Module | Responsibility |
| --- | --- |
| `money.py` | Decimal money type, quantize 0.01 ROUND_HALF_UP, float rejection, formatting |
| `models.py` | Pydantic domain models and enums |
| `config.py` | pydantic-settings `Settings`, YAML loaders (pricing, accounts, thresholds) |
| `generator.py` | Seeded synthetic bank + GL + labels with planted breaks |
| `io.py` | CSV loaders, sign normalization, file hashing |
| `warehouse/` | `Warehouse` interface, `DuckDBWarehouse`, `SnowflakeWarehouse` stub |
| `matching.py` | Exact, near, and many-to-one passes with evidence |
| `schedule.py` | Bank rec schedule and TIED / UNRECONCILED status |
| `journal.py` | Balanced adjusting JE proposals (book side only) |
| `agent/tools.py` | Read-only investigation tools over a ledger view |
| `agent/rules.py` | Deterministic classification rules (shared by mock policy and fallback) |
| `agent/providers/` | `LLMProvider` interface, `MockProvider`, `GeminiProvider` |
| `agent/validation.py` | Finding validation (schema, evidence IDs, side, sign, pairing) |
| `agent/investigator.py` | Tool-calling loop, retry, fallback, step logging |
| `telemetry.py` | Cost estimation, run and global aggregates, ROI assumptions |
| `pipeline.py` | End-to-end reconcile orchestration and result assembly |
| `audit.py` | JSONL audit trail export |
| `review.py` | Maker-checker review actions |
| `report.py` + `templates/` | Jinja2 HTML and Markdown reports |
| `evals.py` | Multi-seed evaluation harness with thresholds |
| `api.py` | FastAPI app |
| `cli.py` | Typer CLI |

## Phase checklist

- [x] Phase 0: scaffold, PLAN.md, DECISIONS.md, tooling
- [x] Phase 1: domain models and synthetic data generator
- [x] Phase 2: storage and deterministic matching engine
- [x] Phase 3: tie-out schedule and JE proposals
- [x] Phase 4: exception investigation agent
- [x] Phase 5: telemetry, audit trail, human review
- [x] Phase 6: reports and CLI
- [x] Phase 7: evals
- [ ] Phase 8: API
- [ ] Phase 9: CI, docs, sample run
- [ ] Phase 10: final verification

Each phase gate: `ruff check`, `ruff format --check`, `mypy`, `pytest` all green before the next phase.
