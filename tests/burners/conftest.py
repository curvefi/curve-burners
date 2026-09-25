import pytest
from eth_abi import encode
from eth_utils import keccak


def custom_err(signature: str, *args, nested: bool = False) -> str:
    """Expected boa.reverts matcher for a Vyper custom error.

    boa 0.2.x does not decode custom errors, so the expected value is the
    raw pretty_vm_reason string: ``Revert(b'<selector><abi-encoded args>')``.
    Argument types are taken from the signature, e.g.
    ``custom_err("PollTryAtEpoch(uint256,string)", timestamp, reason)``.

    ``nested=True`` targets reverts that bubble through another contract
    (e.g. burner errors surfacing via ``fee_collector.collect``): boa renders
    those frames as plain strings, matched by substring, so the expected
    value is just the bytes repr without the ``Revert(...)`` wrapper.
    """
    data = keccak(text=signature)[:4]
    if args:
        types = signature[signature.index("(") + 1 : -1].split(",")
        data += encode(types, list(args))
    if nested:
        return repr(data)
    return f"Revert({data!r})"


@pytest.fixture(scope="module")
def coins(coins, target):
    return [coin for coin in coins if coin != target]
