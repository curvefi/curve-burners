"""Mainnet fork simulation of the Bebop JAM rail: the real JamSettlement fills
orders built by BebopJamAdapter.order_for against a DutchAuctionBurner behind
a FeeCollector, through settleInternal (a maker paying from its own balance)
and settle (a solver's interactions leaving the payment on the settlement).

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
from dotenv import load_dotenv
from eth_abi import encode
from eth_utils import keccak

from tests.burners.conftest import custom_err
from tests.burners.test_bebop_jam_module import (
    BALANCE_MANAGER,
    EMPTY_HOOKS_HASH,
    ORDER_ABI,
    SETTLEMENT,
    domain_separator_reference,
    order_digest_reference,
)
from tests.burners.test_dutch_auction_v2 import (
    FLOOR_TOTAL,
    START_TOTAL,
    STEP_DURATION,
    WAD,
    _move_to_epoch,
    _move_to_timestamp,
)
from tests.conftest import ZERO_ADDRESS, Epoch

MAX_UINT256 = 2**256 - 1
NONCE = 0x5AFE_C0DE

RPC_ENV_KEYS = ("ETH_RPC_URL", "MAINNET_RPC_URL", "FORK_MAINNET_URL")

ORDER_COMPONENTS = [
    {"name": "taker", "type": "address"},
    {"name": "receiver", "type": "address"},
    {"name": "expiry", "type": "uint256"},
    {"name": "exclusivityDeadline", "type": "uint256"},
    {"name": "nonce", "type": "uint256"},
    {"name": "executor", "type": "address"},
    {"name": "partnerInfo", "type": "uint256"},
    {"name": "sellTokens", "type": "address[]"},
    {"name": "buyTokens", "type": "address[]"},
    {"name": "sellAmounts", "type": "uint256[]"},
    {"name": "buyAmounts", "type": "uint256[]"},
    {"name": "usingPermit2", "type": "bool"},
]
INTERACTION_COMPONENTS = [
    {"name": "result", "type": "bool"},
    {"name": "to", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "data", "type": "bytes"},
]
SETTLEMENT_ABI = [
    {
        "type": "function",
        "name": "settle",
        "stateMutability": "payable",
        "inputs": [
            {"name": "order", "type": "tuple", "components": ORDER_COMPONENTS},
            {"name": "signature", "type": "bytes"},
            {"name": "interactions", "type": "tuple[]", "components": INTERACTION_COMPONENTS},
            {"name": "hooksData", "type": "bytes"},
            {"name": "balanceRecipient", "type": "address"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "settleInternal",
        "stateMutability": "payable",
        "inputs": [
            {"name": "order", "type": "tuple", "components": ORDER_COMPONENTS},
            {"name": "signature", "type": "bytes"},
            {"name": "filledAmounts", "type": "uint256[]"},
            {"name": "hooksData", "type": "bytes"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "hashJamOrder",
        "stateMutability": "view",
        "inputs": [
            {"name": "order", "type": "tuple", "components": ORDER_COMPONENTS},
            {"name": "hooksHash", "type": "bytes32"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "DOMAIN_SEPARATOR",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "balanceManager",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
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


def _fork(rpc_url: str) -> None:
    if hasattr(boa, "fork"):
        boa.fork(rpc_url, block_identifier="latest")
    else:
        boa.env.fork(rpc_url, block_identifier="latest")


def _abi_contract(abi: list[dict[str, Any]], name: str, address: str) -> Any:
    return boa.loads_abi(json.dumps(abi), name=name).at(address)


def _signed(order: Any) -> tuple:
    """The order as the adapter returns it, hooksHash as bytes."""
    order = tuple(order)
    return (*order[:7], *(list(field) for field in order[7:11]), bytes(order[11]))


def _calldata(order: tuple) -> tuple:
    """The order as the settlement ABI takes it: usingPermit2 in place of hooksHash."""
    return (*order[:11], False)


def _transfer_from_calldata(sender: str, receiver: str, amount: int) -> bytes:
    return keccak(text="transferFrom(address,address,uint256)")[:4] + encode(
        ["address", "address", "uint256"], [sender, receiver, amount]
    )


def test_mainnet_real_settlement_fills_published_orders():
    rpc_url = _rpc_url()
    if rpc_url is None:
        message = f"Mainnet fork RPC unavailable; set one of {', '.join(RPC_ENV_KEYS)}"
        if os.environ.get("REQUIRE_MAINNET_FORK"):
            pytest.fail(message)
        pytest.skip(message)

    fork_env = boa.Env()
    with boa.swap_env(fork_env):
        _fork(rpc_url)

        assert boa.env.get_code(SETTLEMENT), f"Missing deployed code at {SETTLEMENT}"
        settlement = _abi_contract(SETTLEMENT_ABI, "JamSettlement", SETTLEMENT)
        domain = domain_separator_reference(1, SETTLEMENT)
        assert bytes(settlement.DOMAIN_SEPARATOR()) == domain
        assert settlement.balanceManager() == BALANCE_MANAGER

        owner = boa.env.generate_address("owner")
        emergency_owner = boa.env.generate_address("emergency_owner")
        keeper = boa.env.generate_address("keeper")
        solver = boa.env.generate_address("solver")

        erc20 = boa.load_partial("contracts/testing/ERC20Mock.vy")
        target = erc20.deploy("Fork Target", "TARGET", 18)
        sell_token = erc20.deploy("Fork Sell Token", "SELL", 18)
        weth = boa.load("contracts/testing/WETH.vy")
        fee_collector = boa.load("contracts/FeeCollector.vy", target, weth, owner, emergency_owner)
        registry = boa.load("contracts/burners/adapters/AdapterRegistry.vy", fee_collector.address)
        # The deployment cross-checks its domain against the settlement's
        # DOMAIN_SEPARATOR and hashJamOrder.
        adapter = boa.load("contracts/burners/adapters/bebop/BebopJamAdapter.vy", SETTLEMENT)
        burner = boa.load(
            "contracts/burners/DutchAuctionBurner.vy",
            fee_collector,
            START_TOTAL,
            FLOOR_TOTAL,
            STEP_DURATION,
            registry,
        )
        with boa.env.prank(owner):
            fee_collector.set_burner(burner)
            fee_collector.set_killed([(ZERO_ADDRESS, 0)])
            # The balance manager is the executor: a fill pulls the sell
            # tokens through it.
            registry.set_adapter(adapter, adapter.balance_manager())
            registry.activate_adapter(adapter)
        assert adapter.settlement() == SETTLEMENT
        assert adapter.balance_manager() == BALANCE_MANAGER
        assert bytes(adapter.domain_separator()) == domain
        assert registry.is_executor_active(BALANCE_MANAGER)

        _move_to_epoch(fee_collector, Epoch.COLLECT)
        amount = 1_000 * WAD
        sell_token._mint_for_testing(fee_collector, amount)
        with boa.env.prank(keeper):
            fee_collector.collect([sell_token.address], keeper)
            burner.sync_executor_approvals(BALANCE_MANAGER, [sell_token.address])
        assert sell_token.allowance(burner, BALANCE_MANAGER) == MAX_UINT256
        lot_start, lot_end = burner.window(sell_token)
        initial_amount = burner.lots(sell_token).initial_amount
        assert initial_amount > 0
        _move_to_timestamp(lot_start + 3 * STEP_DURATION)

        # What a publisher hands to solvers, straight from the adapter: a
        # quarter of the lot at the live quote to the FeeCollector, and the
        # signature the settlement will forward to the burner.
        quarter = initial_amount // 4
        order, signature = adapter.order_for(burner.address, sell_token.address, NONCE, quarter)
        order = _signed(order)
        signature = bytes(signature)
        quote = burner.getAmountNeeded(sell_token, quarter)
        assert order == (
            burner.address,
            fee_collector.address,
            lot_end,
            0,
            NONCE,
            ZERO_ADDRESS,
            0,
            [sell_token.address],
            [target.address],
            [quarter],
            [quote],
            EMPTY_HOOKS_HASH,
        )
        assert signature == bytes.fromhex(adapter.address[2:]) + encode([ORDER_ABI], [order])
        digest = bytes(settlement.hashJamOrder(_calldata(order), EMPTY_HOOKS_HASH))
        assert digest == order_digest_reference(order, domain)
        with boa.env.prank(SETTLEMENT):
            assert burner.isValidSignature(digest, signature) == bytes.fromhex("1626ba7e")
            assert burner.isValidSignature(digest, signature[20:]) == bytes.fromhex("ffffffff")

        target._mint_for_testing(solver, 10 * quote)
        with boa.env.prank(solver):
            target.approve(BALANCE_MANAGER, MAX_UINT256)
            target.approve(SETTLEMENT, MAX_UINT256)

        # An underpriced order of the solver's own making fails inside the
        # real fill with the auction's typed reason: the settlement lets the
        # contract signature check's revert bubble.
        cheaper = (*order[:10], [quote - 1], order[11])
        cheaper_sig = bytes.fromhex(adapter.address[2:]) + encode([ORDER_ABI], [cheaper])
        with boa.env.prank(solver), boa.reverts(custom_err("BadBuyAmount()", nested=True)):
            settlement.settleInternal(_calldata(cheaper), cheaper_sig, [], b"")

        # settleInternal: the real settlement verifies the digest through
        # the burner's router, pulls the quarter from the burner through the
        # balance manager and moves the solver's payment to the FeeCollector.
        with boa.env.prank(solver):
            settlement.settleInternal(_calldata(order), signature, [], b"")
        assert sell_token.balanceOf(solver) == quarter
        assert target.balanceOf(fee_collector) == quote
        assert burner.available(sell_token) == initial_amount - quarter
        # Fill-or-kill: the nonce is burnt.
        with boa.env.prank(solver), boa.reverts(custom_err("InvalidNonce()", nested=True)):
            settlement.settleInternal(_calldata(order), signature, [], b"")

        # settle: a second order over the rest, paid by an interaction that
        # leaves the payment (with surplus) on the settlement; the receiver
        # gets the whole balance.
        rest = initial_amount - quarter
        order2, signature2 = adapter.order_for(burner.address, sell_token.address, NONCE + 1)
        order2 = _signed(order2)
        assert order2[9] == [rest]
        quote2 = order2[10][0]
        surplus = WAD
        interactions = [
            (True, target.address, 0, _transfer_from_calldata(solver, SETTLEMENT, quote2 + surplus))
        ]
        with boa.env.prank(solver):
            settlement.settle(_calldata(order2), bytes(signature2), interactions, b"", solver)
        assert sell_token.balanceOf(solver) == initial_amount
        assert target.balanceOf(fee_collector) == quote + quote2 + surplus
        assert burner.available(sell_token) == 0

        # The settlement checks the contract signature on every fill: once
        # the adapter is disabled, a fresh order over a restaged lot answers
        # 0xffffffff through the router and the settlement reports
        # InvalidContractSignature. The allowance sync then completes the
        # emergency procedure.
        sell_token._mint_for_testing(burner, rest)
        fresh, fresh_sig = adapter.order_for(burner.address, sell_token.address, NONCE + 2)
        with boa.env.prank(emergency_owner):
            registry.disable_adapter(adapter)
        with (
            boa.env.prank(solver),
            boa.reverts(custom_err("InvalidContractSignature()", nested=True)),
        ):
            settlement.settleInternal(_calldata(_signed(fresh)), bytes(fresh_sig), [], b"")
        with boa.env.prank(keeper):
            burner.sync_executor_approvals(BALANCE_MANAGER, [sell_token.address])
        assert sell_token.allowance(burner, BALANCE_MANAGER) == 0
        assert sell_token.balanceOf(burner) == rest
