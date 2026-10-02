# Live tests against hosted Studio using the network's own validators and a
# real LLM (no mocks). Slow (minutes per test) and slightly non-deterministic
# by nature, so the assertions are on invariants, not exact wording.
#
#   # fresh throwaway deployment per test:
#   pytest tests/live -v --network studionet
#
#   # every transaction against ONE already-deployed contract (so they show
#   # up under that address on the explorer):
#   BLAMECOURT_ADDRESS=0x... pytest tests/live -v --network studionet
#
# Only `genvm_datetime` is overridden (never the validators), so the appeal
# window and deadlines can be exercised without waiting an hour. Job ids are
# unique per run and every ledger/credit check is a before/after delta, so the
# tests are safe on a contract that already holds other jobs.

import json
import os
import time

from gltest import get_contract_factory
from gltest.assertions import tx_execution_succeeded, tx_execution_failed

SPEC_URL = "https://raw.githubusercontent.com/octocat/Hello-World/master/README"
BROKEN_URL = "https://raw.githubusercontent.com/octocat/Hello-World/master/THIS-FILE-DOES-NOT-EXIST"

RUBRIC = (
    'The implementer\'s repo artifact must be fetchable. If the implementer\'s '
    'artifact fetch fails (FETCH_FAILED) while every other agent\'s artifact '
    'fetches successfully, the cause is "implement_fail" and the implementer '
    "bears all blame (10000 bps to the implementer, 0 to everyone else). Do "
    "not assign blame to any other agent in that scenario."
)

DEADLINE = "2099-01-01T00:00:00Z"
T_CREATE = "2030-01-01T00:00:00Z"
T_ADJUDICATE = "2030-01-02T00:00:00Z"
T_APPEAL = "2030-01-02T00:10:00Z"
T_WINDOW_EDGE = "2030-01-02T01:00:00Z"
T_WINDOW_CLOSED = "2030-01-02T01:00:01Z"
ROLES = ["researcher", "implementer", "qa", "publisher"]
ESCROW = 100_000
RUN = str(int(time.time()))
ATTACH = os.environ.get("BLAMECOURT_ADDRESS", "").strip()


def _ctx(when):
    return {"genvm_datetime": when}


def _why(receipt):
    text = json.dumps(receipt, default=str)
    hits = []
    for key in ("error", "stderr", "payload", "message"):
        i = text.find('"%s"' % key)
        if i != -1:
            hits.append(text[i : i + 300])
    return " || ".join(hits) or text[:1200]


_STEPS = [0]


def _step(kind, receipt):
    _STEPS[0] += 1
    print(
        "[step %d] %s tx=%s" % (_STEPS[0], kind, (receipt or {}).get("hash")),
        flush=True,
    )


def _ok(receipt):
    assert tx_execution_succeeded(receipt), _why(receipt)
    _step("ok", receipt)


def _refused(receipt, pattern):
    assert tx_execution_failed(receipt, match_std_err=pattern), _why(receipt)
    _step("refused as expected (%s)" % pattern, receipt)


def _contract(accounts):
    factory = get_contract_factory("BlameCourt")
    if ATTACH:
        return factory.build_contract(ATTACH, account=accounts[0])
    return factory.deploy(account=accounts[0])


def _agents_json(accounts):
    return json.dumps(
        [{"addr": a.address.lower(), "role": r} for a, r in zip(accounts[:4], ROLES)]
    )


def _ledger(contract):
    return json.loads(contract.get_ledger().call())


def _credits(contract, accounts):
    return [contract.get_credit(args=[a.address]).call() for a in accounts[:4]]


def _delta(after, before):
    return [a - b for a, b in zip(after, before)]


def _new_job(accounts, name, submit_all=True):
    contract = _contract(accounts)
    job_id = "%s-%s" % (name, RUN)
    _ok(
        contract.create_job(
            args=[job_id, SPEC_URL, RUBRIC, DEADLINE, _agents_json(accounts)]
        ).transact(value=ESCROW)
    )
    if submit_all:
        urls = [SPEC_URL, BROKEN_URL, SPEC_URL, SPEC_URL]
        for acct, url, kind in zip(accounts[:4], urls, ["note", "repo", "log", "api"]):
            _ok(contract.connect(acct).submit_artifact(args=[job_id, url, kind]).transact())
    return contract, job_id


