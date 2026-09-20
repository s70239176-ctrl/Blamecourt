# Direct tests for BlameCourt using gltest's mocked LLM / mocked web
# facilities. Exact mock plumbing (MockedLLMResponse / MockedWebResponse
# shapes, substring-matching on the internal prompt) follows the pattern
# documented for genlayer-test; if the installed gltest version's fixture
# names differ, only this file needs updating -- the contract itself does
# not depend on test tooling.
#
# NOTE: `mock_llm_response["nondet_exec_prompt"]` keys are matched by
# substring against the prompt BlameCourt builds in `_build_prompt`. Each
# test pins a short, unambiguous fragment of that prompt (spec_url or an
# agent address) as the key so the right canned JSON verdict comes back.

import json
import pytest

from gltest import get_contract_factory, get_validator_factory
from gltest.assertions import tx_execution_succeeded, tx_execution_failed
from gltest.types import MockedLLMResponse, MockedWebResponse

RESEARCHER = "0x1000000000000000000000000000000000000a"
IMPLEMENTER = "0x1000000000000000000000000000000000000b"
QA = "0x1000000000000000000000000000000000000c"
PUBLISHER = "0x1000000000000000000000000000000000000d"

SPEC_URL = "https://example.test/spec/job-1"
REPO_URL = "https://example.test/repo/job-1"
QA_LOG_URL = "https://example.test/qa/job-1"
PUBLISH_URL = "https://example.test/publish/job-1"

AGENTS_JSON = json.dumps(
    [
        {"addr": RESEARCHER, "role": "researcher", "bond": 100},
        {"addr": IMPLEMENTER, "role": "implementer", "bond": 100},
        {"addr": QA, "role": "qa", "bond": 100},
        {"addr": PUBLISHER, "role": "publisher", "bond": 100},
    ]
)

RUBRIC = (
    "Blame the implementer if their repo artifact is missing or does not "
    "address the spec. Blame QA if they signed off on broken work. Blame "
    "the publisher if the publish URL 404s after a good QA sign-off. If "
    "both implement and QA are clearly at fault, split blame between them "
    "and mark cause 'multi'."
)


def _deploy(gl_client, default_account, escrow=100_000):
    factory = get_contract_factory("BlameCourt")
    return factory.deploy(account=default_account, value=0)


def _create_job(contract, account, job_id, escrow_value):
    receipt = contract.create_job(
        args=[job_id, SPEC_URL, RUBRIC, "2099-01-01T00:00:00Z", AGENTS_JSON],
        value=escrow_value,
        account=account,
    ).transact()
    assert tx_execution_succeeded(receipt)


def _validators_with(mock_response: MockedLLMResponse, mock_web=None):
    validator_factory = get_validator_factory()
    return validator_factory.batch_create_mock_validators(
        count=5, mock_llm_response=mock_response, mock_web_response=mock_web
    )


# ---------------------------------------------------------------------
# Case A: implementer repo FETCH_FAILED, QA log shows a blocking issue
#         -> expect cause=implement_fail, implementer holds the largest
#            share, implementer's payout is slashed toward zero.
# ---------------------------------------------------------------------


def test_case_a_implement_fail(default_account, accounts):
    verdict_a = {
        "cause": "implement_fail",
        "shares": {
            RESEARCHER: 0,
            IMPLEMENTER: 8000,
            QA: 2000,
            PUBLISHER: 0,
        },
        "evidence_used": [SPEC_URL, QA_LOG_URL],
        "rationale": "Implementer artifact failed to fetch; QA log confirms blocked work.",
    }
    mock_response: MockedLLMResponse = {
        "nondet_exec_prompt": {IMPLEMENTER: json.dumps(verdict_a)}
    }
    mock_web: MockedWebResponse = {
        SPEC_URL: {"status": 200, "body": "Spec: implement endpoint X per ticket."},
        QA_LOG_URL: {"status": 200, "body": "QA: blocked on missing API from implementer."},
        REPO_URL: {"status": 404, "body": ""},  # -> FETCH_FAILED in the contract
    }
    validators = _validators_with(mock_response, mock_web)
    transaction_context = {
        "validators": [v.to_dict() for v in validators],
        "genvm_datetime": "2030-01-01T00:00:00Z",
    }

    factory = get_contract_factory("BlameCourt")
    contract = factory.deploy(
        account=default_account, transaction_context=transaction_context
    )

    job_id = "job-a"
    r = contract.create_job(
        args=[job_id, SPEC_URL, RUBRIC, "2099-01-01T00:00:00Z", AGENTS_JSON],
        value=100_000,
        account=default_account,
    ).transact(transaction_context=transaction_context)
    assert tx_execution_succeeded(r)

    r = contract.submit_artifact(
        args=[job_id, QA_LOG_URL, "log"], account=default_account
    ).transact(transaction_context=transaction_context)
    assert tx_execution_succeeded(r)

    r = contract.flag_failed(args=[job_id], account=default_account).transact(
        transaction_context=transaction_context
    )
    assert tx_execution_succeeded(r)

    r = contract.adjudicate(args=[job_id], account=default_account).transact(
        transaction_context=transaction_context
    )
    assert tx_execution_succeeded(r)

    status = contract.get_status(args=[job_id]).call()
    assert status == "adjudicated"

    verdict = json.loads(contract.get_verdict(args=[job_id]).call())
    assert verdict["cause"] == "implement_fail"
    assert sum(verdict["shares"].values()) == 10_000
    assert verdict["shares"][IMPLEMENTER] > verdict["shares"][QA]
    assert verdict["shares"][RESEARCHER] == 0
    assert verdict["shares"][PUBLISHER] == 0


