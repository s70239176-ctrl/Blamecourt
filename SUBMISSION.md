# Submission notes

## What's implemented

One Intelligent Contract, `contracts/blamecourt.py`: multi-agent blame
assignment and escrow arbitration on GenLayer. Full job lifecycle
(`create_job` → `submit_artifact` → `flag_failed` → `adjudicate` →
optional `appeal`), live evidence fetch inside a nondet block,
comparative-equivalence LLM verdicts, deterministic post-consensus
validation, and a pull-payment distribution formula. See `README.md` for
the overview and `docs/` for the full design writeups.

## Known GenLayer API guesses

The exact current GenVM/`genlayer-py` surface could not be fully confirmed
from documentation available while building this. Everything below is
isolated to a small, commented spot in `contracts/blamecourt.py`:

1. **Reading the value sent with a payable call** — `gl.message.value`.
2. **Reading current chain time** — `gl.message_raw["datetime"]` (an
   ISO-8601 string; defined by the SDK's `MessageRawType`). An earlier
   revision probed `gl.message.chain_datetime`/`datetime`/`timestamp`, none
   of which exist, so it silently never enforced a deadline. The clock now
   hard-fails if unreadable instead of disabling deadlines.
3. **Outbound value transfer in `withdraw()`** — `gl.get_contract_at(addr)
   .emit_transfer(value=...)`. An earlier revision called `gl.eth_send`,
   which does not exist in the SDK: `withdraw()` raised `AttributeError` on
   a real chain, so no payout could ever be collected. Found by running the
   live Studio tests; the in-process mocks could not see it.
4. **Nested structured storage** — rather than guess at whether
   `@allow_storage`-decorated dataclasses can be `TreeMap` values on this
   build, every job is one canonical-JSON string keyed by `job_id`. See
   `docs/ARCHITECTURE.md`.
5. **Agent bonds were removed.** `agents_json` used to declare a `bond`
   per agent that was never collected or slashed. Agents are now
   `{addr, role}` and `create_job` rejects `bond`. The only money paths are
   the escrow distribution and the one-off appeal bond.
6. **`gltest.config.yaml`'s exact schema** was not verified against a
   real `gltest` install — see the comment in that file.

## Escrow-safety revision

Review found three problems, all fixed in `contracts/blamecourt.py`:

1. **Appeals could double-pay.** Credits were written at `adjudicate`, and
   an upheld appeal "reversed" them by crediting a negative amount, which
   `_credit` ignored — so the new distribution was added on top of the
   original and the contract owed more than it held. Credits are now only
   written from the final verdict (`finalize` after the appeal window, or
   `appeal`), so nothing is ever reversed. `total_in >= total_credited >=
   total_withdrawn` is enforced on every write.
2. **Premature flagging.** `flag_failed` could be called at any time, and
   the deadline check never fired (see API guess 2), so silence could be
   judged as non-delivery immediately. `flag_failed` now requires a passed
   deadline or that every agent has submitted.
3. **Unused bond fields** — removed (API guess 5).

`tests/unit/` covers each of these.

### Found by running against live Studio

4. **`withdraw()` could never pay out** — it called `gl.eth_send`, which does
   not exist in the SDK (`AttributeError` on a real chain). Now
   `gl.get_contract_at(addr).emit_transfer(value=...)`.
5. **Spurious `MAJORITY_DISAGREE`** — see `docs/CONSENSUS.md`. Agreement is now
   a code-level comparison (`_same_decision`) on the canonical shares instead
   of an LLM-judged equivalence. `tests/unit/test_consensus.py` covers it.

## Bugs hit and fixed during development (in order)

This list exists because several of these were structural bugs that
produced no Python traceback at all, and are worth knowing about before
editing this file further:

1. **Schema extraction failure** — early revision used `Address` as a
   `TreeMap` key/public-arg type, and `int`/`dict`/`Optional` return
   types on public views. Fixed by normalizing every public signature to
   `str`/`u256`/`bool` and storing addresses as plain lowercase strings.
2. **`TreeMap <- TreeMap` runtime AssertionError in `__init__`** —
   explicitly constructing `self.jobs = TreeMap[str, str]()` conflicted
   with the storage slot GenVM already allocates from the class
   annotation. Fixed by declaring the fields and never re-assigning them
   in `__init__`.
3. **False "caller is not a registered agent"** — address casing
   mismatch between `str(gl.message.sender_address)` and hand-typed
   `agents_json` addresses. Fixed with `_norm_addr()` applied everywhere
   an address is stored or compared.
4. **`UserWarning: Detected pickling storage class` + corrupted verdict
   data** — the nondet closure called `self._fetch_evidence_pack(...)` /
   `self._build_prompt(...)`, bound methods that capture `self` (and
   therefore the storage-backed `TreeMap`s) into the nondet execution
   environment. Fixed by making both plain module-level functions.
5. **Empty Studio method panel with no traceback, deploy tx still
   finalized** — the two module-level functions from fix #4 were
   physically inserted *between* two class methods in the file, at the
   same 4-space indentation as the surrounding method bodies. Python
   silently parsed everything after them as functions nested inside the
   last module-level function, not methods of `BlameCourt` — the class
   ended up with zero public methods, and nothing ever raised. Fixed by
   moving all module-level helpers to before the class, and verified
   with an AST walk (now codified as `scripts/preflight.py`, which
   reproduces and catches this exact bug — see its own test run in the
   script's development history).
6. **`verdict shares keys do not match job agents` from an empty
   `shares` dict** — `json.loads(raw)` was throwing on the LLM's raw
   output (most likely a `` ```json `` fence, or `response_format="json"`
   not enforcing what was assumed) and silently falling back to a safe
   default with empty shares. Fixed with `_coerce_to_json_obj()` (handles
   an already-parsed object, bytes, a fenced string, or stray wrapping
   text) and a distinct, more specific error message when this happens.
7. **Genuine LLM judgment divergence between independent verdicts** — not
   a code bug: two validators' independent LLM calls landed on
   meaningfully different blame splits (`cause` matched, `shares` did
   not, by more than the 500 bps tolerance) because the original test
   setup left one role's artifact totally unsubmitted while another
   role's fetch merely failed, and the prompt didn't say those should be
   treated the same. Fixed by making that equivalence explicit in the
   prompt (see `docs/VERDICT_FORMAT.md`'s cause-selection rule) and by
   changing the manual test walkthrough so every role submits something.

## Verified live run

The following verdict was produced by a real Studio deployment on the
low-ambiguity scenario in
`tests/integration/low_ambiguity_single_fault_walkthrough.md`, and matches
the scenario's predicted verdict exactly:

```json
{
  "cause": "implement_fail",
  "evidence_used": [
    "https://raw.githubusercontent.com/octocat/Hello-World/master/README",
    "https://raw.githubusercontent.com/octocat/Hello-World/master/THIS-FILE-DOES-NOT-EXIST"
  ],
  "rationale": "The rubric states that if the implementer repo artifact is FETCH_FAILED while every other agent artifact fetches successfully, the cause is implement_fail with 10000 bps to the implementer. The researcher, QA, and publisher artifacts all have usable excerpts, while the implementer repo artifact is exactly FETCH_FAILED.",
  "shares": {
    "0x3779aef2f9b6cd470332a9dc2a3330475b507644": 0,
    "0x5f512824eb3785fa3f3532158c288a27b9b5fc58": 0,
    "0x7255ffa64b297c3af0064ec56f07beef1c04f2ef": 10000,
    "0x790695ee6e46e813b99c50069c0e608acd1a7e3a": 0
  }
}
```

## Deployment

Studio (studionet) address of the current revision (commit `1d89fda`):

```
0x8202BaA1C97FABB3824BACD82BA8FB53d92d18D4
```

This is independently checkable rather than asserted. Against
`https://studio.genlayer.com/api`, `gen_getContractSchema` for that address
lists the 13 public methods in `contracts/blamecourt.py`, and
`gen_getContractCode` returns the contract source (base64), which was
compared with this repo's `contracts/blamecourt.py` and is identical (it uses
`run_nondet_unsafe` + `_same_decision` and `emit_transfer`; it has no
`prompt_comparative` or `eth_send`).

### Live verification on that address

`BLAMECOURT_ADDRESS=` the address above `pytest tests/live -v --network
studionet` ran against it: 3 passed, 39 transactions, real validators and a
real LLM, no mocks. Only `genvm_datetime` was overridden so the appeal window
could be exercised. The jobs are `gate-<ts>`, `fin-<ts>` and `app-<ts>`.

| test | what it confirmed on-chain |
|---|---|
| `test_flag_is_gated_and_bonds_and_bad_deadlines_are_refused` | `create_job` rejects a `bond` field, a malformed agent address, a past deadline and an unparseable deadline. `flag_failed` reverts `cannot flag yet` and `adjudicate` reverts `must be flagged` with nothing submitted and with 3 of 4 agents submitted; the flag succeeds once all four have submitted (`flag_reason: all_submitted`). |
| `test_finalize_path_on_real_validators` | A real-LLM `adjudicate` reaches consensus; stored shares sum to 10000 and are multiples of 1000. Afterwards every credit is unchanged, `withdraw` reverts `nothing to withdraw`, `finalize` reverts `still open` at +10 min and at exactly +3600 s, `appeal` reverts `window has closed` at +3601 s. `finalize` then succeeds once, a second `finalize` is refused, credits equal the canonical verdict's distribution, and every holder's `withdraw` (the `emit_transfer` path) clears their balance with `total_withdrawn` rising by exactly the amount owed. |
| `test_appeal_path_on_real_validators_never_overpays` | An appeal inside the window settles the job once: credits equal escrow + bond (never more), `total_in` rises by exactly the bond, a second appeal and a second `finalize` are both refused. |

Final ledger of that contract after the run (`get_ledger`): `total_in` 300500
>= `total_credited` 200500 >= `total_withdrawn` 100000. The gap between
`total_in` and `total_credited` is the `gate-<ts>` job's 100000 escrow, which
was never judged or settled; the gap between credited and withdrawn is the
`app-<ts>` job's payouts, which were credited but not withdrawn.

Not covered live: an appeal that *changes* the verdict (the real LLM re-run
cannot be forced to disagree). That replace-not-add behaviour is covered by
`tests/unit/test_money_flow.py`, including 200 randomized scenarios.

The verdict below came from an earlier deployment of the low-ambiguity
walkthrough, not from this address.

## Known open issue: "transaction ended undetermined"

Observed during development on a genuinely ambiguous evidence set: every
validator that completed execution computed an *identical*
`contract_state_hash` (and `nondet_disagree: 0`) yet the transaction still
did not accumulate enough `"agree"` votes, and two of five validators
errored out entirely before producing a comparable result at all. This
looks like a consensus/validator-set infrastructure condition, not a bug
in `adjudicate()`'s logic — see `docs/CONSENSUS.md`'s closing section for
the full reasoning. It could not be resolved from the contract side alone
during this build; a reviewer with visibility into Studio's validator
timeout/count configuration would be better positioned to say whether
it's tunable.

## How to verify this submission

```
python scripts/preflight.py contracts/blamecourt.py
pip install pytest
pytest tests/unit -q
pip install -r requirements-test.txt
pytest tests/direct -q
```

Then walk through `tests/integration/low_ambiguity_single_fault_walkthrough.md`
against a live Studio deployment — it's the fastest way to confirm the
full pipeline (schema → deploy → address handling → live fetch → LLM
verdict → consensus → payout) end to end with low variance, before trying
the genuinely ambiguous `ambiguous_multirole_walkthrough.md` scenarios.
