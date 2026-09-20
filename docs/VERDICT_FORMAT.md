# Verdict format & distribution formula

## Schema

```json
{
  "cause": "spec_gap | upstream_fail | implement_fail | qa_miss | publish_fail | multi | non_delivery",
  "shares": {"<agent_address>": "<int bps, sums to 10000>", "...": "..."},
  "evidence_used": ["<url from spec_url or a submitted artifact>", "..."],
  "rationale": "<free text -- NOT an equivalence field>"
}
```

`cause`, `shares`, and `evidence_used` are the equivalence fields (see
`docs/CONSENSUS.md`). `rationale` is explicitly excluded from comparison.

### Cause-selection rule (as given to the model)

1. If every agent has share `0` except exactly one agent at `10000`, pick
   the cause matching that agent's specific failure
   (`implement_fail` / `qa_miss` / `publish_fail` / `spec_gap` /
   `upstream_fail`).
2. If **two or more** agents have a nonzero share, the cause **must** be
   `"multi"` — even if each agent's individual reason was a delivery
   failure.
3. `"non_delivery"` is used only when no agent produced any usable
   artifact at all.

A role with **zero submitted artifacts** must be treated identically to a
role whose artifact excerpt is exactly `FETCH_FAILED` — both mean that
role delivered nothing usable. This equivalence was added after an early
test run showed two independent LLM calls disagreeing specifically
because one treated "never submitted" as weaker evidence than "submission
failed to fetch."

## Distribution formula

For a job with `escrow_total` and `N` registered agents:

```
base_pay_i     = escrow_total // N
slashed_i      = min(base_pay_i, escrow_total * shares[i] // 10000)
pay_i          = base_pay_i - slashed_i
creator_refund = escrow_total - sum(pay_i)
```

Properties this guarantees:

- An agent with ~0 blame share keeps ~all of their base pay.
- An agent with 10000 bps (100% of blame) is slashed to zero pay, capped
  so they can never be pushed negative.
- The books always balance exactly: `sum(pay_i) + creator_refund ==
  escrow_total`.
- A false "100% on agent A" verdict costs A their pay and benefits the
  creator (refund), not the other agents — no incentive for collusion on
  a scapegoat for pure profit beyond simply not losing one's own pay.

Money is credited into `TreeMap[str, u256] credits` and pulled via
`withdraw()` — never pushed directly from `adjudicate`/`appeal`.

## Worked example

`escrow_total = 100000`, 4 agents (researcher, implementer, qa,
publisher), `base_pay = 25000` each.

Verdict:
```json
{
  "cause": "implement_fail",
  "shares": {"researcher": 0, "implementer": 8000, "qa": 2000, "publisher": 0},
  "evidence_used": ["<spec_url>", "<qa_log_url>"],
  "rationale": "..."
}
```

| agent | share (bps) | slashed = min(25000, 100000·share/10000) | pay_i |
|---|---|---|---|
| researcher | 0 | 0 | 25000 |
| implementer | 8000 | 20000 | 5000 |
| qa | 2000 | 5000 | 20000 |
| publisher | 0 | 0 | 25000 |

`creator_refund = 100000 - (25000+5000+20000+25000) = 25000`.

## Verified live example

A verified live Studio run of the deliberately low-ambiguity scenario in
`tests/integration/` produced (addresses redacted to role):

```json
{
  "cause": "implement_fail",
  "evidence_used": [
    "https://raw.githubusercontent.com/octocat/Hello-World/master/README",
    "https://raw.githubusercontent.com/octocat/Hello-World/master/THIS-FILE-DOES-NOT-EXIST"
  ],
  "rationale": "The rubric states that if the implementer repo artifact is FETCH_FAILED while every other agent artifact fetches successfully, the cause is implement_fail with 10000 bps to the implementer. The researcher, QA, and publisher artifacts all have usable excerpts, while the implementer repo artifact is exactly FETCH_FAILED.",
  "shares": {"implementer": 10000, "researcher": 0, "qa": 0, "publisher": 0}
}
```

Exact match to the predicted verdict for that scenario. See
`SUBMISSION.md` for the full build/verification log.
