# BlameCourt — a clear, low-ambiguity test case

The earlier walkthrough deliberately mixed a "no submission at all" role
(publisher) with a "fetch failed" role (QA), which is exactly the kind of
gap that produced disagreeing LLM verdicts and an "undetermined" tx. This
version removes every source of that ambiguity on purpose:

- **Every single role submits an artifact.** No role is ever silently
  absent, so there's nothing for the model to interpret inconsistently
  between runs.
- **Exactly one artifact is broken (404); all three others fetch
  successfully** and are unambiguous. Only one plausible fault exists.
- **The rubric spells out the exact expected mapping** instead of leaving
  causal interpretation open, to keep independent LLM calls converging.

This is a deliberately "easy" case to prove the pipeline works end to end
with low variance — not a realistic dispute. Once this passes cleanly, go
back to `studio_testing_guide.md` / `examples.md` for the harder,
genuinely ambiguous scenarios (that's where you *want* some disagreement
and appeal-worthy nuance).

---

## 0. Before you start

Open Studio's **Accounts** panel. Pick 4 real account addresses from your
session and write them down somewhere — you'll paste them in as plain
`0x...` strings with **no brackets, no quotes beyond the JSON's own
quotes, no extra characters**. (A literal `<` or `>` around an address, or
an extra/missing hex digit, is a real bug that happened last round — the
value in the JSON must be the bare address and nothing else.)

Call them, in your own notes, `ADDR_1`, `ADDR_2`, `ADDR_3`, `ADDR_4` —
just so you know which one is which below. You'll use `ADDR_1` for both
the job creator and the researcher role, same as before, to keep this to 4
accounts.

Deploy `contract.py` fresh (no constructor args).

---

## 1. `create_job` (payable, as the account you're calling `ADDR_1`)

Set **value** to `100000`.

| field | value |
|---|---|
| `job_id` | `"clear-test-001"` |
| `spec_url` | `"https://raw.githubusercontent.com/octocat/Hello-World/master/README"` |
| `deadline` | `"2099-01-01T00:00:00Z"` |

`rubric` — paste exactly:
```
The implementer's repo artifact must be fetchable. If the implementer's
artifact fetch fails (FETCH_FAILED) while every other agent's artifact
fetches successfully, the cause is "implement_fail" and the implementer
bears all blame (10000 bps to the implementer, 0 to everyone else). Do
not assign blame to any other agent in that scenario.
```

`agents_json` — replace each `ADDR_n` below with the real address string
you wrote down, keeping everything else character-for-character identical
(one line, valid JSON, no trailing comma):
```json
[{"addr":"ADDR_1","role":"researcher","bond":100},{"addr":"ADDR_2","role":"implementer","bond":100},{"addr":"ADDR_3","role":"qa","bond":100},{"addr":"ADDR_4","role":"publisher","bond":100}]
```

So if, say, your four Studio addresses were
`0x1111111111111111111111111111111111aaaa`,
`0x2222222222222222222222222222222222bbbb`,
`0x3333333333333333333333333333333333cccc`, and
`0x4444444444444444444444444444444444dddd`, the field you'd actually paste
in is:
```json
[{"addr":"0x1111111111111111111111111111111111aaaa","role":"researcher","bond":100},{"addr":"0x2222222222222222222222222222222222bbbb","role":"implementer","bond":100},{"addr":"0x3333333333333333333333333333333333cccc","role":"qa","bond":100},{"addr":"0x4444444444444444444444444444444444dddd","role":"publisher","bond":100}]
```
(That example is illustrative only — use your own real addresses, not
these.)

---

## 2. Submit one artifact per role — every role, no exceptions

Call `submit_artifact` four times, once per registered address, with
`job_id = "clear-test-001"` each time.

| caller | `url` | `kind` |
|---|---|---|
| `ADDR_1` (researcher) | `"https://raw.githubusercontent.com/octocat/Hello-World/master/README"` | `"note"` |
| `ADDR_2` (implementer) | `"https://raw.githubusercontent.com/octocat/Hello-World/master/THIS-FILE-DOES-NOT-EXIST"` | `"repo"` |
| `ADDR_3` (qa) | `"https://raw.githubusercontent.com/octocat/Hello-World/master/README"` | `"log"` |
| `ADDR_4` (publisher) | `"https://raw.githubusercontent.com/octocat/Hello-World/master/README"` | `"api"` |

Only the implementer's URL 404s. All three other roles deliberately reuse
the *exact same* `Hello-World/master/README` URL as the spec — that one
has already been directly confirmed to fetch successfully through GenVM
in your earlier test run (the model read its content as `"Hello World!"`),
so it's not a guess this time. Reusing one confirmed-good URL three times
is intentional: it removes any dependency on a second repo/path whose
exact branch or filename can't be verified in advance, which is exactly
what broke the previous version of this test (a guessed Spoon-Knife path
that turned out not to resolve, silently turning a clean single-fault case
into a 3-way failure).

---

## 3. `flag_failed` (as `ADDR_1`, the creator)

`job_id = "clear-test-001"`.

## 4. `adjudicate` (as any account)

`job_id = "clear-test-001"`.

Expected verdict, given the setup above:
```json
{"cause":"implement_fail","shares":{"ADDR_1":0,"ADDR_2":10000,"ADDR_3":0,"ADDR_4":0},"evidence_used":["https://raw.githubusercontent.com/octocat/Hello-World/master/README","https://raw.githubusercontent.com/octocat/Hello-World/master/THIS-FILE-DOES-NOT-EXIST"],"rationale":"..."}
```
(`ADDR_2` is the implementer in this example — replace with whichever
address you actually assigned that role. `evidence_used` should list at
most those two distinct URLs, since the other three artifacts all point
at the same URL as the spec.)

---

## 5. Check the result

```
get_status("clear-test-001")   -> "adjudicated"
get_verdict("clear-test-001")  -> the JSON above (cause, shares may vary
                                   slightly in rationale wording, but
                                   cause and shares should match exactly
                                   given how tightly the rubric pins them)
get_credit(ADDR_2)             -> 0          (implementer, fully slashed)
get_credit(ADDR_1)             -> 50000      (25000 base pay as researcher
                                                + 25000 creator refund)
get_credit(ADDR_3)             -> 25000      (qa, untouched)
get_credit(ADDR_4)             -> 25000      (publisher, untouched)
```

If this comes back clean and matches, the entire pipeline — schema,
deploy, address handling, live fetch, LLM verdict, consensus, and payout
— is confirmed working. From there, the ambiguous multi-role scenarios in
`studio_testing_guide.md` are worth trying specifically *because* they can
disagree — that's the system correctly refusing to force a verdict on
genuinely unclear evidence, not a failure.
