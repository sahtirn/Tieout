# Decision Log

Each entry records the decision, the alternatives considered, and why the chosen option is the most
defensible for a finance audience. Entries marked **Review** deserve a human look.

## D-001 Repository already initialized
- **Decision:** Reuse the existing git repository (it held only a README) instead of running `git init`.
- **Alternatives:** Re-initialize and lose the existing initial commit.
- **Rationale:** Same outcome, and history is preserved.

## D-002 Opening and ending balances come from running-balance columns
- **Decision:** Both CSVs carry a `balance` column (as real bank exports and GL detail reports do).
  The loader derives the opening balance from the first row and verifies that every running balance
  foots. A break raises `InputError` before any matching runs.
- **Alternatives:** Separate balance file; header comment rows; CLI flags.
- **Rationale:** Keeps the generator to the three requested files, mirrors real exports, and turns
  the balance into an input-integrity control rather than a trusted number.

## D-003 Bank file uses a signed amount; GL file uses debit and credit columns
- **Decision:** The bank CSV is already signed from the account holder's view. The GL CSV has
  separate debit and credit columns and is normalized to `amount = debit - credit` on load.
- **Rationale:** Exercises the sign-normalization path that every real implementation needs.

## D-004 Prior period fully cleared (opening bank balance equals opening book balance)
- **Decision:** The synthetic month starts with no prior-period reconciling items.
- **Alternatives:** Carry prior-month outstanding checks that clear this month.
- **Rationale:** Keeps ground truth unambiguous for evals. Prior-period carry-forward is a roadmap
  item; it would need the prior rec as an input. **Review.**

## D-005 Generator enforces unique absolute amounts for unrelated transactions
- **Decision:** Every independently drawn amount is unique within a dataset (duplicates and NSF
  returns are deliberately equal to their originals).
- **Alternatives:** Allow random collisions.
- **Rationale:** Random collisions make the ground truth itself ambiguous, which would measure the
  generator, not the matcher. The matcher still breaks ties deterministically and conservatively.

## D-006 Bank errors are modeled as misposted items carrying a bank adjustment notice
- **Decision:** A `BANK_ERROR` is a bank line (a check from another account's series, or a deposit
  that is not ours) whose reference is a bank adjustment notice number (`ADJN...`). The bank-side
  adjustment reverses it.
- **Alternatives:** Encoding errors (bank amount differs from a correct GL amount).
- **Rationale:** An encoding error looks identical to a GL transposition unless one side carries
  proof. The prompt asks that every planted break carry evidence; an adjustment notice is the
  realistic proof a bank provides.

## D-007 Default Gemini model is `gemini-3.6-flash`
- **Decision:** `GEMINI_MODEL` defaults to `gemini-3.6-flash`, listed as the stable Flash model on
  Google's Gemini API model page as of October 2026. The installed google-genai SDK (2.31.0) knows
  `gemini-3.5-flash` in its local tokenizer map, which confirms the 3.x naming. Gemini 2.5 models
  were scheduled for shutdown on 2026-10-16.
- **Rationale:** Model IDs move quickly, so it is a single env var. **Review** before a live run.

## D-008 Pricing and ROI figures are estimates
- **Decision:** `config/pricing.yaml` and `config/assumptions.yaml` are explicitly labeled as
  estimates and assumptions. Prices are YAML strings parsed to Decimal.
- **Rationale:** Prices could not be fetched from Google's pricing page in this environment.
  **Review** before quoting any cost or ROI number.

## D-009 LLM cost is stored at six decimal places
- **Decision:** Ledger money is quantized to 0.01. Estimated LLM cost is stored as DECIMAL(18,6).
- **Rationale:** A single Flash call costs a fraction of a cent, so cents would round every call to
  zero and make telemetry useless. Cost never touches a ledger balance.

## D-010 Description similarity = mean of token-set and leading-token similarity
- **Decision:** After removing transaction-type stopwords and all numeric tokens, similarity is the
  mean of rapidfuzz `token_set_ratio` and the ratio of the first remaining token. Near-match
  threshold defaults to 0.78. Check numbers and references are compared exactly and score 1.0.
- **Alternatives:** Plain `token_set_ratio`, `partial_ratio`, `WRatio`.
- **Rationale:** Measured on 19 seeds: plain token-set put legitimate pairs as low as 0.70 while
  unrelated payees sharing a word like "supply" reached 1.00. The blended score separates them
  cleanly (legitimate minimum 0.85, unrelated maximum 0.73), because banks truncate payee names but
  rarely change the first word. Numbers are excluded because check serials one digit apart are
  fuzzy-similar but are different checks.

## D-011 A `match_items` child table enforces "matched at most once"
- **Decision:** Alongside `matches`, a `match_items(run_id, side, item_id)` table with that primary
  key records every matched item.
- **Rationale:** The invariant becomes a database constraint, not just application logic, and the
  candidate queries can exclude matched items with a set-based `NOT EXISTS` anti-join.

## D-012 Many-to-one refuses ambiguity
- **Decision:** The subset-sum search collects up to three exact solutions. One solution matches.
  Several solutions match only if one sits strictly closest to the deposit date; otherwise the
  deposit stays unmatched and is listed in `batch_ambiguous`. The search uses integer cents and a
  per-run time budget (`BATCH_TIME_BUDGET_MS`) and stops cleanly when the budget is spent.
- **Rationale:** A wrong batch match silently hides two errors. An unmatched deposit is visible
  and goes to investigation.

## D-013 TIED is necessary, not sufficient
- **Decision:** Evals score adjusted-balance accuracy against ground truth in addition to tie-out
  rate, and the agent's findings pass side and sign rules before they reach the schedule.
