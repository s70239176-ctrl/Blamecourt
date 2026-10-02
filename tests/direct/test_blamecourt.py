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
    r = contract.create_job(
        args=[job_id, SPEC_URL, RUBRIC, DEADLINE, cast.agents_json],
        value=escrow,
        account=cast.creator,
    ).transact(transaction_context=_context(validators, T_CREATE))
    assert tx_execution_succeeded(r)
    return contract


def _submit(contract, validators, job_id, account, url, kind):
    r = contract.submit_artifact(args=[job_id, url, kind], account=account).transact(
        transaction_context=_context(validators, T_CREATE)
    )
    assert tx_execution_succeeded(r)


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
    r = contract.flag_failed(args=["job-a"], account=cast.creator).transact(
        transaction_context=_context(validators, T_CREATE)
    )
    assert tx_execution_failed(r, match_std_err=r"cannot flag yet")

    late = _context(validators, T_ADJUDICATE)
    r = contract.flag_failed(args=["job-a"], account=cast.creator).transact(
        transaction_context=late
    )
    assert tx_execution_succeeded(r)
    r = contract.adjudicate(args=["job-a"], account=cast.creator).transact(
        transaction_context=late
    )
    assert tx_execution_succeeded(r)

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
    contract.flag_failed(args=["job-b"], account=cast.creator).transact(
        transaction_context=late
    )
    r = contract.adjudicate(args=["job-b"], account=cast.creator).transact(
        transaction_context=late
    )
    assert tx_execution_succeeded(r)

    verdict = json.loads(contract.get_verdict(args=["job-b"]).call())
    assert verdict["cause"] == "publish_fail"
    assert verdict["shares"][cast.publisher] == 10_000
    assert sum(verdict["shares"].values()) == 10_000

    # Before the window closes: nothing credited, finalize refused.
    assert contract.get_credit(args=[cast.qa]).call() == 0
    r = contract.finalize(args=["job-b"], account=cast.creator).transact(
        transaction_context=late
    )
    assert tx_execution_failed(r, match_std_err=r"still open")

    r = contract.finalize(args=["job-b"], account=cast.creator).transact(
        transaction_context=_context(validators, T_FINALIZE)
    )
    assert tx_execution_succeeded(r)
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
    contract.flag_failed(args=["job-c"], account=cast.creator).transact(
        transaction_context=late
    )
    r = contract.adjudicate(args=["job-c"], account=cast.creator).transact(
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
    r = contract.create_job(
        args=["job-d1", SPEC_URL, RUBRIC, DEADLINE, with_bond],
        value=1_000,
        account=cast.creator,
    ).transact(transaction_context=_context(validators, T_CREATE))
    assert tx_execution_failed(r, match_std_err=r"bonds are not implemented")

    r = contract.create_job(
        args=["job-d2", SPEC_URL, RUBRIC, "2029-12-31T00:00:00Z", cast.agents_json],
        value=1_000,
        account=cast.creator,
    ).transact(transaction_context=_context(validators, T_CREATE))
    assert tx_execution_failed(r, match_std_err=r"deadline must be in the future")
