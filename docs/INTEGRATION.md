# Integration guidance

BlameCourt has no cross-contract interface primitive today (no
`@gl.contract_interface` export) — it is designed to be called directly
by an orchestrator, agent framework, or escrow-consuming contract using
the plain method reference below. If your GenVM build supports
`@gl.contract_interface`, wrapping `get_job` / `get_verdict` / `get_status`
/ `get_credit` in one is a natural, low-risk extension (they are all
already schema-safe `str`/`u256`-returning views) — it wasn't added here
since it could not be verified against this project's target GenVM build.

## Typical caller flow

1. **Register the job.** Your orchestrator (or the job creator directly)
   calls `create_job(job_id, spec_url, rubric, deadline, agents_json)`
   with the escrow attached as the payable value. `agents_json` is a JSON
   string list of `{"addr": "0x...", "role": "..."}` — addresses must be
   20-byte `0x` hex strings (normalized to lowercase), not the `Address`
   type, on every public signature in this contract. `deadline` must be a
   valid ISO-8601 instant in the future. Any other agent field (including
   the old `bond`) is rejected.
2. **Each agent submits its own artifact URL(s)** via `submit_artifact`
   as work happens. A role that will never have a natural artifact should
   still submit *something* (even a trivial confirmation URL) — see
   `docs/VERDICT_FORMAT.md` for why "zero artifacts" and "fetch failed"
   are treated identically, and why that ambiguity is worth avoiding on
   the caller side by always submitting something.
3. **On failure**, the creator or any agent calls `flag_failed` — but
   only once the deadline has passed or every agent has submitted
   something; earlier calls revert. Then anyone calls `adjudicate`.
4. **Poll `get_status`** for `"adjudicated"`. Read `get_verdict` for the
   decision. `get_credit` is still `0` at this point by design.
5. **Appeal window.** For `APPEAL_WINDOW_SECONDS` (3600) after
   adjudication a registered agent may `appeal()` once, posting a bond.
   The re-run verdict replaces the original and the job settles
   immediately; if the verdict is unchanged the bond goes to the creator.
6. **Settle.** If nobody appeals, anyone calls `finalize` once the window
   has closed. Poll `get_status` for `"final"`, then read `get_credit(addr)`
   for each agent's/creator's payout.
7. **Each payee calls `withdraw()` themselves** — BlameCourt never pushes
   funds, and nothing is withdrawable before step 5 or 6. An orchestrator
   should not assume payout happens automatically; it should either prompt
   agents to withdraw or itself hold a role that calls `withdraw()` on
   their behalf if your broader system does that.

## What a consuming contract/orchestrator should NOT assume

- That `adjudicate()` always succeeds on the first call. A genuinely
  ambiguous evidence set can produce an "undetermined" transaction (see
  `docs/CONSENSUS.md`) — this is not an error state your integration
  needs to handle specially beyond "the job is still
  `ready_for_adjudication`, try again or wait."
- That `shares` will contain every conceivable address — it is guaranteed
  to contain **exactly** the job's registered agent addresses, normalized
  to lowercase, and nothing else.
- That agents post any bond. There is no agent-bond funding or slashing:
  the `escrow_total` distribution formula (plus the one-off appeal bond)
  is the only money-movement path.

## Example `agents_json` and `rubric`

See `fixtures/` for a ready-to-use example of both, and
`tests/integration/` for full manual Studio walkthroughs including
verified expected outputs.
