import asyncio
import os
import json
import time
import requests

from web3 import Web3
from web3.eth import AsyncEth
from web3.middleware import geth_poa_middleware
from getpass import getpass
from eth_account import account

chain = "ethereum"  # ethereum|xdai
RPC = {
    "ethereum": f"http://localhost:8545",
    "xdai": f"https://rpc.gnosischain.com",
}
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ETH_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
CRVUSD = {
    "ethereum": "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E",
    "xdai": "0xaBEf652195F98A91E490f047A5006B71c85f058d",
}[chain]
FEE_COLLECTOR = {
    "ethereum": "0xa2Bcd1a4Efbd04B63cd03f5aFf2561106ebCCE00",
    "xdai": "0xBb7404F9965487a9DdE721B3A5F0F3CcfA9aa4C5",
}[chain]
PROXY = {
    "ethereum": "0xeCb456EA5365865EbAb8a2661B0c503410e9B347",
    "xdai": "0x3B48eE129D74A63461FE54Ec7226C019F5b6b203",
}[chain]
EMPTY_HOOK_INPUT = (0, 0, b"")

web3 = Web3(
    provider=Web3.HTTPProvider(
        RPC[chain],
    ),
)
if chain == "xdai":
    web3.middleware_onion.inject(geth_poa_middleware, layer=0)


