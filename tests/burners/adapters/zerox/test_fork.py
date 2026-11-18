"""Mainnet fork simulation of the 0x rail: the real Permit2 and the live
SettlerMetaTxn / SettlerIntent (read from the 0x deployer registry) settle
orders built by ZeroExSettlerAdapter.order_for against a DutchAuctionBurner
behind a FeeCollector, selling real USDC for real crvUSD through a Curve
pool with a self-authored action list (METATXN_TRANSFER_FROM + BASIC). A
real Gasless fill is also re-derived: its Permit2 digest is recomputed by
the adapter from the transaction's calldata and recovers the taker.

Skips without a mainnet RPC (ETH_RPC_URL / MAINNET_RPC_URL, or an
ALCHEMY_API_TOKEN / DRPC_API_KEY to build one); set REQUIRE_MAINNET_FORK=1 to
turn the skip into a hard failure.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import boa
import pytest
import requests
from dotenv import load_dotenv
from eth_abi import decode, encode
from eth_keys import keys
from eth_utils import to_checksum_address

from tests.burners.adapters.zerox.test_module import (
    ORDER_ABI,
    hash_actions_reference,
    permit_digest_reference,
)
from tests.burners.conftest import custom_err
from tests.burners.test_dutch_auction_v2 import (
    FLOOR_TOTAL,
    START_TOTAL,
    STEP_DURATION,
    _move_to_epoch,
)
from tests.conftest import ZERO_ADDRESS, Epoch

# Canonical deployments. The Settler implementations are read from the 0x
# deployer registry (ownerOf(feature): 3 = SettlerMetaTxn, 4 =
# SettlerIntent); the ones below are what it answered on 2026-11-02.
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
ZEROX_DEPLOYER = "0x00000000000004533Fe15556B1E086BB1A72cEae"
EXPECTED_SETTLER_METATXN = "0x9a6350d532ed83a970d452b736be5847b2856768"
EXPECTED_SETTLER_INTENT = "0xbfa23e39ebcb074696414fdd75ed2c9effbde688"
FEATURE_METATXN = 3
FEATURE_INTENT = 4

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"
# Curve crvUSD/USDC stableswap-ng pool: coin 0 USDC, coin 1 crvUSD.
CRVUSD_USDC_POOL = "0x4DEcE678ceceb27446b35C672dC7d61F30bAD69E"

# A real Gasless API fill (block 26328493): executeMetaTxn on the previous
# SettlerMetaTxn by relayer 0x2afaede2… for taker 0xdd811c1d… (an EOA, so
# the 65-byte sig recovers to it under the adapter's digest).
REAL_METATXN_TX = "0xa612cd6a2e5f5a17316c16608273966047ac1bacb0147e9229426f8320dc4202"
REAL_METATXN_SETTLER = "0xecc95d4bf74782c60009d5f645adc75828a8f550"
REAL_METATXN_TAKER = "0xdd811C1d2Af8eD30A14c42297d3203F64F2c1d56"

MAX_UINT256 = 2**256 - 1
NONCE = 0x5AFE_C0DE
LOT_AMOUNT = 1_000 * 10**6
BASIS = 1_000_000

RPC_ENV_KEYS = ("ETH_RPC_URL", "MAINNET_RPC_URL", "FORK_MAINNET_URL")

METATXN_TRANSFER_FROM = bytes.fromhex("0dfeb419")
BASIC = bytes.fromhex("38c9c147")
EXCHANGE = bytes.fromhex("3df02124")  # exchange(int128,int128,uint256,uint256)

SETTLER_ABI = [
    {
        "type": "function",
        "name": "executeMetaTxn",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "slippage",
                "type": "tuple",
                "components": [
                    {"name": "recipient", "type": "address"},
                    {"name": "buyToken", "type": "address"},
                    {"name": "minAmountOut", "type": "uint256"},
                ],
            },
            {"name": "actions", "type": "bytes[]"},
            {"name": "zid", "type": "bytes32"},
            {"name": "msgSender", "type": "address"},
            {"name": "sig", "type": "bytes"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "getSolvers",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address[]"}],
    },
]
DEPLOYER_ABI = [
    {
        "type": "function",
        "name": "prev",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "uint128"}],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "name": "ownerOf",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "uint256"}],
        "outputs": [{"name": "", "type": "address"}],
    },
]
PERMIT2_ABI = [
    {
        "type": "function",
        "name": "DOMAIN_SEPARATOR",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "nonceBitmap",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "address"}, {"name": "", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]
POOL_ABI = [
    {
        "type": "function",
        "name": "coins",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "uint256"}],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "name": "get_dy",
        "stateMutability": "view",
        "inputs": [
            {"name": "i", "type": "int128"},
            {"name": "j", "type": "int128"},
            {"name": "dx", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


def _rpc_url() -> str | None:
    load_dotenv(Path.home() / ".env", override=False)
    url = next((os.environ[key] for key in RPC_ENV_KEYS if os.environ.get(key)), None)
    if url:
        return url
    if os.environ.get("ALCHEMY_API_TOKEN"):
        return f"https://eth-mainnet.g.alchemy.com/v2/{os.environ['ALCHEMY_API_TOKEN']}"
    if os.environ.get("DRPC_API_KEY"):
        return f"https://lb.drpc.org/ogrpc?network=ethereum&dkey={os.environ['DRPC_API_KEY']}"
    return None


def _require_rpc() -> str:
    rpc_url = _rpc_url()
    if rpc_url is None:
        message = f"Mainnet fork RPC unavailable; set one of {', '.join(RPC_ENV_KEYS)}"
        if os.environ.get("REQUIRE_MAINNET_FORK"):
            pytest.fail(message)
        pytest.skip(message)
    return rpc_url


def _fork(rpc_url: str) -> None:
    if hasattr(boa, "fork"):
        boa.fork(rpc_url)
    else:
        boa.env.fork(rpc_url, block_identifier="latest")


def _rpc(rpc_url: str, method: str, params: list) -> Any:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = requests.post(rpc_url, json=body, timeout=60)
    response.raise_for_status()
    return response.json()["result"]


def _abi_contract(abi: list[dict[str, Any]], name: str, address: str) -> Any:
    return boa.loads_abi(json.dumps(abi), name=name).at(address)


def _to_tuple(order: Any) -> tuple:
    permit, slippage, actions_hash = order
    return (tuple(permit), tuple(slippage), bytes(actions_hash))


def _timestamp() -> int:
    return boa.env.evm.vm.state.timestamp


def _transfer_from_action(settler: str, permit: tuple) -> bytes:
    """METATXN_TRANSFER_FROM(recipient = the Settler, permit)."""
    return METATXN_TRANSFER_FROM + encode(
        ["address", "((address,uint256),uint256,uint256)"],
        [settler, ((permit[0], permit[1]), permit[2], permit[3])],
    )


def _curve_basic_action() -> bytes:
    """BASIC: sell the Settler's whole USDC balance to the Curve pool.

    The Settler patches the sell amount at `offset` of the call data (the
    third word, dx) and approves the pool before the call; min_dy is 0
    because the Settler's own slippage check is the floor.
    """
    data = EXCHANGE + encode(["int128", "int128", "uint256", "uint256"], [0, 1, 0, 0])
    return BASIC + encode(
        ["address", "uint256", "address", "uint256", "bytes"],
        [USDC, BASIS, CRVUSD_USDC_POOL, 4 + 2 * 32, data],
    )


class Deployment:
    """A FeeCollector + DutchAuctionBurner on the fork with one adapter."""

    def __init__(self, settler: str, intent: bool) -> None:
        self.owner = boa.env.generate_address("owner")
        self.emergency_owner = boa.env.generate_address("emergency_owner")
        self.keeper = boa.env.generate_address("keeper")
        erc20 = boa.load_partial("contracts/testing/ERC20Mock.vy")
        self.usdc = erc20.at(USDC)
        self.crvusd = erc20.at(CRVUSD)
        weth = boa.load("contracts/testing/WETH.vy")
        self.fee_collector = boa.load(
            "contracts/FeeCollector.vy", CRVUSD, weth, self.owner, self.emergency_owner
        )
        self.registry = boa.load(
            "contracts/burners/adapters/AdapterRegistry.vy", self.fee_collector.address
        )
        self.adapter = boa.load(
            "contracts/burners/adapters/zerox/ZeroExSettlerAdapter.vy",
            settler,
            ZEROX_DEPLOYER,
            PERMIT2,
            intent,
        )
        self.burner = boa.load(
            "contracts/burners/DutchAuctionBurner.vy",
            self.fee_collector,
            START_TOTAL,
            FLOOR_TOTAL,
            STEP_DURATION,
            self.registry,
        )
        with boa.env.prank(self.owner):
            self.fee_collector.set_burner(self.burner)
            self.fee_collector.set_killed([(ZERO_ADDRESS, 0)])
            # Permit2 is the executor: the Settler pulls the lot through it.
            self.registry.set_adapter(self.adapter, PERMIT2)
            self.registry.activate_adapter(self.adapter)
        assert self.registry.is_executor_active(PERMIT2)

    def stage_lot(self) -> tuple[int, int, int]:
        """Collect LOT_AMOUNT of USDC into the burner; returns (start, end, initial_amount)."""
        _move_to_epoch(self.fee_collector, Epoch.COLLECT)
        boa.deal(self.usdc, self.fee_collector.address, LOT_AMOUNT)
        with boa.env.prank(self.keeper):
            self.fee_collector.collect([USDC], self.keeper)
            self.burner.sync_executor_approvals(PERMIT2, [USDC])
        assert self.usdc.allowance(self.burner, PERMIT2) == MAX_UINT256
        lot_start, lot_end = self.burner.window(USDC)
        initial_amount = self.burner.lots(USDC).initial_amount
        assert initial_amount > 0
        return lot_start, lot_end, initial_amount

    def move_below_market(self, amount: int, market_out: int) -> int:
        """Travel to the first step where the live quote is at most market_out."""
        lot_start, lot_end = self.burner.window(USDC)
        # Quotes at a given timestamp are only meaningful once the lot is
        # live (an inactive lot quotes 0 whatever the timestamp asked).
        if _timestamp() <= lot_start:
            boa.env.time_travel(seconds=lot_start + 1 - _timestamp())
        low, high = _timestamp() + 1, lot_end - 1
        assert self.burner.getAmountNeeded(USDC, amount, high) <= market_out
        while low < high:
            mid = (low + high) // 2
            if self.burner.getAmountNeeded(USDC, amount, mid) <= market_out:
                high = mid
            else:
                low = mid + 1
        boa.env.time_travel(seconds=low - _timestamp())
        quote = self.burner.getAmountNeeded(USDC, amount)
        assert quote <= market_out
        return quote


def _settle_through_live_settler(settler_address: str, intent: bool, submitter: str) -> None:
    deployment = Deployment(settler_address, intent)
    settler = _abi_contract(SETTLER_ABI, "Settler", settler_address)
    permit2 = _abi_contract(PERMIT2_ABI, "Permit2", PERMIT2)
    pool = _abi_contract(POOL_ABI, "CurvePool", CRVUSD_USDC_POOL)
    assert (pool.coins(0), pool.coins(1)) == (USDC, CRVUSD)
    assert bytes(deployment.adapter.domain_separator()) == bytes(permit2.DOMAIN_SEPARATOR())
    # The adapter pinned the real registry and the flavor's feature; the
    # live Settler it validates for is that feature's current instance.
    assert deployment.adapter.deployer() == ZEROX_DEPLOYER
    assert deployment.adapter.feature() == (FEATURE_INTENT if intent else FEATURE_METATXN)

    _lot_start, lot_end, initial_amount = deployment.stage_lot()
    market_out = pool.get_dy(0, 1, initial_amount)
    # The Settler pays the recipient its whole buy-token balance minus one
    # wei, so pricing at the pool output itself would be one wei short.
    quote = deployment.move_below_market(initial_amount, market_out - 1)
    burner, fee_collector, adapter = deployment.burner, deployment.fee_collector, deployment.adapter

    # The action list a publisher would author for its own route: pull the
    # lot into the Settler, swap it on Curve; the Settler's slippage check
    # then pays the FeeCollector. For a SettlerMetaTxn it is signed through
    # its hash; for a SettlerIntent the solver picks it.
    permit = (USDC, initial_amount, NONCE, lot_end)
    actions = [_transfer_from_action(settler_address, permit), _curve_basic_action()]
    actions_hash = bytes(32) if intent else adapter.hash_actions(actions)
    if not intent:
        assert bytes(actions_hash) == hash_actions_reference(actions)
    order, signature = adapter.order_for(burner.address, USDC, NONCE, 0, 0, bytes(actions_hash))
    order, signature = _to_tuple(order), bytes(signature)
    assert order == (permit, (fee_collector.address, CRVUSD, quote), bytes(actions_hash))
    assert signature == bytes.fromhex(adapter.address[2:]) + encode([ORDER_ABI], [order])
    slippage = order[1]

    digest = permit_digest_reference(
        order, settler_address, bytes(permit2.DOMAIN_SEPARATOR()), intent
    )
    _, described_digest = adapter.describe(signature[20:])
    assert bytes(described_digest) == digest
    with boa.env.prank(PERMIT2):
        assert burner.isValidSignature(digest, signature) == bytes.fromhex("1626ba7e")
        assert burner.isValidSignature(digest, signature[20:]) == bytes.fromhex("ffffffff")

    # An underpriced order of the submitter's own making fails inside the
    # real settlement path with the auction's typed error, bubbled by
    # Permit2 and the Settler.
    cheaper = (permit, (fee_collector.address, CRVUSD, quote - 1), bytes(actions_hash))
    cheaper_sig = bytes.fromhex(adapter.address[2:]) + encode([ORDER_ABI], [cheaper])
    with boa.env.prank(submitter):
        with boa.reverts(custom_err("BadBuyAmount()", nested=True)):
            settler.executeMetaTxn(cheaper[1], actions, bytes(32), burner.address, cheaper_sig)

    # A disabled adapter answers 0xffffffff through the router, which
    # Permit2 reports as InvalidContractSignature.
    with boa.env.prank(deployment.emergency_owner):
        deployment.registry.disable_adapter(adapter)
    with boa.env.prank(submitter):
        with boa.reverts(custom_err("InvalidContractSignature()", nested=True)):
            settler.executeMetaTxn(slippage, actions, bytes(32), burner.address, signature)
    with boa.env.prank(deployment.owner):
        deployment.registry.activate_adapter(adapter)

    # The real Settler + Permit2 settle the published order: Permit2
    # verifies the digest through the burner's router and pulls the lot
    # into the Settler, the BASIC action swaps it on Curve, and the
    # Settler's slippage check pays the FeeCollector at least minAmountOut.
    fee_collector_before = deployment.crvusd.balanceOf(fee_collector)
    with boa.env.prank(submitter):
        settler.executeMetaTxn(slippage, actions, bytes(32), burner.address, signature)
    paid = deployment.crvusd.balanceOf(fee_collector) - fee_collector_before
    assert quote <= paid <= market_out
    assert deployment.usdc.balanceOf(burner) == 0
    # The Settler spends its balance minus one wei (fastSpendableBalance).
    assert deployment.usdc.balanceOf(settler_address) <= 1
    assert burner.available(USDC) == 0
    # Permit2 burnt the nonce: the order is one-shot even if the lot were refilled.
    assert permit2.nonceBitmap(burner.address, NONCE >> 8) & (1 << (NONCE & 0xFF))
    with boa.env.prank(submitter), boa.reverts():
        settler.executeMetaTxn(slippage, actions, bytes(32), burner.address, signature)


def test_mainnet_live_settler_metatxn_settles_a_published_order():
    rpc_url = _require_rpc()
    fork_env = boa.Env()
    with boa.swap_env(fork_env):
        _fork(rpc_url)
        deployer = _abi_contract(DEPLOYER_ABI, "ZeroExDeployer", ZEROX_DEPLOYER)
        settler_address = deployer.ownerOf(FEATURE_METATXN)
        assert settler_address.lower() == EXPECTED_SETTLER_METATXN
        assert boa.env.get_code(PERMIT2) and boa.env.get_code(settler_address)
        # Relaying is permissionless: any account other than the taker.
        relayer = boa.env.generate_address("relayer")
        _settle_through_live_settler(settler_address, intent=False, submitter=relayer)


def test_mainnet_live_settler_intent_settles_a_published_order():
    rpc_url = _require_rpc()
    fork_env = boa.Env()
    with boa.swap_env(fork_env):
        _fork(rpc_url)
        deployer = _abi_contract(DEPLOYER_ABI, "ZeroExDeployer", ZEROX_DEPLOYER)
        settler_address = deployer.ownerOf(FEATURE_INTENT)
        assert settler_address.lower() == EXPECTED_SETTLER_INTENT
        settler = _abi_contract(SETTLER_ABI, "SettlerIntent", settler_address)
        # Submission is onlySolver: an allow-listed solver routes and relays.
        solvers = settler.getSolvers()
        assert solvers
        outsider = boa.env.generate_address("outsider")
        deployment = Deployment(settler_address, intent=True)
        with boa.env.prank(outsider):
            with boa.reverts(custom_err("PermissionDenied()", nested=True)):
                settler.executeMetaTxn(
                    (deployment.fee_collector.address, CRVUSD, 1), [], bytes(32), outsider, b""
                )
        _settle_through_live_settler(settler_address, intent=True, submitter=solvers[0])


def test_mainnet_real_gasless_fill_digest_recovers_the_taker():
    rpc_url = _require_rpc()
    tx = _rpc(rpc_url, "eth_getTransactionByHash", [REAL_METATXN_TX])
    assert tx["to"].lower() == REAL_METATXN_SETTLER
    slippage, actions, _zid, taker, sig = decode(
        ["(address,address,uint256)", "bytes[]", "bytes32", "address", "bytes"],
        bytes.fromhex(tx["input"][10:]),
    )
    assert to_checksum_address(taker) == REAL_METATXN_TAKER
    assert actions[0][:4] == METATXN_TRANSFER_FROM
    _recipient, permit = decode(["address", "((address,uint256),uint256,uint256)"], actions[0][4:])

    fork_env = boa.Env()
    with boa.swap_env(fork_env):
        _fork(rpc_url)
        # The adapter only deploys for a Settler the registry still lists
        # (ownerOf(3) or prev(3)); once 0x has rotated past this one, the
        # real fill to re-derive has to be refreshed too.
        deployer = _abi_contract(DEPLOYER_ABI, "ZeroExDeployer", ZEROX_DEPLOYER)
        listed = {deployer.ownerOf(FEATURE_METATXN).lower(), deployer.prev(FEATURE_METATXN).lower()}
        if REAL_METATXN_SETTLER not in listed:
            pytest.skip(f"0x retired SettlerMetaTxn {REAL_METATXN_SETTLER}; pick a newer real fill")
        adapter = boa.load(
            "contracts/burners/adapters/zerox/ZeroExSettlerAdapter.vy",
            to_checksum_address(REAL_METATXN_SETTLER),
            ZEROX_DEPLOYER,
            PERMIT2,
            False,
        )
        actions_hash = bytes(adapter.hash_actions(list(actions)))
        assert actions_hash == hash_actions_reference(list(actions))
        order = ((permit[0][0], permit[0][1], permit[1], permit[2]), tuple(slippage), actions_hash)
        _, digest = adapter.describe(encode([ORDER_ABI], [order]))
        digest = bytes(digest)
        assert digest == permit_digest_reference(
            order,
            to_checksum_address(REAL_METATXN_SETTLER),
            bytes(adapter.domain_separator()),
            False,
        )
    # The taker is an EOA: its 65-byte signature over this digest recovers it.
    assert len(sig) == 65
    v = sig[64] - 27 if sig[64] >= 27 else sig[64]
    recovered = keys.Signature(
        vrs=(v, int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:64], "big"))
    ).recover_public_key_from_msg_hash(digest)
    assert recovered.to_checksum_address() == REAL_METATXN_TAKER