# ---------------------------------------------------------------------
# Case B: implementation matches spec, QA signed off, publish 404s
#         -> expect cause=publish_fail, publisher takes the blame.
# ---------------------------------------------------------------------


def test_case_b_publish_fail(default_account):
    verdict_b = {
        "cause": "publish_fail",
        "shares": {
            RESEARCHER: 0,
            IMPLEMENTER: 0,
            QA: 0,
            PUBLISHER: 10_000,
        },
        "evidence_used": [SPEC_URL, REPO_URL, QA_LOG_URL, PUBLISH_URL],
        "rationale": "Implementation and QA sign-off both check out; publish URL 404s.",
    }
    mock_response: MockedLLMResponse = {
        "nondet_exec_prompt": {PUBLISHER: json.dumps(verdict_b)}
    }
    mock_web: MockedWebResponse = {
        SPEC_URL: {"status": 200, "body": "Spec: publish the build artifact."},
        REPO_URL: {"status": 200, "body": "Implementation complete, matches spec."},
        QA_LOG_URL: {"status": 200, "body": "QA: signed off, all checks pass."},
        PUBLISH_URL: {"status": 404, "body": ""},
    }
    validators = _validators_with(mock_response, mock_web)
    transaction_context = {
        "validators": [v.to_dict() for v in validators],
        "genvm_datetime": "2030-01-01T00:00:00Z",
    }

    factory = get_contract_factory("BlameCourt")
    contract = factory.deploy(
        account=default_account, transaction_context=transaction_context
    )

    job_id = "job-b"
    contract.create_job(
        args=[job_id, SPEC_URL, RUBRIC, "2099-01-01T00:00:00Z", AGENTS_JSON],
        value=100_000,
        account=default_account,
    ).transact(transaction_context=transaction_context)
    for url, kind in [(REPO_URL, "repo"), (QA_LOG_URL, "log"), (PUBLISH_URL, "api")]:
        contract.submit_artifact(args=[job_id, url, kind], account=default_account).transact(
            transaction_context=transaction_context
        )
    contract.flag_failed(args=[job_id], account=default_account).transact(
        transaction_context=transaction_context
    )
    r = contract.adjudicate(args=[job_id], account=default_account).transact(
        transaction_context=transaction_context
    )
    assert tx_execution_succeeded(r)

    verdict = json.loads(contract.get_verdict(args=[job_id]).call())
    assert verdict["cause"] == "publish_fail"
    assert verdict["shares"][PUBLISHER] == 10_000
    assert sum(verdict["shares"].values()) == 10_000

    # Publisher's payout must be fully slashed; others keep their base pay.
    n_agents = 4
    base_pay = 100_000 // n_agents
    assert contract.get_credit(args=[PUBLISHER]).call() == 0
    assert contract.get_credit(args=[RESEARCHER]).call() == base_pay
    assert contract.get_credit(args=[IMPLEMENTER]).call() == base_pay
    assert contract.get_credit(args=[QA]).call() == base_pay


# ---------------------------------------------------------------------
# Case C: conflicting artifacts, rubric calls for a split -> cause=multi,
#         and we assert the contract REJECTS an accepted payload whose
#         shares do not sum to 10000 rather than silently coercing it.
# ---------------------------------------------------------------------


def test_case_c_multi_and_invalid_shares_rejected(default_account):
    bad_verdict = {
        "cause": "multi",
        "shares": {
            RESEARCHER: 0,
            IMPLEMENTER: 4000,
            QA: 4000,
            PUBLISHER: 0,
        },  # sums to 8000, not 10000 -- must be rejected
        "evidence_used": [SPEC_URL],
        "rationale": "Both implement and QA missed test coverage.",
    }
    mock_response: MockedLLMResponse = {
        "nondet_exec_prompt": {QA: json.dumps(bad_verdict)}
    }
    mock_web: MockedWebResponse = {
        SPEC_URL: {"status": 200, "body": "Spec requires full test coverage."},
        REPO_URL: {"status": 200, "body": "Implementation present, tests thin."},
        QA_LOG_URL: {"status": 200, "body": "QA: approved without running full suite."},
    }
    validators = _validators_with(mock_response, mock_web)
    transaction_context = {
        "validators": [v.to_dict() for v in validators],
        "genvm_datetime": "2030-01-01T00:00:00Z",
    }

    factory = get_contract_factory("BlameCourt")
    contract = factory.deploy(
        account=default_account, transaction_context=transaction_context
    )

    job_id = "job-c"
    contract.create_job(
        args=[job_id, SPEC_URL, RUBRIC, "2099-01-01T00:00:00Z", AGENTS_JSON],
        value=100_000,
        account=default_account,
    ).transact(transaction_context=transaction_context)
    contract.submit_artifact(args=[job_id, REPO_URL, "repo"], account=default_account).transact(
        transaction_context=transaction_context
    )
    contract.submit_artifact(
        args=[job_id, QA_LOG_URL, "log"], account=default_account
    ).transact(transaction_context=transaction_context)
    contract.flag_failed(args=[job_id], account=default_account).transact(
        transaction_context=transaction_context
    )

    r = contract.adjudicate(args=[job_id], account=default_account).transact(
        transaction_context=transaction_context
    )
    # Shares don't sum to 10000 -> hard validation failure, not silent
    # coercion, per spec.
    assert tx_execution_failed(r, match_std_err=r"shares sum to")
    assert contract.get_status(args=[job_id]).call() == "ready_for_adjudication"
