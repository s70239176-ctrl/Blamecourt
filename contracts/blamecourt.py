# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
import json
import datetime as _dt

# ----------------------------------------------------------------------
# BlameCourt -- multi-agent blame assignment & escrow arbitration
# ----------------------------------------------------------------------
# This file was rewritten specifically to pass Studio's schema extraction
# (getContractSchemaForCode). All persistent fields and ALL public method
# argument/return types use only schema-safe primitives (str, u256) or
# fully-specialized TreeMap[str, str] / TreeMap[str, u256]. Everything
# structured (jobs, agents, artifacts, verdicts) lives inside plain JSON
# strings, parsed/dumped with the stdlib `json` module. See the notes at
# the bottom of this file for the full list of schema-breaking constructs
# that were removed from the previous version.
#
# Product behavior (job lifecycle, evidence fetch, LLM verdict, comparative
# consensus, distribution math, appeal) is unchanged from the prior
# version -- only types and storage shape changed.

# ----------------------------------------------------------------------
# Constants (module-level, not part of the ABI)
# ----------------------------------------------------------------------

MAX_ARTIFACTS_PER_JOB = 16
SPEC_TRUNCATE_CHARS = 20000
ARTIFACT_TRUNCATE_CHARS = 8000
SHARE_BPS_TOTAL = 10000
# Payout-driving shares are canonicalized (see `_quantize_shares`) onto a
# grid this wide *before* validators compare them, so consensus agreement
# and the money that agreement authorizes are the same numbers -- not an
# agreement-within-tolerance on one set of numbers that then pays out
# whichever leader's un-quantized numbers happened to be accepted.
SHARE_BUCKET_BPS = 1000
APPEAL_SHARE_TOLERANCE_BPS = 500
MAX_APPEALS = 1
# Credits are NOT written to the withdrawable ledger when a verdict is
# produced. They are written exactly once, when the verdict becomes final:
# either an appeal resolves, or this many seconds pass with no appeal and
# someone calls `finalize`. Until then nothing can be withdrawn, so a
# verdict that an appeal replaces never has to be clawed back.
APPEAL_WINDOW_SECONDS = 3600

VALID_CAUSES = (
    "spec_gap",
    "upstream_fail",
    "implement_fail",
    "qa_miss",
    "publish_fail",
    "multi",
    "non_delivery",
)

STATUS_OPEN = "open"
STATUS_SUBMITTED = "submitted"
STATUS_READY = "ready_for_adjudication"
STATUS_ADJUDICATED = "adjudicated"
STATUS_FINAL = "final"

FLAG_DEADLINE_PASSED = "deadline_passed"
FLAG_ALL_SUBMITTED = "all_submitted"


# ----------------------------------------------------------------------
# Plain helper functions (module-level, not methods -- never touched by
# schema extraction at all).
# ----------------------------------------------------------------------