def account_load_pkey(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return pkey
wallet_address = "0x71F718D3e4d1449D1502A6A7595eb84eBcCB1683"
wallet_pk = account_load_pkey("curve")


BUILDERS = [
    "https://rpc.beaverbuild.org/",
    "https://rpc.titanbuilder.xyz/",
    "https://rsync-builder.xyz",
    "https://relay.flashbots.net",
]

class DataFetcher:
    web3 = Web3(
        provider=Web3.AsyncHTTPProvider(
            RPC[chain],
            {"verify_ssl": False}
        ),
        modules={"eth": (AsyncEth,)},
    )
    POOL_BLACKLIST = [

    ]

    def __init__(self):
        self.POOL_BLACKLIST = [pool.lower() for pool in self.POOL_BLACKLIST]

    def fetch_sources(self):
        pool_data = []
        for registry in ["main", "factory", "factory-crvusd"]:
            pool_data.extend(requests.get(
                f"https://api.curve.fi/api/getPools/{chain}/{registry}",
            ).json().get("data", {}).get("poolData", []))

        crvusd_pools = []
        for pool_dict in pool_data:
            if pool_dict["usdTotal"] <= 1000:
                continue
            for i, coin in enumerate(pool_dict["coins"]):
                if coin["address"].lower() == CRVUSD.lower():
                    crvusd_pools.append((pool_dict["address"], i))
                    break

        crvusd_pools = [(pool, i) for pool, i in crvusd_pools if pool.lower() not in self.POOL_BLACKLIST]
        self.crvusd_pools = crvusd_pools

        self.controllers = [
            "0xA920De414eA4Ab66b97dA1bFE9e6EcA7d4219635",  # ETH
            "0x4e59541306910aD6dC1daC0AC9dFB29bD9F15c67",  # wBTC
            "0x100dAa78fC509Db39Ef7D04DE0c1ABD299f4C6CE",  # wstETH
            "0xEC0820EfafC41D8943EE8dE495fC9Ba8495B15cf",  # sfrxETH 2
            "0x1C91da0223c763d2e0173243eAdaA0A2ea47E704",  # tBTC
            "0x8472A9A7632b173c8Cf3a86D3afec50c35548e76",  # sfrxETH
        ] if chain == "ethereum" else []

        self.bridge_txs = [  # ((contract, data), amount * 10 ** 18)
        ]

    async def get_amounts(self):
        pool_amounts = {}
        for pool, i in self.crvusd_pools:
            contract = web3.eth.contract(pool, abi=[{"stateMutability":"view","type":"function","name":"admin_balances","inputs":[{"name":"arg0","type":"uint256"}],"outputs":[{"name":"","type":"uint256"}]},])
            pool_amounts[pool] = contract.functions.admin_balances(i).call()

        controller_amounts = {}
        if chain == "ethereum":
            for controller in self.controllers:
                contract = web3.eth.contract(controller, abi=[{"stateMutability":"view","type":"function","name":"admin_fees","inputs":[],"outputs":[{"name":"","type":"uint256"}]},])
                controller_amounts[controller] = contract.functions.admin_fees().call()

        contract = web3.eth.contract(CRVUSD, abi=[{"stateMutability":"view","type":"function","name":"balanceOf","inputs":[{"name":"arg0","type":"address"}],"outputs":[{"name":"","type":"uint256"}]},])
        balance = contract.functions.balanceOf(FEE_COLLECTOR).call()
        proxy_balance = contract.functions.balanceOf(PROXY).call()

        return pool_amounts, controller_amounts, self.bridge_txs if chain == "ethereum" else [], balance, proxy_balance


def forward(prev_tx, calls):
    multicall = web3.eth.contract("0xcA11bde05977b3631167028862bE2a173976CA11", abi=[{"inputs": [{"components": [{"internalType": "address", "name": "target", "type": "address"},{"internalType": "bool", "name": "allowFailure", "type": "bool"},{"internalType": "bytes", "name": "callData", "type": "bytes"}], "internalType": "struct Multicall3.Call3[]","name": "calls","type": "tuple[]"}],"name": "aggregate3", "outputs": [{"components": [{"internalType": "bool", "name": "success", "type": "bool"},{"internalType": "bytes", "name": "returnData", "type": "bytes"}],"internalType": "struct Multicall3.Result[]", "name": "returnData", "type": "tuple[]"}],"stateMutability": "payable","type": "function"}, ])
    fee_collector = web3.eth.contract(FEE_COLLECTOR, abi=[{"stateMutability": "payable", "type": "function", "name": "forward", "inputs": [{"name": "_hook_inputs", "type": "tuple[]","components": [{"name": "hook_id", "type": "uint8"}, {"name": "value", "type": "uint256"},{"name": "data", "type": "bytes"}]}], "outputs": [{"name": "", "type": "uint256"}]},{"stateMutability": "payable", "type": "function", "name": "forward", "inputs": [{"name": "_hook_inputs", "type": "tuple[]","components": [{"name": "hook_id", "type": "uint8"}, {"name": "value", "type": "uint256"},{"name": "data", "type": "bytes"}]}, {"name": "_receiver", "type": "address"}],"outputs": [{"name": "", "type": "uint256"}]}, ], )

    nonce = web3.eth.get_transaction_count(wallet_address)
    calls += [
        (fee_collector.address, False, fee_collector.encodeABI("forward", ([EMPTY_HOOK_INPUT], "0xcb78EA4Bc3c545EB48dDC9b8302Fa9B03d1B1B61"))),
    ]
    max_fee = 20 * 10 ** 9  # even 10 GWEI should be enough for Wednesday morning
    max_priority = 1000000000

    txs = []
    if prev_tx:
        txs.append(prev_tx.build_transaction({
            "from": wallet_address, "nonce": nonce,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": max_priority,
        }))
    txs.append(multicall.functions.aggregate3(calls).build_transaction({
        "from": wallet_address, "nonce": nonce + (1 if prev_tx else 0),
        "maxFeePerGas": max_fee, "maxPriorityFeePerGas": max_priority,
    }))

    iters = 0
    while web3.eth.get_transaction_count(wallet_address) <= nonce and iters < 2:
        try:
            for tx in txs:
                gas_estimate = web3.eth.estimate_gas(tx)
                tx["gas"] = int(1.1 * gas_estimate)
        except Exception as e:
            print("Could not estimate gas", repr(e))
            return
        signed_txs = [web3.eth.account.sign_transaction(tx, private_key=wallet_pk) for tx in txs]
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
                            "body": [{"tx": tx.rawTransaction.hex(), "canRevert": True} for tx in signed_txs],
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
                            "txs": [tx.rawTransaction.hex() for tx in signed_txs],
                            "blockNumber": str(hex(block)),
                        }
                    ]
                })
            print(builder, r.json())
        iters += 1
        time.sleep(2 * 12)  # wait for a couple of blocks
    if web3.eth.get_transaction_count(wallet_address) > nonce:
        print("Go check ur wallet, I dit sth for ya ^&^")


