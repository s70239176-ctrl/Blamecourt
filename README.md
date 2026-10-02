# BlameCourt

**A multi-agent blame-assignment and escrow-arbitration primitive on GenLayer.**

BlameCourt answers a question ordinary escrow contracts cannot:
> When a multi-agent workflow fails, who is actually at fault — based on
> live, independently-verified evidence, not on whichever agent tells the
> most convincing story?

It is a **standalone Intelligent Contract primitive**, not a product
application. There is intentionally no frontend and no backend service.
Any workflow orchestrator, agent framework, or marketplace that pays
multiple agents out of a shared bounty can point its escrow at BlameCourt
and get a fault-based payout when the job fails.

## Why this exists

[#why-this-exists](#why-this-exists)

A failed multi-agent job (research → implement → QA → publish, or any
N-agent pipeline) usually comes with N self-serving explanations:

- the implementer says QA never actually ran the tests;
- QA says the implementer's branch was never really finished;
- the publisher says nobody told them it was ready.

A normal escrow contract can only pay out by a rule agreed in advance
(all-or-nothing, or an arbiter's word). It cannot itself determine *whose*
account of the failure is true. A centralized arbiter could, but then one
party's word (or one server's word) decides who gets paid. BlameCourt
splits the problem the way GenLayer is built for:

1. **Live evidence fetch** — the spec URL and every agent's declared
   artifact URLs are fetched fresh, inside the same consensus round, not
   taken from anyone's say-so.
2. **GenLayer consensus** resolves a bounded blame-assignment decision
   from that evidence via `gl.vm.run_nondet_unsafe`, with validators
   comparing the canonical payout shares in code.
3. **Deterministic settlement** computes exactly who gets paid, by a
   documented, auditable formula — never coerced or eyeballed.
4. **Versioned, appealable state** — a verdict can be appealed once, and
   appeal re-runs the whole evidence-and-judgment pipeline independently
   rather than trusting the first result forever. Nothing is withdrawable
   until the verdict is final, so a replaced verdict never has to be
   clawed back.

## Core primitive

A job has one escrow, N registered agents (each with a declared role), a
spec, a rubric describing how blame should be assigned, and a set of
agent-submitted artifact URLs. BlameCourt does not care what the pipeline
actually builds — only that its participants, evidence, and payout are
each addressable and auditable.

## Lifecycle

```
create_job (escrow locked)
      |
      v
submit_artifact  (each agent, 0..16 per job)
      |
      v
flag_failed  (only after the deadline, or once every agent has submitted)
      |
      v
adjudicate
  validators independently fetch spec + artifacts,
  independently ask an LLM for a verdict,
  comparative consensus on cause + shares + evidence_used
      |
      v
verdict stored, appeal window opens (nothing is withdrawable yet)
      |
      +---------------------------+------------------------------+
      v                           v                              v
 window closes, no appeal    appeal (once, in window)     (appeal rejected)
 finalize()                  re-runs the whole pipeline   bond -> creator
      |                           |   verdict replaced           |
      +------------+--------------+------------------------------+
                   v
        settled once from the final verdict -> withdraw()
```

## Verdict schema

```json
{
  "cause": "spec_gap | upstream_fail | implement_fail | qa_miss | publish_fail | multi | non_delivery",
  "shares": {"<agent_address>": "<int bps, sums to 10000>", "...": "..."},
  "evidence_used": ["<a URL that was actually fetched this round>", "..."],
  "rationale": "<free text -- explicitly NOT an equivalence field>"
}
```

See [docs/VERDICT_FORMAT.md](docs/VERDICT_FORMAT.md) for the full field
reference, the distribution formula, and worked examples.

## What consensus actually does

BlameCourt uses `gl.vm.run_nondet_unsafe` with a leader function and a
**validator function written in plain code** (`_same_decision`), not
`strict_eq` and not an LLM judge. The non-deterministic step is "fetch live
pages, then have an LLM write free-form rationale plus a structured verdict",
so two honest validators will never produce byte-identical JSON — `strict_eq`
would make them disagree by construction. But asking *another* LLM to decide
whether two verdicts are "equivalent" (`prompt_comparative`) made agreement
depend on a model following an instruction, and on validators running
different model families it produced spurious disagreements.

Each validator independently re-runs the fetch + LLM step and agrees with the
leader iff:
- `cause` is an identical string;
- `shares` cover the same agent addresses (case-insensitively) and are,
  address by address, EXACTLY equal. Each producer first canonicalizes its own
  raw shares onto a fixed 1000-bps grid (largest-remainder apportionment), so
  the numbers compared are the same numbers `_compute_distribution` pays out.
  A tolerance on the *raw* numbers cannot bound the payout once only one
  side's numbers are the ones actually spent, so none is used (see
  [docs/CONSENSUS.md](docs/CONSENSUS.md));
