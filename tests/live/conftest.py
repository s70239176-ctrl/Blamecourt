"""Hosted Studio sits behind a gateway that intermittently answers 502/HTML
while a transaction is being polled. Retry only *read* requests, never ones
that submit a transaction, so a flaky gateway cannot duplicate a write."""

import time

import pytest
from genlayer_py.exceptions import GenLayerError
from genlayer_py.provider.provider import GenLayerProvider

READ_METHODS = (
    "eth_getTransactionByHash",
    "eth_getTransactionReceipt",
    "eth_call",
    "eth_blockNumber",
    "eth_getBalance",
    "eth_getTransactionCount",
    "gen_call",
    "gen_getContractSchema",
    "gen_getContractCode",
)
TRANSIENT = ("invalid JSON", "502", "503", "504", "Bad gateway", "Request to")


@pytest.fixture(autouse=True, scope="session")
def _retry_flaky_gateway_reads():
    original = GenLayerProvider.make_request

    def make_request(self, method, params):
        if str(method) not in READ_METHODS:
            return original(self, method, params)
        delay = 2.0
        for attempt in range(6):
            try:
                return original(self, method, params)
            except GenLayerError as err:
                if attempt == 5 or not any(t in str(err) for t in TRANSIENT):
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 20)

    GenLayerProvider.make_request = make_request
    yield
    GenLayerProvider.make_request = original
