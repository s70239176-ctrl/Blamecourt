"""Hosted Studio sits behind a gateway that is sometimes slow, answers 502/HTML
while a transaction is polled, or refuses a connection outright.

Two protections, both safe against duplicating a write:

* a connect timeout, so a dead connection fails in seconds instead of hanging
  for minutes;
* retries -- for *read* requests on any transient error, and for *any* request
  only when the failure happened while connecting (the request provably never
  reached Studio, so resending cannot submit a transaction twice).
"""

import time

import pytest
import requests
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
NEVER_SENT = (
    "ConnectTimeout",
    "Failed to establish a new connection",
    "NameResolutionError",
    "Connection to studio.genlayer.com timed out",
)
ATTEMPTS = 8


@pytest.fixture(autouse=True, scope="session")
def _tolerate_flaky_gateway():
    original_make_request = GenLayerProvider.make_request
    original_post = requests.post

    def post(url, *args, **kwargs):
        kwargs.setdefault("timeout", (15, 240))
        return original_post(url, *args, **kwargs)

    def make_request(self, method, params):
        is_read = str(method) in READ_METHODS
        delay = 3.0
        for attempt in range(ATTEMPTS):
            try:
                return original_make_request(self, method, params)
            except GenLayerError as err:
                text = str(err)
                retryable = any(t in text for t in NEVER_SENT) or (
                    is_read and any(t in text for t in TRANSIENT)
                )
                if attempt == ATTEMPTS - 1 or not retryable:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 30)

    requests.post = post
    GenLayerProvider.make_request = make_request
    yield
    requests.post = original_post
    GenLayerProvider.make_request = original_make_request
