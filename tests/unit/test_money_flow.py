"""Accounting tests for contracts/blamecourt.py, run against an in-process
SDK stub (see genlayer_stub.py). They check the properties a reviewer cares
about for escrow safety:

  * an upheld appeal REPLACES the original distribution, never adds to it;
  * nothing is withdrawable until the appeal window closes or an appeal
    resolves;
  * a job cannot be judged before a validated deadline or an explicit
    completion/failure condition;
  * agent bonds are not accepted (they were never implemented);
  * the books always balance: withdrawn <= credited <= received.

    pip install pytest
    pytest tests/unit -q
"""

import importlib.util
import json
import pathlib
import random
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import genlayer_stub as stub  # noqa: E402

sys.modules["genlayer"] = stub.module
_path = pathlib.Path(__file__).resolve().parents[2] / "contracts" / "blamecourt.py"
_spec = importlib.util.spec_from_file_location("blamecourt_under_test", _path)
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)

CREATOR = "0x" + "c0" * 20
RESEARCHER = "0x" + "a1" * 20
IMPLEMENTER = "0x" + "a2" * 20
QA = "0x" + "a3" * 20
PUBLISHER = "0x" + "a4" * 20
AGENTS = [RESEARCHER, IMPLEMENTER, QA, PUBLISHER]
OUTSIDER = "0x" + "ee" * 20

SPEC = "https://example.test/spec"
URLS = {a: "https://example.test/art/" + a[2:6] for a in AGENTS}

T0 = "2030-01-01T00:00:00Z"
DEADLINE = "2030-01-02T00:00:00Z"
AFTER_DEADLINE = "2030-01-02T00:00:01Z"
WINDOW = bc.APPEAL_WINDOW_SECONDS


def agents_json():
    roles = ["researcher", "implementer", "qa", "publisher"]
    return json.dumps([{"addr": a, "role": r} for a, r in zip(AGENTS, roles)])


def verdict(shares, cause="implement_fail"):
    return json.dumps(
        {
            "cause": cause,
            "shares": shares,
            "evidence_used": [SPEC],
            "rationale": "test",
        }
    )


def blame(addr_or_map):
    if isinstance(addr_or_map, dict):
        shares = {a: addr_or_map.get(a, 0) for a in AGENTS}
    else:
        shares = {a: (10000 if a == addr_or_map else 0) for a in AGENTS}
    return shares


@pytest.fixture
def world():
    stub.chain.reset()
    stub.chain.web[SPEC] = "spec text"
    for url in URLS.values():
        stub.chain.web[url] = "artifact text"
    return stub.chain


def call(contract, sender, fn, *args, value=0):
    stub.gl.message.sender_address = sender
    stub.gl.message.value = value
    return getattr(contract, fn)(*args)


def new_job(world, escrow=100_000, deadline=DEADLINE):
    world.datetime = T0
    c = bc.BlameCourt()
    call(c, CREATOR, "create_job", "j1", SPEC, "rubric", deadline, agents_json(), value=escrow)
    return c


def submit_all(c):
    for a in AGENTS:
        call(c, a, "submit_artifact", "j1", URLS[a], "note")


def adjudicate(world, c, shares, cause="implement_fail", at=AFTER_DEADLINE):
    world.llm = lambda prompt: verdict(shares, cause)
    world.datetime = at
    call(c, CREATOR, "flag_failed", "j1")
    call(c, OUTSIDER, "adjudicate", "j1")


def ledger(c):
    return json.loads(call(c, OUTSIDER, "get_ledger"))


def credits(c):
    return {a: call(c, OUTSIDER, "get_credit", a) for a in AGENTS + [CREATOR]}


