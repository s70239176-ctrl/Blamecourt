"""Validator agreement. The leader's verdict is accepted only if an
independently re-run validator produces the same cause and EXACTLY the same
canonical shares (compared in code, not by an LLM). Rationale and the exact
URLs cited are presentation, so they may differ.

The stub runs the leader first and then the validator, so the first LLM answer
in each test is the leader's and the second is the validator's.
"""

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import test_money_flow as mf  # noqa: E402  (loads the contract + stub)
from test_money_flow import (  # noqa: E402,F401
    AGENTS, CREATOR, IMPLEMENTER, OUTSIDER, PUBLISHER, QA, RESEARCHER, SPEC,
    URLS, call, new_job, submit_all, world,
)

stub = mf.stub


def answer(cause, shares, evidence=None, rationale="r"):
    return json.dumps(
        {
            "cause": cause,
            "shares": shares,
            "evidence_used": [SPEC] if evidence is None else evidence,
            "rationale": rationale,
        }
    )


def two_runs(world, leader, validator):
    """First LLM call = leader, second = validator."""
    replies = [leader, validator]
    world.llm = lambda prompt: replies.pop(0) if len(replies) > 1 else replies[0]


def adjudicate_with(world, leader, validator):
    c = new_job(world)
    submit_all(c)
    two_runs(world, leader, validator)
    world.datetime = mf.AFTER_DEADLINE
    call(c, CREATOR, "flag_failed", "j1")
    return c


def split(imp, qa):
    return {RESEARCHER: 0, IMPLEMENTER: imp, QA: qa, PUBLISHER: 0}


def test_identical_verdicts_agree(world):
    v = answer("implement_fail", mf.blame(IMPLEMENTER))
    c = adjudicate_with(world, v, v)
    call(c, OUTSIDER, "adjudicate", "j1")
    assert call(c, OUTSIDER, "get_status", "j1") == "adjudicated"


def test_rationale_and_cited_urls_may_differ(world):
    leader = answer("implement_fail", mf.blame(IMPLEMENTER), [SPEC], "short")
    validator = answer("implement_fail", mf.blame(IMPLEMENTER), [], "a much longer explanation")
    c = adjudicate_with(world, leader, validator)
    call(c, OUTSIDER, "adjudicate", "j1")
    assert call(c, OUTSIDER, "get_status", "j1") == "adjudicated"


def test_raw_shares_in_the_same_bucket_agree_and_are_stored_canonically(world):
    # 6500/3500 and 6600/3400 both quantize to the same 1000-bps allocation.
    c = adjudicate_with(
        world,
        answer("multi", split(6500, 3500)),
        answer("multi", split(6600, 3400)),
    )
    call(c, OUTSIDER, "adjudicate", "j1")
    shares = json.loads(call(c, OUTSIDER, "get_verdict", "j1"))["shares"]
    assert sorted(shares.values()) == [0, 0, 3000, 7000]


@pytest.mark.parametrize(
    "leader,validator",
    [
        # different cause
        (answer("implement_fail", mf.blame(IMPLEMENTER)), answer("qa_miss", mf.blame(IMPLEMENTER))),
        # different blamed agent
        (answer("implement_fail", mf.blame(IMPLEMENTER)), answer("implement_fail", mf.blame(QA))),
        # one whole bucket apart: 7000/3000 vs 5000/5000
        (answer("multi", split(6500, 3500)), answer("multi", split(5400, 4600))),
        # validator produced nothing usable
        (answer("implement_fail", mf.blame(IMPLEMENTER)), "not json at all"),
        (answer("implement_fail", mf.blame(IMPLEMENTER)), answer("implement_fail", {})),
        # the leader cites a URL that was never in the evidence pack
        (
            answer("implement_fail", mf.blame(IMPLEMENTER), ["https://evil.example/x"]),
            answer("implement_fail", mf.blame(IMPLEMENTER)),
        ),
    ],
    ids=["cause", "agent", "one-bucket", "garbage", "empty-shares", "unfetched-url"],
)
def test_disagreement_blocks_the_verdict_and_leaves_the_job_unjudged(world, leader, validator):
    c = adjudicate_with(world, leader, validator)
    with pytest.raises(Exception):
        call(c, OUTSIDER, "adjudicate", "j1")
    assert call(c, OUTSIDER, "get_status", "j1") == "ready_for_adjudication"
    assert all(call(c, OUTSIDER, "get_credit", a) == 0 for a in AGENTS)


def test_address_case_does_not_cause_disagreement(world):
    shares = mf.blame(IMPLEMENTER)
    checksummed = {k.upper().replace("0X", "0x"): v for k, v in shares.items()}
    c = adjudicate_with(
        world,
        answer("implement_fail", shares),
        answer("implement_fail", checksummed),
    )
    call(c, OUTSIDER, "adjudicate", "j1")
    assert call(c, OUTSIDER, "get_status", "j1") == "adjudicated"
