# Direct tests for BlameCourt using gltest's mocked LLM / mocked web
# facilities. Exact mock plumbing (MockedLLMResponse / MockedWebResponse
# shapes, substring-matching on the internal prompt) follows the pattern
# documented for genlayer-test; if the installed gltest version's fixture
# names differ, only this file needs updating -- the contract itself does
# not depend on test tooling.
#
# The accounting rules (appeal replaces rather than adds, nothing
# withdrawable before the appeal window closes, gated flagging, no bonds)
# are covered exhaustively and without GenVM in tests/unit/. This file checks
# the same flow end to end through the gltest transaction pipeline.
#
# NOTE: `mock_llm_response["nondet_exec_prompt"]` keys are matched by
# substring against the prompt BlameCourt builds in `_build_prompt`. Each
# test pins an agent address as the key so the right canned JSON verdict
# comes back. Agents are real test accounts: submit_artifact requires the
# caller to be a registered agent.

import json

from gltest import get_contract_factory, get_validator_factory
from gltest.assertions import tx_execution_succeeded, tx_execution_failed
from gltest.types import MockedLLMResponse, MockedWebResponse

SPEC_URL = "https://example.test/spec/job-1"
REPO_URL = "https://example.test/repo/job-1"
QA_LOG_URL = "https://example.test/qa/job-1"
PUBLISH_URL = "https://example.test/publish/job-1"

T_CREATE = "2030-01-01T00:00:00Z"
DEADLINE = "2030-06-01T00:00:00Z"
T_ADJUDICATE = "2030-07-01T00:00:00Z"
T_FINALIZE = "2030-07-01T01:00:01Z"  # one hour + 1s after T_ADJUDICATE

RUBRIC = (
    "Blame the implementer if their repo artifact is missing or does not "
    "address the spec. Blame QA if they signed off on broken work. Blame "
    "the publisher if the publish URL 404s after a good QA sign-off. If "
    "both implement and QA are clearly at fault, split blame between them "
    "and mark cause 'multi'."
)


class Cast:
    """Four registered agents, backed by real test accounts. Account 0 is
    both the job creator and the researcher."""

    def __init__(self, accounts):
        self.accounts = accounts[:4]
        self.researcher, self.implementer, self.qa, self.publisher = [
            a.address.lower() for a in self.accounts
        ]
        self.agents_json = json.dumps(
            [
                {"addr": self.researcher, "role": "researcher"},
                {"addr": self.implementer, "role": "implementer"},
                {"addr": self.qa, "role": "qa"},
                {"addr": self.publisher, "role": "publisher"},
            ]
        )

    @property
    def creator(self):
        return self.accounts[0]


def _why(receipt):
    """Short, readable failure reason for a transaction receipt."""
    text = json.dumps(receipt, default=str)
    hits = []
    for key in ("stderr", "payload", "message", "error"):
        i = text.find('"%s"' % key)
        while i != -1 and len(hits) < 6:
            hits.append(text[i : i + 400])
            i = text.find('"%s"' % key, i + 400)
    return " || ".join(hits) or text[:1500]


def _context(validators, when):
    return {
        "validators": [v.to_dict() for v in validators],
        "genvm_datetime": when,
    }


def _validators_with(mock_response: MockedLLMResponse, mock_web=None):
    validator_factory = get_validator_factory()
    return validator_factory.batch_create_mock_validators(
        count=5, mock_llm_response=mock_response, mock_web_response=mock_web
    )


def _new_job(cast, validators, job_id, escrow=100_000):
    factory = get_contract_factory("BlameCourt")
    contract = factory.deploy(
        account=cast.creator, transaction_context=_context(validators, T_CREATE)
    )
    r = contract.connect(cast.creator).create_job(args=[job_id, SPEC_URL, RUBRIC, DEADLINE, cast.agents_json]).transact(value=escrow, transaction_context=_context(validators, T_CREATE))
    assert tx_execution_succeeded(r), _why(r)
    return contract


def _submit(contract, validators, job_id, account, url, kind):
    r = contract.connect(account).submit_artifact(args=[job_id, url, kind]).transact(
        transaction_context=_context(validators, T_CREATE)
    )
    assert tx_execution_succeeded(r), _why(r)


