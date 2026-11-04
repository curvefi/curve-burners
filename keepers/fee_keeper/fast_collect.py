"""
Script template to one-time collect of fees found manually through some special route.
Follow "# ALTER" lines to fill all needed data.
"""
import time
import requests
import os
from dotenv import load_dotenv

from web3 import Web3
from eth_account import Account


chain = "etherum"  # ALTER: chain
FEE_COLLECTOR = {
    "ethereum": "0xa2Bcd1a4Efbd04B63cd03f5aFf2561106ebCCE00",
    "xdai": "0xBb7404F9965487a9DdE721B3A5F0F3CcfA9aa4C5",
}["ethereum"]
MULTICALL = "0xcA11bde05977b3631167028862bE2a173976CA11"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ETH_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

BUILDERS = [
    "https://rpc.beaverbuild.org/",
    "https://rpc.titanbuilder.xyz/",
    "https://rsync-builder.xyz",
    # "https://relay.flashbots.net",
]

web3 = Web3(
    provider=Web3.HTTPProvider(
        "http://localhost:8545",  # ALTER: Chain RPC
    ),
)
if os.path.exists(".env"):
    load_dotenv()
account = Account.from_key(os.getenv("ACCOUNT_PK"))  # ALTER: load private key


def route_logic():
    # ALTER: add logic to collect coins, example with frxeth pool below
    prev_owner = web3.eth.contract("0x173428bD726dBAb910de28527A7b7DDf2AC0444f", abi=[{'stateMutability': 'nonpayable', 'type': 'constructor', 'inputs': [], 'outputs': []}, {'stateMutability': 'nonpayable', 'type': 'function', 'name': 'commit_transfer_ownership', 'inputs': [{'name': '_new_owner', 'type': 'address'}], 'outputs': []}, {'stateMutability': 'nonpayable', 'type': 'function', 'name': 'apply_transfer_ownership', 'inputs': [], 'outputs': []}, {'stateMutability': 'nonpayable', 'type': 'function', 'name': 'revert_transfer_ownership', 'inputs': [], 'outputs': []}, {'stateMutability': 'view', 'type': 'function', 'name': 'admin', 'inputs': [], 'outputs': [{'name': '', 'type': 'address'}]}, {'stateMutability': 'view', 'type': 'function', 'name': 'pool', 'inputs': [], 'outputs': [{'name': '', 'type': 'address'}]}, {'stateMutability': 'view', 'type': 'function', 'name': 'future_pool_owner', 'inputs': [], 'outputs': [{'name': '', 'type': 'address'}]}])
    frxeth_owner = web3.eth.contract("0xA78f1256C2e00bE91d26C4504FA5E9A6dEDeB306", abi=[{'stateMutability': 'payable', 'type': 'fallback'}, {'stateMutability': 'nonpayable', 'type': 'function', 'name': 'commit_transfer_ownership', 'inputs': [{'name': '_new_owner', 'type': 'address'}], 'outputs': []}, {'stateMutability': 'nonpayable', 'type': 'function', 'name': 'apply_transfer_ownership', 'inputs': [], 'outputs': []}, {'stateMutability': 'nonpayable', 'type': 'function', 'name': 'revert_transfer_ownership', 'inputs': [], 'outputs': []}, {'stateMutability': 'nonpayable', 'type': 'function', 'name': 'withdraw_admin_fees', 'inputs': [], 'outputs': []}, {'stateMutability': 'nonpayable', 'type': 'function', 'name': 'set_fee_receiver', 'inputs': [{'name': '_fee_receiver', 'type': 'address'}], 'outputs': []}, {'stateMutability': 'view', 'type': 'function', 'name': 'admin', 'inputs': [], 'outputs': [{'name': '', 'type': 'address'}]}, {'stateMutability': 'view', 'type': 'function', 'name': 'pool', 'inputs': [], 'outputs': [{'name': '', 'type': 'address'}]}, {'stateMutability': 'view', 'type': 'function', 'name': 'future_pool_owner', 'inputs': [], 'outputs': [{'name': '', 'type': 'address'}]}, {'stateMutability': 'view', 'type': 'function', 'name': 'fee_receiver', 'inputs': [], 'outputs': [{'name': '', 'type': 'address'}]}, {'stateMutability': 'nonpayable', 'type': 'constructor', 'inputs': [], 'outputs': []}])
    return [
        # (target, allowFailure, callData)
        (prev_owner.address, True, prev_owner.encodeABI(fn_name="apply_transfer_ownership", args=[])),
        (frxeth_owner.address, False, frxeth_owner.encodeABI(fn_name="withdraw_admin_fees", args=[])),
    ]


