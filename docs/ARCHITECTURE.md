# Architecture

## Job lifecycle

```
open --submit_artifact--> submitted --flag_failed / past deadline-->
ready_for_adjudication --adjudicate--> adjudicated
    --finalize (window closed, no appeal)--> final
    --appeal  (inside the window)---------> final
```

**Flagging is gated.** `flag_failed` only succeeds once the job's deadline
has validly passed (`deadline_passed`) or every registered agent has
submitted at least one artifact (`all_submitted`). Before that, a missing
artifact is not evidence of anything, so nobody can rush a job into
adjudication and have silence judged as non-delivery. An unflagged job can
also be adjudicated directly once its deadline has passed. The deadline is
parsed at `create_job` (it must be a valid ISO-8601 instant in the future)
and compared against the transaction time from `gl.message_raw["datetime"]`;
if either cannot be read, the call fails rather than skipping the check.

**Nothing is paid at adjudication.** `adjudicate` stores the verdict and
opens an appeal window of `APPEAL_WINDOW_SECONDS` (3600). No credit is
written until the verdict is final:

- `finalize(job_id)` — anyone, after the window closes with no appeal.
- `appeal(job_id)` — a registered agent, inside the window, with a bond.
  The re-run verdict *replaces* the original (or the original stands and
  the bond goes to the creator). Either way the job settles immediately,
  once, from the single final verdict.

Because credits are only ever written from a final verdict, an upheld
appeal never has to reverse anything: there is nothing to claw back, and
the escrow is distributed exactly once (`_settle` refuses a second run and
checks that the payouts sum to the escrow). `MAX_APPEALS = 1`.

## Storage shape

Every job is stored as **one canonical-JSON string** keyed by `job_id` in
`TreeMap[str, str] jobs`. Nested `Agent` / `Artifact` / `Verdict` records
are never separate typed storage graphs — they live inside that JSON
string as plain dict/list values.

This was a deliberate simplification, not an oversight: whether nested
`@allow_storage`-style dataclasses can live inside a `TreeMap` value on a
given GenVM build could not be confirmed with confidence, and getting the
storage shape wrong is exactly what caused the earliest schema-extraction
failures during development (see `SUBMISSION.md`). A flat
`TreeMap[str, str]` of canonical JSON also directly satisfies "canonical
JSON, sorted keys" for every structured value that's persisted or
compared, with no extra bookkeeping.

Payouts are tracked separately in a pull-payment ledger,
`TreeMap[str, u256] credits`, withdrawn via `withdraw()`. Money is never
pushed out of `adjudicate()`/`appeal()` directly, and credits only exist
after a job settles (see above).

Three counters keep the books honest and are exposed by `get_ledger()`:
`total_in` (every escrow and appeal bond received), `total_credited`
(everything made withdrawable) and `total_withdrawn` (everything paid
out). The contract enforces `total_withdrawn <= total_credited <=
total_in` on every write, so it cannot promise or pay more than it holds.

Agents are `{addr, role}` only. An earlier revision accepted a per-agent
`bond` that was never collected or slashed; `create_job` now rejects
`bond` (and any other unknown agent field) instead of silently ignoring it.

Every address used as a dict key or compared against another address is
passed through `_norm_addr()` (lowercased, whitespace-stripped) first —
see `docs/CONSENSUS.md` for why this matters.

## Constructor

`__init__(self)` takes no arguments and only assigns `job_count = 0`.
`jobs` and `credits` are declared on the class body
(`jobs: TreeMap[str, str]`, `credits: TreeMap[str, u256]`) and are **not**
explicitly re-assigned a freshly constructed `TreeMap[...]()` instance in
`__init__` — GenVM allocates their storage slot (and its backing empty
map) from the class annotation itself. Explicitly assigning a new
instance was tried during development and rejected at runtime
(`Is right the same storage type? TreeMap <- TreeMap`) — see
`SUBMISSION.md`.

## Module layout inside `contracts/blamecourt.py`

The file is intentionally laid out as:

1. Module-level constants (cause enum, status strings, tolerances).
2. Module-level pure helper functions (`_canon`, `_truncate`,
   `_norm_addr`, `_extract_json_text`, `_coerce_to_json_obj`,
   `_fetch_evidence_pack`, `_build_prompt`) — **all together, before the
   class**.
3. Exactly one class, `BlameCourt(gl.Contract)`, with a contiguous body
   from `__init__` through the last `@gl.public.view` method.

Point 2 is not a style preference — `_fetch_evidence_pack` and
`_build_prompt` are called from inside the non-deterministic
`produce_verdict` closure in `adjudicate`, and must be plain functions
(not bound methods) so nothing inside that closure can capture `self`
(and therefore the storage-backed `TreeMap`s — see `docs/CONSENSUS.md`).
Point 3 — keeping every module-level function *before*, not interleaved
with, the class body — is also load-bearing: an earlier revision placed
two module-level functions physically between two class methods, and
because their bodies were indented at the same 4-space level as the
surrounding methods, everything after them was silently parsed as nested
functions *inside* the last module-level function rather than as methods
of `BlameCourt`. That produced a contract with zero public methods and no
Python exception at all — a purely structural bug, documented in full in
`SUBMISSION.md` because it's a sharp edge worth knowing about for anyone
editing this file.

## Public method reference

| Method | Decorator | Who can call | Notes |
|---|---|---|---|
| `create_job(job_id, spec_url, rubric, deadline, agents_json)` | `@gl.public.write.payable` | anyone | Locks `gl.message.value` as escrow. Rejects duplicate `job_id`, a deadline that is unparseable or not in the future, malformed agent addresses, and any agent field other than `addr`/`role`. |
| `submit_artifact(job_id, url, kind)` | `@gl.public.write` | a registered agent | Capped at 16 artifacts/job; rejected after the deadline. |
| `flag_failed(job_id)` | `@gl.public.write` | creator or any agent | Only after the deadline has passed or every agent has submitted; otherwise reverts. Moves the job to `ready_for_adjudication`. |
| `adjudicate(job_id)` | `@gl.public.write` | anyone | Requires flagged or past-deadline; not callable twice. Stores the verdict and opens the appeal window; pays nothing. |
| `appeal(job_id)` | `@gl.public.write.payable` | a registered agent | Inside the appeal window only; requires a bond; once per job; re-runs the full pipeline and settles the job from the final verdict. |
| `finalize(job_id)` | `@gl.public.write` | anyone | After the appeal window closes with no appeal; settles the job. |
| `withdraw()` | `@gl.public.write` | anyone with a credit balance | Pull-payment pattern; balances exist only for settled jobs. |
| `get_job(job_id) -> str` | `@gl.public.view` | anyone | Canonical JSON of the job. |
| `get_verdict(job_id) -> str` | `@gl.public.view` | anyone | Canonical JSON of the verdict, or `"null"`. |
| `get_status(job_id) -> str` | `@gl.public.view` | anyone | |
| `get_credit(addr) -> u256` | `@gl.public.view` | anyone | `addr` is a plain `str`, normalized on lookup. |
| `get_job_count() -> u256` | `@gl.public.view` | anyone | |
| `get_ledger() -> str` | `@gl.public.view` | anyone | Canonical JSON of `total_in`, `total_credited`, `total_withdrawn`. |