# ---------------------------------------------------------------------
# Case A: implementer repo FETCH_FAILED, QA log shows a blocking issue
#         -> expect cause=implement_fail, implementer holds the largest
#            share. Only QA submitted, so flagging must wait for the
#            deadline; nothing is payable until the job is finalized.
# ---------------------------------------------------------------------


def test_case_a_implement_fail(accounts):
    cast = Cast(accounts)
    verdict_a = {
        "cause": "implement_fail",
        "shares": {
            cast.researcher: 0,
            cast.implementer: 8000,
            cast.qa: 2000,
            cast.publisher: 0,
        },
        "evidence_used": [SPEC_URL, QA_LOG_URL],
        "rationale": "Implementer artifact failed to fetch; QA log confirms blocked work.",
    }
    mock_response: MockedLLMResponse = {
        "nondet_exec_prompt": {cast.implementer: json.dumps(verdict_a)}
    }
    mock_web: MockedWebResponse = {
        "nondet_web_request": {
            SPEC_URL: {"method": "GET", "status": 200, "body": "Spec: implement endpoint X per ticket."},
            QA_LOG_URL: {"method": "GET", "status": 200, "body": "QA: blocked on missing API from implementer."},
            REPO_URL: {"method": "GET", "status": 404, "body": ""},  # -> FETCH_FAILED in the contract
        }
    }
    validators = _validators_with(mock_response, mock_web)
    contract = _new_job(cast, validators, "job-a")
    _submit(contract, validators, "job-a", cast.accounts[2], QA_LOG_URL, "log")

    # Premature: deadline not reached and three agents have not submitted.
    r = contract.connect(cast.creator).flag_failed(args=["job-a"]).transact(
        transaction_context=_context(validators, T_CREATE)
    )
    assert tx_execution_failed(r, match_std_err=r"cannot flag yet")

    late = _context(validators, T_ADJUDICATE)
    r = contract.connect(cast.creator).flag_failed(args=["job-a"]).transact(
        transaction_context=late
    )
    assert tx_execution_succeeded(r), _why(r)
    r = contract.connect(cast.creator).adjudicate(args=["job-a"]).transact(
        transaction_context=late
    )
    assert tx_execution_succeeded(r), _why(r)

    assert contract.get_status(args=["job-a"]).call() == "adjudicated"
    verdict = json.loads(contract.get_verdict(args=["job-a"]).call())
    assert verdict["cause"] == "implement_fail"
    assert sum(verdict["shares"].values()) == 10_000
    assert verdict["shares"][cast.implementer] > verdict["shares"][cast.qa]
    assert verdict["shares"][cast.researcher] == 0
    assert verdict["shares"][cast.publisher] == 0

    # Nothing is withdrawable until the appeal window closes.
    assert contract.get_credit(args=[cast.implementer]).call() == 0
    assert contract.get_credit(args=[cast.creator.address]).call() == 0


# ---------------------------------------------------------------------
# Case B: implementation matches spec, QA signed off, publish 404s
#         -> expect cause=publish_fail, publisher takes the blame, and the
#            payout appears only after finalize().
# ---------------------------------------------------------------------


