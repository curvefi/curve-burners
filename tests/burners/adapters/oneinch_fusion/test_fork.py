"""Mainnet fork simulation of the 1inch Fusion rail: the real
AggregationRouterV6 fills an order built by OneInchFusionAdapter.order_for
over the real Fusion Settlement (0x2Ad5...1Ef5) against a DutchAuctionBurner
behind a FeeCollector, with a whitelisted fork resolver: during the resolver
auction the taking amount carries the rate bump, the settlement keeps the
fees and its share of the surplus over the estimate, and the FeeCollector
gets the rest, at least the linear base.

Skips without a mainnet RPC (ETH_RPC_URL / MAINNET_RPC_URL, or an
ALCHEMY_API_TOKEN / DRPC_API_KEY to build one); set REQUIRE_MAINNET_FORK=1 to
turn the skip into a hard failure.
"""

from __future__ import annotations

import os

import boa
import pytest

from tests.burners.adapters.oneinch.test_fork import (
    ROUTER_ABI,
    RPC_ENV_KEYS,
    _abi_contract,
    _fork,
    _router_order,
    _rpc_url,
)
from tests.burners.adapters.oneinch.test_module import (
    ALLOW_MULTIPLE_FILLS_FLAG,
    EXPIRATION_OFFSET,
    HAS_EXTENSION_FLAG,
    ROUTER,
    build_extension,
)
from tests.burners.adapters.oneinch_feetaker.test_fork import (
    FILL_ARGS_ABI,
    GETTER_ABI,
    _taker_traits,
)
from tests.burners.adapters.oneinch_feetaker.test_module import (
    POST_INTERACTION_CALL_FLAG,
    bind_salt_reference,
    encode_fees,
    fee_split,
    gross_up,
)
from tests.burners.adapters.oneinch_fusion.test_module import (
    SETTLEMENT,
    auction_bump_reference,
    bump_up,
    encode_auction_details,
    encode_surplus,
    fusion_fields,
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
SALT = 0x5AFE_C0DE
FEES = (500, 40, 1000, 50)
AUCTION_DURATION = 180
INITIAL_RATE_BUMP = 100_000
POINTS = [(60_000, 60), (20_000, 60)]
SURPLUS_FEE = 50


def _now() -> int:
    return boa.env.evm.vm.state.timestamp


def test_mainnet_real_settlement_fills_a_published_order_during_the_auction():
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
        assert boa.env.get_code(SETTLEMENT), f"Missing deployed code at {SETTLEMENT}"
        # The Ethereum Settlement caps the priority fee relative to the base
        # fee (tx.gasprice - basefee, unchecked): fill at a zero base fee and
        # the zero gas price boa uses.
        boa.env.evm.vm.state.execution_context._base_fee_per_gas = 0
        router = _abi_contract(ROUTER_ABI + FILL_ARGS_ABI, "AggregationRouterV6", ROUTER)
        settlement = _abi_contract(GETTER_ABI, "Settlement", SETTLEMENT)

        owner = boa.env.generate_address("owner")
        emergency_owner = boa.env.generate_address("emergency_owner")
        keeper = boa.env.generate_address("keeper")
        resolver = boa.env.generate_address("resolver")
        integrator = boa.env.generate_address("integrator")
        protocol_fee_receiver = boa.env.generate_address("protocol_fee_receiver")

        erc20 = boa.load_partial("contracts/testing/ERC20Mock.vy")
        target = erc20.deploy("Fork Target", "TARGET", 18)
        sell_token = erc20.deploy("Fork Sell Token", "SELL", 18)
        weth = boa.load("contracts/testing/WETH.vy")
        fee_collector = boa.load("contracts/FeeCollector.vy", target, weth, owner, emergency_owner)
        registry = boa.load("contracts/burners/adapters/AdapterRegistry.vy", fee_collector.address)
        # The deployment probes the real settlement's bump and gross-up on
        # the pinned layout, next to the router's domain cross-check.
        adapter = boa.load(
            "contracts/burners/adapters/oneinch/OneInchFusionAdapter.vy", ROUTER, SETTLEMENT
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
            registry.set_adapter(adapter, adapter.limit_order_protocol())
            registry.activate_adapter(adapter)
        assert adapter.settlement() == SETTLEMENT

        _move_to_epoch(fee_collector, Epoch.COLLECT)
        amount = 1_000 * WAD
        sell_token._mint_for_testing(fee_collector, amount)
        with boa.env.prank(keeper):
            fee_collector.collect([sell_token.address], keeper)
            burner.sync_executor_approvals(ROUTER, [sell_token.address])
        lot_start, lot_end = burner.window(sell_token)
        initial_amount = burner.lots(sell_token).initial_amount
        _move_to_timestamp(lot_start + 3 * STEP_DURATION)
        quote = burner.getAmountNeeded(sell_token, initial_amount)

        # The extension a keeper would build with the Fusion SDK from a quote
        # with a custom preset ending at our quote: a 3-minute auction from
        # now, the resolver whitelisted from now, the estimate at the quote.
        start = _now()
        extension = build_extension(
            fusion_fields(
                SETTLEMENT,
                encode_auction_details(start, AUCTION_DURATION, INITIAL_RATE_BUMP, POINTS),
                encode_fees(*FEES),
                [(resolver, 0)],
                start,
                integrator,
                protocol_fee_receiver,
                fee_collector.address,
                encode_surplus(quote, SURPLUS_FEE),
            )
        )
        order, signature = adapter.order_for(burner.address, sell_token.address, SALT, 0, extension)
        order = tuple(order)
        signature = bytes(signature)
        assert order == (
            bind_salt_reference(SALT, extension),
            burner.address,
            SETTLEMENT,
            sell_token.address,
            target.address,
            initial_amount,
            quote,
            ALLOW_MULTIPLE_FILLS_FLAG
            | HAS_EXTENSION_FLAG
            | POST_INTERACTION_CALL_FLAG
            | (lot_end << EXPIRATION_OFFSET),
        )
        router_order = _router_order(order)
        digest = bytes(router.hashOrder(router_order))
        with boa.env.prank(ROUTER):
            assert burner.isValidSignature(digest, signature) == bytes.fromhex("1626ba7e")

        # Half a minute into the auction the real settlement prices half the
        # order with the interpolated bump on top of the fee gross-up.
        boa.env.time_travel(seconds=30)
        half = initial_amount // 2
        linear = -(-quote * half // initial_amount)
        bump = auction_bump_reference(_now(), start, AUCTION_DURATION, INITIAL_RATE_BUMP, POINTS)
        assert 0 < bump < INITIAL_RATE_BUMP
        taking = bump_up(gross_up(linear, 500, 500), bump)
        getter_data = extension[32 + 20 : 32 + 20 + 28 + 17]
        assert (
            settlement.getTakingAmount(
                router_order, extension, digest, resolver, half, initial_amount, getter_data
            )
            == taking
        )

        target._mint_for_testing(resolver, 2 * quote)
        with boa.env.prank(resolver):
            target.approve(ROUTER, MAX_UINT256)
            router.fillContractOrderArgs(
                router_order, signature, half, _taker_traits(extension), extension
            )
        integrator_amount, protocol_amount = fee_split(taking, 500, 40, 500)
        actual = taking - integrator_amount - protocol_amount
        # The estimate scaled to the fill is the linear base; the settlement
        # keeps half of the surplus above it.
        surplus = actual - linear
        assert surplus > 0
        protocol_amount += surplus * SURPLUS_FEE // 100
        assert sell_token.balanceOf(resolver) == half
        assert target.balanceOf(resolver) == 2 * quote - taking
        assert target.balanceOf(integrator) == integrator_amount
        assert target.balanceOf(protocol_fee_receiver) == protocol_amount
        assert target.balanceOf(fee_collector) == taking - integrator_amount - protocol_amount
        assert target.balanceOf(fee_collector) >= linear
        assert target.balanceOf(SETTLEMENT) == 0
        assert burner.available(sell_token) == initial_amount - half

        # After the auction the rest fills at the fee-grossed linear price
        # and the FeeCollector gets exactly the base (fees out, no surplus).
        boa.env.time_travel(seconds=AUCTION_DURATION)
        before = target.balanceOf(fee_collector)
        with boa.env.prank(resolver):
            router.fillContractOrderArgs(
                router_order, signature, half, _taker_traits(extension), extension
            )
        linear_rest = -(-quote * half // initial_amount)
        assert linear_rest <= target.balanceOf(fee_collector) - before <= linear_rest + 1
        assert burner.available(sell_token) == initial_amount - 2 * half
