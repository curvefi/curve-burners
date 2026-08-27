from __future__ import annotations

from typing import Any

import pytest
from eth_abi import encode
from eth_utils import keccak

from scripts import dutch_auction_preflight as preflight


CHAIN_ID = 100
FEE_COLLECTOR = "0x0000000000000000000000000000000000000001"
TARGET = "0x0000000000000000000000000000000000000002"
COMPOSABLE_COW = "0x0000000000000000000000000000000000000003"
SETTLEMENT = "0x0000000000000000000000000000000000000004"
VAULT_RELAYER = "0x0000000000000000000000000000000000000005"
BURNER = "0x0000000000000000000000000000000000000006"
OWNER = "0x0000000000000000000000000000000000000007"
EMERGENCY_OWNER = "0x0000000000000000000000000000000000000008"
REGISTRY = "0x0000000000000000000000000000000000000009"
VERIFIER = "0x000000000000000000000000000000000000000b"
EXECUTOR = "0x000000000000000000000000000000000000000E"
COW_ADAPTER = "0x000000000000000000000000000000000000000F"
SELL_TOKEN = "0x000000000000000000000000000000000000000C"
HANDLER = "0x000000000000000000000000000000000000000D"
APP_DATA = "0x" + "11" * 32
DOMAIN_SEPARATOR = bytes.fromhex("22" * 32)
FAKE_CODE = b"\x60\x00"
FAKE_CODE_HASH = keccak(FAKE_CODE)


class FakeRpc:
    def __init__(
        self,
        *,
        settlement_domain_separator: bytes = DOMAIN_SEPARATOR,
        cancun_error: bool = False,
    ):
        self.settlement_domain_separator = settlement_domain_separator
        self.cancun_error = cancun_error
        self.read_addresses: list[str] = []
        self.probe_code: bytes | None = None

    def chain_id(self) -> int:
        return CHAIN_ID

    def code(self, address: str) -> bytes:
        return FAKE_CODE

    def latest_timestamp(self) -> int:
        return 1

    def eth_create_call(self, init_code: bytes) -> bytes:
        self.probe_code = init_code
        if self.cancun_error:
            raise preflight.PreflightError("invalid opcode 0x5d")
        return preflight.CANCUN_PROBE_RESULT

    def eth_call(self, address: str, data: bytes) -> bytes:
        self.read_addresses.append(address)
        selector = data[:4]

        if selector == preflight._selector("target()"):
            value = TARGET
            return encode(["address"], [value])
        if selector == preflight._selector("owner()"):
            return encode(["address"], [OWNER])
        if selector == preflight._selector("emergency_owner()"):
            return encode(["address"], [EMERGENCY_OWNER])
        if selector == preflight._selector("decimals()"):
            return encode(["uint256"], [18])
        if selector == preflight._selector("epoch_time_frame(uint256,uint256)"):
            return encode(["uint256", "uint256"], [0, 2])
        if selector == preflight._selector("domainSeparator()"):
            value = (
                self.settlement_domain_separator
                if address == SETTLEMENT
                else DOMAIN_SEPARATOR
            )
            return encode(["bytes32"], [value])
        if selector == preflight._selector("vaultRelayer()"):
            return encode(["address"], [VAULT_RELAYER])
        if selector == preflight._selector("supportsInterface(bytes4)"):
            interface_id = data[4:8]
            if (
                address == BURNER
                and interface_id == preflight.CONDITIONAL_ORDER_INTERFACE_ID
            ):
                # The burner never claims the generator interface; the
                # standalone handler does.
                return encode(["bool"], [False])
            return encode(["bool"], [True])
        if selector == preflight._selector("fee_collector()"):
            return encode(["address"], [FEE_COLLECTOR])
        if selector == preflight._selector("app_data()"):
            assert address == COW_ADAPTER
            return encode(["bytes32"], [bytes.fromhex(APP_DATA[2:])])
        if selector == preflight._selector("start_total()"):
            return encode(["uint256"], [1])
        if selector == preflight._selector("floor_total()"):
            return encode(["uint256"], [1])
        if selector == preflight._selector("decay_factor_ray()"):
            return encode(["uint256"], [preflight.RAY - 1])
        if selector in {
            preflight._selector("step_duration()"),
            preflight._selector("cow_generation()"),
        }:
            return encode(["uint256"], [1])
        if selector == preflight._selector("order_validity()"):
            assert address == COW_ADAPTER
            return encode(["uint256"], [1])
        if selector == preflight._selector("cow_enabled()"):
            return encode(["bool"], [True])
        if selector == preflight._selector("composable_cow()"):
            return encode(["address"], [COMPOSABLE_COW])
        if selector == preflight._selector("vault_relayer()"):
            assert address == COW_ADAPTER
            return encode(["address"], [VAULT_RELAYER])
        if selector == preflight._selector("settlement()"):
            assert address == COW_ADAPTER
            return encode(["address"], [SETTLEMENT])
        if selector == preflight._selector("cow_handler()"):
            return encode(["address"], [HANDLER])
        if selector == preflight._selector("fallback_adapter()"):
            return encode(["address"], [COW_ADAPTER])
        if selector == preflight._selector("domain_separator()"):
            assert address == COW_ADAPTER
            return encode(["bytes32"], [self.settlement_domain_separator])
        if selector == preflight._selector("registry()"):
            return encode(["address"], [REGISTRY])
        if selector == preflight._selector("executor_refcount(address)"):
            return encode(["uint256"], [1])
        if selector == preflight._selector("enabled_adapters(address)"):
            return encode(["bool"], [True])
        if selector == preflight._selector("get_adapter(address)"):
            return encode(["address", "bool"], [EXECUTOR, True])
        raise AssertionError(f"unexpected eth_call to {address}: 0x{data.hex()}")


