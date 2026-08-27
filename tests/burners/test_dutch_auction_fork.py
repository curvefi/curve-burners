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

from ..conftest import ZERO_ADDRESS, Epoch, WEEK
from .test_dutch_auction_v2 import (
    APP_DATA,
    BURNER_INTERFACE,
    CONDITIONAL_ORDER_INTERFACE,
    COW_ORDER_VALIDITY,
    DECAY_FACTOR_RAY,
    ERC20_BALANCE,
    ERC1271_MAGIC_VALUE,
    FLOOR_TOTAL,
    LOT_END,
    LOT_INITIAL_AMOUNT,
    LOT_START,
    MAX_UINT256,
    ORDER_BUY_AMOUNT,
    ORDER_BUY_BALANCE,
    ORDER_BUY_TOKEN,
    ORDER_FEE_AMOUNT,
    ORDER_KIND,
    ORDER_PARTIALLY_FILLABLE,
    ORDER_RECEIVER,
    ORDER_SELL_AMOUNT,
    ORDER_SELL_BALANCE,
    ORDER_SELL_TOKEN,
    ORDER_VALID_TO,
    SELL_KIND,
    START_TOTAL,
    STEP_DURATION,
    WAD,
    ZERO_BYTES32,
)


# Official deterministic deployments shared by Gnosis Chain.
COMPOSABLE_COW = "0xfdaFc9d1902f4e0b84f65F49f244b32b31013b74"
GPV2_SETTLEMENT = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"
GPV2_VAULT_RELAYER = "0xC92E8bdf79f0507f65a392b0ab4667716BFE0110"

RPC_ENV_KEYS = (
    "GNOSIS_RPC_URL",
    "FORK_GNOSIS_URL",
    "GNOSIS_RPC",
    "GNOSIS_URL",
)

GPV2_ORDER_COMPONENTS = [
    {"name": "sellToken", "type": "address"},
    {"name": "buyToken", "type": "address"},
    {"name": "receiver", "type": "address"},
    {"name": "sellAmount", "type": "uint256"},
    {"name": "buyAmount", "type": "uint256"},
    {"name": "validTo", "type": "uint32"},
    {"name": "appData", "type": "bytes32"},
    {"name": "feeAmount", "type": "uint256"},
    {"name": "kind", "type": "bytes32"},
    {"name": "partiallyFillable", "type": "bool"},
    {"name": "sellTokenBalance", "type": "bytes32"},
    {"name": "buyTokenBalance", "type": "bytes32"},
]

CONDITIONAL_ORDER_COMPONENTS = [
    {"name": "handler", "type": "address"},
    {"name": "salt", "type": "bytes32"},
    {"name": "staticData", "type": "bytes"},
]

COMPOSABLE_COW_ABI = [
    {
        "type": "function",
        "name": "getTradeableOrderWithSignature",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {
                "name": "params",
                "type": "tuple",
                "components": CONDITIONAL_ORDER_COMPONENTS,
            },
            {"name": "offchainInput", "type": "bytes"},
            {"name": "proof", "type": "bytes32[]"},
        ],
        "outputs": [
            {
                "name": "order",
                "type": "tuple",
                "components": GPV2_ORDER_COMPONENTS,
            },
            {"name": "signature", "type": "bytes"},
        ],
    },
    {
        "type": "function",
        "name": "domainSeparator",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
]

SETTLEMENT_ABI = [
    {
        "type": "function",
        "name": "domainSeparator",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "bytes32"}],
    }
]

VAULT_RELAYER_ABI = [
    {
        "type": "function",
        "name": "transferFromAccounts",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "transfers",
                "type": "tuple[]",
                "components": [
                    {"name": "account", "type": "address"},
                    {"name": "token", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                    {"name": "balance", "type": "bytes32"},
                ],
            },
        ],
        "outputs": [],
    }
]


def _rpc_url() -> str | None:
    load_dotenv(Path.home() / ".env", override=False)
    return next((os.environ[key] for key in RPC_ENV_KEYS if os.environ.get(key)), None)


def _fork(rpc_url: str) -> None:
    if hasattr(boa, "fork"):
        boa.fork(rpc_url)
    else:
        boa.env.fork(rpc_url, block_identifier="latest")


def _timestamp() -> int:
    return boa.env.evm.vm.state.timestamp


def _move_to_epoch(fee_collector: Any, epoch: Epoch) -> None:
    start, end = fee_collector.epoch_time_frame(epoch)
    target = (start + end) // 2
    while target <= _timestamp():
        target += WEEK
    boa.env.time_travel(seconds=target - _timestamp())


def _move_to_timestamp(timestamp: int) -> None:
    boa.env.time_travel(seconds=timestamp - _timestamp())


def _static_input(token: Any, generation: int) -> bytes:
    return bytes.fromhex(str(token.address)[2:]) + generation.to_bytes(32, "big")


