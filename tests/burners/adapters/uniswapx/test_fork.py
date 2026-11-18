"""Mainnet fork simulation of the UniswapX rail: the real Permit2 and the real
ExclusiveDutchOrderReactor settle an order built by UniswapXAdapter.order_for
against a DutchAuctionBurner behind a FeeCollector.

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

from tests.burners.adapters.uniswapx.test_module import ORDER_ABI, permit_digest_reference
from tests.burners.conftest import custom_err
from tests.burners.test_dutch_auction_v2 import (
    FLOOR_TOTAL,
    START_TOTAL,
    STEP_DURATION,
    WAD,
    _move_to_epoch,
    _move_to_timestamp,
)
from tests.conftest import ZERO_ADDRESS, Epoch

# Canonical deployments (UniswapX SDK REACTOR_ADDRESS_MAPPING[1].Dutch and
# PERMIT2_MAPPING[1]).
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
EXCLUSIVE_DUTCH_ORDER_REACTOR = "0x6000da47483062A0D734Ba3dc7576Ce6A0B645C4"
MAX_UINT256 = 2**256 - 1
NONCE = 0x5AFE_C0DE

RPC_ENV_KEYS = ("ETH_RPC_URL", "MAINNET_RPC_URL", "FORK_MAINNET_URL")

REACTOR_ABI = [
    {
        "type": "function",
        "name": "execute",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "order",
                "type": "tuple",
                "components": [
                    {"name": "order", "type": "bytes"},
                    {"name": "sig", "type": "bytes"},
                ],
            }
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "permit2",
        "stateMutability": "view",
        "inputs": [],
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
        boa.fork(rpc_url)
    else:
        boa.env.fork(rpc_url, block_identifier="latest")


def _abi_contract(abi: list[dict[str, Any]], name: str, address: str) -> Any:
    return boa.loads_abi(json.dumps(abi), name=name).at(address)


def _to_tuple(order: Any) -> tuple:
    info, decay_start, decay_end, exclusive_filler, override_bps, input_, outputs = order
    return (
        tuple(info),
        decay_start,
        decay_end,
        exclusive_filler,
        override_bps,
        tuple(input_),
        [tuple(o) for o in outputs],
    )


def test_mainnet_real_reactor_settles_a_published_order():
    rpc_url = _rpc_url()
    if rpc_url is None:
        message = f"Mainnet fork RPC unavailable; set one of {', '.join(RPC_ENV_KEYS)}"
        if os.environ.get("REQUIRE_MAINNET_FORK"):
            pytest.fail(message)
        pytest.skip(message)

    fork_env = boa.Env()
    with boa.swap_env(fork_env):
        _fork(rpc_url)

        for address in (PERMIT2, EXCLUSIVE_DUTCH_ORDER_REACTOR):
            assert boa.env.get_code(address), f"Missing deployed code at {address}"
        reactor = _abi_contract(
            REACTOR_ABI, "ExclusiveDutchOrderReactor", EXCLUSIVE_DUTCH_ORDER_REACTOR
        )
        permit2 = _abi_contract(PERMIT2_ABI, "Permit2", PERMIT2)
        assert reactor.permit2() == PERMIT2

        owner = boa.env.generate_address("owner")
        emergency_owner = boa.env.generate_address("emergency_owner")
        keeper = boa.env.generate_address("keeper")
        filler = boa.env.generate_address("filler")

        erc20 = boa.load_partial("contracts/testing/ERC20Mock.vy")
        target = erc20.deploy("Fork Target", "TARGET", 18)
        sell_token = erc20.deploy("Fork Sell Token", "SELL", 18)
        weth = boa.load("contracts/testing/WETH.vy")
        fee_collector = boa.load("contracts/FeeCollector.vy", target, weth, owner, emergency_owner)
        registry = boa.load("contracts/burners/adapters/AdapterRegistry.vy", fee_collector.address)
        adapter = boa.load(
            "contracts/burners/adapters/uniswapx/UniswapXAdapter.vy", EXCLUSIVE_DUTCH_ORDER_REACTOR
        )
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
            # Permit2 is the executor: the reactor pulls the input through it.
            registry.set_adapter(adapter, adapter.permit2())
            registry.activate_adapter(adapter)
        assert adapter.permit2() == PERMIT2
        assert bytes(adapter.domain_separator()) == bytes(permit2.DOMAIN_SEPARATOR())
        assert registry.is_executor_active(PERMIT2)

        _move_to_epoch(fee_collector, Epoch.COLLECT)
        amount = 1_000 * WAD
        sell_token._mint_for_testing(fee_collector, amount)
        with boa.env.prank(keeper):
            fee_collector.collect([sell_token.address], keeper)
            burner.sync_executor_approvals(PERMIT2, [sell_token.address])
        assert sell_token.allowance(burner, PERMIT2) == MAX_UINT256
        lot_start, lot_end = burner.window(sell_token)
        initial_amount = burner.lots(sell_token).initial_amount
        assert initial_amount > 0
        _move_to_timestamp(lot_start + 3 * STEP_DURATION)

        # What a publisher hands to fillers, straight from the adapter: the
        # whole lot at the live quote to the FeeCollector, and the signature
        # Permit2 will forward to the burner.
        available = burner.available(sell_token)
        order, signature = adapter.order_for(burner.address, sell_token.address, NONCE)
        order = _to_tuple(order)
        signature = bytes(signature)
        encoded = encode([ORDER_ABI], [order])
        assert signature == bytes.fromhex(adapter.address[2:]) + encoded
        assert order[0][:4] == (EXCLUSIVE_DUTCH_ORDER_REACTOR, burner.address, NONCE, lot_end)
        assert (
            order[5] == (sell_token.address, initial_amount, initial_amount)
            and available == initial_amount
        )
        quote = burner.getAmountNeeded(sell_token, initial_amount)
        assert order[6] == [(target.address, quote, quote, fee_collector.address)]

        digest = permit_digest_reference(order, bytes(permit2.DOMAIN_SEPARATOR()))
        with boa.env.prank(PERMIT2):
            assert burner.isValidSignature(digest, signature) == bytes.fromhex("1626ba7e")
            assert burner.isValidSignature(digest, signature[20:]) == bytes.fromhex("ffffffff")

        # An underpriced order of the filler's own making fails inside the
        # real settlement path with the auction's typed error.
        cheaper = (*order[:6], [(target.address, quote - 1, quote - 1, fee_collector.address)])
        cheaper_sig = bytes.fromhex(adapter.address[2:]) + encode([ORDER_ABI], [cheaper])
        target._mint_for_testing(filler, 2 * quote)
        with boa.env.prank(filler):
            target.approve(EXCLUSIVE_DUTCH_ORDER_REACTOR, MAX_UINT256)
            with boa.reverts(custom_err("BadBuyAmount()", nested=True)):
                reactor.execute((encode([ORDER_ABI], [cheaper]), cheaper_sig))

        # The real reactor + Permit2 settle the published order: Permit2
        # verifies the digest through the burner's router, pulls the lot to
        # the filler, and the reactor moves the filler's payment to the
        # FeeCollector.
        with boa.env.prank(filler):
            reactor.execute((encoded, signature))
        assert sell_token.balanceOf(filler) == initial_amount
        assert sell_token.balanceOf(burner) == 0
        assert target.balanceOf(fee_collector) == quote
        assert target.balanceOf(filler) == quote
        assert burner.available(sell_token) == 0
        # Permit2 burnt the nonce: the order is one-shot even if the lot were refilled.
        assert permit2.nonceBitmap(burner.address, NONCE >> 8) & (1 << (NONCE & 0xFF))
        with boa.env.prank(filler), boa.reverts():
            reactor.execute((encoded, signature))
