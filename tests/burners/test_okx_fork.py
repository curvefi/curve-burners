"""Mainnet fork simulation of the OKX rail: the real OKX Intent Swap
Settlement (legacy schema) fills an order built by OKXIntentAdapter.order_for
against a DutchAuctionBurner behind a FeeCollector, settled by an allowlisted
solver whose interactions take the sold tokens and leave the payment on the
settlement.

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
from tests.burners.test_dutch_auction_v2 import (
    FLOOR_TOTAL,
    START_TOTAL,
    STEP_DURATION,
    WAD,
    _move_to_epoch,
    _move_to_timestamp,
)
from tests.burners.test_okx_module import (
    EIP1271_TRADE_FLAGS,
    EXACT_IN,
    LEGACY_SETTLEMENT,
    ORDER_ABI,
    THRESHOLD_SETTLEMENT,
    TOKEN_APPROVE,
    TOKEN_APPROVE_PROXY,
    as_tuple,
    domain_separator_reference,
    order_digest_reference,
)
from tests.conftest import ZERO_ADDRESS, Epoch

MAX_UINT256 = 2**256 - 1
# An allowlisted solver of the legacy settlement (isSolver == true at the
# time of writing, the sender of recent settle transactions).
SOLVER = "0x0270ce9d9c8ae0bfb6333d216efed90042b877d6"

RPC_ENV_KEYS = ("ETH_RPC_URL", "MAINNET_RPC_URL", "FORK_MAINNET_URL")

COMMISSION_COMPONENTS = [
    {"name": "feePercent", "type": "uint256"},
    {"name": "referrerWalletAddress", "type": "address"},
    {"name": "flag", "type": "uint256"},
]
TRADE_COMPONENTS = [
    {"name": "fromTokenAddressIndex", "type": "uint256"},
    {"name": "toTokenAddressIndex", "type": "uint256"},
    {"name": "owner", "type": "address"},
    {"name": "receiver", "type": "address"},
    {"name": "fromTokenAmount", "type": "uint256"},
    {"name": "toTokenAmount", "type": "uint256"},
    {"name": "validTo", "type": "uint32"},
    {"name": "appData", "type": "bytes32"},
    {"name": "flags", "type": "uint256"},
    {"name": "executedAmount", "type": "uint256"},
    {"name": "commissionInfos", "type": "tuple[]", "components": COMMISSION_COMPONENTS},
    {"name": "signature", "type": "bytes"},
    {
        "name": "solverFeeInfo",
        "type": "tuple",
        "components": [
            {"name": "feePercent", "type": "uint256"},
            {"name": "solverAddr", "type": "address"},
            {"name": "flag", "type": "uint256"},
        ],
    },
]
INTERACTION_COMPONENTS = [
    {"name": "target", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "callData", "type": "bytes"},
]
SETTLEMENT_ABI = [
    {
        "type": "function",
        "name": "settle",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "settleId", "type": "uint256"},
            {"name": "tokens", "type": "address[]"},
            {"name": "clearingPrices", "type": "uint256[]"},
            {"name": "trades", "type": "tuple[]", "components": TRADE_COMPONENTS},
            {"name": "interactions", "type": "tuple[][3]", "components": INTERACTION_COMPONENTS},
            {
                "name": "surplusFeeInfo",
                "type": "tuple",
                "components": [
                    {"name": "feePercent", "type": "uint256"},
                    {"name": "trimReceiver", "type": "address"},
                    {"name": "flag", "type": "uint256"},
                ],
            },
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "domainSeparator",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "tokenApproveProxy",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "name": "isSolver",
        "stateMutability": "view",
        "inputs": [{"name": "prospectiveSolver", "type": "address"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "filledAmount",
        "stateMutability": "view",
        "inputs": [{"name": "orderUid", "type": "bytes"}],
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


def _fork(rpc_url: str) -> None:
    if hasattr(boa, "fork"):
        boa.fork(rpc_url, block_identifier="latest")
    else:
        boa.env.fork(rpc_url, block_identifier="latest")


def _abi_contract(abi: list[dict[str, Any]], name: str, address: str) -> Any:
    return boa.loads_abi(json.dumps(abi), name=name).at(address)


def _trade(order: tuple, signature: bytes) -> tuple:
    """The legacy TradeLib.Data: tokens [from, to], the EIP-1271 scheme, no
    solver fee."""
    return (
        0,
        1,
        order[2],
        order[3],
        order[4],
        order[5],
        order[6],
        order[7],
        EIP1271_TRADE_FLAGS,
        order[4],
        order[10],
        signature,
        (0, ZERO_ADDRESS, 0),
    )


def _transfer_calldata(receiver: str, amount: int) -> bytes:
    return keccak(text="transfer(address,uint256)")[:4] + encode(
        ["address", "uint256"], [receiver, amount]
    )


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

        for address in (
            LEGACY_SETTLEMENT,
            THRESHOLD_SETTLEMENT,
            TOKEN_APPROVE_PROXY,
            TOKEN_APPROVE,
        ):
            assert boa.env.get_code(address), f"Missing deployed code at {address}"
        settlement = _abi_contract(SETTLEMENT_ABI, "Settlement", LEGACY_SETTLEMENT)
        domain = domain_separator_reference(1, LEGACY_SETTLEMENT)
        assert bytes(settlement.domainSeparator()) == domain
        assert settlement.tokenApproveProxy() == TOKEN_APPROVE_PROXY
        if not settlement.isSolver(SOLVER):
            pytest.skip(f"{SOLVER} is no longer an allowlisted solver; pick a recent settle sender")

        owner = boa.env.generate_address("owner")
        emergency_owner = boa.env.generate_address("emergency_owner")
        keeper = boa.env.generate_address("keeper")

        erc20 = boa.load_partial("contracts/testing/ERC20Mock.vy")
        target = erc20.deploy("Fork Target", "TARGET", 18)
        sell_token = erc20.deploy("Fork Sell Token", "SELL", 18)
        weth = boa.load("contracts/testing/WETH.vy")
        fee_collector = boa.load("contracts/FeeCollector.vy", target, weth, owner, emergency_owner)
        registry = boa.load("contracts/burners/adapters/AdapterRegistry.vy", fee_collector.address)
        # The deployment cross-checks its domain against the settlement's
        # domainSeparator and follows the proxy to the approval target; the
        # threshold settlement pins the same way (another domain, the same
        # executor).
        adapter = boa.load("contracts/burners/adapters/okx/OKXIntentAdapter.vy", LEGACY_SETTLEMENT)
        threshold_adapter = boa.load(
            "contracts/burners/adapters/okx/OKXIntentAdapter.vy", THRESHOLD_SETTLEMENT
        )
        assert threshold_adapter.token_approve() == adapter.token_approve() == TOKEN_APPROVE
        assert threshold_adapter.domain_separator() != adapter.domain_separator()
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
            # TokenApprove is the executor: a fill pulls the sold tokens
            # through it.
            registry.set_adapter(adapter, adapter.token_approve())
            registry.activate_adapter(adapter)
        assert adapter.settlement() == LEGACY_SETTLEMENT
        assert adapter.token_approve_proxy() == TOKEN_APPROVE_PROXY
        assert bytes(adapter.domain_separator()) == domain
        assert registry.is_executor_active(TOKEN_APPROVE)

        _move_to_epoch(fee_collector, Epoch.COLLECT)
        amount = 1_000 * WAD
        sell_token._mint_for_testing(fee_collector, amount)
        with boa.env.prank(keeper):
            fee_collector.collect([sell_token.address], keeper)
            burner.sync_executor_approvals(TOKEN_APPROVE, [sell_token.address])
        assert sell_token.allowance(burner, TOKEN_APPROVE) == MAX_UINT256
        lot_start, lot_end = burner.window(sell_token)
        initial_amount = burner.lots(sell_token).initial_amount
        assert initial_amount > 0
        _move_to_timestamp(lot_start + 3 * STEP_DURATION)

        # What a publisher hands to a solver, straight from the adapter: a
        # quarter of the lot at the live quote to the FeeCollector, and the
        # signature the settlement will forward to the burner.
        quarter = initial_amount // 4
        order, signature = adapter.order_for(burner.address, sell_token.address, quarter)
        order = as_tuple(order)
        signature = bytes(signature)
        quote = burner.getAmountNeeded(sell_token, quarter)
        assert order == (
            sell_token.address,
            target.address,
            burner.address,
            fee_collector.address,
            quarter,
            quote,
            lot_end,
            bytes(32),
            EXACT_IN,
            False,
            [],
        )
        assert signature == bytes.fromhex(adapter.address[2:]) + encode([ORDER_ABI], [order])
        digest = order_digest_reference(order, domain)
        _, described_digest, uid = adapter.describe(signature[20:])
        assert bytes(described_digest) == digest
        with boa.env.prank(LEGACY_SETTLEMENT):
            assert burner.isValidSignature(digest, signature) == bytes.fromhex("1626ba7e")
            assert burner.isValidSignature(digest, signature[20:]) == bytes.fromhex("ffffffff")

        target._mint_for_testing(SOLVER, 10 * quote)
        with boa.env.prank(SOLVER):
            target.approve(LEGACY_SETTLEMENT, MAX_UINT256)
        tokens = [sell_token.address, target.address]
        # Clearing prices at the signed limit: the gross proceeds are exactly
        # the quote. The solver's mid-settlement interactions take the pulled
        # tokens and leave the payment on the settlement, which pays the
        # receiver; nothing is left for the surplus distribution.
        prices = [quote, quarter]
        owner_signature = bytes.fromhex(burner.address[2:]) + signature
        interactions = [
            [],
            [
                (sell_token.address, 0, _transfer_calldata(SOLVER, quarter)),
                (target.address, 0, _transfer_from_calldata(SOLVER, LEGACY_SETTLEMENT, quote)),
            ],
            [],
        ]
        surplus = (0, ZERO_ADDRESS, 0)

        # An underpriced order of the solver's own making fails inside the
        # real fill with the auction's typed reason: the settlement lets the
        # contract signature check's revert bubble.
        cheaper = (*order[:5], quote - 1, *order[6:])
        cheaper_signature = (
            bytes.fromhex(burner.address[2:])
            + bytes.fromhex(adapter.address[2:])
            + encode([ORDER_ABI], [cheaper])
        )
        with boa.env.prank(SOLVER), boa.reverts(custom_err("BadBuyAmount()", nested=True)):
            settlement.settle(
                1, tokens, prices, [_trade(cheaper, cheaper_signature)], interactions, surplus
            )

        # The real settlement verifies the digest through the burner's
        # router, pulls the quarter from the burner through TokenApprove and
        # pays the FeeCollector from the solver's payment.
        with boa.env.prank(SOLVER):
            settlement.settle(
                1, tokens, prices, [_trade(order, owner_signature)], interactions, surplus
            )
        assert sell_token.balanceOf(SOLVER) == quarter
        assert target.balanceOf(fee_collector) == quote
        assert burner.available(sell_token) == initial_amount - quarter
        assert settlement.filledAmount(bytes(uid)) == quarter
        # Fill-or-kill: the uid is filled.
        with boa.env.prank(SOLVER), boa.reverts("order filled"):
            settlement.settle(
                1, tokens, prices, [_trade(order, owner_signature)], interactions, surplus
            )

        # The settlement checks the contract signature on every fill: once
        # the adapter is disabled, a fresh order answers 0xffffffff through
        # the router and the settlement reports its own invalid-signature
        # error. The allowance sync then completes the emergency procedure.
        fresh, fresh_signature = adapter.order_for(burner.address, sell_token.address)
        fresh = as_tuple(fresh)
        with boa.env.prank(emergency_owner):
            registry.disable_adapter(adapter)
        with boa.env.prank(SOLVER), boa.reverts("invalid eip1271 signature"):
            settlement.settle(
                2,
                tokens,
                [fresh[5], fresh[4]],
                [_trade(fresh, bytes.fromhex(burner.address[2:]) + bytes(fresh_signature))],
                [
                    [],
                    [
                        (sell_token.address, 0, _transfer_calldata(SOLVER, fresh[4])),
                        (
                            target.address,
                            0,
                            _transfer_from_calldata(SOLVER, LEGACY_SETTLEMENT, fresh[5]),
                        ),
                    ],
                    [],
                ],
                surplus,
            )
        with boa.env.prank(keeper):
            burner.sync_executor_approvals(TOKEN_APPROVE, [sell_token.address])
        assert sell_token.allowance(burner, TOKEN_APPROVE) == 0
        assert sell_token.balanceOf(burner) == initial_amount - quarter
