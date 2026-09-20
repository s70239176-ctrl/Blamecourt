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
   from that evidence via `gl.eq_principle.prompt_comparative`.
3. **Deterministic settlement** computes exactly who gets paid, by a
   documented, auditable formula — never coerced or eyeballed.
4. **Versioned, appealable state** — a verdict can be appealed once, and
   appeal re-runs the whole evidence-and-judgment pipeline independently
   rather than trusting the first result forever.

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
flag_failed  /  deadline passes
      |
      v
adjudicate
  validators independently fetch spec + artifacts,
  independently ask an LLM for a verdict,
  comparative consensus on cause + shares + evidence_used
      |
      v
verdict stored, escrow distributed by documented formula
      |
      +----------------------+
                             v
                       appeal (once)
                 re-runs the whole pipeline independently
                             |
                             v
                       final distribution
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

BlameCourt uses `gl.eq_principle.prompt_comparative`, not `strict_eq`,
because the non-deterministic step is "fetch live pages, then have an LLM
write free-form rationale plus a structured verdict" — two honest
validators running that independently will not produce byte-identical
JSON. `strict_eq` would make honest validators disagree by construction.

Validators are told two verdicts are equivalent iff:
- `cause` is an identical string;
- `shares` cover the same agent addresses (compared case-insensitively),
  each value is within 500 bps of the other verdict's, and both sum to
  10000 (±1 for rounding);
- every `evidence_used` URL in either verdict was actually part of the
  fetched evidence pack — a citation of an unfetched URL invalidates that
  verdict, it does not just make it "different";
- `rationale` wording is ignored entirely.

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