- **Rationale:** The reconciliation difference is linear: a bank-side adjustment of +x and a
  book-side adjustment of -x move it identically. Calling a deposit in transit a GL duplicate
  therefore still ties while misstating both adjusted balances (covered by
  `test_side_swapped_misclassification_still_ties_but_misstates_balance`). A controller signing
  off on "TIED" needs the classification controls too. **Review.**

## D-014 Only auto-accepted or reviewer-approved findings enter the schedule
- **Decision:** NEEDS_REVIEW findings are excluded from the schedule. Their items appear as
  unexplained, so the rec reports UNRECONCILED with the exact amount at stake until a reviewer
  approves them in `tieout review`. Rejected findings stay out.
- **Alternatives:** Count low-confidence classifications provisionally.
- **Rationale:** A model guess that fails a control must not be able to make the rec tie. Human
  approval, not model confidence, closes the gap.

## D-015 JEs are proposed for every non-rejected book-side finding
- **Decision:** Book-side findings get a balanced two-line proposal even while NEEDS_REVIEW (the
  memo says so). Every JE starts as PENDING_APPROVAL. Bank-side items never get a JE.
- **Rationale:** The reviewer sees the full consequence of approving a classification in one
  place. Posting remains a human decision.

## D-016 One deterministic policy powers both MockProvider and the fallback classifier
- **Decision:** `agent/policy.py` decides the next tool call from prior observations, then a
  final finding. MockProvider replays it through the real message and tool-calling protocol; the
  fallback classifier runs it directly against the tools.
- **Alternatives:** A mock that returns canned answers; a separate fallback rule engine.
- **Rationale:** CI and evals exercise the exact loop, logging, validation, and telemetry a live
  model uses, and the fallback cannot drift from what the evals measure.

## D-017 The final answer is a `submit_finding` function call, with function calling mode ANY
- **Decision:** The AgentFinding schema is exposed as a tool. Gemini runs with
  `FunctionCallingConfig(mode="ANY")`, so every turn is structured. A text answer is still parsed
  leniently (first JSON object) and validated like any other.
- **Rationale:** Structured arguments are more reliable than JSON-in-text, and combining a
  response schema with tools is not supported uniformly across models.

## D-018 `AgentFinding` gains `counterpart_id`, and an `UNIDENTIFIED` break type exists
- **Decision:** Transpositions and wrong amounts involve two items, so the finding names the
  opposite-ledger counterpart and code re-verifies the claim (digit swap, sign, unmatched). An
  explicit `UNIDENTIFIED` type lets the model (or fallback) say "cannot determine"; it is always
  routed to review, never counted on the schedule, and cannot be approved as-is.
- **Rationale:** Forcing a guess into one of nine buckets would make a wrong classification look
  confident. **Review:** this extends the enum in the brief.

## D-019 Exceptions are investigated bank side first; paired items are skipped
- **Decision:** Bank exceptions are investigated before GL exceptions. When a finding names a
  counterpart, that item is marked resolved and is not investigated separately.
- **Rationale:** The bank side of a transposition or wrong amount carries the true amount, and
  pairing once avoids double-counting the correction on the schedule.

## D-020 Validation errors go back to the model as the `submit_finding` tool result
- **Decision:** On the single retry, the rejected call is answered with
  `{"accepted": false, "error": ...}` rather than a new user message.
- **Rationale:** Function-calling APIs expect every call to be answered; this keeps the
  conversation well-formed for Gemini while giving the model the exact control that failed.

## D-021 Bulk loads stage through a temporary CSV and COPY
- **Decision:** `DuckDBWarehouse` writes bulk rows to a temporary CSV (every value a quoted
  string, NULL as `\N`) and loads it with `COPY`. Single-row writes still use bound parameters.
- **Alternatives:** `executemany`; multi-row `INSERT ... VALUES` with bound parameters.
- **Rationale:** Profiling showed DuckDB's Python binder probing for pandas on every bound value
  (about 250 microseconds each), which made a single reconcile spend most of its time binding. COPY
  is about 100x faster, keeps Decimal precision exact, and mirrors a Snowflake stage plus
  `COPY INTO`. Test suite time dropped from 69 s to 10 s.

## D-022 Review actions are append-only and enforce maker-checker
- **Decision:** `review_actions` is insert-only; current state is derived by replaying actions in
  order, so a changed decision is visible in the trail. A reviewer whose name matches the run's
  preparer (case-insensitive) is refused. Rejections require a note. UNIDENTIFIED findings cannot
  be approved as-is.
- **Rationale:** These are the minimum controls a SOX-minded reviewer expects. Identity here is a
  name string; in production it would come from SSO. **Review.**

## D-023 Audit trail = entity records plus one trace per reconciling item
- **Decision:** `tieout audit` writes JSONL with run, source_row, match, exception, agent_step,
  llm_call, finding, journal_entry, review_action, and tieout records, plus a
  `reconciling_item_trace` per finding that inlines source rows, step summary, classification,
  schedule section and amount, JE ids, and review actions.
- **Rationale:** Entity records support machine analysis; trace records let a human follow one
  item without joining anything.

## D-024 Config and prompts resolve from the repo first, then from the wheel
- **Decision:** `config/` and `prompts/` live at the repo root as requested, and the wheel build
  force-includes copies under `tieout/_data/`. At runtime the repo copy wins when present.
- **Rationale:** Editable installs keep the files reviewers edit in obvious places, and a plain
  `pip install .` still works.

## D-025 Reports re-render from stored state
- **Decision:** `reconcile` writes `runs/<run_id>/report.html`, `report.md`, and `result.json`.
  `report` and `review` rebuild the result from the warehouse (applying review actions) and
  re-render, so the sign-off copy always reflects the latest decisions.
- **Rationale:** One source of truth (the warehouse) instead of mutable files.
