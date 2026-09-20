# Testing BlameCourt in GenLayer Studio — step by step

Studio actually fetches live URLs and calls a real LLM (no mocks), so this
walkthrough uses real, always-reachable pages instead of made-up ones.
Copy/paste each call in order.

## 0. Before you start

In Studio, open the **Accounts** panel and note at least 4 account
addresses (Studio creates several test accounts by default — you can also
create more). You'll assign one address per role below. Everywhere you see
`<ADDR_A>` / `<ADDR_B>` / `<ADDR_C>` / `<ADDR_D>`, substitute a real address
from your session, all in `0x...` hex string form (that's the only format
this contract accepts — see `get_credit(addr: str)`).

Deploy `contract.py`. `__init__` takes no arguments, so the deploy form
should show no constructor fields — just deploy.

---

## 1. `create_job` (payable)

Call as **Account A** (this account becomes `creator`). Set the **value**
field on the call to `100000` (or any positive integer — this is the
escrow).

| field | value |
|---|---|
| `job_id` | `"job-001"` |
| `spec_url` | `"https://raw.githubusercontent.com/octocat/Hello-World/master/README"` |
| `rubric` | see below |
| `deadline` | `"2099-01-01T00:00:00Z"` |
| `agents_json` | see below |

`rubric` (paste as one string):
```
Blame the implementer if their repo artifact is missing, unreachable, or
does not address the spec. Blame QA if they signed off on work that later
proves broken. Blame the publisher if implementation and QA both check out
but the publish URL 404s. Blame the researcher only if the spec itself is
ambiguous. If two or more roles are clearly at fault, use cause "multi" and
split blame across them. If no agent produced any usable artifact, use
cause "non_delivery".
```

`agents_json` (replace the four addresses with your own, keep it valid
JSON on one line):
```json
[{"addr":"<ADDR_A>","role":"researcher","bond":100},{"addr":"<ADDR_B>","role":"implementer","bond":100},{"addr":"<ADDR_C>","role":"qa","bond":100},{"addr":"<ADDR_D>","role":"publisher","bond":100}]
```

Note: Account A is both `creator` **and** the `researcher` agent here —
that's fine, the contract doesn't forbid it, and it keeps this walkthrough
to 4 accounts total.

Expect: transaction succeeds, no return value.

---

## 2. `submit_artifact` — a real, fetchable artifact (as Account B / implementer)

| field | value |
|---|---|
| `job_id` | `"job-001"` |
| `url` | `"https://raw.githubusercontent.com/octocat/Hello-World/master/README"` |
| `kind` | `"repo"` |

(Re-using the same page as the spec is fine for a smoke test — the point
is just to prove a *successful* fetch path end to end. Swap in any other
publicly reachable page you like.)

## 3. `submit_artifact` — a deliberately broken artifact (as Account C / qa)

| field | value |
|---|---|
| `job_id` | `"job-001"` |
| `url` | `"https://raw.githubusercontent.com/this-path-does-not-exist-9f8x/does-not-exist/main/nope.txt"` |
| `kind` | `"log"` |

This one will 404, so the evidence pack the LLM sees for this artifact
should read `"excerpt": "FETCH_FAILED"` — good for confirming the
FETCH_FAILED → likely-fault-signal path actually reaches the prompt.

---

## 4. `flag_failed` (as Account A, the creator)

| field | value |
|---|---|
| `job_id` | `"job-001"` |

Expect: `get_status("job-001")` now returns `"ready_for_adjudication"`.

---

## 5. `adjudicate` (as any account)

| field | value |
|---|---|
| `job_id` | `"job-001"` |

This is the interesting one to watch in Studio's validator panel: each
validator independently fetches both URLs above, calls the LLM, and you
can inspect whether they converge under the comparative equivalence check.

If it reverts, the traceback will tell you which local validation step
failed (`shares sum to ...`, `verdict cites unknown/unfetched URL`, etc.) —
those are all intentional hard-fails, not bugs, per the "no silent
coercion" requirement. A real LLM's shares may not sum to exactly 10000
sometimes; a revert there is expected behavior, not a contract defect —
just retry.

---

## 6. Inspect results

```
get_status("job-001")      -> "adjudicated"
get_verdict("job-001")     -> JSON string, e.g.
  {"cause":"implement_fail","evidence_used":[...],
   "rationale":"...","shares":{"<ADDR_A>":0,"<ADDR_B>":...,...}}
get_job("job-001")         -> full job JSON, including escrow_total & agents
get_credit("<ADDR_B>")     -> u256, the implementer's credited payout
get_credit("<ADDR_A>")     -> u256, includes creator's refund share
```

With `escrow_total = 100000` and 4 agents, `base_pay = 25000` per agent
before slashing — use that to sanity-check the numbers against whatever
`shares` the LLM actually returned (same math as `examples.md`).

---

## 7. `withdraw` (as whichever account has a nonzero credit)

| field | value |
|---|---|
| *(no args)* | |

Expect: their `credits` entry zeroes out and `get_credit` for that address
returns `0` afterward. (If Studio's simulated chain doesn't actually move
GEN on `gl.eth_send` the way a real network would, that's expected in a
local/simulator context — the accounting side, which is what this test
plan is checking, still updates correctly.)

---

## 8. Optional: `appeal` (as any registered agent)

| field | value |
|---|---|
| `job_id` | `"job-001"` |

Set **value** to any positive integer (e.g. `10000`) — this is the appeal
bond. This re-runs the whole fetch+LLM pipeline again from scratch; compare
the new `get_verdict("job-001")` to the one from step 6. `get_status` should
end at `"final"` either way — check `get_credit` on the appellant's address
to see whether their bond was forfeited (verdict held) or refunded
(verdict changed).

---

## Things to try if you want to exercise the failure paths

- Call `create_job` twice with the same `job_id` → expect a revert
  (`duplicate job_id`).
- Call `submit_artifact` from an account **not** in `agents_json` → expect
  `caller is not a registered agent on this job`.
- Call `adjudicate` on a job with zero artifacts submitted → expect
  `no artifacts submitted; nothing to adjudicate`.
- Call `adjudicate` a second time on an already-`adjudicated` job (without
  appealing) → expect `use appeal() instead`.