def _gpv2_order_digest(order: Any, domain_separator: bytes) -> bytes:
    order_type_hash = keccak(
        text=(
            "Order(address sellToken,address buyToken,address receiver,uint256 sellAmount,"
            "uint256 buyAmount,uint32 validTo,bytes32 appData,uint256 feeAmount,string kind,"
            "bool partiallyFillable,string sellTokenBalance,string buyTokenBalance)"
        )
    )
    struct_hash = keccak(
        encode(
            [
                "bytes32",
                "address",
                "address",
                "address",
                "uint256",
                "uint256",
                "uint32",
                "bytes32",
                "uint256",
                "bytes32",
                "bool",
                "bytes32",
                "bytes32",
            ],
            [order_type_hash, *order],
        )
    )
    return keccak(b"\x19\x01" + bytes(domain_separator) + struct_hash)


def _abi_contract(abi: list[dict[str, Any]], name: str, address: str) -> Any:
    return boa.loads_abi(json.dumps(abi), name=name).at(address)


def test_gnosis_real_composable_cow_signature_and_vault_relayer_custody():
    """Production-like Gnosis fork simulation of the CoW rail.

    Real components: deployed ComposableCoW (order generation and signature
    encoding), GPv2Settlement (domain separator, ERC-1271 caller) and
    GPv2VaultRelayer (allowance spend, Settlement-only access control).
    Simulated boundary: the solver-side settlement netting is external to the
    chain, so the target payment is modeled as a direct transfer of the exact
    on-chain quote to the FeeCollector instead of a full GPv2 settle() batch.

    Skips without a Gnosis RPC; set REQUIRE_GNOSIS_FORK=1 (CI quality gate)
    to turn the skip into a hard failure.
    """
    rpc_url = _rpc_url()
    if rpc_url is None:
        message = f"Gnosis fork RPC unavailable; set one of {', '.join(RPC_ENV_KEYS)}"
        if os.environ.get("REQUIRE_GNOSIS_FORK"):
            pytest.fail(message)
        pytest.skip(message)

    fork_env = boa.Env()
    with boa.swap_env(fork_env):
        _fork(rpc_url)

        for address in (COMPOSABLE_COW, GPV2_SETTLEMENT, GPV2_VAULT_RELAYER):
            assert boa.env.get_code(address), f"Missing deployed code at {address}"

        composable_cow = _abi_contract(
            COMPOSABLE_COW_ABI, "ComposableCoW", COMPOSABLE_COW
        )
        settlement = _abi_contract(SETTLEMENT_ABI, "GPv2Settlement", GPV2_SETTLEMENT)
        vault_relayer = _abi_contract(
            VAULT_RELAYER_ABI, "GPv2VaultRelayer", GPV2_VAULT_RELAYER
        )

        owner = boa.env.generate_address("owner")
        emergency_owner = boa.env.generate_address("emergency_owner")
        keeper = boa.env.generate_address("keeper")
        simulated_solver = boa.env.generate_address("simulated_solver")

        handler = boa.load("contracts/burners/cow/WatchtowerHandler.vy")
        erc20 = boa.load_partial("contracts/testing/ERC20Mock.vy")
        target = erc20.deploy("Fork Target", "TARGET", 18)
        sell_token = erc20.deploy("Fork Sell Token", "SELL", 18)
        weth = boa.load("contracts/testing/WETH.vy")
        fee_collector = boa.load(
            "contracts/FeeCollector.vy", target, weth, owner, emergency_owner
        )
        registry = boa.load(
            "contracts/burners/auction/adapters/AdapterRegistry.vy", fee_collector.address
        )
        cow_adapter = boa.load(
            "contracts/burners/cow/CowAdapter.vy",
            GPV2_SETTLEMENT,
            APP_DATA,
            COW_ORDER_VALIDITY,
        )
        burner = boa.load(
            "contracts/burners/DutchAuctionBurner.vy",
            fee_collector,
            START_TOTAL,
            FLOOR_TOTAL,
            DECAY_FACTOR_RAY,
            STEP_DURATION,
            registry,
        )
        with boa.env.prank(owner):
            fee_collector.set_burner(burner)
            fee_collector.set_killed([(ZERO_ADDRESS, 0)])
            registry.set_adapter(cow_adapter, cow_adapter.vault_relayer())
            registry.activate_adapter(cow_adapter)
            burner.enable_adapter(cow_adapter)
            burner.set_fallback_adapter(cow_adapter)
            burner.configure_watchtower(COMPOSABLE_COW, handler)

        # The relayer and domain separator are read from the real settlement.
        assert cow_adapter.vault_relayer() == GPV2_VAULT_RELAYER
        assert bytes(cow_adapter.domain_separator()) == bytes(
            settlement.domainSeparator()
        )
        assert burner.cow_enabled()
        assert burner.supportsInterface(BURNER_INTERFACE)
        # The generator interface lives on the standalone handler now.
        assert not burner.supportsInterface(CONDITIONAL_ORDER_INTERFACE)
        assert handler.supportsInterface(CONDITIONAL_ORDER_INTERFACE)

        _move_to_epoch(fee_collector, Epoch.COLLECT)
        amount = 1_000 * WAD
        sell_token._mint_for_testing(fee_collector, amount)
        with boa.env.prank(keeper):
            fee_collector.collect([sell_token.address], keeper)

        lot = burner.lots(sell_token)
        # The contract stores no time bounds; extend the record so LOT_START
        # and LOT_END keep indexing the epoch window.
        lot = (*lot, *burner.epoch_bounds(lot[0]))
        assert lot[LOT_INITIAL_AMOUNT] > 0
        assert sell_token.allowance(burner, GPV2_VAULT_RELAYER) == MAX_UINT256
        _move_to_timestamp(lot[LOT_START])

        generation = burner.cow_generation()
        static_input = _static_input(sell_token, generation)
        params = (handler.address, ZERO_BYTES32, static_input)
        order, signature = composable_cow.getTradeableOrderWithSignature(
            burner.address, params, b"", []
        )

        assert order[ORDER_SELL_TOKEN] == sell_token.address
        assert order[ORDER_BUY_TOKEN] == target.address
        assert order[ORDER_RECEIVER] == fee_collector.address
        assert order[ORDER_SELL_AMOUNT] == lot[LOT_INITIAL_AMOUNT]
        assert order[ORDER_BUY_AMOUNT] == burner.getAmountNeeded(
            sell_token, order[ORDER_SELL_AMOUNT]
        )
        assert order[ORDER_VALID_TO] <= lot[LOT_END]
        assert order[ORDER_FEE_AMOUNT] == 0
        assert order[ORDER_KIND] == SELL_KIND
        assert order[ORDER_PARTIALLY_FILLABLE]
        assert order[ORDER_SELL_BALANCE] == ERC20_BALANCE
        assert order[ORDER_BUY_BALANCE] == ERC20_BALANCE

        order_digest = _gpv2_order_digest(order, settlement.domainSeparator())
        with boa.env.prank(GPV2_SETTLEMENT):
            assert burner.isValidSignature(order_digest, signature) == ERC1271_MAGIC_VALUE
            # Yearn-style self-published rail: the bare abi-encoded order is a
            # valid ERC-1271 signature all by itself — no registration needed.
            bare_signature = encode(
                [
                    "(address,address,address,uint256,uint256,uint32,bytes32,"
                    "uint256,bytes32,bool,bytes32,bytes32)"
                ],
                [tuple(order)],
            )
            assert (
                burner.isValidSignature(order_digest, bare_signature)
                == ERC1271_MAGIC_VALUE
            )

        # The real relayer enforces Settlement-only access and spends the burner's allowance.
        partial_amount = order[ORDER_SELL_AMOUNT] // 3
        with boa.env.prank(simulated_solver), boa.reverts():
            vault_relayer.transferFromAccounts(
                [(burner.address, sell_token.address, partial_amount, ERC20_BALANCE)]
            )
        with boa.env.prank(GPV2_SETTLEMENT):
            vault_relayer.transferFromAccounts(
                [(burner.address, sell_token.address, partial_amount, ERC20_BALANCE)]
            )
            sell_token.transfer(simulated_solver, partial_amount)
        assert sell_token.balanceOf(simulated_solver) == partial_amount
        assert sell_token.balanceOf(GPV2_SETTLEMENT) == 0
        assert sell_token.balanceOf(burner) == lot[LOT_INITIAL_AMOUNT] - partial_amount
        assert (
            sell_token.allowance(burner, GPV2_VAULT_RELAYER)
            == MAX_UINT256 - partial_amount
        )
        assert burner.available(sell_token) == lot[LOT_INITIAL_AMOUNT] - partial_amount

        # The allowance-guarded sync only re-grants from zero: the decremented
        # allowance (ERC20Mock has no infinite-allowance special case) is
        # deliberately left untouched — it is still effectively unlimited.
        with boa.env.prank(keeper):
            burner.sync_executor_approvals(GPV2_VAULT_RELAYER, [sell_token.address])
        assert (
            sell_token.allowance(burner, GPV2_VAULT_RELAYER)
            == MAX_UINT256 - partial_amount
        )

        payment = burner.getAmountNeeded(sell_token, partial_amount)
        target._mint_for_testing(simulated_solver, payment)
        with boa.env.prank(simulated_solver):
            target.transfer(fee_collector, payment)
        assert target.balanceOf(fee_collector) == payment

        # A solver cannot turn the production-compatible signature into a below-curve order.
        invalid_order = list(order)
        invalid_order[ORDER_BUY_AMOUNT] = order[ORDER_BUY_AMOUNT] - 1
        domain_separator = settlement.domainSeparator()
        invalid_digest = _gpv2_order_digest(invalid_order, domain_separator)
        with boa.reverts():
            handler.verify(
                burner.address,
                GPV2_SETTLEMENT,
                invalid_digest,
                domain_separator,
                ZERO_BYTES32,
                static_input,
                b"",
                invalid_order,
            )