def _full_config() -> dict[str, Any]:
    return {
        "chainId": CHAIN_ID,
        "feeCollector": FEE_COLLECTOR,
        "target": TARGET,
        "targetDecimals": 18,
        "cowEnabled": True,
        "composableCow": COMPOSABLE_COW,
        "settlement": SETTLEMENT,
        "vaultRelayer": VAULT_RELAYER,
        "handler": HANDLER,
        "appData": APP_DATA,
        "defaultX": 1,
        "floor": 1,
        "decayFactorRay": preflight.RAY - 1,
        "stepDuration": 1,
        "cowOrderValidity": 1,
        "owner": OWNER,
        "emergencyOwner": EMERGENCY_OWNER,
        "burner": BURNER,
        "registry": REGISTRY,
        "cowAdapter": COW_ADAPTER,
        "adapters": [
            {
                "verifier": VERIFIER,
                "executor": EXECUTOR,
                "verifierCodeHash": "0x" + FAKE_CODE_HASH.hex(),
            }
        ],
    }


def test_full_preflight_preserves_existing_checks_and_adds_cancun_probe():
    rpc = FakeRpc()

    report = preflight.run_preflight(rpc, _full_config())

    assert not report.errors
    assert report.checks["evm.cancunOpcodes"] is True
    assert rpc.probe_code == preflight.CANCUN_PROBE_INIT_CODE
    assert report.checks["burner.interface.erc1271"] is True
    assert report.checks["burner.interface.conditionalOrder"] is False
    assert report.checks["handler.interface.conditionalOrder"] is True
    assert report.checks["burner.fallbackAdapter"] == COW_ADAPTER
    assert report.checks["cowAdapter.settlement"] == SETTLEMENT
    assert report.checks["cowAdapter.vaultRelayer"] == preflight.to_checksum_address(
        VAULT_RELAYER
    )
    assert report.checks["burner.cowHandler"] == preflight.to_checksum_address(HANDLER)
    assert report.checks["burner.registry"] == REGISTRY
    assert report.checks["burner.vaultRelayerRefcount.positive"] is True
    adapter_label = f"adapter.{preflight.to_checksum_address(VERIFIER)}"
    assert report.checks[f"{adapter_label}.enabled"] is True
    assert report.checks[f"{adapter_label}.active"] is True
    assert report.checks[f"{adapter_label}.executor"] == preflight.to_checksum_address(
        EXECUTOR
    )
    assert report.checks[f"{adapter_label}.pinnedCodeHash"] == (
        "0x" + FAKE_CODE_HASH.hex()
    )
    assert report.checks[f"{adapter_label}.executorRefcount.positive"] is True