def test_case_b_publish_fail_and_finalize(accounts):
    cast = Cast(accounts)
    verdict_b = {
        "cause": "publish_fail",
        "shares": {
            cast.researcher: 0,
            cast.implementer: 0,
            cast.qa: 0,
            cast.publisher: 10_000,
        },
        "evidence_used": [SPEC_URL, REPO_URL, QA_LOG_URL, PUBLISH_URL],
        "rationale": "Implementation and QA sign-off both check out; publish URL 404s.",
    }
    mock_response: MockedLLMResponse = {
        "nondet_exec_prompt": {cast.publisher: json.dumps(verdict_b)}
    }
    mock_web: MockedWebResponse = {
        "nondet_web_request": {
            SPEC_URL: {"method": "GET", "status": 200, "body": "Spec: publish the build artifact."},
            REPO_URL: {"method": "GET", "status": 200, "body": "Implementation complete, matches spec."},
            QA_LOG_URL: {"method": "GET", "status": 200, "body": "QA: signed off, all checks pass."},
            PUBLISH_URL: {"method": "GET", "status": 404, "body": ""},
        }
    }
    validators = _validators_with(mock_response, mock_web)
    contract = _new_job(cast, validators, "job-b")
    # The researcher is also silent here, so this runs on the deadline route.
    for account, url, kind in [
        (cast.accounts[1], REPO_URL, "repo"),
        (cast.accounts[2], QA_LOG_URL, "log"),
        (cast.accounts[3], PUBLISH_URL, "api"),
    ]:
        _submit(contract, validators, "job-b", account, url, kind)

    late = _context(validators, T_ADJUDICATE)
    contract.connect(cast.creator).flag_failed(args=["job-b"]).transact(
        transaction_context=late
    )
    r = contract.connect(cast.creator).adjudicate(args=["job-b"]).transact(
        transaction_context=late
    )
    assert tx_execution_succeeded(r), _why(r)

    verdict = json.loads(contract.get_verdict(args=["job-b"]).call())
    assert verdict["cause"] == "publish_fail"
    assert verdict["shares"][cast.publisher] == 10_000
    assert sum(verdict["shares"].values()) == 10_000

    # Before the window closes: nothing credited, finalize refused.
    assert contract.get_credit(args=[cast.qa]).call() == 0
    r = contract.connect(cast.creator).finalize(args=["job-b"]).transact(
        transaction_context=late
    )
    assert tx_execution_failed(r, match_std_err=r"still open")

    r = contract.connect(cast.creator).finalize(args=["job-b"]).transact(
        transaction_context=_context(validators, T_FINALIZE)
    )
    assert tx_execution_succeeded(r), _why(r)
    assert contract.get_status(args=["job-b"]).call() == "final"

    # Publisher's payout is fully slashed; others keep their base pay; the
    # creator (account 0, also the researcher) also receives the refund.
    base_pay = 100_000 // 4
    assert contract.get_credit(args=[cast.publisher]).call() == 0
    assert contract.get_credit(args=[cast.implementer]).call() == base_pay
    assert contract.get_credit(args=[cast.qa]).call() == base_pay
    assert contract.get_credit(args=[cast.researcher]).call() == base_pay + base_pay
    ledger = json.loads(contract.get_ledger().call())
    assert ledger["total_in"] == ledger["total_credited"] == 100_000


# ---------------------------------------------------------------------
# Case C: conflicting artifacts, rubric calls for a split -> cause=multi,
#         and we assert the contract REJECTS an accepted payload whose
#         shares do not sum to 10000 rather than silently coercing it.
# ---------------------------------------------------------------------


def test_case_c_multi_and_invalid_shares_rejected(accounts):
    cast = Cast(accounts)
    bad_verdict = {
        "cause": "multi",
        "shares": {
            cast.researcher: 0,
            cast.implementer: 4000,
            cast.qa: 4000,
            cast.publisher: 0,
        },  # sums to 8000, not 10000 -- must be rejected
        "evidence_used": [SPEC_URL],
        "rationale": "Both implement and QA missed test coverage.",
    }
    mock_response: MockedLLMResponse = {
        "nondet_exec_prompt": {cast.qa: json.dumps(bad_verdict)}
    }
    mock_web: MockedWebResponse = {
        "nondet_web_request": {
            SPEC_URL: {"method": "GET", "status": 200, "body": "Spec requires full test coverage."},
            REPO_URL: {"method": "GET", "status": 200, "body": "Implementation present, tests thin."},
            QA_LOG_URL: {"method": "GET", "status": 200, "body": "QA: approved without running full suite."},
        }
    }
    validators = _validators_with(mock_response, mock_web)
    contract = _new_job(cast, validators, "job-c")
    _submit(contract, validators, "job-c", cast.accounts[1], REPO_URL, "repo")
    _submit(contract, validators, "job-c", cast.accounts[2], QA_LOG_URL, "log")

    late = _context(validators, T_ADJUDICATE)
    contract.connect(cast.creator).flag_failed(args=["job-c"]).transact(
        transaction_context=late
    )
    r = contract.connect(cast.creator).adjudicate(args=["job-c"]).transact(
        transaction_context=late
    )
    # Shares don't sum to 10000 -> hard validation failure, not silent
    # coercion, per spec.
    assert tx_execution_failed(r, match_std_err=r"shares sum to")
    assert contract.get_status(args=["job-c"]).call() == "ready_for_adjudication"


# ---------------------------------------------------------------------
# Case D: agent bonds are rejected, and a past deadline is rejected.
# ---------------------------------------------------------------------


