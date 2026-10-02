"""Minimal in-process stand-in for the `genlayer` SDK, just enough to execute
contracts/blamecourt.py unmodified and drive its state machine from plain
pytest. It is NOT a GenVM: there is no consensus, storage encoding or
sandboxing. It exists to check the contract's own accounting (who is credited
what, when, and that the books balance), which does not depend on those.
"""

import types


class _TreeMap(dict):
    def __class_getitem__(cls, item):
        return cls


class _Public:
    """gl.public.write / gl.public.write.payable / gl.public.view."""

    class _Deco:
        def __call__(self, fn):
            return fn

        @property
        def payable(self):
            return self

    write = _Deco()
    view = _Deco()


class _Message:
    sender_address = "0x" + "00" * 20
    value = 0


class _Contract:
    def __new__(cls, *args, **kwargs):
        obj = super().__new__(cls)
        for klass in reversed(cls.__mro__):
            for name, ann in getattr(klass, "__annotations__", {}).items():
                if ann is _TreeMap:
                    setattr(obj, name, _TreeMap())
                elif ann is int:
                    setattr(obj, name, 0)
        return obj


class Chain:
    """Mutable world state the tests drive between calls."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.datetime = "2030-01-01T00:00:00Z"
        self.sent = []  # (address, amount) for every emit_transfer
        self.web = {}  # url -> body; missing url raises (-> FETCH_FAILED)
        self.llm = lambda prompt: "{}"  # prompt -> raw LLM string


chain = Chain()


def _render(url, mode="text"):
    if url not in chain.web:
        raise Exception("404")
    return chain.web[url]


def _exec_prompt(prompt, response_format="json"):
    return chain.llm(prompt)


def _prompt_comparative(fn, principle=""):
    return fn()


class _Return:
    def __init__(self, calldata):
        self.calldata = calldata


class _Disagree(Exception):
    pass


def _spawn_sandbox(fn):
    return _Return(fn())


def _unpack_result(res):
    return res.calldata


def _run_nondet_unsafe(leader_fn, validator_fn):
    """Leader runs, then the validator checks the leader's result. Like the
    real GenVM, a False verdict terminates the call (here: raises)."""
    result = leader_fn()
    if not validator_fn(_Return(result)):
        raise _Disagree("validators disagree with the leader")
    return result


class _ContractAt:
    def __init__(self, addr):
        self.addr = addr

    def emit_transfer(self, *, value, on="finalized"):
        if value <= 0:
            raise ValueError("value must be greater than 0 for emit_transfer")
        chain.sent.append((str(self.addr), int(value)))


gl = types.SimpleNamespace(
    Contract=_Contract,
    public=_Public,
    message=_Message,
    get_contract_at=_ContractAt,
    vm=types.SimpleNamespace(
        Return=_Return,
        spawn_sandbox=_spawn_sandbox,
        unpack_result=_unpack_result,
        run_nondet_unsafe=_run_nondet_unsafe,
    ),
    eq_principle=types.SimpleNamespace(prompt_comparative=_prompt_comparative),
    nondet=types.SimpleNamespace(
        web=types.SimpleNamespace(render=_render), exec_prompt=_exec_prompt
    ),
)


class _MessageRaw(dict):
    def __getitem__(self, key):
        if key == "datetime":
            return chain.datetime
        return super().__getitem__(key)


gl.message_raw = _MessageRaw()

module = types.ModuleType("genlayer")
module.gl = gl
module.TreeMap = _TreeMap
module.u256 = int
module.Address = str
module.allow_storage = lambda c: c
module.__all__ = ["gl", "TreeMap", "u256", "Address", "allow_storage"]