def seconds_after(ts, seconds):
    return bc._dt.datetime.fromtimestamp(
        bc._parse_ts(ts) + seconds, bc._dt.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- flagging


def test_cannot_flag_before_deadline_with_nothing_submitted(world):
    c = new_job(world)
    with pytest.raises(Exception, match="cannot flag yet"):
        call(c, CREATOR, "flag_failed", "j1")


def test_cannot_flag_early_when_only_some_agents_submitted(world):
    c = new_job(world)
    call(c, QA, "submit_artifact", "j1", URLS[QA], "log")
    with pytest.raises(Exception, match="cannot flag yet"):
        call(c, CREATOR, "flag_failed", "j1")
    with pytest.raises(Exception, match="must be flagged"):
        call(c, OUTSIDER, "adjudicate", "j1")


def test_flag_allowed_once_every_agent_has_submitted(world):
    c = new_job(world)
    submit_all(c)
    call(c, CREATOR, "flag_failed", "j1")
    assert json.loads(call(c, OUTSIDER, "get_job", "j1"))["flag_reason"] == "all_submitted"


def test_all_submitted_still_needs_an_explicit_flag_to_adjudicate(world):
    c = new_job(world)
    submit_all(c)
    with pytest.raises(Exception, match="must be flagged"):
        call(c, OUTSIDER, "adjudicate", "j1")


def test_flag_allowed_after_deadline_and_silence_counts_after_it(world):
    c = new_job(world)
    call(c, QA, "submit_artifact", "j1", URLS[QA], "log")
    world.datetime = AFTER_DEADLINE
    call(c, CREATOR, "flag_failed", "j1")
    assert json.loads(call(c, OUTSIDER, "get_job", "j1"))["flag_reason"] == "deadline_passed"


def test_unflagged_job_can_be_adjudicated_after_deadline(world):
    c = new_job(world)
    world.llm = lambda p: verdict(blame(IMPLEMENTER))
    world.datetime = AFTER_DEADLINE
    call(c, OUTSIDER, "adjudicate", "j1")
    assert call(c, OUTSIDER, "get_status", "j1") == "adjudicated"


def test_outsider_cannot_flag(world):
    c = new_job(world)
    submit_all(c)
    with pytest.raises(Exception, match="only the creator or a registered agent"):
        call(c, OUTSIDER, "flag_failed", "j1")


def test_deadline_is_enforced_for_submissions(world):
    c = new_job(world)
    world.datetime = AFTER_DEADLINE
    with pytest.raises(Exception, match="deadline has passed"):
        call(c, QA, "submit_artifact", "j1", URLS[QA], "log")


@pytest.mark.parametrize("bad", ["not-a-date", "", "2029-12-31T00:00:00Z", T0])
def test_deadline_must_parse_and_be_in_the_future(world, bad):
    world.datetime = T0
    c = bc.BlameCourt()
    with pytest.raises(Exception):
        call(c, CREATOR, "create_job", "j1", SPEC, "rubric", bad, agents_json(), value=1000)


# ------------------------------------------------------------------- bonds


def test_agent_bonds_are_rejected_not_silently_ignored(world):
    world.datetime = T0
    c = bc.BlameCourt()
    with_bond = json.dumps([{"addr": RESEARCHER, "role": "researcher", "bond": 100}])
    with pytest.raises(Exception, match="bonds are not implemented"):
        call(c, CREATOR, "create_job", "j1", SPEC, "r", DEADLINE, with_bond, value=1000)


@pytest.mark.parametrize("addr", ["0x1234", "<" + RESEARCHER + ">", "0x" + "zz" * 20, ""])
def test_malformed_agent_address_is_rejected(world, addr):
    world.datetime = T0
    c = bc.BlameCourt()
    bad = json.dumps([{"addr": addr, "role": "researcher"}])
    with pytest.raises(Exception, match="20-byte"):
        call(c, CREATOR, "create_job", "j1", SPEC, "r", DEADLINE, bad, value=1000)


# ------------------------------------------- credits are deferred, not instant


def test_nothing_is_withdrawable_at_adjudication(world):
    c = new_job(world)
    submit_all(c)
    adjudicate(world, c, blame(IMPLEMENTER))
    assert all(v == 0 for v in credits(c).values())
    assert ledger(c)["total_credited"] == 0
    with pytest.raises(Exception, match="nothing to withdraw"):
        call(c, QA, "withdraw")
    assert world.sent == []


def test_finalize_only_after_the_window_closes(world):
    c = new_job(world)
    submit_all(c)
    adjudicate(world, c, blame(IMPLEMENTER))
    world.datetime = seconds_after(AFTER_DEADLINE, WINDOW)  # boundary: still open
    with pytest.raises(Exception, match="still open"):
        call(c, OUTSIDER, "finalize", "j1")
    world.datetime = seconds_after(AFTER_DEADLINE, WINDOW + 1)
    call(c, OUTSIDER, "finalize", "j1")
    assert call(c, OUTSIDER, "get_status", "j1") == "final"
    got = credits(c)
    assert got[IMPLEMENTER] == 0
    assert got[RESEARCHER] == got[QA] == got[PUBLISHER] == 25_000
    assert got[CREATOR] == 25_000
    assert sum(got.values()) == 100_000


def test_finalize_cannot_run_twice(world):
    c = new_job(world)
    submit_all(c)
    adjudicate(world, c, blame(IMPLEMENTER))
    world.datetime = seconds_after(AFTER_DEADLINE, WINDOW + 1)
    call(c, OUTSIDER, "finalize", "j1")
    with pytest.raises(Exception, match="only an 'adjudicated' job"):
        call(c, OUTSIDER, "finalize", "j1")
    assert sum(credits(c).values()) == 100_000


def test_no_appeal_after_the_window(world):
    c = new_job(world)
    submit_all(c)
    adjudicate(world, c, blame(IMPLEMENTER))
    world.datetime = seconds_after(AFTER_DEADLINE, WINDOW + 1)
    with pytest.raises(Exception, match="window has closed"):
        call(c, QA, "appeal", "j1", value=500)


# --------------------------------------------------------- appeals replace


def test_upheld_appeal_replaces_the_original_distribution(world):
    c = new_job(world)
    submit_all(c)
    adjudicate(world, c, blame(IMPLEMENTER))

    world.llm = lambda p: verdict(blame(PUBLISHER), "publish_fail")
    world.datetime = seconds_after(AFTER_DEADLINE, 60)
    call(c, PUBLISHER, "appeal", "j1", value=500)

    got = credits(c)
    assert call(c, OUTSIDER, "get_status", "j1") == "final"
    # New verdict only: implementer is made whole, publisher is slashed.
    assert got[IMPLEMENTER] == 25_000
    assert got[PUBLISHER] == 0 + 500  # fully slashed, plus their refunded bond
    assert got[RESEARCHER] == got[QA] == 25_000
    assert got[CREATOR] == 25_000
    assert sum(got.values()) == 100_000 + 500
    book = ledger(c)
    assert book["total_in"] == book["total_credited"] == 100_500


def test_rejected_appeal_keeps_original_and_forfeits_bond_to_creator(world):
    c = new_job(world)
    submit_all(c)
    adjudicate(world, c, blame(IMPLEMENTER))
    world.llm = lambda p: verdict(blame(IMPLEMENTER))
    world.datetime = seconds_after(AFTER_DEADLINE, 60)
    call(c, IMPLEMENTER, "appeal", "j1", value=500)
    got = credits(c)
    assert got[IMPLEMENTER] == 0
    assert got[CREATOR] == 25_000 + 500
    assert sum(got.values()) == 100_500
    book = ledger(c)
    assert book["total_in"] == book["total_credited"] == 100_500


def test_appeal_is_once_only_and_agents_only(world):
    c = new_job(world)
    submit_all(c)
    adjudicate(world, c, blame(IMPLEMENTER))
    world.datetime = seconds_after(AFTER_DEADLINE, 60)
    with pytest.raises(Exception, match="not a registered agent"):
        call(c, OUTSIDER, "appeal", "j1", value=500)
    with pytest.raises(Exception, match="bond value > 0"):
        call(c, QA, "appeal", "j1", value=0)
    call(c, QA, "appeal", "j1", value=500)
    with pytest.raises(Exception, match="can only appeal"):
        call(c, QA, "appeal", "j1", value=500)


def test_an_appeal_that_fails_consensus_changes_nothing(world):
    c = new_job(world)
    submit_all(c)
    adjudicate(world, c, blame(IMPLEMENTER))
    before = (call(c, OUTSIDER, "get_job", "j1"), ledger(c))
    world.llm = lambda p: "{}"  # no usable verdict -> adjudicate_once raises
    world.datetime = seconds_after(AFTER_DEADLINE, 60)
    with pytest.raises(Exception):
        call(c, QA, "appeal", "j1", value=500)
    # On a real chain the whole tx (and its attached value) reverts. The stub
    # has no revert, so assert the contract wrote nothing before it raised
    # apart from the in-memory ledger intake it would have rolled back.
    after_job = call(c, OUTSIDER, "get_job", "j1")
    assert after_job == before[0]
    assert ledger(c)["total_credited"] == before[1]["total_credited"]


# --------------------------------------------------------------- solvency


def test_everyone_can_withdraw_and_the_contract_pays_out_exactly_what_it_took_in(world):
    c = new_job(world, escrow=100_003)  # not divisible by 4: dust goes to creator
    submit_all(c)
    adjudicate(world, c, {RESEARCHER: 0, IMPLEMENTER: 7000, QA: 3000, PUBLISHER: 0}, "multi")
    world.llm = lambda p: verdict({RESEARCHER: 0, IMPLEMENTER: 2000, QA: 8000, PUBLISHER: 0}, "multi")
    world.datetime = seconds_after(AFTER_DEADLINE, 60)
    call(c, IMPLEMENTER, "appeal", "j1", value=777)

    for who in AGENTS + [CREATOR]:
        if call(c, OUTSIDER, "get_credit", who) > 0:
            call(c, who, "withdraw")
    paid = sum(amount for _, amount in world.sent)
    assert paid == 100_003 + 777
    book = ledger(c)
    assert book["total_withdrawn"] == book["total_credited"] == book["total_in"] == paid
    with pytest.raises(Exception, match="nothing to withdraw"):
        call(c, CREATOR, "withdraw")


def test_random_scenarios_never_overpay(world):
    rng = random.Random(1234)
    for n in range(200):
        world.reset()
        world.web[SPEC] = "spec"
        for url in URLS.values():
            world.web[url] = "x"
        escrow = rng.randint(1, 10**9)
        c = new_job(world, escrow=escrow)
        submit_all(c)

        def random_split():
            cuts = sorted(rng.randint(0, 10000) for _ in range(3))
            parts = [cuts[0], cuts[1] - cuts[0], cuts[2] - cuts[1], 10000 - cuts[2]]
            return dict(zip(AGENTS, parts))

        adjudicate(world, c, random_split(), "multi")
        bond = 0
        if rng.random() < 0.6:
            bond = rng.randint(1, 10**6)
            appeal_split = random_split()  # one scripted answer per round
            world.llm = lambda p, s=appeal_split: verdict(s, "multi")
            world.datetime = seconds_after(AFTER_DEADLINE, 60)
            call(c, rng.choice(AGENTS), "appeal", "j1", value=bond)
        else:
            world.datetime = seconds_after(AFTER_DEADLINE, WINDOW + 1)
            call(c, OUTSIDER, "finalize", "j1")

        got = credits(c)
        assert sum(got.values()) == escrow + bond, n
        assert all(v >= 0 for v in got.values())
        book = ledger(c)
        assert book["total_credited"] == book["total_in"] == escrow + bond
        for who in AGENTS + [CREATOR]:
            if got[who] > 0:
                call(c, who, "withdraw")
        assert sum(a for _, a in world.sent) == escrow + bond


# ------------------------------------------------- canonical shares still hold


def test_stored_shares_are_on_the_canonical_grid(world):
    c = new_job(world)
    submit_all(c)
    adjudicate(world, c, {RESEARCHER: 0, IMPLEMENTER: 6500, QA: 3500, PUBLISHER: 0}, "multi")
    shares = json.loads(call(c, OUTSIDER, "get_verdict", "j1"))["shares"]
    assert sum(shares.values()) == 10_000
    assert all(v % bc.SHARE_BUCKET_BPS == 0 for v in shares.values())
