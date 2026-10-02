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
| `deadline` | **now + 5 minutes**, UTC, e.g. `"2026-10-02T14:35:00Z"` |
| `agents_json` | see below |

The deadline must be a valid ISO-8601 time in the future (the call reverts
otherwise). It is short on purpose: only two of the four roles submit an
artifact in this scenario, so `flag_failed` is not allowed until the
deadline has passed (step 4).

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
[{"addr":"<ADDR_A>","role":"researcher"},{"addr":"<ADDR_B>","role":"implementer"},{"addr":"<ADDR_C>","role":"qa"},{"addr":"<ADDR_D>","role":"publisher"}]
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

First call it **right away**, before the deadline. Expect a revert:
`cannot flag yet: the deadline has not passed and not every registered
agent has submitted an artifact`. (Only two of four roles have submitted,
so this is the guard against judging silence as non-delivery too early.)

Then wait until the deadline from step 1 has passed and call it again.
Expect success, and `get_status("job-001")` now returns
`"ready_for_adjudication"` (`get_job` shows `"flag_reason":
"deadline_passed"`).

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

## 6. Inspect the verdict (nothing is payable yet)

```
get_status("job-001")      -> "adjudicated"
get_verdict("job-001")     -> JSON string, e.g.
  {"cause":"implement_fail","evidence_used":[...],
   "rationale":"...","shares":{"<ADDR_A>":0,"<ADDR_B>":...,...}}
get_job("job-001")         -> full job JSON; note appeal_deadline_ts
get_credit("<ADDR_B>")     -> 0   (every address is 0 at this point)
get_ledger()               -> total_in=100000, total_credited=0, total_withdrawn=0
```

Every `get_credit` is `0` and `withdraw` reverts with `nothing to
withdraw`: credits are only written once the verdict is final (step 7 or
8). Every share is a multiple of 1000 and they sum to 10000.

---

## 7. Either appeal (inside the 1-hour window) ...

Call `appeal` as any registered agent with `job_id = "job-001"` and **value**
set to any positive integer (e.g. `10000`) — this is the appeal bond. It
re-runs the whole fetch+LLM pipeline from scratch and settles the job
immediately. `get_status` ends at `"final"`. Then check `get_credit` for
every address:

- verdict **changed**: the new verdict *replaces* the old one. Credits are
  the new distribution only (plus the refunded bond on the appellant), and
  they add up to `escrow + bond` — never more.
- verdict **unchanged**: the original distribution stands and the bond is
  credited to the creator.

## 8. ... or finalize (after the window)

If nobody appeals, wait until `appeal_deadline_ts` (one hour after step 5)
has passed, then call `finalize("job-001")` as anyone. Calling it earlier
reverts with `appeal window is still open`. `get_status` becomes `"final"`
and `get_credit` now returns each payout (`base_pay = 25000` per agent
before slashing; see `docs/VERDICT_FORMAT.md`).

## 9. `withdraw` (as whichever account has a nonzero credit)

| field | value |
|---|---|
| *(no args)* | |

Expect: their `credits` entry zeroes out and `get_credit` for that address
returns `0` afterward. When everyone has withdrawn, `get_ledger()` shows
`total_in == total_credited == total_withdrawn`. (If Studio's simulated
chain doesn't actually move GEN on `gl.eth_send` the way a real network
would, that's expected in a local/simulator context — the accounting side
still updates correctly.)

---

## Things to try if you want to exercise the failure paths

- Call `create_job` twice with the same `job_id` → expect a revert
  (`duplicate job_id`).
- Call `submit_artifact` from an account **not** in `agents_json` → expect
  `caller is not a registered agent on this job`.
- Call `flag_failed` or `adjudicate` before the deadline while some agent
  has not submitted → expect `cannot flag yet` / `job must be flagged ...`.
- Call `create_job` with `"bond"` in an agent entry → expect `agent bonds are
  not implemented`.
- Call `adjudicate` a second time on an already-`adjudicated` job →
  expect `use appeal() or finalize()`.
- Call `appeal` after the window → expect `appeal window has closed`.