- every `evidence_used` URL the leader cites was actually in the fetched
  evidence pack;
- `rationale` wording and the exact set of URLs cited are ignored.

After consensus, a second, fully deterministic, storage-touching pass
re-validates the accepted payload (enum membership, exact key-set match,
exact bps sum, evidence subset) and **hard-fails rather than silently
coercing** an invalid result. See
[docs/CONSENSUS.md](docs/CONSENSUS.md) for the full design rationale,
the attack model (false-positive / false-negative blame), and what a
genuinely "undetermined" transaction means versus a code bug.

## Security / robustness properties

- Every address that enters storage or a comparison is normalized
  (`_norm_addr`) — casing differences between how GenVM stringifies an
  `Address` and how an address was typed by hand cannot silently produce
  a false "not a registered agent."
- The nondet evidence-fetch and prompt-construction helpers are
  module-level functions, never bound methods — nothing inside the
  non-deterministic block can capture `self` and pull the storage-backed
  `TreeMap`s into a nondet execution context.
- LLM output is defensively coerced (handles an already-parsed
  object, bytes, a ```json-fenced string, or stray wrapping text) before
  being treated as the verdict, rather than trusting `response_format`
  enforcement blindly.
- A validation failure names exactly what's wrong (which addresses were
  extra/missing, whether `shares` was empty because parsing failed vs.
  genuinely mismatched) instead of a single generic error, precisely
  because debugging this contract against a real GenVM build surfaced how
  costly an opaque failure is.
- Money moves only through a pull-payment ledger (`credits` +
  `withdraw()`), never a direct push transfer out of `adjudicate`/`appeal`.
- Credits are written exactly once per job, from the final verdict, so an
  upheld appeal replaces the original distribution instead of adding to
  it. A ledger (`total_in` >= `total_credited` >= `total_withdrawn`) is
  enforced on every write and readable via `get_ledger()`.
- A job cannot be flagged as failed (and so cannot have silence judged as
  non-delivery) before its validated deadline, unless every agent has
  already submitted.
- Agents are `{addr, role}`. There are no agent bonds; `create_job`
  rejects the field rather than ignoring it.

## Repository layout

```
contracts/blamecourt.py             Intelligent Contract
docs/ARCHITECTURE.md                Job lifecycle, storage shape, state design
docs/CONSENSUS.md                   Equivalence principle design + attack model
docs/VERDICT_FORMAT.md              Verdict schema, distribution formula, examples
docs/INTEGRATION.md                 How another contract/orchestrator should call this
fixtures/                           Example agents_json + rubric used in tests/docs
scripts/preflight.py                Static structural checks (schema-safety, class shape)
scripts/deploy_studionet.sh         Minimal Studio/StudioNet deploy helper
tests/unit/                         Money-flow/solvency tests, plain pytest + SDK stub
tests/direct/                       gltest suite with mocked web + mocked LLM
tests/integration/                  Manual Studio walkthroughs (no mocks -- live fetch/LLM)
SUBMISSION.md                       Build log: known API guesses, verified runs, open issues
```

## Tests

Run the static structural checks (no GenVM required — this is what would
have caught the schema-nesting bug documented in `SUBMISSION.md` before
ever deploying):
```
python scripts/preflight.py contracts/blamecourt.py
```

Run the money-flow and solvency tests (plain pytest; they execute the
real `contracts/blamecourt.py` against a small in-process SDK stand-in, so
no GenVM is needed):
```
pip install pytest
pytest tests/unit -q
```

Run the mocked gltest suite:
```
pip install -r requirements-test.txt
pytest tests/direct -q
```

Manual, live-fetch/live-LLM walkthroughs (Studio or StudioNet, no mocks):
see `tests/integration/`.

## Deployment

```
genlayer network set studionet
genlayer deploy --contract contracts/blamecourt.py
```

See `scripts/deploy_studionet.sh` for a minimal helper, and
`SUBMISSION.md` for this revision's actual verified deploy/adjudicate
evidence.

**Studio deployment of this revision:** `0x987165A94d2E865fd04291Dd495ec0a4c719740b` (studionet). Its source
can be re-checked against `contracts/blamecourt.py` with the
`gen_getContractCode` RPC call (base64-encoded) — see `SUBMISSION.md`.

## Why this is a primitive, not an app

BlameCourt does not orchestrate the underlying workflow, run agents, or
provide a dashboard. It answers one reusable shared question:
> **Given this job's declared spec, rubric, and live evidence, who is at
> fault, and how should the escrow be split?**

That decision can be consumed by any orchestrator, marketplace, or escrow
system built on top of a multi-agent pipeline, without trusting a
centralized arbiter.

## License

MIT
