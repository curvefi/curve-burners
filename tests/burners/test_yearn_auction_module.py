from pathlib import Path

from vyper.compiler import compile_code
from vyper.compiler.input_bundle import FilesystemInputBundle


MODULE_DIR = Path(__file__).resolve().parents[2] / "contracts" / "burners" / "modules"

HARNESS = """
# pragma version 0.5.0a4

import yearn_auction

initializes: yearn_auction
exports: (
    yearn_auction.want,
    yearn_auction.available,
    yearn_auction.price,
    yearn_auction.getAmountNeeded,
    yearn_auction.take,
)


@override(yearn_auction)
@view
def _want() -> address:
    return 0x0000000000000000000000000000000000000001


@override(yearn_auction)
@view
def _available(_from: address) -> uint256:
    return 11


@override(yearn_auction)
@view
def _price(_from: address) -> uint256:
    return 22


@override(yearn_auction)
@view
def _get_amount_needed(_from: address, _amount_to_take: uint256) -> uint256:
    return _amount_to_take


@override(yearn_auction)
def _take(
    _from: address,
    _max_amount: uint256,
    _taker_receiver: address,
    _data: Bytes[yearn_auction.MAX_CALLBACK_DATA],
) -> uint256:
    return _max_amount
"""


def test_yearn_auction_module_compile_harness():
    output = compile_code(
        HARNESS,
        contract_path="yearn_auction_harness.vy",
        input_bundle=FilesystemInputBundle([MODULE_DIR]),
        output_formats=("abi", "bytecode", "method_identifiers", "opcodes_runtime"),
    )

    assert output["bytecode"] != "0x"
    assert output["method_identifiers"] == {
        "available(address)": "0x10098ad5",
        "getAmountNeeded(address,uint256)": "0xd987b444",
        "price(address)": "0xaea91078",
        "take(address,uint256,address,bytes)": "0x66ae5880",
        "want()": "0x1f1fcd51",
    }

    functions = {item["name"]: item for item in output["abi"] if item["type"] == "function"}
    assert functions == {
        "want": {
            "stateMutability": "view",
            "type": "function",
            "name": "want",
            "inputs": [],
            "outputs": [{"name": "", "type": "address"}],
        },
        "available": {
            "stateMutability": "view",
            "type": "function",
            "name": "available",
            "inputs": [{"name": "_from", "type": "address"}],
            "outputs": [{"name": "", "type": "uint256"}],
        },
        "price": {
            "stateMutability": "view",
            "type": "function",
            "name": "price",
            "inputs": [{"name": "_from", "type": "address"}],
            "outputs": [{"name": "", "type": "uint256"}],
        },
        "getAmountNeeded": {
            "stateMutability": "view",
            "type": "function",
            "name": "getAmountNeeded",
            "inputs": [
                {"name": "_from", "type": "address"},
                {"name": "amountToTake", "type": "uint256"},
            ],
            "outputs": [{"name": "", "type": "uint256"}],
        },
        "take": {
            "stateMutability": "nonpayable",
            "type": "function",
            "name": "take",
            "inputs": [
                {"name": "_from", "type": "address"},
                {"name": "maxAmount", "type": "uint256"},
                {"name": "takerReceiver", "type": "address"},
                {"name": "data", "type": "bytes"},
            ],
            "outputs": [{"name": "", "type": "uint256"}],
        },
    }

    runtime_opcodes = output["opcodes_runtime"].split()
    assert runtime_opcodes.count("TSTORE") == 2  # acquire and release the transient lock
    assert "TLOAD" in runtime_opcodes
    assert not {"CALL", "CALLCODE", "DELEGATECALL", "STATICCALL"}.intersection(runtime_opcodes)