def forward_l2(prev_tx, calls):
    multicall = web3.eth.contract("0xcA11bde05977b3631167028862bE2a173976CA11", abi=[{"inputs": [{"components": [{"internalType": "address", "name": "target", "type": "address"},{"internalType": "bool", "name": "allowFailure", "type": "bool"},{"internalType": "bytes", "name": "callData", "type": "bytes"}], "internalType": "struct Multicall3.Call3[]","name": "calls","type": "tuple[]"}],"name": "aggregate3", "outputs": [{"components": [{"internalType": "bool", "name": "success", "type": "bool"},{"internalType": "bytes", "name": "returnData", "type": "bytes"}],"internalType": "struct Multicall3.Result[]", "name": "returnData", "type": "tuple[]"}],"stateMutability": "payable","type": "function"}, ])
    fee_collector = web3.eth.contract(FEE_COLLECTOR, abi=[{"stateMutability": "payable", "type": "function", "name": "forward", "inputs": [{"name": "_hook_inputs", "type": "tuple[]","components": [{"name": "hook_id", "type": "uint8"}, {"name": "value", "type": "uint256"},{"name": "data", "type": "bytes"}]}], "outputs": [{"name": "", "type": "uint256"}]},{"stateMutability": "payable", "type": "function", "name": "forward", "inputs": [{"name": "_hook_inputs", "type": "tuple[]","components": [{"name": "hook_id", "type": "uint8"}, {"name": "value", "type": "uint256"},{"name": "data", "type": "bytes"}]}, {"name": "_receiver", "type": "address"}],"outputs": [{"name": "", "type": "uint256"}]}, ], )

    nonce = web3.eth.get_transaction_count(wallet_address)
    calls += [
        (fee_collector.address, True, fee_collector.encodeABI("forward", ([EMPTY_HOOK_INPUT], "0x8C95d2ad015f12B03ad4712a48a37c2A68970f62"))),
    ]
    txs = []
    if prev_tx:
        txs.append(prev_tx.build_transaction({"from": wallet_address, "nonce": nonce,}))
    txs.append(multicall.functions.aggregate3(calls).build_transaction({
        "from": wallet_address,"nonce": nonce + (1 if prev_tx else 0),
    }))

    iters = 0
    for tx in txs:
        while web3.eth.get_transaction_count(wallet_address) <= nonce and iters < 2:
            signed_tx = web3.eth.account.sign_transaction(tx, private_key=wallet_pk)
            web3.eth.send_raw_transaction(signed_tx.rawTransaction)
            iters += 1
            time.sleep(2 * 12)  # wait for a couple of blocks
        nonce += 1
    if web3.eth.get_transaction_count(wallet_address) > nonce:
        print("Go check ur wallet, I dit sth for ya ^&^")


async def run():
    data_fetcher = DataFetcher()
    data_fetcher.fetch_sources()

    latest_block = web3.eth.get_block("latest")
    base_fee = latest_block["baseFeePerGas"]
    ts = latest_block["timestamp"] + 12
    while 6 * 24 * 3600 < (ts - 1600300800) % (7 * 24 * 3600) < 7 * 24 * 3600:
        if chain == "ethereum":
            safe_amount = 30 * (base_fee / (10 * 10 ** 9)) * (3500 / 3500)
        else:
            safe_amount = 1000
        fee = 0.01 * ((ts - 1600300800) % (24 * 3600)) / (24 * 3600)
        safe_threshold = int(safe_amount / fee) * 10 ** 18 if chain == "ethereum" else 100 * 10 ** 18

        pools, controllers, bridge_txs, balance, proxy_balance = await data_fetcher.get_amounts()
        calls, cnt, total = [], 0, 0
        for pool, amount in pools.items():
            try:
                if amount >= safe_threshold:
                    calls.append((pool, False, bytes.fromhex("30c54085")))
                    cnt += 1 ; total += amount
            except Exception as e:
                print(f"{pool} admin_balances {repr(e)}")
                return
        for controller, amount in controllers.items():
            try:
                if amount >= safe_threshold:
                    calls.append((controller, False, bytes.fromhex("1e0cfcef")))
                    cnt += 1 ; total += amount
            except Exception as e:
                print(f"{controller} admin_fees() {repr(e)}")
                return
        for (bridge, tx), amount in bridge_txs:
            if amount >= safe_threshold:
                calls.append((bridge, False, tx))
                cnt += 1 ; total += amount
        if balance >= safe_threshold:
            cnt += 1
            total += balance

        prev_tx = None
        if proxy_balance >= safe_threshold:
            contract = web3.eth.contract(PROXY, abi=[{"name":"burn","outputs":[],"inputs":[{"type":"address","name":"_coin"}],"stateMutability":"nonpayable","type":"function","gas":93478},])
            prev_tx = contract.functions.burn(CRVUSD)
            cnt += 1 ; total += proxy_balance

        if cnt > 0:
            print(f"Trying to profit {total * fee / 10 ** 18:.2f} crvUSD from {cnt} sources")
            if chain == "ethereum":
                forward(prev_tx, calls)
            else:
                forward_l2(prev_tx, calls)

        latest_block = web3.eth.get_block("latest")
        base_fee = latest_block["baseFeePerGas"]
        ts = latest_block["timestamp"] + 12


if __name__ == "__main__":
    asyncio.run(run())