def _canon(obj) -> str:
    """Canonical JSON: sorted keys, compact separators. Used any time a
    structured blob is stored or compared."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _truncate(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated " + str(len(text) - limit) + " chars]"


def _norm_addr(addr) -> str:
    """Canonical address form used for every dict key / comparison in this
    contract. `str(gl.message.sender_address)` and an address typed by
    hand into `agents_json` are not guaranteed to share the same casing
    (e.g. EIP-55 checksummed vs lowercase) or to be free of incidental
    whitespace from copy/paste -- both would make an exact-string dict
    lookup fail even though the underlying address is identical. Every
    address this contract stores or compares goes through this function
    first."""
    return str(addr).strip().lower()


def _is_hex_addr(value: str) -> bool:
    if len(value) != 42 or not value.startswith("0x"):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


def _parse_ts(value) -> int:
    """ISO-8601 -> epoch seconds. Raises on anything unparseable: a
    deadline that cannot be read must never silently mean 'no deadline'."""
    text = str(value).strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        raise Exception("not a valid ISO-8601 timestamp: " + str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return int(parsed.timestamp())


def _now_ts() -> int:
    """Transaction time in epoch seconds, from the SDK's `message_raw`
    (the only chain-time accessor the SDK defines). Hard-fails rather than
    returning a default, so a missing clock can never disable a deadline."""
    return _parse_ts(gl.message_raw["datetime"])


def _extract_json_text(raw) -> str:
    """Best-effort extraction of a JSON object from raw LLM output.
    `response_format="json"` is passed to `gl.nondet.exec_prompt` to ask
    for JSON-only output, but that parameter's actual enforcement could
    not be confirmed for this GenVM build, and LLMs commonly wrap JSON in
    a ```json ... ``` fence even when told not to. Rather than let a
    fenced or lightly-decorated response fall through to the
    unparseable-JSON fallback (which zeroes out `shares` and therefore
    ALWAYS fails the "shares keys match agents" check downstream), this
    strips a leading/trailing code fence and, failing that, extracts the
    first-to-last `{...}` span before giving up."""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        json.loads(text)
        return text
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            pass
    return text  # give up -- caller's json.loads will fail and use the safe fallback


def _coerce_to_json_obj(raw):
    """Turn whatever `gl.nondet.exec_prompt(..., response_format="json")`
    actually returned into a parsed JSON object. Handles three shapes,
    since it could not be confirmed which one this GenVM build produces:
    (a) already a parsed dict/list -- `response_format="json"` may mean
        the runtime parses it for you, in which case treating `raw` as a
        string (as the previous version of this function did) throws
        immediately and was silently swallowed by the caller's fallback;
    (b) bytes -- decoded as UTF-8;
    (c) a str -- run through `_extract_json_text` (handles a ```json
        fence or stray leading/trailing text) then `json.loads`.
    Raises on genuine failure; the caller is responsible for the safe
    fallback."""
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return json.loads(_extract_json_text(raw if isinstance(raw, str) else str(raw)))


def _quantize_shares(shares: dict, bucket_bps: int, total_bps: int) -> dict:
    """Collapse a raw, LLM-produced bps allocation onto a coarse fixed grid
    (multiples of `bucket_bps`) via the largest-remainder method, so that
    the numbers validators compare -- and the numbers `_compute_distribution`
    actually pays out -- are drawn from a small, discrete set instead of the
    full 0..10000 range. Two independent LLM calls reading the same
    evidence virtually never agree bit-for-bit, but they overwhelmingly
    land in the same bucket once quantized; when they genuinely don't
    (a raw share sits right on a bucket boundary between the two runs),
    that is a real disagreement about blame, not rounding noise, and
    letting the equivalence check reject it is correct, not a false
    negative to be tolerated away.

    Only ever called on a `shares` dict that has already passed the
    key-set/non-negative/sums-to-`total_bps` checks -- it does not
    re-validate those, it only re-grids values that are already sane, and
    it is deterministic given its input: same raw shares in, same
    canonical allocation out, every time, on either side of a comparison."""
    if not shares:
        return {}
    addrs = list(shares.keys())
    units = {a: int(shares[a]) // bucket_bps for a in addrs}
    remainder = {a: int(shares[a]) - units[a] * bucket_bps for a in addrs}
    target_units = total_bps // bucket_bps
    deficit = target_units - sum(units.values())

    # Hand out the +1 bucket bumps needed to reach target_units to the
    # addresses with the largest leftover bps first (classic largest-
    # remainder / Hamilton apportionment). Ties broken by address string
    # so two runs over identical input always pick the same winners.
    give = sorted(addrs, key=lambda a: (-remainder[a], a))
    i = 0
    while deficit > 0 and i < len(give):
        units[give[i]] += 1
        deficit -= 1
        i += 1

    # Symmetric case: rounding down left one bucket too many -- claw one
    # back from the smallest-remainder addresses first.
    take = sorted(addrs, key=lambda a: (remainder[a], a))
    j = 0
    while deficit < 0 and j < len(take):
        if units[take[j]] > 0:
            units[take[j]] -= 1
            deficit += 1
        j += 1

    return {a: units[a] * bucket_bps for a in addrs}


def _fetch_evidence_pack(spec_url, artifacts):
    """Runs INSIDE a nondet context. Deliberately a module-level function,
    NOT a bound method -- a bound method (`self._fetch_evidence_pack`)
    closes over `self`, which holds the storage-backed `jobs`/`credits`
    TreeMaps, and pulling that into the nondet closure is exactly what
    triggers `UserWarning: Detected pickling storage class` / "Reading
    storage in nondet mode is not supported." This function only ever
    sees plain str/list arguments, so no storage reference can leak in."""
    try:
        spec_text = gl.nondet.web.render(spec_url, mode="text")
    except Exception:
        spec_text = "FETCH_FAILED"
    spec_text = _truncate(spec_text, SPEC_TRUNCATE_CHARS)

    fetched = []
    for art in artifacts:
        try:
            body = gl.nondet.web.render(art["url"], mode="text")
            excerpt = _truncate(body, ARTIFACT_TRUNCATE_CHARS)
        except Exception:
            excerpt = "FETCH_FAILED"
        fetched.append(
            {
                "url": art["url"],
                "kind": art["kind"],
                "submitter": art["submitter"],
                "excerpt": excerpt,
            }
        )
    return {"spec_excerpt": spec_text, "artifacts": fetched}


def _build_prompt(rubric, spec_url, agents_desc, evidence):
    """Also module-level for the same reason as `_fetch_evidence_pack`
    above -- called from inside the nondet closure, so it must not be a
    bound method that captures `self`."""
    known_urls = [spec_url] + [a["url"] for a in evidence["artifacts"]]
    payload = {
        "rubric": rubric,
        "spec_url": spec_url,
        "spec_excerpt": evidence["spec_excerpt"],
        "agents": agents_desc,
        "artifacts": evidence["artifacts"],
        "known_urls": known_urls,
        "cause_enum": list(VALID_CAUSES),
    }
    return (
        "You are an impartial blame-assignment arbitrator for a multi-agent "
        "software workflow. Decide why the job failed and how blame splits "
        "across the agents.\n\n"
        "STRICT RULES:\n"
        "- Use ONLY the text given below in `spec_excerpt` and each artifact's "
        "`excerpt`. Do not use outside knowledge of any project, repo, or URL.\n"
        "- Never invent URLs, commit hashes, log lines, or filenames that do not "
        "literally appear in the excerpts.\n"
        "- If an agent's role was expected (per the rubric) to have produced a "
        "given artifact and that artifact's excerpt is exactly the string "
        "'FETCH_FAILED', treat that as strong evidence of non_delivery for that "
        "agent's role, unless another artifact clearly shows the work was done.\n"
        "- `cause` MUST be exactly one of the values in `cause_enum` below. "
        "Choose it by this rule, in order: if every agent has a share of 0 "
        "except exactly one agent at 10000, pick the cause that matches that "
        "agent's specific failure (e.g. implement_fail, qa_miss, "
        "publish_fail, spec_gap, upstream_fail). If TWO OR MORE agents have "
        "a nonzero share, you MUST use \"multi\" -- do not use \"non_delivery\" "
        "in that case even if the reason for each agent's fault was that "
        "they failed to deliver something. Use \"non_delivery\" ONLY when "
        "no agent produced any usable artifact at all (i.e. every artifact "
        "excerpt is FETCH_FAILED or an agent's role has zero artifacts) and "
        "you are assigning shares based on which role(s) were first in the "
        "pipeline to fail to deliver.\n"
        "- An agent whose role has ZERO artifacts listed in `artifacts` "
        "below (their role never appears as a `submitter`) must be treated "
        "IDENTICALLY to an agent whose artifact excerpt is exactly "
        "'FETCH_FAILED' -- both mean that role delivered nothing usable. Do "
        "not treat 'no submission at all' as weaker or stronger evidence "
        "than 'submission failed to fetch'; they are the same signal.\n"
        "- `shares` MUST have one entry per agent address in `agents`, each a "
        "non-negative integer number of basis points, and the values MUST sum "
        "to exactly 10000. Copy each agent's address EXACTLY as it appears in "
        "`agents` (same case, same characters) -- do not reformat, checksum, or "
        "otherwise alter the address strings.\n"
        "- `evidence_used` MUST be a subset of the URLs in `known_urls`. Do not "
        "list any URL that is not in that list.\n"
        "- Return JSON ONLY. No markdown, no code fences, no commentary.\n\n"
        "JSON schema:\n"
        '{"cause": "<one of cause_enum>", '
        '"shares": {"<agent_address>": <int bps>, ...}, '
        '"evidence_used": ["<url>", ...], '
        '"rationale": "<short free-text explanation>"}\n\n'
        "INPUT:\n" + _canon(payload) + "\n"
    )


# ----------------------------------------------------------------------
# Contract
# ----------------------------------------------------------------------


class BlameCourt(gl.Contract):
    # Every job is one canonical-JSON string keyed by job_id. Nested
    # Agent/Artifact/Verdict records live INSIDE that JSON string, not as
    # separate typed storage graphs -- this is what keeps the ABI schema
    # flat and encoder-safe.
    jobs: TreeMap[str, str]

    # Pull-payment ledger, keyed by the lowercase hex string form of an
    # address (not the Address type -- see notes at the bottom of the
    # file). u256 is used for the value itself since it is an ABI-safe
    # sized integer.
    credits: TreeMap[str, u256]

    job_count: u256

    # Solvency ledger. `total_in` is every wei ever received (escrows and
    # appeal bonds). `total_credited` is every wei ever made withdrawable.
    # `total_withdrawn` is every wei ever paid out. The contract enforces
    # total_withdrawn <= total_credited <= total_in on every write.
    total_in: u256
    total_credited: u256
    total_withdrawn: u256

    def __init__(self):
        # `jobs` and `credits` are declared as TreeMap[...] on the class
        # body above; GenVM allocates their storage slot (and its backing
        # empty TreeMap) from that annotation automatically. Explicitly
        # assigning a freshly-constructed `TreeMap[...]()` here was
        # REJECTED at runtime (`Is right the same storage type? TreeMap
        # <- TreeMap`) because that new instance's type descriptor does
        # not match the slot's own descriptor. Only scalar fields need an
        # explicit initial value.
        self.job_count = 0
        self.total_in = 0
        self.total_credited = 0
        self.total_withdrawn = 0

    # ------------------------------------------------------------------
    # Internal helpers (undecorated -- not part of the public ABI, so
    # they are free to use plain Python dict/list/int types).
    # ------------------------------------------------------------------

    def _load(self, job_id: str) -> dict:
        if job_id not in self.jobs:
            raise Exception("unknown job")
        return json.loads(self.jobs[job_id])

    def _save(self, job_id: str, job: dict) -> None:
        self.jobs[job_id] = _canon(job)

    def _require_agent(self, job: dict, who: str) -> dict:
        agent = job["agents"].get(who)
        if agent is None:
            raise Exception("caller is not a registered agent on this job")
        return agent

    def _credit(self, addr: str, amount: int) -> None:
        if amount < 0:
            raise Exception("negative credit")
        if amount == 0:
            return
        current = int(self.credits[addr]) if addr in self.credits else 0
        self.credits[addr] = current + amount
        self.total_credited = int(self.total_credited) + amount
        if int(self.total_credited) > int(self.total_in):
            raise Exception("ledger would become insolvent")

    def _past_deadline(self, job: dict) -> bool:
        return _now_ts() > int(job["deadline_ts"])

    def _all_agents_submitted(self, job: dict) -> bool:
        submitters = set(a["submitter"] for a in job["artifacts"])
        return set(job["agents"].keys()).issubset(submitters)

    def _failure_condition(self, job: dict):
        """Why this job may be declared failed right now, or None. A job
        may only be judged once its deadline has validly passed, or once
        every registered agent has submitted something (so the failure is
        about the work, not about anyone's silence)."""
        if self._past_deadline(job):
            return FLAG_DEADLINE_PASSED
        if self._all_agents_submitted(job):
            return FLAG_ALL_SUBMITTED
        return None

    def _settle(self, job_id: str, job: dict) -> None:
        """Make the final verdict withdrawable. Runs exactly once per job,
        and only after the verdict can no longer change."""
        if job["settled"]:
            raise Exception("job already settled")
        if job["verdict"] is None:
            raise Exception("cannot settle a job with no verdict")
        pay, creator_refund = self._compute_distribution(job, job["verdict"])
        if sum(pay.values()) + creator_refund != int(job["escrow_total"]):
            raise Exception("distribution does not sum to the escrow")
        for addr, amount in pay.items():
            self._credit(addr, amount)
        self._credit(job["creator"], creator_refund)
        job["settled"] = True
        job["status"] = STATUS_FINAL
        self._save(job_id, job)

    # ------------------------------------------------------------------
    # Evidence pack + LLM verdict (internal; runs inside a nondet
    # closure). All values the closure needs are copied into plain local
    # variables BEFORE the eq_principle call, so the nondet block never
    # touches self.jobs / self.credits directly. The actual fetch/prompt
    # helper functions live at module scope above (see
    # `_fetch_evidence_pack` / `_build_prompt`), NOT as methods here --
    # keeping them out of the class also keeps this class body
    # contiguous, which matters for schema extraction.
    # ------------------------------------------------------------------

    def _adjudicate_once(self, job: dict) -> dict:
        """Runs the nondet fetch+LLM step and reaches validator consensus on
        the decision fields only. Returns the accepted verdict dict."""

        # Copy everything the nondet closure needs into plain local
        # variables BEFORE the eq_principle call -- the closure must not
        # reference self / storage.
        spec_url = job["spec_url"]
        rubric = job["rubric"]
        artifacts = list(job["artifacts"])
        agents_desc = [
            {"addr": addr, "role": a["role"]} for addr, a in job["agents"].items()
        ]
        known_agent_addrs = set(job["agents"].keys())

        def produce_verdict() -> str:
            # Calls the module-level helper functions above, NOT
            # `self._fetch_evidence_pack` / `self._build_prompt` -- this
            # closure must not reference `self` at all, or the contract
            # instance (and the storage TreeMaps it holds) gets pulled
            # into the nondet execution's environment.
            evidence = _fetch_evidence_pack(spec_url, artifacts)
            prompt = _build_prompt(rubric, spec_url, agents_desc, evidence)
            raw = gl.nondet.exec_prompt(prompt, response_format="json")
            try:
                parsed = _coerce_to_json_obj(raw)
            except Exception:
                snippet = str(raw)
                if len(snippet) > 300:
                    snippet = snippet[:300] + "...[truncated]"
                parsed = {
                    "cause": "non_delivery",
                    "shares": {},
                    "evidence_used": [],
                    "rationale": (
                        "LLM returned unparseable output (raw python type="
                        + type(raw).__name__
                        + "): "
                        + snippet
                    ),
                }

            # Canonicalize the payout-driving `shares` field onto the fixed
            # bps grid BEFORE this verdict is handed to the equivalence
            # check, so what validators compare is what actually gets paid.
            # Only attempted when the raw shares already look sane (right
            # keys, non-negative, sums to ~10000) -- anything else is left
            # untouched and falls through to the existing hard-fail checks
            # in `_adjudicate_once` after consensus, unchanged.
            raw_shares = parsed.get("shares")
            if isinstance(raw_shares, dict):
                normalized = {}
                sane = True
                for k, v in raw_shares.items():
                    try:
                        iv = int(v)
                    except (TypeError, ValueError):
                        sane = False
                        break
                    if iv < 0:
                        sane = False
                        break
                    normalized[_norm_addr(k)] = iv
                if (
                    sane
                    and set(normalized.keys()) == known_agent_addrs
                    and abs(sum(normalized.values()) - SHARE_BPS_TOTAL) <= 1
                ):
                    parsed["shares"] = _quantize_shares(
                        normalized, SHARE_BUCKET_BPS, SHARE_BPS_TOTAL
                    )

            return _canon(parsed)

        # Comparative equivalence: validators independently re-run the fetch
        # + LLM step and vote on whether their own result is EQUIVALENT to
        # the leader's. strict_eq is deliberately NOT used here: two
        # independent LLM calls over live web text will not produce
        # byte-identical JSON, so strict_eq would make honest validators
        # disagree by construction. prompt_comparative lets validators agree
        # on the fields that matter (cause, shares, evidence_used) while
        # explicitly ignoring free-text rationale.
        principle = (
            "Two JSON blame verdicts are EQUIVALENT if and only if ALL of the "
            "following hold:\n"
            "1. Their `cause` fields are identical strings.\n"
            "2. Their `shares` objects have the same set of address keys "
            "(compare address keys CASE-INSENSITIVELY -- '0xAbC...' and "
            "'0xabc...' refer to the same address and must be treated as the "
            "same key). Each verdict's `shares` values have already been "
            "canonicalized by its own producer onto a fixed "
            + str(SHARE_BUCKET_BPS) + "-bps grid before you see them -- "
            "compare those values for EXACT equality, address by address. Do "
            "NOT treat two different values as 'close enough'; the values you "
            "are comparing are the same numbers that determine the payout, so "
            "a difference of even one bucket is a real disagreement about "
            "blame, not rounding noise, and must make the verdicts "
            "non-equivalent.\n"
            "3. Every URL in `evidence_used` in EITHER verdict actually appears "
            "in the evidence pack's known URL list; a verdict that cites a URL "
            "not in the known list is INVALID and must NOT be treated as "
            "equivalent to a valid one.\n"
            "Ignore the `rationale` field entirely -- differences in wording, "
            "length, or phrasing of `rationale` must never cause two verdicts to "
            "be judged non-equivalent."
        )

        accepted_json = gl.eq_principle.prompt_comparative(
            produce_verdict, principle=principle
        )
        try:
            accepted = json.loads(accepted_json)
        except Exception:
            raise Exception("consensus returned unparseable verdict JSON")

        # ---- Local, deterministic, post-consensus validation ----
        # Runs OUTSIDE the nondet block, after the accepted result is back,
        # and is allowed to touch storage. Hard-fails on an invalid payload
        # rather than silently coercing shares.
        cause = accepted.get("cause")
        if cause not in VALID_CAUSES:
            raise Exception("invalid cause in accepted verdict: " + str(cause))

        shares = accepted.get("shares") or {}
        # Defensive: normalize keys the same way agent addresses are
        # normalized everywhere else in this contract (see _norm_addr).
        # LLMs sometimes reformat hex address casing (e.g. to an EIP-55
        # checksum) even when explicitly told not to -- comparing raw,
        # un-normalized keys against job["agents"] would then reject a
        # verdict that is actually correct, just differently cased.
        shares = {_norm_addr(k): v for k, v in shares.items()}
        agent_addrs = set(job["agents"].keys())
        if not shares:
            # An empty `shares` dict here almost always means
            # `_extract_json_text` + `json.loads` could not parse the raw
            # LLM output at all (malformed JSON, truncated response, etc.)
            # and the safe fallback in `produce_verdict` kicked in -- not
            # that the model genuinely returned zero shares. Surface that
            # distinction instead of the generic key-mismatch message.
            raise Exception(
                "accepted verdict has no shares -- the LLM's raw output "
                "likely failed JSON parsing (check the model's raw "
                "response in Studio's validator panel); rationale field "
                "from the fallback: " + str(accepted.get("rationale", ""))
            )
        if set(shares.keys()) != agent_addrs:
            extra = sorted(set(shares.keys()) - agent_addrs)
            missing = sorted(agent_addrs - set(shares.keys()))
            raise Exception(
                "verdict shares keys do not match job agents -- "
                "extra (in verdict, not a real agent): "
                + str(extra)
                + "; missing (a real agent with no share entry): "
                + str(missing)
            )
        total = 0
        for addr, bps in shares.items():
            bps = int(bps)
            if bps < 0:
                raise Exception("negative share for " + str(addr))
            total += bps
        if total != SHARE_BPS_TOTAL:
            raise Exception(
                "shares sum to " + str(total) + ", expected " + str(SHARE_BPS_TOTAL)
            )

        known_urls = set([job["spec_url"]])
        for a in job["artifacts"]:
            known_urls.add(a["url"])
        evidence_used = accepted.get("evidence_used") or []
        for u in evidence_used:
            if u not in known_urls:
                raise Exception("verdict cites unknown/unfetched URL: " + str(u))

        return {
            "cause": cause,
            "shares": {str(k): int(v) for k, v in shares.items()},
            "evidence_used": list(evidence_used),
            "rationale": str(accepted.get("rationale", "")),
        }

    # ------------------------------------------------------------------
    # Distribution math (pure function of job + verdict). See README for
    # the documented rule and why it guarantees "a false verdict has a
    # winner."
    # ------------------------------------------------------------------

    def _compute_distribution(self, job: dict, verdict: dict):
        agents = list(job["agents"].keys())
        n = len(agents)
        escrow_total = int(job["escrow_total"])
        base_pay = escrow_total // n
        pay = {}
        for addr in agents:
            share = int(verdict["shares"].get(addr, 0))
            slashed = min(base_pay, (escrow_total * share) // SHARE_BPS_TOTAL)
            pay[addr] = base_pay - slashed
        creator_refund = escrow_total - sum(pay.values())
        return pay, creator_refund

    # ==================================================================
    # PUBLIC ABI -- every arg/return type below is schema-safe.
    # ==================================================================

    @gl.public.write.payable
    def create_job(
        self,
        job_id: str,
        spec_url: str,
        rubric: str,
        deadline: str,
        agents_json: str,
    ) -> None:
        if job_id in self.jobs:
            raise Exception("duplicate job_id")
        if not spec_url or not spec_url.startswith(("http://", "https://")):
            raise Exception("spec_url must be an http(s) URL")
        if not rubric:
            raise Exception("rubric is required")

        # The deadline is what makes silence (an agent that never submits)
        # count as non-delivery, so it must be a real, future instant.
        deadline_ts = _parse_ts(deadline)
        if deadline_ts <= _now_ts():
            raise Exception("deadline must be in the future")

        try:
            raw_agents = json.loads(agents_json)
        except Exception:
            raise Exception("agents_json is not valid JSON")
        if not isinstance(raw_agents, list) or len(raw_agents) == 0:
            raise Exception("agents_json must be a non-empty JSON list")

        agents = {}
        for entry in raw_agents:
            if not isinstance(entry, dict):
                raise Exception("each agents_json entry must be an object")
            extra = sorted(set(entry.keys()) - set(["addr", "role"]))
            if extra:
                raise Exception(
                    "unsupported agent field(s) " + str(extra)
                    + " -- agents are {addr, role} only; agent bonds are "
                    "not implemented"
                )
            addr = _norm_addr(entry.get("addr", ""))
            role = str(entry.get("role", "")).strip()
            if not _is_hex_addr(addr):
                raise Exception("agent addr must be a 20-byte 0x hex address: " + addr)
            if not role:
                raise Exception("agent role is required")
            if addr in agents:
                raise Exception("duplicate agent address " + addr)
            agents[addr] = {"role": role}

        escrow_total = int(gl.message.value)
        if escrow_total <= 0:
            raise Exception("create_job must be sent with escrow value > 0")

        job = {
            "job_id": job_id,
            "creator": _norm_addr(gl.message.sender_address),
            "spec_url": spec_url,
            "rubric": rubric,
            "deadline": deadline,
            "deadline_ts": deadline_ts,
            "escrow_total": escrow_total,
            "status": STATUS_OPEN,
            "flag_reason": "",
            "agents": agents,
            "artifacts": [],
            "verdict": None,
            "appeal_count": 0,
            "appeal_deadline_ts": 0,
            "settled": False,
        }
        self._save(job_id, job)
        self.job_count = int(self.job_count) + 1
        self.total_in = int(self.total_in) + escrow_total

    @gl.public.write
    def submit_artifact(self, job_id: str, url: str, kind: str) -> None:
        job = self._load(job_id)
        caller = _norm_addr(gl.message.sender_address)
        self._require_agent(job, caller)

        if job["status"] not in (STATUS_OPEN, STATUS_SUBMITTED):
            raise Exception(
                "cannot submit artifacts while job is '" + job["status"] + "'"
            )
        if self._past_deadline(job):
            raise Exception("deadline has passed")
        if not url or not url.startswith(("http://", "https://")):
            raise Exception("artifact url must be http(s)")
        if len(job["artifacts"]) >= MAX_ARTIFACTS_PER_JOB:
            raise Exception("artifact cap reached for this job")

        job["artifacts"].append(
            {
                "url": url,
                "kind": kind,
                "submitter": caller,
                "submitted_at": _now_ts(),
            }
        )
        if job["status"] == STATUS_OPEN:
            job["status"] = STATUS_SUBMITTED
        self._save(job_id, job)

    @gl.public.write
    def flag_failed(self, job_id: str) -> None:
        job = self._load(job_id)
        caller = _norm_addr(gl.message.sender_address)
        if caller != job["creator"] and caller not in job["agents"]:
            raise Exception("only the creator or a registered agent may flag")
        if job["status"] not in (STATUS_OPEN, STATUS_SUBMITTED):
            raise Exception(
                "job cannot be flagged while '" + job["status"] + "'"
            )
        reason = self._failure_condition(job)
        if reason is None:
            raise Exception(
                "cannot flag yet: the deadline has not passed and not every "
                "registered agent has submitted an artifact"
            )
        job["flag_reason"] = reason
        job["status"] = STATUS_READY
        self._save(job_id, job)

    @gl.public.write
    def adjudicate(self, job_id: str) -> None:
        job = self._load(job_id)

        if job["status"] in (STATUS_OPEN, STATUS_SUBMITTED):
            # Unflagged jobs may only be judged on a validated deadline.
            # "Every agent submitted" is a completion signal that someone
            # must still explicitly declare a failure on via flag_failed.
            if not self._past_deadline(job):
                raise Exception(
                    "job must be flagged (flag_failed) or past its deadline "
                    "before it can be adjudicated"
                )
        elif job["status"] != STATUS_READY:
            raise Exception(
                "job already adjudicated (status='"
                + job["status"]
                + "'); use appeal() or finalize()"
            )

        verdict = self._adjudicate_once(job)
        job["verdict"] = verdict
        job["status"] = STATUS_ADJUDICATED
        job["appeal_deadline_ts"] = _now_ts() + APPEAL_WINDOW_SECONDS
        # No credits here. Nothing is withdrawable until the appeal window
        # closes (finalize) or an appeal resolves (appeal).
        self._save(job_id, job)

    @gl.public.write.payable
    def appeal(self, job_id: str) -> None:
        job = self._load(job_id)
        caller = _norm_addr(gl.message.sender_address)
        self._require_agent(job, caller)

        if job["status"] != STATUS_ADJUDICATED:
            raise Exception("can only appeal a job that is 'adjudicated'")
        if int(job["appeal_count"]) >= MAX_APPEALS:
            raise Exception("appeal limit reached")
        if _now_ts() > int(job["appeal_deadline_ts"]):
            raise Exception("appeal window has closed; call finalize()")

        appeal_bond = int(gl.message.value)
        if appeal_bond <= 0:
            raise Exception("appeal must be sent with a bond value > 0")

        old_verdict = job["verdict"]
        new_verdict = self._adjudicate_once(job)
        self.total_in = int(self.total_in) + appeal_bond
        job["appeal_count"] = int(job["appeal_count"]) + 1

        # Both verdicts' shares are already canonicalized onto the
        # SHARE_BUCKET_BPS grid (see `_quantize_shares`), so any two
        # distinct values differ by at least SHARE_BUCKET_BPS (1000) --
        # this tolerance check is therefore already an exact-bucket
        # comparison, not a fuzzy one; it is kept as a named tolerance
        # (rather than `!=`) only so a future change to either constant
        # can't silently reopen the gap this canonicalization closes.
        same_cause = new_verdict["cause"] == old_verdict["cause"]
        same_shares = True
        for addr in job["agents"].keys():
            old_s = int(old_verdict["shares"].get(addr, 0))
            new_s = int(new_verdict["shares"].get(addr, 0))
            if abs(old_s - new_s) > APPEAL_SHARE_TOLERANCE_BPS:
                same_shares = False
                break

        if same_cause and same_shares:
            # Appeal rejected: the bond is forfeited to the creator and the
            # original verdict stands.
            self._credit(job["creator"], appeal_bond)
        else:
            # Appeal upheld: the new verdict REPLACES the old one. Nothing
            # was credited for the old verdict, so there is nothing to
            # reverse and the escrow is distributed exactly once.
            job["verdict"] = new_verdict
            self._credit(caller, appeal_bond)

        self._settle(job_id, job)

    @gl.public.write
    def finalize(self, job_id: str) -> None:
        job = self._load(job_id)
        if job["status"] != STATUS_ADJUDICATED:
            raise Exception("only an 'adjudicated' job can be finalized")
        if _now_ts() <= int(job["appeal_deadline_ts"]):
            raise Exception("appeal window is still open")
        self._settle(job_id, job)

    @gl.public.write
    def withdraw(self) -> None:
        caller = _norm_addr(gl.message.sender_address)
        amount = int(self.credits[caller]) if caller in self.credits else 0
        if amount <= 0:
            raise Exception("nothing to withdraw")
        self.credits[caller] = 0
        self.total_withdrawn = int(self.total_withdrawn) + amount
        if int(self.total_withdrawn) > int(self.total_credited):
            raise Exception("withdrawal exceeds credited funds")
        gl.eth_send(gl.message.sender_address, amount)

    @gl.public.view
    def get_job(self, job_id: str) -> str:
        if job_id not in self.jobs:
            raise Exception("unknown job")
        return self.jobs[job_id]

    @gl.public.view
    def get_verdict(self, job_id: str) -> str:
        job = self._load(job_id)
        return _canon(job["verdict"]) if job["verdict"] is not None else "null"

    @gl.public.view
    def get_status(self, job_id: str) -> str:
        job = self._load(job_id)
        return job["status"]

    @gl.public.view
    def get_credit(self, addr: str) -> u256:
        norm = _norm_addr(addr)
        return int(self.credits[norm]) if norm in self.credits else 0

    @gl.public.view
    def get_job_count(self) -> u256:
        return int(self.job_count)

    @gl.public.view
    def get_ledger(self) -> str:
        return _canon(
            {
                "total_in": int(self.total_in),
                "total_credited": int(self.total_credited),
                "total_withdrawn": int(self.total_withdrawn),
            }
        )


# ----------------------------------------------------------------------
# Schema-breaking constructs removed from the previous version
# ----------------------------------------------------------------------
#
# 1. `credits: TreeMap[Address, u256]` -> `TreeMap[str, u256]`. `Address`
#    as a TreeMap key (and anywhere else in a persistent field or public
#    signature) is the single most likely cause of a schema-extraction
#    failure called out in the brief, so it was removed everywhere. All
#    addresses are now the plain `str(...)` form of `gl.message.sender_address`.
# 2. `get_credit(self, addr: Address) -> int` -> `get_credit(self, addr: str) -> u256`.
#    Bare `int` is not an ABI-safe type; `Address` as a public arg was
#    also removed per (1).
# 3. `get_job_count(self) -> int` -> `-> u256`. Same reason.
# 4. `job_count: u256` with `self.job_count += 1` -> rewritten as
#    `self.job_count = int(self.job_count) + 1` to avoid relying on
#    in-place `+=` against a sized-integer storage field, which some
#    encoders reject even though the underlying type is fine.
# 5. `gl.Rollback("...")` -> plain `raise Exception("...")`. `gl.Rollback`
#    is a runtime/consensus concern, not a schema concern, but it was
#    swapped out anyway since it is not part of the ABI-relevant surface
#    and some GenVM builds only special-case `Exception`/subclasses for
#    write-method revert semantics; using the stdlib exception keeps this
#    file dependent on strictly fewer non-core `gl.*` symbols.
# 6. No public method anywhere returns `dict`, `list`, `Optional[...]`,
#    or `Any`. Every view returns `str` or `u256`; every write method
#    returns `None` (no annotation ambiguity -- declared `-> None`
#    explicitly on all of them).
# 7. `__init__` takes no arguments beyond `self`, is undecorated, and
#    only assigns declared fields (`self.jobs`, `self.credits`,
#    `self.job_count`) -- nothing computed from `gl.message.*` happens in
#    the constructor.
# 8. Exactly one class extends `gl.Contract`. All helper methods
#    (`_load`, `_save`, `_require_agent`, `_credit`, `_settle`,
#    `_past_deadline`, `_all_agents_submitted`, `_failure_condition`, `_fetch_evidence_pack`, `_build_prompt`,
#    `_adjudicate_once`, `_compute_distribution`)
#    are plain undecorated instance methods, never exposed with a
#    `@gl.public.*` decorator, so they are not part of what Studio
#    reflects into the ABI at all.
# 9. `Agent` / `Artifact` / `Verdict` are no longer separate types
#    anywhere in the file, let alone in a public signature -- they only
#    ever exist as plain dict/list values inside a job dict that is
#    JSON-encoded before it touches storage.
# 10. Line 1 is the exact `Depends` magic comment given in the brief,
#     with nothing above it and no BOM.
#
# Confirmations:
# - No public method returns dict / list / Any / Optional: confirmed --
#   all views return `str` or `u256`; all writes return `None`.
# - Depends is line 1: confirmed.
# - `__init__` exists and is undecorated: confirmed.
