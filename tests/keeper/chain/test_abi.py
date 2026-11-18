from eth_abi import encode
from eth_utils import keccak

from keepers.auction_keeper.chain.abi import Function, describe_revert, selector, split_types


def test_split_types_handles_nested_tuples():
    assert split_types("address,(address,bytes)[],uint256") == [
        "address",
        "(address,bytes)[]",
        "uint256",
    ]
    assert split_types("") == []


def test_function_encodes_selector_and_arguments():
    fn = Function("getAmountNeeded(address,uint256,uint256)", ("uint256",))
    token = "0x" + "11" * 20
    data = fn.encode(token, 5, 7)
    assert data[:4] == keccak(text="getAmountNeeded(address,uint256,uint256)")[:4]
    assert data[4:] == encode(["address", "uint256", "uint256"], [token, 5, 7])
    assert fn.decode(encode(["uint256"], [42])) == (42,)


def test_function_rejects_wrong_arity():
    try:
        Function("want()").encode(1)
    except ValueError as exc:
        assert "takes 0 arguments" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_describe_revert_names_typed_errors():
    assert describe_revert(selector("LotInactive()")) == "LotInactive()"
    payload = selector("OrderNotValid(string)") + encode(["string"], ["BadAppData"])
    assert describe_revert(payload) == "OrderNotValid('BadAppData')"
    error = selector("Error(string)") + encode(["string"], ["nope"])
    assert describe_revert(error) == "Error('nope')"
    assert describe_revert(b"") == "reverted without data"
    assert describe_revert(b"\xde\xad\xbe\xef") == "0xdeadbeef"