def test_case_d_bonds_and_past_deadline_rejected(accounts):
    cast = Cast(accounts)
    validators = _validators_with({"nondet_exec_prompt": {}})
    factory = get_contract_factory("BlameCourt")
    contract = factory.deploy(
        account=cast.creator, transaction_context=_context(validators, T_CREATE)
    )

    with_bond = json.dumps([{"addr": cast.researcher, "role": "researcher", "bond": 100}])
    r = contract.connect(cast.creator).create_job(args=["job-d1", SPEC_URL, RUBRIC, DEADLINE, with_bond]).transact(value=1_000, transaction_context=_context(validators, T_CREATE))
    assert tx_execution_failed(r, match_std_err=r"bonds are not implemented")

    r = contract.connect(cast.creator).create_job(args=["job-d2", SPEC_URL, RUBRIC, "2029-12-31T00:00:00Z", cast.agents_json]).transact(value=1_000, transaction_context=_context(validators, T_CREATE))
    assert tx_execution_failed(r, match_std_err=r"deadline must be in the future")

    bad_addr = json.dumps([{"addr": "<" + cast.researcher + ">", "role": "researcher"}])
    r = contract.connect(cast.creator).create_job(args=["job-d3", SPEC_URL, RUBRIC, DEADLINE, bad_addr]).transact(value=1_000, transaction_context=_context(validators, T_CREATE))
    assert tx_execution_failed(r, match_std_err=r"20-byte")


# ---------------------------------------------------------------------
# Helpers for the settlement tests below
# ---------------------------------------------------------------------

T_ADJ_ALL = "2030-01-02T00:00:00Z"  # adjudication time for all-submitted jobs
T_APPEAL = "2030-01-02T00:10:00Z"  # 10 minutes later: inside the window
T_WINDOW_EDGE = "2030-01-02T01:00:00Z"  # exactly adjudication + 3600s: still open
T_WINDOW_CLOSED = "2030-01-02T01:00:01Z"  # one second past the window

ROLES = ["researcher", "implementer", "qa", "publisher"]
ALL_URLS = {
    "researcher": "https://example.test/research/job",
    "implementer": REPO_URL,
    "qa": QA_LOG_URL,
    "publisher": PUBLISH_URL,
}
ALL_WEB: MockedWebResponse = {
    "nondet_web_request": {
        SPEC_URL: {"method": "GET", "status": 200, "body": "Spec text."},
        **{
            u: {"method": "GET", "status": 200, "body": "artifact text"}
            for u in ALL_URLS.values()
        },
    }
}
ZERO = {k: 0 for k in ROLES}


def _verdict_validators(key_addr, cause, shares_by_addr, web=ALL_WEB):
    verdict = {
        "cause": cause,
        "shares": shares_by_addr,
        "evidence_used": [SPEC_URL],
        "rationale": "scripted verdict for a live Studio test",
    }
    return _validators_with({"nondet_exec_prompt": {key_addr: json.dumps(verdict)}}, web)


def _shares(cast, **by_role):
    addrs = dict(zip(ROLES, [cast.researcher, cast.implementer, cast.qa, cast.publisher]))
    return {addr: by_role.get(role, 0) for role, addr in addrs.items()}


def _everyone_submits(contract, cast, validators, job_id):
    for role, account in zip(ROLES, cast.accounts):
        _submit(contract, validators, job_id, account, ALL_URLS[role], "note")


def _ok(receipt):
    assert tx_execution_succeeded(receipt), _why(receipt)


def _credits(contract, cast):
    addrs = [cast.researcher, cast.implementer, cast.qa, cast.publisher]
    return {
        role: contract.get_credit(args=[addr]).call()
        for role, addr in zip(ROLES, addrs)
    }


def _flag_and_adjudicate(contract, cast, validators, job_id, when=T_ADJ_ALL):
    ctx = _context(validators, when)
    _ok(contract.connect(cast.creator).flag_failed(args=[job_id]).transact(transaction_context=ctx))
    _ok(contract.connect(cast.creator).adjudicate(args=[job_id]).transact(transaction_context=ctx))


# ---------------------------------------------------------------------
# Steward: validator agreement must bind the exact, payout-driving
# shares. A raw 6500/3500 split must be stored on the 1000-bps grid.
# ---------------------------------------------------------------------


