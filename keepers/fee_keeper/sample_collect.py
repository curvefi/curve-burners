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
RECEIVER = {
    "ethereum": "0xcb78EA4Bc3c545EB48dDC9b8302Fa9B03d1B1B61",
    "xdai": "0x8C95d2ad015f12B03ad4712a48a37c2A68970f62",
}[chain]
EXTREME_AMOUNT = {  # basically CoWBurner target_threshold
    "ethereum": 400,
    "xdai": 1,
}[chain]

web3 = Web3(
    provider=Web3.HTTPProvider(
        RPC[chain],
    ),
)
if chain == "xdai":
    web3.middleware_onion.inject(geth_poa_middleware, layer=0)

class DataFetcher:
    web3 = Web3(
        provider=Web3.AsyncHTTPProvider(
            RPC[chain],
            {"verify_ssl": False},
        ),
        modules={"eth": (AsyncEth,)},
    )
    POOL_BLACKLIST = ["0xF9440930043eb3997fc70e1339dBb11F341de7A8", "0xa1F8A6807c402E4A15ef4EBa36528A3FED24E577",]
    COINS_BLACKLIST = [CRVUSD, "0xE8449F1495012eE18dB7Aa18cD5706b47e69627c", "0x4D1941a887eC788F059b3bfcC8eE1E97b968825B", "0x470EBf5f030Ed85Fc1ed4C2d36B9DD02e77CF1b7"]
    I128_BALANCES_LIST = ["0x93054188d876f558f4a66b2ef1d97d16edf0895b", "0x7fc77b5c7614e1533320ea6ddc2eb61fa00a9714", "0xa5407eae9ba41422680e2e00537571bcc53efbfd", "0x52EA46506B9CC5Ef470C5bf89f17Dc28bB35D85C", "0xA2B47E3D5c44877cca798226B7B8118F9BFb7A56", "0x79a8C46DeA5aDa233ABaFFD40F3A0A2B1e5A4F27", "0x06364f10B501e868329afBc005b3492902d6C763"]
    PROXY_RECEIVER = {
        "ethereum": [],  # need to fill in
        "xdai": ["0x7f90122BF0700F9E7e1F688fe926940E8839F353"],
    }[chain]

    def __init__(self):
        self.POOL_BLACKLIST = [pool.lower() for pool in self.POOL_BLACKLIST]
        self.COINS_BLACKLIST = [coin.lower() for coin in self.COINS_BLACKLIST]
        self.I128_BALANCES_LIST = [pool.lower() for pool in self.I128_BALANCES_LIST]
        self.PROXY_RECEIVER = [pool.lower() for pool in self.PROXY_RECEIVER]

    def fetch_prices(self):
        prices = dict()

        def update_if_not_set(coin, price, dec):
            if coin.lower() in self.COINS_BLACKLIST or not price:
                return
            if not prices.get(coin.lower(), None):
                prices[coin.lower()] = (price, int(dec))

        response = requests.get(
            f"https://api.curve.fi/api/getPools/all/{chain}/",
        ).json()["data"]["poolData"]
        for pool_dict in response:
            for coin, dec in zip(pool_dict["coins"], pool_dict["decimals"]):
                update_if_not_set(coin["address"], coin["usdPrice"], dec)
            update_if_not_set(pool_dict["lpTokenAddress"], pool_dict["usdTotal"] * (int(pool_dict["virtualPrice"]) / max(int(pool_dict["totalSupply"]), 1)), 18)  # approximation
        self.prices = prices

        all_coins = list(prices.keys())
        print(f"all_coins from prices: {len(all_coins)}")
        unpriced_coins = [coin for coin in all_coins if not prices.get(coin)]
        print(f"of them unpriced_coins: {len(unpriced_coins)}")

        self.all_coins = list(set(all_coins) - set(unpriced_coins))

    def balance_of(self, coin, of):
        of = Web3.to_checksum_address(of)
        if coin == "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee":
            return self.web3.eth.get_balance(of)
        contract = self.web3.eth.contract(
            address=Web3.to_checksum_address(coin),
            abi=[{"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "payable": False, "stateMutability": "view", "type": "function"}, ],
        )
        return contract.functions.balanceOf(of).call()

    async def get_balances(self, coins, of, _decimals=None):
        balances = {}
        for coin in coins:
            balances[coin] = self.balance_of(coin, of)
        for coin in coins:
            price, dec = self.prices.get(coin, (0., 0))
            balances[coin] = ((await balances[coin]) / (10 ** dec)) * price
        return balances

    def fetch_sources(self):
        stable_data = []
        for registry in ["main", "factory", "factory-crvusd"]:  # "factory-stable-ng" should withdraw automatically, may be not all
            stable_data.extend(requests.get(
                f"https://api.curve.fi/api/getPools/{chain}/{registry}",
            ).json().get("data", {}).get("poolData", []))

        self.stable_pools = []
        for pool_dict in stable_data:
            pool = pool_dict["address"].lower()
            if pool_dict["usdTotal"] <= 1_000_000 or pool in self.POOL_BLACKLIST:
                continue
            self.stable_pools.append({"address": pool,
                    "coins": [coin["address"].lower() for coin in pool_dict["coins"] if coin["address"].lower() != ZERO_ADDRESS],
                    "decimals": [int(dec) for dec in pool_dict["decimals"] if int(dec) > 0],})

        self.peg_keepers = [
            ("0x9201da0D97CaAAff53f01B2fB56767C7072dE340", "0x4DEcE678ceceb27446b35C672dC7d61F30bAD69E".lower()),  # USDC
            ("0xFb726F57d251aB5C731E5C64eD4F5F94351eF9F3", "0x390f3595bCa2Df7d23783dFd126427CCeb997BF4".lower()),  # USDT
            ("0x3fA20eAa107DE08B38a8734063D605d5842fe09C", "0x625E92624Bc2D88619ACCc1788365A69767f6200".lower()),  # pyUSD
            ("0x0a05FF644878B908eF8EB29542aa88C07D9797D3", "0x34D655069F4cAc1547E4C8cA284FfFF5ad4A8db0".lower()),  # TUSD
        ] if chain == "ethereum" else []
        # Add crypto pools


    async def get_amounts(self):
        for pool in self.stable_pools:
            try:
                contract = self.web3.eth.contract(
                    address=Web3.to_checksum_address(pool["address"]),
                    abi=[{"name": "balances", "outputs": [{"type": "uint256", "name": ""}], "inputs": [{"type": "uint256", "name": "i"}], "stateMutability": "view", "type": "function", "gas": 5076},] if pool["address"] not in self.I128_BALANCES_LIST else [{"name": "balances", "outputs": [{"type": "uint256", "name": ""}], "inputs": [{"type": "int128", "name": "i"}], "stateMutability": "view", "type": "function", "gas": 5076},],
                )
                pool["balances"] = [[self.balance_of(coin, pool["address"]), contract.functions.balances(i).call()] for i, coin in enumerate(pool["coins"])]
            except Exception as e:
                print(f"Couldn't get balances for {pool['address']}",  repr(e))

        for pool in self.stable_pools:
            pool["amount"] = 0
            for i, bals in enumerate(pool["balances"]):
                for j in range(len(bals)):
                    try:
                        pool["balances"][i][j] = await pool["balances"][i][j]
                    except Exception as e:
                        print(f"Couldn't get balances for {pool['address']} {i} {'inner' if j == 1 else 'balanceOf'}", repr(e))
                        pool["balances"][i][j] = 0
            for coin, (bal, inner_bal) in zip(pool["coins"], pool["balances"]):
                if coin in self.COINS_BLACKLIST:
                    continue
                price, dec = self.prices.get(coin, (0., 0))
                pool["amount"] += ((bal - inner_bal) / 10 ** dec) * price

        pks = []
        for pk, pool in self.peg_keepers:
            contract = self.web3.eth.contract(address=pk, abi=[{"stateMutability":"view","type":"function","name":"calc_profit","inputs":[],"outputs":[{"name":"","type":"uint256"}]},])
            pks.append((pk, pool, (await contract.functions.calc_profit().call()) / 10 ** 18))

        print(f"fetched stable pools amounts")
        proxy_balances = await self.get_balances(self.all_coins, PROXY)
        print(f"fetched proxy balances")
        collector_balances = await self.get_balances(self.all_coins, FEE_COLLECTOR)
        print(f"fetched collector balances")

        return self.stable_pools, proxy_balances, pks, collector_balances


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


def collect_l1(withdraw_proxy, burn, withdraw_fc, pk_profit, collect, iters=5):
    # multicall = web3.eth.contract("0xcA11bde05977b3631167028862bE2a173976CA11", abi=[{"inputs": [{"components": [{"internalType": "address", "name": "target", "type": "address"},{"internalType": "bool", "name": "allowFailure", "type": "bool"},{"internalType": "bytes", "name": "callData", "type": "bytes"}], "internalType": "struct Multicall3.Call3[]","name": "calls","type": "tuple[]"}],"name": "aggregate3", "outputs": [{"components": [{"internalType": "bool", "name": "success", "type": "bool"},{"internalType": "bytes", "name": "returnData", "type": "bytes"}],"internalType": "struct Multicall3.Result[]", "name": "returnData", "type": "tuple[]"}],"stateMutability": "payable","type": "function"}, ])
    fee_collector = web3.eth.contract(FEE_COLLECTOR, abi=[
        {"stateMutability":"nonpayable", "type": "function", "name": "withdraw_many", "inputs": [{"name": "_pools", "type": "address[]"}], "outputs": []},
        {"stateMutability":"nonpayable","type":"function","name":"collect","inputs":[{"name":"_coins","type":"address[]"},{"name":"_receiver","type":"address"}],"outputs":[]},], )

    nonce = web3.eth.get_transaction_count(wallet_address)
    max_fee = 20 * 10 ** 9  # even 10 GWEI should be enough for Wednesday morning
    max_priority = 2 * 10 ** 9 + 10 ** 8

    txs = []
    if withdraw_proxy:  # proxy.burn() has tx.origin check
        withdraw_proxy = [web3.to_checksum_address(coin) for coin in withdraw_proxy]
        if len(withdraw_proxy) % 20:
            withdraw_proxy += [ZERO_ADDRESS] * (20 - (len(withdraw_proxy) % 20))
        proxy = web3.eth.contract(PROXY, abi=[{"name":"withdraw_many","outputs":[],"inputs":[{"type":"address[20]","name":"_pools"}],"stateMutability":"nonpayable","type":"function","gas":93116},])
        for i in range(0, len(withdraw_proxy), 20):
            print("WITHDRAW PROXY", withdraw_proxy[i: i + 20])
            txs.append(proxy.functions.withdraw_many(withdraw_proxy[i: i + 20]).build_transaction({
                "from": wallet_address, "nonce": nonce,
                "maxFeePerGas": max_fee, "maxPriorityFeePerGas": max_priority,
            }))
            nonce += 1

    if burn:
        burn = [web3.to_checksum_address(coin) for coin in burn]
        if len(burn) % 20:
            burn += [ZERO_ADDRESS] * (20 - (len(burn) % 20))
        proxy = web3.eth.contract(PROXY, abi=[{"name":"burn_many","outputs":[],"inputs":[{"type":"address[20]","name":"_coins"}],"stateMutability":"nonpayable","type":"function","gas":780568},])
        for i in range(0, len(burn), 20):
            print("BURN PROXY", burn[i: i + 20])
            txs.append(proxy.functions.burn_many(burn[i: i + 20]).build_transaction({
                "from": wallet_address, "nonce": nonce,
                "maxFeePerGas": max_fee, "maxPriorityFeePerGas": max_priority,
            }))
            nonce += 1
    if withdraw_fc:
        withdraw_fc = [web3.to_checksum_address(coin) for coin in withdraw_fc]
        print("WITHDRAW FC", withdraw_fc)
        txs.append(fee_collector.functions.withdraw_many(withdraw_fc).build_transaction({
            "from": wallet_address, "nonce": nonce,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": max_priority,
        }))
        nonce += 1

    if pk_profit:
        pk_profit = [web3.to_checksum_address(pk) for pk in pk_profit]
        print("PK PROFIT", pk_profit)
        for pk in pk_profit:
            contract = web3.eth.contract(pk, abi=[{"stateMutability":"nonpayable","type":"function","name":"withdraw_profit","inputs":[],"outputs":[{"name":"","type":"uint256"}]},])
            txs.append(contract.functions.withdraw_profit().build_transaction({
                "from": wallet_address, "nonce": nonce,
                "maxFeePerGas": max_fee, "maxPriorityFeePerGas": max_priority,
            }))
            nonce += 1

    if ETH_ADDRESS.lower() in collect:
        collect.remove(ETH_ADDRESS.lower())
        if "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2".lower() not in collect:
            collect.append("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2".lower())
    collect = list(sorted(collect, key=lambda coin: int(coin, base=16)))
    collect = [web3.to_checksum_address(coin) for coin in collect]
    print("COLLECT", collect)
    for i in range(0, len(collect), 64):
        txs.append(fee_collector.functions.collect(collect[i: min(i + 64, len(collect))], RECEIVER).build_transaction({
            "from": wallet_address, "nonce": nonce,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": max_priority,
        }))
        nonce += 1

    try:
        for tx in txs:
            gas_estimate = web3.eth.estimate_gas(tx)
            tx["gas"] = int(2 * gas_estimate)
    except Exception as e:
        print("Could not estimate gas", repr(e))
        return
    signed_txs = [web3.eth.account.sign_transaction(tx, private_key=wallet_pk) for tx in txs]
    # block = web3.eth.get_block_number()
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
        iters -= 1
        # block += 1
        time.sleep(6)  # wait some time between blocks
    if web3.eth.get_transaction_count(wallet_address) > nonce:
        print("Go check ur wallet, I dit sth for ya ^&^")


def collect_l2(withdraw_proxy, burn, withdraw_fc, collect):
    # multicall = web3.eth.contract("0xcA11bde05977b3631167028862bE2a173976CA11", abi=[{"inputs": [{"components": [{"internalType": "address", "name": "target", "type": "address"},{"internalType": "bool", "name": "allowFailure", "type": "bool"},{"internalType": "bytes", "name": "callData", "type": "bytes"}], "internalType": "struct Multicall3.Call3[]","name": "calls","type": "tuple[]"}],"name": "aggregate3", "outputs": [{"components": [{"internalType": "bool", "name": "success", "type": "bool"},{"internalType": "bytes", "name": "returnData", "type": "bytes"}],"internalType": "struct Multicall3.Result[]", "name": "returnData", "type": "tuple[]"}],"stateMutability": "payable","type": "function"}, ])
    fee_collector = web3.eth.contract(FEE_COLLECTOR, abi=[
        {"stateMutability":"nonpayable", "type": "function", "name": "withdraw_many", "inputs": [{"name": "_pools", "type": "address[]"}], "outputs": []},
        {"stateMutability":"nonpayable","type":"function","name":"collect","inputs":[{"name":"_coins","type":"address[]"},{"name":"_receiver","type":"address"}],"outputs":[]},], )

    nonce = web3.eth.get_transaction_count(wallet_address)
    # max_fee = 20 * 10 ** 9  # even 10 GWEI should be enough for Wednesday morning
    # max_priority = 2 * 10 ** 9

    txs = []
    if withdraw_proxy:  # proxy.burn() has tx.origin check
        withdraw_proxy = [web3.to_checksum_address(coin) for coin in withdraw_proxy]
        if len(withdraw_proxy) % 20:
            withdraw_proxy += [ZERO_ADDRESS] * (20 - (len(withdraw_proxy) % 20))
        proxy = web3.eth.contract(PROXY, abi=[{"name":"withdraw_many","outputs":[],"inputs":[{"type":"address[20]","name":"_pools"}],"stateMutability":"nonpayable","type":"function","gas":93116},])
        for i in range(0, len(withdraw_proxy), 20):
            print("WITHDRAW PROXY", withdraw_proxy[i: i + 20])
            txs.append(proxy.functions.withdraw_many(withdraw_proxy[i: i + 20]).build_transaction({"from": wallet_address}))
            txs[-1]["nonce"] = nonce
            txs[-1]["gas"] = int(1.1 * txs[-1]["gas"])
            nonce += 1

    if burn:
        burn = [web3.to_checksum_address(coin) for coin in burn]
        if len(burn) % 20:
            burn += [ZERO_ADDRESS] * (20 - (len(burn) % 20))
        proxy = web3.eth.contract(PROXY, abi=[{"name":"burn_many","outputs":[],"inputs":[{"type":"address[20]","name":"_coins"}],"stateMutability":"nonpayable","type":"function","gas":780568},])
        for i in range(0, len(burn), 20):
            print("BURN PROXY", burn[i: i + 20])
            txs.append(proxy.functions.burn_many(burn[i: i + 20]).build_transaction({"from": wallet_address}))
            txs[-1]["nonce"] = nonce
            txs[-1]["gas"] = int(1.1 * txs[-1]["gas"])
            nonce += 1
    if withdraw_fc:
        withdraw_fc = [web3.to_checksum_address(coin) for coin in withdraw_fc]
        print("WITHDRAW FC", withdraw_fc)
        txs.append(fee_collector.functions.withdraw_many(withdraw_fc).build_transaction({"from": wallet_address}))
        txs[-1]["nonce"] = nonce
        txs[-1]["gas"] = int(1.1 * txs[-1]["gas"])
        nonce += 1

    collect = list(sorted(collect, key=lambda coin: int(coin, base=16)))
    collect = [web3.to_checksum_address(coin) for coin in collect]
    print("COLLECT", collect)
    txs.append(fee_collector.functions.collect(collect, RECEIVER).build_transaction({"from": wallet_address}))
    txs[-1]["nonce"] = nonce
    txs[-1]["gas"] = int(1.1 * txs[-1]["gas"])

    for tx in txs:
        signed_tx = web3.eth.account.sign_transaction(tx, private_key=wallet_pk)
        web3.eth.send_raw_transaction(signed_tx.rawTransaction)
    print("Go check ur wallet, I dit sth for ya ^&^")


def collect(withdraw_proxy, burn, withdraw_fc, pk_profit, collect, iters=None):
    if chain == "ethereum":
        collect_l1(withdraw_proxy, burn, withdraw_fc, pk_profit, collect, **({"iters": iters} if iters else {}))
    else:
        collect_l2(withdraw_proxy, burn, withdraw_fc, collect)


async def run():
    data_fetcher = DataFetcher()
    data_fetcher.fetch_prices()
    data_fetcher.fetch_sources()

    latest_block = web3.eth.get_block("latest")
    base_fee = latest_block["baseFeePerGas"]
    ts = latest_block["timestamp"] + 12
    while 4 * 24 * 3600 < (ts - 1600300800) % (7 * 24 * 3600) < 5 * 24 * 3600:
        if chain == "ethereum":
            safe_amount = 30 * (base_fee / (10 * 10 ** 9)) * (3500 / 3500)
        else:
            safe_amount = 1000
        fee = 0.02 * ((ts - 1600300800) % (24 * 3600)) / (24 * 3600)
        safe_threshold = max(int(safe_amount / fee) if chain == "ethereum" else 100, EXTREME_AMOUNT)
        print(f"Safe amount: {safe_threshold}")

        stable_pools, proxy_balances, pks, fc_balances = await data_fetcher.get_amounts()
        proxy_withdraw, to_burn, fc_withdraw, to_collect = [], set(), [], set()
        cnt, total = 0, 0
        for pool in stable_pools:
            try:
                if pool.get("amount", 0) >= safe_threshold * len(pool["coins"]):
                    cs = [coin for coin in pool["coins"] if coin not in data_fetcher.COINS_BLACKLIST]
                    if pool["address"].lower() in data_fetcher.PROXY_RECEIVER:
                        proxy_withdraw.append(pool["address"])
                        to_burn.update(cs)
                    else:
                        proxy_withdraw.append(pool["address"])
                    to_collect.update(cs)
                    cnt += 1 ; total += pool["amount"]
            except Exception as e:
                print(f"{pool} admin_balances {repr(e)}")
        for coin, amount in proxy_balances.items():
            try:
                if amount >= safe_threshold:
                    to_burn.add(coin)
                    to_collect.add(coin)
                    cnt += 1 ; total += amount
            except Exception as e:
                print(f"Proxy balances {repr(e)}")
        pk_profit = []
        for pk, pool, amount in pks:
            if amount >= safe_threshold:
                pk_profit.append(pk)
                to_collect.add(pool)
                cnt += 1 ; total += amount
        for coin, amount in fc_balances.items():
            try:
                if amount >= safe_threshold:
                    to_collect.add(coin)
                    cnt += 1 ; total += amount
            except Exception as e:
                print(f"Proxy balances {repr(e)}")

        if cnt > 0:
            print(f"Trying to profit {total * fee:.2f} crvUSD from {cnt} sources")
            collect(proxy_withdraw, list(to_burn), fc_withdraw, pk_profit, list(to_collect))

        latest_block = web3.eth.get_block("latest")
        base_fee = latest_block["baseFeePerGas"]
        ts = latest_block["timestamp"] + 12


if __name__ == "__main__":
    # collect(
    #     withdraw_proxy=[],
    #     burn=[],
    #     withdraw_fc=[],
    #     pk_profit=[],
    #     collect=[],
    #     iters=50,
    # )
    asyncio.run(run())