def test_full_preflight_check_count_is_pinned():
    report = preflight.run_preflight(FakeRpc(), _full_config())
    assert not report.errors
    assert len(report.checks) == 61


def test_cow_domain_separator_mismatch_is_an_error():
    report = preflight.run_preflight(
        FakeRpc(settlement_domain_separator=bytes.fromhex("33" * 32)),
        _full_config(),
    )

    assert any(
        error.startswith("composableCow.domainSeparator:")
        and "must be nonzero and exactly equal Settlement" in error
        for error in report.errors
    )


def test_native_only_cancun_probe_failure_is_an_error_without_cow_reads():
    rpc = FakeRpc(cancun_error=True)
    config = {
        "chainId": CHAIN_ID,
        "feeCollector": FEE_COLLECTOR,
        "target": TARGET,
        "targetDecimals": 18,
        "cowEnabled": False,
        "appData": APP_DATA,
    }

    report = preflight.run_preflight(rpc, config)

    assert report.checks["evm.cancunOpcodes"] is False
    assert any(
        "TSTORE/TLOAD/MCOPY creation probe failed" in error
        for error in report.errors
    )
    assert preflight.ZERO_ADDRESS not in rpc.read_addresses


def test_lifecycle_calldata_covers_cow_adapters_and_executor_sync():
    calls = preflight.lifecycle_calldata(
        _full_config(), BURNER, VAULT_RELAYER, [SELL_TOKEN]
    )

    assert [call["function"] for call in calls["configuration"]] == [
        "enable_adapter(address)",
        "set_fallback_adapter(address)",
        "configure_watchtower(address,address)",
        "enable_adapter(address)",
    ]
    assert calls["configuration"][0]["data"] == preflight.encode_call(
        "enable_adapter(address)", ["address"], [COW_ADAPTER]
    )
    assert calls["configuration"][1]["data"] == preflight.encode_call(
        "set_fallback_adapter(address)", ["address"], [COW_ADAPTER]
    )
    assert calls["configuration"][2]["data"] == preflight.encode_call(
        "configure_watchtower(address,address)",
        ["address", "address"],
        [COMPOSABLE_COW, HANDLER],
    )
    assert [call["function"] for call in calls["emergency"]] == [
        "disable_adapter(address)",
        "disable_adapter(address)",
    ]
    assert [call["function"] for call in calls["permissionless"]] == [
        "sync_executor_approvals(address,address[])",
    ]
    assert all(
        call["to"] == BURNER
        for group in calls.values()
        for call in group
    )

    assert calls["configuration"][3]["data"] == preflight.encode_call(
        "enable_adapter(address)", ["address"], [preflight.to_checksum_address(VERIFIER)]
    )
    assert calls["emergency"][1]["data"] == preflight.encode_call(
        "disable_adapter(address)", ["address"], [preflight.to_checksum_address(VERIFIER)]
    )
    assert calls["permissionless"][0]["data"] == preflight.encode_call(
        "sync_executor_approvals(address,address[])",
        ["address", "address[]"],
        [preflight.to_checksum_address(VAULT_RELAYER), [
            preflight.to_checksum_address(SELL_TOKEN)
        ]],
    )


def test_lifecycle_calldata_requires_executor_and_tokens_together():
    with pytest.raises(ValueError, match="--executor and at least one --token"):
        preflight.lifecycle_calldata(_full_config(), BURNER, VAULT_RELAYER, [])
    with pytest.raises(ValueError, match="--executor and at least one --token"):
        preflight.lifecycle_calldata(_full_config(), BURNER, None, [SELL_TOKEN])
