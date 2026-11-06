"""Mainnet fork simulation of the Velora Delta rail: the real Delta contract
(Portikus V2 adapter with the v2.3.0 settlement modules) fills orders built
by VeloraDeltaAdapter.order_for against a DutchAuctionBurner behind a
FeeCollector, through swapSettleFillable (pro-rata slices) and swapSettle
(fill-or-kill), with a registered agent and a minimal executor paying the
dest token.

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

from tests.burners.conftest import custom_err
from tests.burners.test_dutch_auction_v2 import (
    FLOOR_TOTAL,
    START_TOTAL,
    STEP_DURATION,
    WAD,
    _move_to_epoch,
    _move_to_timestamp,
)
from tests.burners.test_velora_delta_module import (
    EMPTY_BRIDGE,
    MAINNET_DOMAIN,
    SETTLEMENT,
    order_digest_reference,
)
from tests.conftest import ZERO_ADDRESS, Epoch

MAX_UINT256 = 2**256 - 1
NONCE = 0x5AFE_C0DE
HUNDRED_PERCENT = 10_000
# PortikusV2 registry: the agent allow-list the settlement modules check.
PORTIKUS_V2 = "0x0007005729e310000c6003402d8a0fb700da0c00"

RPC_ENV_KEYS = ("ETH_RPC_URL", "MAINNET_RPC_URL", "FORK_MAINNET_URL")

BRIDGE_COMPONENTS = [
    {"name": "protocolSelector", "type": "bytes4"},
    {"name": "destinationChainId", "type": "uint256"},
    {"name": "outputToken", "type": "address"},
    {"name": "scalingFactor", "type": "int8"},
    {"name": "protocolData", "type": "bytes"},
]
ORDER_COMPONENTS = [
    {"name": "owner", "type": "address"},
    {"name": "beneficiary", "type": "address"},
    {"name": "srcToken", "type": "address"},
    {"name": "destToken", "type": "address"},
    {"name": "srcAmount", "type": "uint256"},
    {"name": "destAmount", "type": "uint256"},
    {"name": "expectedAmount", "type": "uint256"},
    {"name": "deadline", "type": "uint256"},
    {"name": "kind", "type": "uint8"},
    {"name": "nonce", "type": "uint256"},
    {"name": "partnerAndFee", "type": "uint256"},
    {"name": "permit", "type": "bytes"},
    {"name": "metadata", "type": "bytes"},
    {"name": "bridge", "type": "tuple", "components": BRIDGE_COMPONENTS},
]
ORDER_WITH_SIG_COMPONENTS = [
    {"name": "order", "type": "tuple", "components": ORDER_COMPONENTS},
    {"name": "signature", "type": "bytes"},
    {
        "name": "bridgeOverride",
        "type": "tuple",
        "components": [
            {"name": "protocolSelector", "type": "bytes4"},
            {"name": "protocolData", "type": "bytes"},
        ],
    },
    {"name": "cosignature", "type": "bytes"},
]
DELTA_ABI = [
    {
        "type": "function",
        "name": "swapSettle",
        "stateMutability": "payable",
        "inputs": [
            {"name": "orderWithSig", "type": "tuple", "components": ORDER_WITH_SIG_COMPONENTS},
            {"name": "executorData", "type": "bytes"},
            {"name": "executor", "type": "address"},
            {"name": "bridgeData", "type": "bytes"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "swapSettleFillable",
        "stateMutability": "payable",
        "inputs": [
            {"name": "orderWithSig", "type": "tuple", "components": ORDER_WITH_SIG_COMPONENTS},
            {"name": "executorData", "type": "bytes"},
            {"name": "executor", "type": "address"},
            {"name": "fillPercent", "type": "uint256"},
            {"name": "bridgeData", "type": "bytes"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "swapFilledAmount",
        "stateMutability": "view",
        "inputs": [{"name": "order", "type": "tuple", "components": ORDER_COMPONENTS}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "isNonceUsed",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}, {"name": "nonce", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "DOMAIN_SEPARATOR",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
]
REGISTRY_ABI = [
    {
        "type": "function",
        "name": "getAgents",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address[]"}],
    },
    {
        "type": "function",
        "name": "isAgentRegistered",
        "stateMutability": "view",
        "inputs": [{"name": "agent", "type": "address"}],
        "outputs": [{"name": "", "type": "bool"}],
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


def _normalized(order: Any) -> tuple:
    """The order as the adapter returns it, bytes fields as bytes."""
    order = tuple(order)
    return (*order[:11], bytes(order[11]), bytes(order[12]), EMPTY_BRIDGE)


def _order_with_sig(order: tuple, signature: bytes) -> tuple:
    return (order, signature, (bytes(4), b""), b"")


def _executor_data(token: Any, amount: int) -> bytes:
    return encode(["address", "uint256"], [token.address, amount])


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
        delta = _abi_contract(DELTA_ABI, "Delta", SETTLEMENT)
        assert bytes(delta.DOMAIN_SEPARATOR()) == MAINNET_DOMAIN
        portikus = _abi_contract(REGISTRY_ABI, "PortikusV2", PORTIKUS_V2)
        agents = portikus.getAgents()
        assert agents, "No registered Delta agents"
        agent = agents[0]
        assert portikus.isAgentRegistered(agent)

        owner = boa.env.generate_address("owner")
        emergency_owner = boa.env.generate_address("emergency_owner")
        keeper = boa.env.generate_address("keeper")
        outsider = boa.env.generate_address("outsider")

        erc20 = boa.load_partial("contracts/testing/ERC20Mock.vy")
        target = erc20.deploy("Fork Target", "TARGET", 18)
        sell_token = erc20.deploy("Fork Sell Token", "SELL", 18)
        weth = boa.load("contracts/testing/WETH.vy")
        fee_collector = boa.load("contracts/FeeCollector.vy", target, weth, owner, emergency_owner)
        registry = boa.load("contracts/burners/adapters/AdapterRegistry.vy", fee_collector.address)
        # The deployment cross-checks its domain against the Delta contract's
        # DOMAIN_SEPARATOR.
        adapter = boa.load("contracts/burners/adapters/velora/VeloraDeltaAdapter.vy", SETTLEMENT)
        executor = boa.load("contracts/testing/dutch_auction/DeltaExecutorMock.vy")
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
            # The Delta contract pulls the sold tokens itself: it is the executor.
            registry.set_adapter(adapter, SETTLEMENT)
            registry.activate_adapter(adapter)
        assert adapter.settlement() == SETTLEMENT
        assert bytes(adapter.domain_separator()) == MAINNET_DOMAIN
        assert registry.is_executor_active(SETTLEMENT)

        _move_to_epoch(fee_collector, Epoch.COLLECT)
        amount = 1_000 * WAD
        sell_token._mint_for_testing(fee_collector, amount)
        with boa.env.prank(keeper):
            fee_collector.collect([sell_token.address], keeper)
            burner.sync_executor_approvals(SETTLEMENT, [sell_token.address])
        assert sell_token.allowance(burner, SETTLEMENT) == MAX_UINT256
        lot_start, lot_end = burner.window(sell_token)
        initial_amount = burner.lots(sell_token).initial_amount
        assert initial_amount > 0
        _move_to_timestamp(lot_start + 3 * STEP_DURATION)

        # What a publisher hands to agents, straight from the adapter: a
        # quarter of the lot at the live quote to the FeeCollector, and the
        # signature the settlement will forward to the burner.
        quarter = initial_amount // 4
        order, signature = adapter.order_for(burner.address, sell_token.address, NONCE, quarter)
        order = _normalized(order)
        signature = bytes(signature)
        quote = order[5]
        assert quote == burner.getAmountNeeded(sell_token, quarter)
        assert order[0] == burner.address and order[1] == fee_collector.address
        assert order[7] == lot_end
        for fillable in (False, True):
            digest = order_digest_reference(order, MAINNET_DOMAIN, fillable)
            assert bytes(burner.isValidSignature(digest, signature)) == bytes.fromhex("1626ba7e")

        # Not an agent: the registry gate comes first.
        with boa.env.prank(outsider), boa.reverts():
            delta.swapSettle(
                _order_with_sig(order, signature), _executor_data(target, quote), executor, b""
            )

        # A pro-rata slice through swapSettleFillable: 40% of the order, the
        # executor paying exactly the sliced floor.
        slice_out = quote * 4000 // HUNDRED_PERCENT
        target._mint_for_testing(executor, slice_out)
        with boa.env.prank(agent):
            delta.swapSettleFillable(
                _order_with_sig(order, signature),
                _executor_data(target, slice_out),
                executor,
                4000,
                b"",
            )
        assert sell_token.balanceOf(executor) == quarter * 4000 // HUNDRED_PERCENT
        assert target.balanceOf(fee_collector) == slice_out
        assert delta.swapFilledAmount(order) == quarter * 4000 // HUNDRED_PERCENT
        assert not delta.isNonceUsed(burner, NONCE)

        # The rest of the same order later on the curve, overpaid: the surplus
        # above expectedAmount is split with the protocol, the FeeCollector
        # still gets at least the sliced floor; the nonce is burnt at 100%.
        _move_to_timestamp(lot_start + 20 * STEP_DURATION)
        rest_out = quote * 6000 // HUNDRED_PERCENT
        target._mint_for_testing(executor, rest_out + 1000)
        before = target.balanceOf(fee_collector)
        with boa.env.prank(agent):
            delta.swapSettleFillable(
                _order_with_sig(order, signature),
                _executor_data(target, rest_out + 1000),
                executor,
                6000,
                b"",
            )
        assert target.balanceOf(fee_collector) - before == rest_out + 1000 - 1000 // 2
        assert delta.swapFilledAmount(order) == quarter
        assert delta.isNonceUsed(burner, NONCE)
        assert sell_token.balanceOf(burner) == initial_amount - quarter

        # A fresh fill-or-kill order for the remainder through swapSettle.
        remainder = burner.available(sell_token)
        order2, signature2 = adapter.order_for(burner.address, sell_token.address, NONCE + 1)
        order2 = _normalized(order2)
        assert order2[4] == remainder
        quote2 = order2[5]
        # The burner's typed economic revert reaches the agent through the
        # real settlement: an underpriced copy is rejected before any pull.
        underpriced = (*order2[:5], quote2 - 1, quote2 - 1, *order2[7:9], NONCE + 2, *order2[10:])
        bad_signature = signature2[:20] + encode(
            [
                "(address,address,address,address,uint256,uint256,uint256,uint256,uint8,uint256,"
                "uint256,bytes,bytes,(bytes4,uint256,address,int8,bytes))"
            ],
            [underpriced],
        )
        target._mint_for_testing(executor, quote2)
        with boa.env.prank(agent), boa.reverts(custom_err("BadBuyAmount()", nested=True)):
            delta.swapSettle(
                _order_with_sig(underpriced, bad_signature),
                _executor_data(target, quote2),
                executor,
                b"",
            )
        before = target.balanceOf(fee_collector)
        with boa.env.prank(agent):
            delta.swapSettle(
                _order_with_sig(order2, bytes(signature2)),
                _executor_data(target, quote2),
                executor,
                b"",
            )
        assert target.balanceOf(fee_collector) - before == quote2
        assert sell_token.balanceOf(burner) == 0
        assert delta.isNonceUsed(burner, NONCE + 1)

        # Emergency: a registry disable turns the router's answer into
        # 0xffffffff, which the real settlement reports as InvalidSignature.
        sell_token._mint_for_testing(burner, quarter)
        order3, signature3 = adapter.order_for(burner.address, sell_token.address, NONCE + 3)
        order3 = _normalized(order3)
        with boa.env.prank(emergency_owner):
            registry.disable_adapter(adapter)
        target._mint_for_testing(executor, order3[5])
        with boa.env.prank(agent), boa.reverts(custom_err("InvalidSignature()", nested=True)):
            delta.swapSettle(
                _order_with_sig(order3, bytes(signature3)),
                _executor_data(target, order3[5]),
                executor,
                b"",
            )
