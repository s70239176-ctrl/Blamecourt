# Consensus design

## Why `prompt_comparative`, not `strict_eq`

The non-deterministic step in `adjudicate()` is "fetch live pages, then
have an LLM write free-form rationale plus a structured verdict." Two
honest validators running that independently will not produce
byte-identical JSON — key order, rationale wording, and minor rounding
differences are expected even when both are *correct*. `strict_eq` would
make honest validators disagree with each other by construction, so it is
never used on the output of `produce_verdict`.

`gl.eq_principle.prompt_comparative` is used instead, with a principle
that tells validators two verdicts are equivalent iff:

1. `cause` fields are identical strings.
2. `shares` objects cover the same agent addresses — **compared
   case-insensitively**, since LLMs sometimes reformat hex address casing
   (e.g. to an EIP-55 checksum) even when explicitly told not to — each
   agent's two share values differ by at most 500 bps, and both sum to
   10000 (±1 for integer rounding).
3. Every `evidence_used` URL in either verdict actually appears in the
   evidence pack's known-URL list. A verdict citing an unfetched URL is
   invalid, not merely "different."
4. `rationale` wording is ignored entirely.

## Deterministic post-consensus validation

After consensus returns an accepted payload, a second pass — outside the
nondet block, allowed to touch storage — re-validates it independently of
whatever the validators agreed to:

- `cause` ∈ the fixed enum.
- `shares` keys, after `_norm_addr()` normalization, are set-equal to the
  job's registered agent addresses (no extras, none missing).
- Every share value is a non-negative int; the total is exactly 10000.
- `evidence_used` ⊆ (`spec_url` ∪ every submitted artifact URL).

**A failure here hard-fails the transaction (`raise Exception(...)`)
rather than silently coercing the verdict.** The job stays in
`ready_for_adjudication` so it can be re-adjudicated. This was an explicit
design requirement, not a shortcut: a contract that "fixes up" an invalid
accepted verdict on the fly is a contract whose stored decisions can no
longer be trusted to mean what they say.

### Diagnostic error messages

Two specific failure modes get their own, more specific error rather than
a single generic "shares don't match" message, because debugging this
contract against a real GenVM build made clear how expensive an opaque
failure is:

- **Empty `shares` after parsing** almost always means the LLM's raw
  output failed to parse as JSON at all (see "Coercing LLM output" below)
  and the safe fallback (`shares: {}`, `cause: "non_delivery"`) kicked in
  — not that the model genuinely returned zero shares. The exception in
  that case says so explicitly and echoes the fallback's `rationale`
  field, which — because of the coercion work below — carries the raw
  output's Python type and a text snippet.
- **Non-empty but mismatched `shares` keys** now name the exact `extra`
  (addresses the verdict invented that aren't real agents) and `missing`
  (real agents the verdict never mentioned) sets, instead of a bare
  "keys do not match" message.

## Coercing LLM output

`gl.nondet.exec_prompt(..., response_format="json")` is asked for JSON,
but its enforcement could not be confirmed for every GenVM build, and
LLMs commonly wrap "JSON-only" output in a ` ```json ... ``` ` fence
anyway. `_coerce_to_json_obj()` handles three possible shapes of what
comes back:

- **already a parsed `dict`/`list`** — used directly (a straightforward
  string-only fence-stripper would throw immediately on this and silently
  fall through to the safe fallback, which is exactly what happened during
  development before this was added);
- **`bytes`** — decoded as UTF-8;
- **`str`** — a leading/trailing code fence is stripped, and failing
  that, the first-`{`-to-last-`}` span is extracted, before `json.loads`.

If none of that produces valid JSON, `produce_verdict` falls back to a
safe default verdict (`non_delivery`, empty shares) whose `rationale`
field is stuffed with the raw output's type and a truncated snippet —
turning the *next* failed `adjudicate` call into a self-diagnosing one
instead of requiring a trip into Studio's validator panel.

## Address normalization

Every address that becomes a dict key or gets compared — `job["creator"]`,
`job["agents"]` keys, a caller's `gl.message.sender_address`, an accepted
verdict's `shares` keys, `get_credit`'s input — goes through
`_norm_addr()` (`str(addr).strip().lower()`) first. `str(...)` of an
`Address` and an address typed by hand into `agents_json` are not
guaranteed to share the same casing, and Python dict/JSON-key comparison
is exact-string, so a casing mismatch would otherwise produce a false
"caller is not a registered agent" or a false shares-key mismatch. Both
happened during development before normalization was added everywhere.

## Attack model

**False positive** ("agent A is 100% at fault" when A did nothing
wrong): the verdict must cite `evidence_used` URLs that were actually
fetched this round, and independent validators re-fetch and re-judge
rather than trust the leader's claim. If A genuinely delivered, the
evidence contains nothing supporting blame on A, and a leader who tries
to force it fails the comparative check. If a false verdict is still
somehow reached, `appeal()` re-runs the entire pipeline independently and
reverses A's slashing if the new run disagrees.

**False negative** ("no fault" / even split when an agent was actually
negligent): a negligent agent cannot get away with a self-serving
paragraph — the contract fetches *their own* declared artifact URL, and
the prompt explicitly instructs that a role's `FETCH_FAILED` (or a role
with zero submitted artifacts, treated identically — see the prompt's
explicit rule for this) is strong evidence of `non_delivery` for that
role. A harmed party can `flag_failed` → `adjudicate`, and `appeal()`
once if the first verdict under-blames the negligent agent.

In both cases, "a false verdict has a winner": the wronged party has a
concrete, bounded-cost lever (`appeal()`), and the distribution formula
(see `docs/VERDICT_FORMAT.md`) guarantees no agent's pay can be pushed
negative regardless of verdict.

## What "transaction ended undetermined" means

This is a **consensus-layer outcome, not a contract-code failure.** It
means individual validators executed successfully (often producing an
*identical* resulting `contract_state_hash` to each other and to the
leader) but still did not accumulate enough `"agree"` votes to finalize —
observed during development even when every validator that completed
computed the same state hash and `nondet_disagree: 0`. Two validators
timing out entirely (`execution_result: "ERROR"`, `vote: "idle"`) before
they could even compare against the leader was also observed contributing
to this. Neither of those is something a contract-code change can fix —
they are Studio/validator-set infrastructure conditions, not a bug in
`adjudicate()`'s logic. When triage is needed, check (a) how many
validators actually completed vs. errored out, and (b) whether the ones
that completed agree with each other, before assuming the contract logic
is at fault.