def test_case_e_shares_are_canonical_on_chain(accounts):
    cast = Cast(accounts)
    raw = _shares(cast, implementer=6500, qa=3500)
    validators = _verdict_validators(cast.implementer, "multi", raw)
    contract = _new_job(cast, validators, "job-e")
    _everyone_submits(contract, cast, validators, "job-e")
    _flag_and_adjudicate(contract, cast, validators, "job-e")

    shares = json.loads(contract.get_verdict(args=["job-e"]).call())["shares"]
    assert sum(shares.values()) == 10_000
    assert all(v % 1000 == 0 for v in shares.values()), shares
    assert sorted(shares.values()) == [0, 0, 3000, 7000], shares


# ---------------------------------------------------------------------
# Steward: nothing is withdrawable until the appeal window closes, and
# the books balance exactly when everyone withdraws.
# ---------------------------------------------------------------------


def test_case_f_credits_deferred_until_finalize(accounts):
    cast = Cast(accounts)
    shares = _shares(cast, implementer=10_000)
    validators = _verdict_validators(cast.implementer, "implement_fail", shares)
    contract = _new_job(cast, validators, "job-f")
    _everyone_submits(contract, cast, validators, "job-f")
    _flag_and_adjudicate(contract, cast, validators, "job-f")
    adj = _context(validators, T_ADJ_ALL)

    # Adjudicated, but nothing is payable and nothing can be withdrawn.
    assert contract.get_status(args=["job-f"]).call() == "adjudicated"
    assert _credits(contract, cast) == ZERO
    ledger = json.loads(contract.get_ledger().call())
    assert ledger == {"total_in": 100_000, "total_credited": 0, "total_withdrawn": 0}
    r = contract.connect(cast.accounts[2]).withdraw(args=[]).transact(transaction_context=adj)
    assert tx_execution_failed(r, match_std_err=r"nothing to withdraw")

    # Finalize is refused while the window is open, including at the edge.
    for when in (T_APPEAL, T_WINDOW_EDGE):
        r = contract.connect(cast.creator).finalize(args=["job-f"]).transact(
            transaction_context=_context(validators, when)
        )
        assert tx_execution_failed(r, match_std_err=r"still open")

    # After the window: appeals are closed, finalize settles exactly once.
    late = _context(validators, T_WINDOW_CLOSED)
    r = contract.connect(cast.accounts[2]).appeal(args=["job-f"]).transact(
        value=500, transaction_context=late
    )
    assert tx_execution_failed(r, match_std_err=r"window has closed")
    _ok(contract.connect(cast.creator).finalize(args=["job-f"]).transact(transaction_context=late))
    assert contract.get_status(args=["job-f"]).call() == "final"
    r = contract.connect(cast.creator).finalize(args=["job-f"]).transact(transaction_context=late)
    assert tx_execution_failed(r, match_std_err=r"only an 'adjudicated' job")

    # researcher (also creator) = 25000 pay + 25000 refund; implementer slashed.
    got = _credits(contract, cast)
    assert got == {"researcher": 50_000, "implementer": 0, "qa": 25_000, "publisher": 25_000}
    assert json.loads(contract.get_ledger().call())["total_credited"] == 100_000

    # Everyone withdraws; the contract pays out exactly what it took in.
    for account in cast.accounts:
        if contract.get_credit(args=[account.address]).call() > 0:
            _ok(contract.connect(account).withdraw(args=[]).transact(transaction_context=late))
    ledger = json.loads(contract.get_ledger().call())
    assert ledger == {"total_in": 100_000, "total_credited": 100_000, "total_withdrawn": 100_000}
    assert _credits(contract, cast) == ZERO


# ---------------------------------------------------------------------
# Steward: an upheld appeal REPLACES the original distribution.
# ---------------------------------------------------------------------