def fee_collector_collect():
    fee_collector = web3.eth.contract(
        FEE_COLLECTOR,
        abi=[
            {"stateMutability":"nonpayable", "type": "function", "name": "withdraw_many", "inputs": [{"name": "_pools", "type": "address[]"}], "outputs": []},
            {"stateMutability":"nonpayable","type":"function","name":"collect","inputs":[{"name":"_coins","type":"address[]"},{"name":"_receiver","type":"address"}],"outputs":[]},
        ],
    )

    return [
        # (target, allowFailure, callData)
        (
            fee_collector.address,
            False,
            fee_collector.encodeABI(
                fn_name="collect",
                args=[
                    [ETH_ADDRESS, "0x5E8422345238F34275888049021821E8E08CAa1f"],  # ALTER: List of coins, that FeeCollector will receive in result of route_logic
                    "0xcb78EA4Bc3c545EB48dDC9b8302Fa9B03d1B1B61",  # ALTER: Address to receive fees from this operation
                ]
            ),
        )
    ]


def build_multicall_transaction(calls):
    multicall = web3.eth.contract(MULTICALL, abi=[{'inputs': [{'components': [{'internalType': 'address', 'name': 'target', 'type': 'address'}, {'internalType': 'bytes', 'name': 'callData', 'type': 'bytes'}], 'internalType': 'struct Multicall3.Call[]', 'name': 'calls', 'type': 'tuple[]'}], 'name': 'aggregate', 'outputs': [{'internalType': 'uint256', 'name': 'blockNumber', 'type': 'uint256'}, {'internalType': 'bytes[]', 'name': 'returnData', 'type': 'bytes[]'}], 'stateMutability': 'payable', 'type': 'function'}, {'inputs': [{'components': [{'internalType': 'address', 'name': 'target', 'type': 'address'}, {'internalType': 'bool', 'name': 'allowFailure', 'type': 'bool'}, {'internalType': 'bytes', 'name': 'callData', 'type': 'bytes'}], 'internalType': 'struct Multicall3.Call3[]', 'name': 'calls', 'type': 'tuple[]'}], 'name': 'aggregate3', 'outputs': [{'components': [{'internalType': 'bool', 'name': 'success', 'type': 'bool'}, {'internalType': 'bytes', 'name': 'returnData', 'type': 'bytes'}], 'internalType': 'struct Multicall3.Result[]', 'name': 'returnData', 'type': 'tuple[]'}], 'stateMutability': 'payable', 'type': 'function'}, {'inputs': [{'components': [{'internalType': 'address', 'name': 'target', 'type': 'address'}, {'internalType': 'bool', 'name': 'allowFailure', 'type': 'bool'}, {'internalType': 'uint256', 'name': 'value', 'type': 'uint256'}, {'internalType': 'bytes', 'name': 'callData', 'type': 'bytes'}], 'internalType': 'struct Multicall3.Call3Value[]', 'name': 'calls', 'type': 'tuple[]'}], 'name': 'aggregate3Value', 'outputs': [{'components': [{'internalType': 'bool', 'name': 'success', 'type': 'bool'}, {'internalType': 'bytes', 'name': 'returnData', 'type': 'bytes'}], 'internalType': 'struct Multicall3.Result[]', 'name': 'returnData', 'type': 'tuple[]'}], 'stateMutability': 'payable', 'type': 'function'}, {'inputs': [{'components': [{'internalType': 'address', 'name': 'target', 'type': 'address'}, {'internalType': 'bytes', 'name': 'callData', 'type': 'bytes'}], 'internalType': 'struct Multicall3.Call[]', 'name': 'calls', 'type': 'tuple[]'}], 'name': 'blockAndAggregate', 'outputs': [{'internalType': 'uint256', 'name': 'blockNumber', 'type': 'uint256'}, {'internalType': 'bytes32', 'name': 'blockHash', 'type': 'bytes32'}, {'components': [{'internalType': 'bool', 'name': 'success', 'type': 'bool'}, {'internalType': 'bytes', 'name': 'returnData', 'type': 'bytes'}], 'internalType': 'struct Multicall3.Result[]', 'name': 'returnData', 'type': 'tuple[]'}], 'stateMutability': 'payable', 'type': 'function'}, {'inputs': [], 'name': 'getBasefee', 'outputs': [{'internalType': 'uint256', 'name': 'basefee', 'type': 'uint256'}], 'stateMutability': 'view', 'type': 'function'}, {'inputs': [{'internalType': 'uint256', 'name': 'blockNumber', 'type': 'uint256'}], 'name': 'getBlockHash', 'outputs': [{'internalType': 'bytes32', 'name': 'blockHash', 'type': 'bytes32'}], 'stateMutability': 'view', 'type': 'function'}, {'inputs': [], 'name': 'getBlockNumber', 'outputs': [{'internalType': 'uint256', 'name': 'blockNumber', 'type': 'uint256'}], 'stateMutability': 'view', 'type': 'function'}, {'inputs': [], 'name': 'getChainId', 'outputs': [{'internalType': 'uint256', 'name': 'chainid', 'type': 'uint256'}], 'stateMutability': 'view', 'type': 'function'}, {'inputs': [], 'name': 'getCurrentBlockCoinbase', 'outputs': [{'internalType': 'address', 'name': 'coinbase', 'type': 'address'}], 'stateMutability': 'view', 'type': 'function'}, {'inputs': [], 'name': 'getCurrentBlockDifficulty', 'outputs': [{'internalType': 'uint256', 'name': 'difficulty', 'type': 'uint256'}], 'stateMutability': 'view', 'type': 'function'}, {'inputs': [], 'name': 'getCurrentBlockGasLimit', 'outputs': [{'internalType': 'uint256', 'name': 'gaslimit', 'type': 'uint256'}], 'stateMutability': 'view', 'type': 'function'}, {'inputs': [], 'name': 'getCurrentBlockTimestamp', 'outputs': [{'internalType': 'uint256', 'name': 'timestamp', 'type': 'uint256'}], 'stateMutability': 'view', 'type': 'function'}, {'inputs': [{'internalType': 'address', 'name': 'addr', 'type': 'address'}], 'name': 'getEthBalance', 'outputs': [{'internalType': 'uint256', 'name': 'balance', 'type': 'uint256'}], 'stateMutability': 'view', 'type': 'function'}, {'inputs': [], 'name': 'getLastBlockHash', 'outputs': [{'internalType': 'bytes32', 'name': 'blockHash', 'type': 'bytes32'}], 'stateMutability': 'view', 'type': 'function'}, {'inputs': [{'internalType': 'bool', 'name': 'requireSuccess', 'type': 'bool'}, {'components': [{'internalType': 'address', 'name': 'target', 'type': 'address'}, {'internalType': 'bytes', 'name': 'callData', 'type': 'bytes'}], 'internalType': 'struct Multicall3.Call[]', 'name': 'calls', 'type': 'tuple[]'}], 'name': 'tryAggregate', 'outputs': [{'components': [{'internalType': 'bool', 'name': 'success', 'type': 'bool'}, {'internalType': 'bytes', 'name': 'returnData', 'type': 'bytes'}], 'internalType': 'struct Multicall3.Result[]', 'name': 'returnData', 'type': 'tuple[]'}], 'stateMutability': 'payable', 'type': 'function'}, {'inputs': [{'internalType': 'bool', 'name': 'requireSuccess', 'type': 'bool'}, {'components': [{'internalType': 'address', 'name': 'target', 'type': 'address'}, {'internalType': 'bytes', 'name': 'callData', 'type': 'bytes'}], 'internalType': 'struct Multicall3.Call[]', 'name': 'calls', 'type': 'tuple[]'}], 'name': 'tryBlockAndAggregate', 'outputs': [{'internalType': 'uint256', 'name': 'blockNumber', 'type': 'uint256'}, {'internalType': 'bytes32', 'name': 'blockHash', 'type': 'bytes32'}, {'components': [{'internalType': 'bool', 'name': 'success', 'type': 'bool'}, {'internalType': 'bytes', 'name': 'returnData', 'type': 'bytes'}], 'internalType': 'struct Multicall3.Result[]', 'name': 'returnData', 'type': 'tuple[]'}], 'stateMutability': 'payable', 'type': 'function'}])
    nonce = web3.eth.get_transaction_count(account=account.address)
    # print(f"multicall calldata: {multicall.encodeABI(fn_name='aggregate3', args=[calls])}")
    tx = multicall.functions.aggregate3(calls).build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "maxFeePerGas": 5 * 10 ** 9,
            "maxPriorityFeePerGas": 1 * 10 ** 9,
        }
    )
    signed_tx = web3.eth.account.sign_transaction(tx, private_key=account.private_key)
    return signed_tx


