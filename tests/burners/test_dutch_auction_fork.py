from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import boa
import pytest
from dotenv import load_dotenv

from tests.burners.conftest import custom_err
from tests.burners.test_dutch_auction_v2 import (
    APP_DATA,
    BURNER_INTERFACE,
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
    _encode_erc1271_signature,
    _gpv2_order_digest,
    _move_to_epoch,
    _move_to_timestamp,
)
from tests.conftest import ZERO_ADDRESS, Epoch

# Official deterministic deployments shared by Gnosis Chain.
GPV2_SETTLEMENT = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"
GPV2_VAULT_RELAYER = "0xC92E8bdf79f0507f65a392b0ab4667716BFE0110"

RPC_ENV_KEYS = (
    "GNOSIS_RPC_URL",
    "FORK_GNOSIS_URL",
    "GNOSIS_RPC",
    "GNOSIS_URL",
)

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


def _abi_contract(abi: list[dict[str, Any]], name: str, address: str) -> Any:
    return boa.loads_abi(json.dumps(abi), name=name).at(address)


def test_gnosis_real_gpv2_signature_and_vault_relayer_custody():
    """Production-like Gnosis fork simulation of the CoW rail.

    Real components: GPv2Settlement (domain separator, ERC-1271 caller) and
    GPv2VaultRelayer (allowance spend, Settlement-only access control); the
    order and its eip1271 signature (`adapter ++ abi.encode(order)`) are built
    the way a publisher posts them to the CoW orderbook.
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

        for address in (GPV2_SETTLEMENT, GPV2_VAULT_RELAYER):
            assert boa.env.get_code(address), f"Missing deployed code at {address}"

        settlement = _abi_contract(SETTLEMENT_ABI, "GPv2Settlement", GPV2_SETTLEMENT)
        vault_relayer = _abi_contract(VAULT_RELAYER_ABI, "GPv2VaultRelayer", GPV2_VAULT_RELAYER)

        owner = boa.env.generate_address("owner")
        emergency_owner = boa.env.generate_address("emergency_owner")
        keeper = boa.env.generate_address("keeper")
        simulated_solver = boa.env.generate_address("simulated_solver")

        erc20 = boa.load_partial("contracts/testing/ERC20Mock.vy")
        target = erc20.deploy("Fork Target", "TARGET", 18)
        sell_token = erc20.deploy("Fork Sell Token", "SELL", 18)
        weth = boa.load("contracts/testing/WETH.vy")
        fee_collector = boa.load("contracts/FeeCollector.vy", target, weth, owner, emergency_owner)
        registry = boa.load("contracts/burners/adapters/AdapterRegistry.vy", fee_collector.address)
        cow_adapter = boa.load(
            "contracts/burners/adapters/cow/CowAdapter.vy", GPV2_SETTLEMENT, APP_DATA
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
            # The registry is the single management point: the burner holds no
            # adapter state and routes iff the adapter is active here.
            registry.set_adapter(cow_adapter, cow_adapter.vault_relayer())
            registry.activate_adapter(cow_adapter)
        assert registry.is_executor_active(GPV2_VAULT_RELAYER)

        # The relayer and domain separator are read from the real settlement.
        assert cow_adapter.vault_relayer() == GPV2_VAULT_RELAYER
        assert bytes(cow_adapter.domain_separator()) == bytes(settlement.domainSeparator())
        assert burner.supportsInterface(BURNER_INTERFACE)
        assert burner.supportsInterface(ERC1271_MAGIC_VALUE)

        _move_to_epoch(fee_collector, Epoch.COLLECT)
        amount = 1_000 * WAD
        sell_token._mint_for_testing(fee_collector, amount)
        with boa.env.prank(keeper):
            fee_collector.collect([sell_token.address], keeper)

        lot = (*burner.window(sell_token), burner.lots(sell_token).initial_amount)
        assert lot[LOT_INITIAL_AMOUNT] > 0
        # Staging grants nothing; the keeper's permissionless sync gives the
        # real vault relayer (the active registry adapter's executor) its allowance.
        assert sell_token.allowance(burner, GPV2_VAULT_RELAYER) == 0
        with boa.env.prank(keeper):
            burner.sync_executor_approvals(GPV2_VAULT_RELAYER, [sell_token.address])
        assert sell_token.allowance(burner, GPV2_VAULT_RELAYER) == MAX_UINT256
        _move_to_timestamp(lot[LOT_START])

        # What a publisher posts to the CoW orderbook for this lot, straight
        # from the adapter's helper: sell everything available at the live
        # quote, proceeds to the FeeCollector, `adapter ++ abi.encode(order)`.
        available = burner.available(sell_token)
        order, signature = cow_adapter.order_for(burner.address, sell_token.address)
        order = tuple(order)
        signature = bytes(signature)
        assert signature == _encode_erc1271_signature(order, cow_adapter)

        assert order[ORDER_SELL_TOKEN] == sell_token.address
        assert order[ORDER_BUY_TOKEN] == target.address
        assert order[ORDER_RECEIVER] == fee_collector.address
        assert order[ORDER_SELL_AMOUNT] == lot[LOT_INITIAL_AMOUNT] == available
        assert order[ORDER_BUY_AMOUNT] == burner.getAmountNeeded(
            sell_token, order[ORDER_SELL_AMOUNT]
        )
        assert order[ORDER_VALID_TO] == lot[LOT_END]
        assert order[ORDER_FEE_AMOUNT] == 0
        assert order[ORDER_KIND] == SELL_KIND
        assert order[ORDER_PARTIALLY_FILLABLE]
        assert order[ORDER_SELL_BALANCE] == ERC20_BALANCE
        assert order[ORDER_BUY_BALANCE] == ERC20_BALANCE

        order_digest = _gpv2_order_digest(order, settlement.domainSeparator())
        with boa.env.prank(GPV2_SETTLEMENT):
            assert burner.isValidSignature(order_digest, signature) == ERC1271_MAGIC_VALUE
            # Anyone may publish, nothing may bypass the prefix route: the
            # unprefixed encoding selects no adapter.
            assert burner.isValidSignature(order_digest, signature[20:]) == bytes.fromhex(
                "ffffffff"
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
        assert sell_token.allowance(burner, GPV2_VAULT_RELAYER) == MAX_UINT256 - partial_amount
        assert burner.available(sell_token) == lot[LOT_INITIAL_AMOUNT] - partial_amount

        # The allowance-guarded sync only re-grants from zero: the decremented
        # allowance (ERC20Mock has no infinite-allowance special case) is
        # deliberately left untouched — it is still effectively unlimited.
        with boa.env.prank(keeper):
            burner.sync_executor_approvals(GPV2_VAULT_RELAYER, [sell_token.address])
        assert sell_token.allowance(burner, GPV2_VAULT_RELAYER) == MAX_UINT256 - partial_amount

        payment = burner.getAmountNeeded(sell_token, partial_amount)
        target._mint_for_testing(simulated_solver, payment)
        with boa.env.prank(simulated_solver):
            target.transfer(fee_collector, payment)
        assert target.balanceOf(fee_collector) == payment

        # A solver cannot turn the published signature into a below-curve order:
        # the digest changes with the order, and an underpriced order of its
        # own fails the live curve check.
        invalid_order = list(order)
        invalid_order[ORDER_BUY_AMOUNT] = order[ORDER_BUY_AMOUNT] - 1
        invalid_digest = _gpv2_order_digest(invalid_order, settlement.domainSeparator())
        with boa.env.prank(GPV2_SETTLEMENT):
            with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
                burner.isValidSignature(invalid_digest, signature)
            with boa.reverts(custom_err("BadBuyAmount()")):
                burner.isValidSignature(
                    invalid_digest, _encode_erc1271_signature(invalid_order, cow_adapter)
                )