def test_case_g_upheld_appeal_replaces_not_adds(accounts):
    cast = Cast(accounts)
    first = _verdict_validators(cast.publisher, "publish_fail", _shares(cast, publisher=10_000))
    second = _verdict_validators(cast.implementer, "implement_fail", _shares(cast, implementer=10_000))
    contract = _new_job(cast, first, "job-g")
    _everyone_submits(contract, cast, first, "job-g")
    _flag_and_adjudicate(contract, cast, first, "job-g")
    assert json.loads(contract.get_verdict(args=["job-g"]).call())["cause"] == "publish_fail"
    assert _credits(contract, cast) == ZERO  # nothing credited for the first verdict

    # The publisher appeals inside the window; the re-run blames the implementer.
    _ok(
        contract.connect(cast.accounts[3]).appeal(args=["job-g"]).transact(
            value=500, transaction_context=_context(second, T_APPEAL)
        )
    )
    assert contract.get_status(args=["job-g"]).call() == "final"
    assert json.loads(contract.get_verdict(args=["job-g"]).call())["cause"] == "implement_fail"

    # New verdict only. Under the old add-on-top behaviour the publisher
    # would also have been slashed by the first verdict.
    got = _credits(contract, cast)
    assert got["implementer"] == 0
    assert got["publisher"] == 25_000 + 500  # base pay + refunded appeal bond
    assert got["qa"] == 25_000
    assert got["researcher"] == 50_000  # 25000 pay + 25000 creator refund
    assert sum(got.values()) == 100_000 + 500
    ledger = json.loads(contract.get_ledger().call())
    assert ledger["total_in"] == ledger["total_credited"] == 100_500


def test_case_h_rejected_appeal_keeps_original_and_pays_bond_to_creator(accounts):
    cast = Cast(accounts)
    same = _verdict_validators(cast.implementer, "implement_fail", _shares(cast, implementer=10_000))
    contract = _new_job(cast, same, "job-h")
    _everyone_submits(contract, cast, same, "job-h")
    _flag_and_adjudicate(contract, cast, same, "job-h")
    _ok(
        contract.connect(cast.accounts[1]).appeal(args=["job-h"]).transact(
            value=700, transaction_context=_context(same, T_APPEAL)
        )
    )
    got = _credits(contract, cast)
    assert got["implementer"] == 0
    assert got["researcher"] == 50_000 + 700  # creator also receives the bond
    assert got["qa"] == got["publisher"] == 25_000
    ledger = json.loads(contract.get_ledger().call())
    assert ledger["total_in"] == ledger["total_credited"] == 100_700


# ---------------------------------------------------------------------
# Steward: no premature flagging.
# ---------------------------------------------------------------------


def test_case_i_flag_is_gated_until_deadline_or_everyone_submitted(accounts):
    cast = Cast(accounts)
    validators = _verdict_validators(cast.implementer, "implement_fail", _shares(cast, implementer=10_000))
    contract = _new_job(cast, validators, "job-i")
    early = _context(validators, T_CREATE)

    r = contract.connect(cast.creator).flag_failed(args=["job-i"]).transact(transaction_context=early)
    assert tx_execution_failed(r, match_std_err=r"cannot flag yet")
    r = contract.connect(cast.creator).adjudicate(args=["job-i"]).transact(transaction_context=early)
    assert tx_execution_failed(r, match_std_err=r"must be flagged")

    # Three of four submitted: still refused.
    for role, account in list(zip(ROLES, cast.accounts))[:3]:
        _submit(contract, validators, "job-i", account, ALL_URLS[role], "note")
    r = contract.connect(cast.creator).flag_failed(args=["job-i"]).transact(transaction_context=early)
    assert tx_execution_failed(r, match_std_err=r"cannot flag yet")
    assert contract.get_status(args=["job-i"]).call() == "submitted"

    # All four have submitted: the explicit completion condition now holds.
    _submit(contract, validators, "job-i", cast.accounts[3], ALL_URLS["publisher"], "note")
    _ok(contract.connect(cast.creator).flag_failed(args=["job-i"]).transact(transaction_context=early))
    job = json.loads(contract.get_job(args=["job-i"]).call())
    assert job["status"] == "ready_for_adjudication"
    assert job["flag_reason"] == "all_submitted"


def test_case_j_unflagged_job_is_judged_only_after_the_deadline(accounts):
    cast = Cast(accounts)
    validators = _verdict_validators(cast.implementer, "non_delivery", _shares(cast, implementer=10_000))
    contract = _new_job(cast, validators, "job-j")
    r = contract.connect(cast.creator).adjudicate(args=["job-j"]).transact(
        transaction_context=_context(validators, T_CREATE)
    )
    assert tx_execution_failed(r, match_std_err=r"must be flagged")
    _ok(
        contract.connect(cast.creator).adjudicate(args=["job-j"]).transact(
            transaction_context=_context(validators, T_ADJUDICATE)
        )
    )
    assert contract.get_status(args=["job-j"]).call() == "adjudicated"