def send_transaction(signed_tx):
    wallet_address = signed_tx["from"]
    nonce = signed_tx["nonce"]
    iters = 5  # ALTER: number of iterations to try
    while web3.eth.get_transaction_count(wallet_address) < nonce and iters > 0:
        block = web3.eth.get_block_number() + 1
        print(f"Trying block: {block}")
        for builder in BUILDERS:
            if "flashbots" in builder:
                r = requests.post(builder, json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_sendBundle",
                    "params": [
                        {
                            "version": "v0.1",
                            "inclusion": {"block": str(hex(block)), "maxBlock": str(hex(block))},
                            "body": [{"tx": signed_tx.rawTransaction.hex(), "canRevert": True}],
                        }
                    ]
                })
            else:
                r = requests.post(builder, json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_sendBundle",
                    "params": [
                        {
                            "txs": [signed_tx.rawTransaction.hex()],
                            "blockNumber": str(hex(block)),
                        }
                    ]
                })
            print(builder, r.json())
        iters -= 1
        time.sleep(6)  # wait some time between blocks


if __name__ == '__main__':
    # construct calldata
    calls = route_logic() + fee_collector_collect()
    print(calls)
    print("Press Enter to proceed", end='') ; input()

    signed_tx = build_multicall_transaction(calls)
    wallet_address = signed_tx["from"]
    nonce = signed_tx["nonce"]
    print(f"Signed transaction from {wallet_address} with nonce {nonce}")

    send_transaction(signed_tx)
    if web3.eth.get_transaction_count(wallet_address) > nonce:
        print("Go check ur wallet, I dit sth for ya ^&^")
