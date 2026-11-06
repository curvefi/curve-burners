"""Mainnet fork simulation of the 1inch FeeTaker rail: the real
AggregationRouterV6 fills an order built by OneInchFeeTakerAdapter.order_for
over the real FeeTaker extension (0xc0DF...E05e) against a DutchAuctionBurner
behind a FeeCollector, and the FeeTaker splits the taking tokens: fees to
the integrator and protocol receivers, the rest (at least the linear base)
to the FeeCollector.

Skips without a mainnet RPC (ETH_RPC_URL / MAINNET_RPC_URL, or an
ALCHEMY_API_TOKEN / DRPC_API_KEY to build one); set REQUIRE_MAINNET_FORK=1 to
turn the skip into a hard failure.
"""

from __future__ import annotations

import os

import boa
import pytest
from eth_abi import encode

from tests.burners.adapters.oneinch.test_fork import (
    ORDER_COMPONENTS,
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
    ORDER_ABI,
    ROUTER,
    build_extension,
)
from tests.burners.adapters.oneinch_feetaker.test_module import (
    FEE_TAKER,
    POST_INTERACTION_CALL_FLAG,
    bind_salt_reference,
    encode_fees,
    fee_split,
    fee_taker_fields,
    gross_up,
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
# TakerTraitsLib: the fill amount is a making amount; the args carry the
# extension (its length at bit 224), no target, no interaction.
TAKER_MAKING_AMOUNT = 1 << 255
ARGS_EXTENSION_LENGTH_OFFSET = 224
# 0.5% integrator fee (40% share), 1% resolver fee with a 50% whitelist
# discount: the public orderbook's shape, with our own receivers.
FEES = (500, 40, 1000, 50)

FILL_ARGS_ABI = [
    {
        "type": "function",
        "name": "fillContractOrderArgs",
        "stateMutability": "payable",
        "inputs": [
            {"name": "order", "type": "tuple", "components": ORDER_COMPONENTS},
            {"name": "signature", "type": "bytes"},
            {"name": "amount", "type": "uint256"},
            {"name": "takerTraits", "type": "uint256"},
            {"name": "args", "type": "bytes"},
        ],
        "outputs": [
            {"name": "makingAmount", "type": "uint256"},
            {"name": "takingAmount", "type": "uint256"},
            {"name": "orderHash", "type": "bytes32"},
        ],
    },
]
GETTER_ABI = [
    {
        "type": "function",
        "name": "getTakingAmount",
        "stateMutability": "view",
        "inputs": [
            {"name": "order", "type": "tuple", "components": ORDER_COMPONENTS},
            {"name": "extension", "type": "bytes"},
            {"name": "orderHash", "type": "bytes32"},
            {"name": "taker", "type": "address"},
            {"name": "makingAmount", "type": "uint256"},
            {"name": "remainingMakingAmount", "type": "uint256"},
            {"name": "extraData", "type": "bytes"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


def _taker_traits(extension: bytes) -> int:
    return TAKER_MAKING_AMOUNT | (len(extension) << ARGS_EXTENSION_LENGTH_OFFSET)


def test_mainnet_real_fee_taker_splits_a_published_order():
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
        assert boa.env.get_code(FEE_TAKER), f"Missing deployed code at {FEE_TAKER}"
        router = _abi_contract(ROUTER_ABI + FILL_ARGS_ABI, "AggregationRouterV6", ROUTER)
        fee_taker = _abi_contract(GETTER_ABI, "FeeTaker", FEE_TAKER)

        owner = boa.env.generate_address("owner")
        emergency_owner = boa.env.generate_address("emergency_owner")
        keeper = boa.env.generate_address("keeper")
        resolver = boa.env.generate_address("resolver")
        outsider = boa.env.generate_address("outsider")
        integrator = boa.env.generate_address("integrator")
        protocol_fee_receiver = boa.env.generate_address("protocol_fee_receiver")

        erc20 = boa.load_partial("contracts/testing/ERC20Mock.vy")
        target = erc20.deploy("Fork Target", "TARGET", 18)
        sell_token = erc20.deploy("Fork Sell Token", "SELL", 18)
        weth = boa.load("contracts/testing/WETH.vy")
        fee_collector = boa.load("contracts/FeeCollector.vy", target, weth, owner, emergency_owner)
        registry = boa.load("contracts/burners/adapters/AdapterRegistry.vy", fee_collector.address)
        # The deployment probes the real FeeTaker's gross-up on the pinned
        # layout, next to the router's domain cross-check.
        adapter = boa.load(
            "contracts/burners/adapters/oneinch/OneInchFeeTakerAdapter.vy", ROUTER, FEE_TAKER
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
        assert adapter.fee_taker() == FEE_TAKER

        _move_to_epoch(fee_collector, Epoch.COLLECT)
        amount = 1_000 * WAD
        sell_token._mint_for_testing(fee_collector, amount)
        with boa.env.prank(keeper):
            fee_collector.collect([sell_token.address], keeper)
            burner.sync_executor_approvals(ROUTER, [sell_token.address])
        lot_start, lot_end = burner.window(sell_token)
        initial_amount = burner.lots(sell_token).initial_amount
        _move_to_timestamp(lot_start + 3 * STEP_DURATION)

        # The extension a publisher would build with the SDK from fee-info:
        # the orderbook's fees and whitelist (here: our resolver), proceeds
        # to the FeeCollector.
        extension = build_extension(
            fee_taker_fields(
                FEE_TAKER,
                encode_fees(*FEES),
                [resolver],
                integrator,
                protocol_fee_receiver,
                fee_collector.address,
            )
        )
        fees, receivers, size = adapter.describe(extension)
        assert tuple(receivers)[2] == fee_collector.address and size == 1
        order, signature = adapter.order_for(burner.address, sell_token.address, SALT, 0, extension)
        order = tuple(order)
        signature = bytes(signature)
        quote = burner.getAmountNeeded(sell_token, initial_amount)
        assert order == (
            bind_salt_reference(SALT, extension),
            burner.address,
            FEE_TAKER,
            sell_token.address,
            target.address,
            initial_amount,
            quote,
            ALLOW_MULTIPLE_FILLS_FLAG
            | HAS_EXTENSION_FLAG
            | POST_INTERACTION_CALL_FLAG
            | (lot_end << EXPIRATION_OFFSET),
        )
        assert signature == bytes.fromhex(adapter.address[2:]) + encode(
            [ORDER_ABI, "bytes"], [order, extension]
        )
        router_order = _router_order(order)
        digest = bytes(router.hashOrder(router_order))
        with boa.env.prank(ROUTER):
            assert burner.isValidSignature(digest, signature) == bytes.fromhex("1626ba7e")

        # The real FeeTaker prices a third of the order exactly as modelled:
        # the whitelisted resolver pays the discounted resolver fee.
        third = initial_amount // 3
        linear = -(-quote * third // initial_amount)
        taking = gross_up(linear, 500, 500)
        getter_data = extension[32 + 20 : 32 + 20 + 27]
        assert (
            fee_taker.getTakingAmount(
                router_order, extension, digest, resolver, third, initial_amount, getter_data
            )
            == taking
        )
        assert fee_taker.getTakingAmount(
            router_order, extension, digest, outsider, third, initial_amount, getter_data
        ) == gross_up(linear, 500, 1000)

        # An outsider holds no access token and is not whitelisted: the
        # FeeTaker refuses the fill inside the real router.
        target._mint_for_testing(outsider, 2 * quote)
        with boa.env.prank(outsider):
            target.approve(ROUTER, MAX_UINT256)
            with boa.reverts(custom_err("OnlyWhitelistOrAccessToken()", nested=True)):
                router.fillContractOrderArgs(
                    router_order, signature, third, _taker_traits(extension), extension
                )

        # The whitelisted resolver fills: the router pulls the lot slice, the
        # FeeTaker receives the grossed-up payment and splits it.
        target._mint_for_testing(resolver, 2 * quote)
        with boa.env.prank(resolver):
            target.approve(ROUTER, MAX_UINT256)
            router.fillContractOrderArgs(
                router_order, signature, third, _taker_traits(extension), extension
            )
        integrator_amount, protocol_amount = fee_split(taking, 500, 40, 500)
        assert sell_token.balanceOf(resolver) == third
        assert target.balanceOf(resolver) == 2 * quote - taking
        assert target.balanceOf(integrator) == integrator_amount
        assert target.balanceOf(protocol_fee_receiver) == protocol_amount
        assert target.balanceOf(fee_collector) == taking - integrator_amount - protocol_amount
        assert target.balanceOf(fee_collector) >= linear
        assert target.balanceOf(FEE_TAKER) == 0
        assert burner.available(sell_token) == initial_amount - third
        assert router.remainingInvalidatorForOrder(burner.address, digest) == (
            initial_amount - third
        )

        # Emergency: a disable alone leaves the remainder fillable (first
        # fill signature check); the allowance sync ends it.
        with boa.env.prank(emergency_owner):
            registry.disable_adapter(adapter)
        with boa.env.prank(resolver):
            router.fillContractOrderArgs(
                router_order, signature, third, _taker_traits(extension), extension
            )
        assert sell_token.balanceOf(resolver) == 2 * third
        with boa.env.prank(keeper):
            burner.sync_executor_approvals(ROUTER, [sell_token.address])
        with boa.env.prank(resolver), boa.reverts():
            router.fillContractOrderArgs(
                router_order, signature, third, _taker_traits(extension), extension
            )
