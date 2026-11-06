"""Mainnet fork simulation of the 1inch rail: the real AggregationRouterV6
(Limit Order Protocol v4) fills an order built by OneInchAdapter.order_for
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

from tests.burners.adapters.oneinch.test_module import (
    ALLOW_MULTIPLE_FILLS_FLAG,
    EXPIRATION_OFFSET,
    ORDER_ABI,
    ROUTER,
    domain_separator_reference,
    order_digest_reference,
)
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

MAX_UINT256 = 2**256 - 1
SALT = 0x5AFE_C0DE
# TakerTraitsLib: the fill amount is a making amount; no threshold, no args.
TAKER_MAKING_AMOUNT = 1 << 255

RPC_ENV_KEYS = ("ETH_RPC_URL", "MAINNET_RPC_URL", "FORK_MAINNET_URL")

ORDER_COMPONENTS = [
    {"name": "salt", "type": "uint256"},
    {"name": "maker", "type": "uint256"},
    {"name": "receiver", "type": "uint256"},
    {"name": "makerAsset", "type": "uint256"},
    {"name": "takerAsset", "type": "uint256"},
    {"name": "makingAmount", "type": "uint256"},
    {"name": "takingAmount", "type": "uint256"},
    {"name": "makerTraits", "type": "uint256"},
]
ROUTER_ABI = [
    {
        "type": "function",
        "name": "fillContractOrder",
        "stateMutability": "payable",
        "inputs": [
            {"name": "order", "type": "tuple", "components": ORDER_COMPONENTS},
            {"name": "signature", "type": "bytes"},
            {"name": "amount", "type": "uint256"},
            {"name": "takerTraits", "type": "uint256"},
        ],
        "outputs": [
            {"name": "makingAmount", "type": "uint256"},
            {"name": "takingAmount", "type": "uint256"},
            {"name": "orderHash", "type": "bytes32"},
        ],
    },
    {
        "type": "function",
        "name": "hashOrder",
        "stateMutability": "view",
        "inputs": [{"name": "order", "type": "tuple", "components": ORDER_COMPONENTS}],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "eip712Domain",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "fields", "type": "bytes1"},
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
            {"name": "salt", "type": "bytes32"},
            {"name": "extensions", "type": "uint256[]"},
        ],
    },
    {
        "type": "function",
        "name": "remainingInvalidatorForOrder",
        "stateMutability": "view",
        "inputs": [
            {"name": "maker", "type": "address"},
            {"name": "orderHash", "type": "bytes32"},
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


def _fork(rpc_url: str) -> None:
    if hasattr(boa, "fork"):
        boa.fork(rpc_url, block_identifier="latest")
    else:
        boa.env.fork(rpc_url, block_identifier="latest")


def _abi_contract(abi: list[dict[str, Any]], name: str, address: str) -> Any:
    return boa.loads_abi(json.dumps(abi), name=name).at(address)


def _router_order(order: tuple) -> tuple:
    """The order as the router ABI takes it: addresses as uint256 words."""
    return tuple(int(str(field), 16) if isinstance(field, str) else field for field in order)


def test_mainnet_real_router_fills_a_published_order():
    rpc_url = _rpc_url()
    if rpc_url is None:
        message = f"Mainnet fork RPC unavailable; set one of {', '.join(RPC_ENV_KEYS)}"
        if os.environ.get("REQUIRE_MAINNET_FORK"):
            pytest.fail(message)
        pytest.skip(message)

    fork_env = boa.Env()
    with boa.swap_env(fork_env):
        _fork(rpc_url)

        assert boa.env.get_code(ROUTER), f"Missing deployed code at {ROUTER}"
        router = _abi_contract(ROUTER_ABI, "AggregationRouterV6", ROUTER)
        fields, name, version, chain_id, verifying_contract, _salt, _ext = router.eip712Domain()
        assert (fields, chain_id, verifying_contract) == (b"\x0f", 1, ROUTER)
        domain = domain_separator_reference(name, version, chain_id, ROUTER)

        owner = boa.env.generate_address("owner")
        emergency_owner = boa.env.generate_address("emergency_owner")
        keeper = boa.env.generate_address("keeper")
        taker = boa.env.generate_address("taker")

        erc20 = boa.load_partial("contracts/testing/ERC20Mock.vy")
        target = erc20.deploy("Fork Target", "TARGET", 18)
        sell_token = erc20.deploy("Fork Sell Token", "SELL", 18)
        weth = boa.load("contracts/testing/WETH.vy")
        fee_collector = boa.load("contracts/FeeCollector.vy", target, weth, owner, emergency_owner)
        registry = boa.load("contracts/burners/adapters/AdapterRegistry.vy", fee_collector.address)
        # The deployment cross-checks its domain against the router's hashOrder.
        adapter = boa.load("contracts/burners/adapters/oneinch/OneInchAdapter.vy", ROUTER)
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
            # The router is the executor: a fill pulls the maker asset itself.
            registry.set_adapter(adapter, adapter.limit_order_protocol())
            registry.activate_adapter(adapter)
        assert adapter.limit_order_protocol() == ROUTER
        assert bytes(adapter.domain_separator()) == domain
        assert registry.is_executor_active(ROUTER)

        _move_to_epoch(fee_collector, Epoch.COLLECT)
        amount = 1_000 * WAD
        sell_token._mint_for_testing(fee_collector, amount)
        with boa.env.prank(keeper):
            fee_collector.collect([sell_token.address], keeper)
            burner.sync_executor_approvals(ROUTER, [sell_token.address])
        assert sell_token.allowance(burner, ROUTER) == MAX_UINT256
        lot_start, lot_end = burner.window(sell_token)
        initial_amount = burner.lots(sell_token).initial_amount
        assert initial_amount > 0
        _move_to_timestamp(lot_start + 3 * STEP_DURATION)

        # What a publisher hands to takers, straight from the adapter: the
        # whole lot at the live quote to the FeeCollector, and the signature
        # the router will forward to the burner.
        order, signature = adapter.order_for(burner.address, sell_token.address, SALT)
        order = tuple(order)
        signature = bytes(signature)
        quote = burner.getAmountNeeded(sell_token, initial_amount)
        assert order == (
            SALT,
            burner.address,
            fee_collector.address,
            sell_token.address,
            target.address,
            initial_amount,
            quote,
            ALLOW_MULTIPLE_FILLS_FLAG | (lot_end << EXPIRATION_OFFSET),
        )
        assert signature == bytes.fromhex(adapter.address[2:]) + encode(
            [ORDER_ABI, "bytes"], [order, b""]
        )
        router_order = _router_order(order)
        digest = bytes(router.hashOrder(router_order))
        assert digest == order_digest_reference(order, domain)
        with boa.env.prank(ROUTER):
            assert burner.isValidSignature(digest, signature) == bytes.fromhex("1626ba7e")
            assert burner.isValidSignature(digest, signature[20:]) == bytes.fromhex("ffffffff")

        # An underpriced order of the taker's own making fails inside the
        # real fill: the router reports every rejected contract signature as
        # its own BadSignature (the auction's typed reason is for eth_call).
        cheaper = (*order[:6], quote - 1, order[7])
        cheaper_sig = bytes.fromhex(adapter.address[2:]) + encode(
            [ORDER_ABI, "bytes"], [cheaper, b""]
        )
        target._mint_for_testing(taker, 2 * quote)
        with boa.env.prank(taker):
            target.approve(ROUTER, MAX_UINT256)
            with boa.reverts(custom_err("BadSignature()", nested=True)):
                router.fillContractOrder(
                    _router_order(cheaper), cheaper_sig, initial_amount, TAKER_MAKING_AMOUNT
                )

        # The real router fills a quarter of the published order: it verifies
        # the digest through the burner's router, pulls the lot slice from the
        # burner, and moves the taker's proportional payment (rounded up) to
        # the FeeCollector.
        quarter = initial_amount // 4
        quarter_quote = -(-quote * quarter // initial_amount)
        with boa.env.prank(taker):
            router.fillContractOrder(router_order, signature, quarter, TAKER_MAKING_AMOUNT)
        assert sell_token.balanceOf(taker) == quarter
        assert target.balanceOf(fee_collector) == quarter_quote
        assert burner.available(sell_token) == initial_amount - quarter
        assert router.remainingInvalidatorForOrder(burner.address, digest) == (
            initial_amount - quarter
        )

        # The router checks a contract signature on the first fill only: once
        # the adapter is disabled, the remainder of this order still fills at
        # its signed price, while a fresh order does not.
        with boa.env.prank(emergency_owner):
            registry.disable_adapter(adapter)
        fresh, fresh_sig = adapter.order_for(burner.address, sell_token.address, SALT + 1)
        with boa.env.prank(taker), boa.reverts(custom_err("BadSignature()", nested=True)):
            router.fillContractOrder(
                _router_order(tuple(fresh)), bytes(fresh_sig), quarter, TAKER_MAKING_AMOUNT
            )
        with boa.env.prank(taker):
            router.fillContractOrder(router_order, signature, quarter, TAKER_MAKING_AMOUNT)
        assert sell_token.balanceOf(taker) == 2 * quarter
        assert target.balanceOf(fee_collector) == 2 * quarter_quote

        # The emergency procedure completes with the allowance sync: with the
        # executor released, the remainder cannot be pulled any more.
        with boa.env.prank(keeper):
            burner.sync_executor_approvals(ROUTER, [sell_token.address])
        assert sell_token.allowance(burner, ROUTER) == 0
        with boa.env.prank(taker), boa.reverts():
            router.fillContractOrder(router_order, signature, quarter, TAKER_MAKING_AMOUNT)
        assert burner.available(sell_token) == initial_amount - 2 * quarter