def _status_eventually(contract, job_id, want, seconds=60):
    # A state read straight after ACCEPTED can briefly lag on hosted Studio.
    status = None
    for _ in range(seconds // 5 + 1):
        status = contract.get_status(args=[job_id]).call()
        if status == want:
            break
        time.sleep(5)
    return status


def _adjudicate(contract, job_id):
    # All four roles submitted, so flagging is allowed immediately.
    _ok(contract.flag_failed(args=[job_id]).transact(transaction_context=_ctx(T_ADJUDICATE)))
    job = json.loads(contract.get_job(args=[job_id]).call())
    assert job["flag_reason"] == "all_submitted"
    receipt = contract.adjudicate(args=[job_id]).transact(
        transaction_context=_ctx(T_ADJUDICATE)
    )
    _ok(receipt)
    status = _status_eventually(contract, job_id, "adjudicated")
    assert status == "adjudicated", (status, _why(receipt), receipt.get("hash"))
    verdict = json.loads(contract.get_verdict(args=[job_id]).call())
    shares = verdict["shares"]
    # Exact, canonical shares: on the grid and summing to the whole.
    assert sum(shares.values()) == 10_000
    assert all(v % 1000 == 0 for v in shares.values()), shares
    return verdict


# ---------------------------------------------------------------------
# No LLM involved: these are fast and cheap, run them first.
# ---------------------------------------------------------------------


def test_flag_is_gated_and_bonds_and_bad_deadlines_are_refused(accounts):
    contract = _contract(accounts)
    agents = _agents_json(accounts)
    job_id = "gate-%s" % RUN

    # Bonds, malformed addresses and non-future deadlines never get in.
    bond = json.dumps([{"addr": accounts[0].address.lower(), "role": "researcher", "bond": 100}])
    r = contract.create_job(args=[job_id + "-b", SPEC_URL, RUBRIC, DEADLINE, bond]).transact(value=1_000)
    _refused(r, r"bonds are not implemented")
    junk = json.dumps([{"addr": "<" + accounts[0].address.lower() + ">", "role": "researcher"}])
    r = contract.create_job(args=[job_id + "-a", SPEC_URL, RUBRIC, DEADLINE, junk]).transact(value=1_000)
    _refused(r, r"20-byte")
    r = contract.create_job(args=[job_id + "-d", SPEC_URL, RUBRIC, "2000-01-01T00:00:00Z", agents]).transact(value=1_000)
    _refused(r, r"deadline must be in the future")
    r = contract.create_job(args=[job_id + "-p", SPEC_URL, RUBRIC, "soon", agents]).transact(value=1_000)
    _refused(r, r"valid ISO-8601")

    _ok(contract.create_job(args=[job_id, SPEC_URL, RUBRIC, DEADLINE, agents]).transact(value=ESCROW))

    # Nothing submitted, deadline far away: neither flagging nor judging.
    r = contract.flag_failed(args=[job_id]).transact()
    _refused(r, r"cannot flag yet")
    r = contract.adjudicate(args=[job_id]).transact()
    _refused(r, r"must be flagged")

    # Three of four submitted: still refused.
    for acct in accounts[:3]:
        _ok(contract.connect(acct).submit_artifact(args=[job_id, SPEC_URL, "note"]).transact())
    r = contract.flag_failed(args=[job_id]).transact()
    _refused(r, r"cannot flag yet")
    assert contract.get_status(args=[job_id]).call() == "submitted"

    # A non-agent cannot flag; the fourth submission completes the set.
    _ok(contract.connect(accounts[3]).submit_artifact(args=[job_id, SPEC_URL, "note"]).transact())
    _ok(contract.flag_failed(args=[job_id]).transact())
    job = json.loads(contract.get_job(args=[job_id]).call())
    assert job["status"] == "ready_for_adjudication"
    assert job["flag_reason"] == "all_submitted"


# ---------------------------------------------------------------------
# Real validators and a real LLM.
# ---------------------------------------------------------------------


def test_finalize_path_on_real_validators(accounts):
    contract, job_id = _new_job(accounts, "fin")
    ledger0 = _ledger(contract)
    credits0 = _credits(contract, accounts)
    verdict = _adjudicate(contract, job_id)

    # Adjudicated: nothing payable, nothing withdrawable, ledger untouched.
    assert _credits(contract, accounts) == credits0
    assert _ledger(contract)["total_credited"] == ledger0["total_credited"]
    # A fresh account with no balance cannot withdraw anything.
    r = contract.connect(accounts[4]).withdraw(args=[]).transact(
        transaction_context=_ctx(T_ADJUDICATE)
    )
    _refused(r, r"nothing to withdraw")

    for when in (T_APPEAL, T_WINDOW_EDGE):
        r = contract.finalize(args=[job_id]).transact(transaction_context=_ctx(when))
        _refused(r, r"still open")

    late = _ctx(T_WINDOW_CLOSED)
    r = contract.connect(accounts[2]).appeal(args=[job_id]).transact(value=500, transaction_context=late)
    _refused(r, r"window has closed")
    _ok(contract.finalize(args=[job_id]).transact(transaction_context=late))
    assert contract.get_status(args=[job_id]).call() == "final"
    r = contract.finalize(args=[job_id]).transact(transaction_context=late)
    _refused(r, r"only an 'adjudicated' job")

    # Payouts follow the stored (canonical) verdict exactly and balance.
    shares = [verdict["shares"][a.address.lower()] for a in accounts[:4]]
    base = ESCROW // 4
    pays = [base - min(base, ESCROW * s // 10_000) for s in shares]
    expected = list(pays)
    expected[0] += ESCROW - sum(pays)  # accounts[0] is also the creator
    assert _delta(_credits(contract, accounts), credits0) == expected
    assert sum(expected) == ESCROW
    assert _ledger(contract)["total_credited"] - ledger0["total_credited"] == ESCROW

    # Withdraw (the SDK's emit_transfer path): balances clear, books balance.
    withdrawn0 = _ledger(contract)["total_withdrawn"]
    owed_now = _credits(contract, accounts)
    for acct, owed in zip(accounts[:4], owed_now):
        if owed > 0:
            _ok(contract.connect(acct).withdraw(args=[]).transact(transaction_context=late))
    assert _credits(contract, accounts) == [0, 0, 0, 0]
    ledger = _ledger(contract)
    assert ledger["total_withdrawn"] - withdrawn0 == sum(owed_now)
    # ledger0 was taken after the deposit, so nothing new came in since.
    assert ledger["total_in"] == ledger0["total_in"]
    assert ledger["total_withdrawn"] <= ledger["total_credited"] <= ledger["total_in"]


def test_appeal_path_on_real_validators_never_overpays(accounts):
    contract, job_id = _new_job(accounts, "app")
    ledger0 = _ledger(contract)
    credits0 = _credits(contract, accounts)
    _adjudicate(contract, job_id)
    assert _credits(contract, accounts) == credits0

    bond = 500
    _ok(
        contract.connect(accounts[1]).appeal(args=[job_id]).transact(
            value=bond, transaction_context=_ctx(T_APPEAL)
        )
    )
    assert contract.get_status(args=[job_id]).call() == "final"

    # Whether the re-run upheld or rejected the appeal, exactly one
    # distribution exists: credits equal escrow + bond, never more.
    got = _delta(_credits(contract, accounts), credits0)
    assert sum(got) == ESCROW + bond, got
    ledger = _ledger(contract)
    assert ledger["total_in"] - ledger0["total_in"] == bond  # escrow was in ledger0
    assert ledger["total_credited"] - ledger0["total_credited"] == ESCROW + bond

    # No second appeal, no second settlement.
    r = contract.connect(accounts[2]).appeal(args=[job_id]).transact(
        value=bond, transaction_context=_ctx(T_APPEAL)
    )
    _refused(r, r"can only appeal")
    r = contract.finalize(args=[job_id]).transact(transaction_context=_ctx(T_WINDOW_CLOSED))
    _refused(r, r"only an 'adjudicated' job")
