import pytest
from requests.exceptions import ConnectionError as ConnectionFailed

from keepers.auction_keeper.chain.client import READ_RETRIES, retry_read


def flaky(failures, error=ConnectionFailed("Connection aborted.")):
    calls = []

    def read():
        calls.append(1)
        if len(calls) <= failures:
            raise error
        return "answer"

    return read, calls


def test_a_read_is_repeated_after_a_connection_error():
    waits = []
    read, calls = flaky(READ_RETRIES)
    assert retry_read(read, sleep=waits.append) == "answer"
    assert len(calls) == READ_RETRIES + 1 and len(waits) == READ_RETRIES


def test_a_read_gives_up_and_other_errors_are_not_retried():
    read, calls = flaky(READ_RETRIES + 1)
    with pytest.raises(ConnectionFailed):
        retry_read(read, sleep=lambda _: None)
    assert len(calls) == READ_RETRIES + 1
    read, calls = flaky(1, error=ValueError("execution reverted"))
    with pytest.raises(ValueError):
        retry_read(read, sleep=lambda _: None)
    assert len(calls) == 1
