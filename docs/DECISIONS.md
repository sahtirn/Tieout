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
