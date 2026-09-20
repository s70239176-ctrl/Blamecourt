# Architecture

## Job lifecycle

```
open --submit_artifact--> submitted --flag_failed / past deadline-->
ready_for_adjudication --adjudicate--> adjudicated
    --appeal--> appealed --(internal)--> final
```

`adjudicated` is a fine terminal state on its own — its distribution has
already been applied. `appeal()` may move a job to `appealed` and then
`final` at most once (`MAX_APPEALS = 1`).

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
pushed out of `adjudicate()`/`appeal()` directly.

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
| `create_job(job_id, spec_url, rubric, deadline, agents_json)` | `@gl.public.write.payable` | anyone | Locks `gl.message.value` as escrow. Rejects duplicate `job_id`. |
| `submit_artifact(job_id, url, kind)` | `@gl.public.write` | a registered agent | Capped at 16 artifacts/job; deadline-gated when a time source is available. |
| `flag_failed(job_id)` | `@gl.public.write` | creator or any agent | Moves the job to `ready_for_adjudication`. |
| `adjudicate(job_id)` | `@gl.public.write` | anyone | Requires flagged or past-deadline; requires ≥1 artifact; not callable twice. |
| `appeal(job_id)` | `@gl.public.write.payable` | a registered agent | Requires a bond; once per job; re-runs the full pipeline. |
| `withdraw()` | `@gl.public.write` | anyone with a credit balance | Pull-payment pattern. |
| `get_job(job_id) -> str` | `@gl.public.view` | anyone | Canonical JSON of the job. |
| `get_verdict(job_id) -> str` | `@gl.public.view` | anyone | Canonical JSON of the verdict, or `"null"`. |
| `get_status(job_id) -> str` | `@gl.public.view` | anyone | |
| `get_credit(addr) -> u256` | `@gl.public.view` | anyone | `addr` is a plain `str`, normalized on lookup. |
| `get_job_count() -> u256` | `@gl.public.view` | anyone | |
