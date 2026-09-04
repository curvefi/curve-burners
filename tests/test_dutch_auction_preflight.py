from __future__ import annotations

from typing import Any

import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from scripts import dutch_auction_preflight as preflight


CHAIN_ID = 100
FEE_COLLECTOR = "0x0000000000000000000000000000000000000001"
TARGET = "0x0000000000000000000000000000000000000002"
SETTLEMENT = "0x0000000000000000000000000000000000000004"
VAULT_RELAYER = "0x0000000000000000000000000000000000000005"
BURNER = "0x0000000000000000000000000000000000000006"
OWNER = "0x0000000000000000000000000000000000000007"
EMERGENCY_OWNER = "0x0000000000000000000000000000000000000008"
REGISTRY = "0x0000000000000000000000000000000000000009"
ADAPTER = "0x000000000000000000000000000000000000000b"
EXECUTOR = "0x000000000000000000000000000000000000000E"
SELL_TOKEN = "0x000000000000000000000000000000000000000C"
COW_ADAPTER = "0x000000000000000000000000000000000000000F"
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
            # Only the FeeCollector exposes target(); the burner's alias is gone.
            assert address == FEE_COLLECTOR
            return encode(["address"], [TARGET])
        if selector == preflight._selector("want()"):
            assert address == BURNER
            return encode(["address"], [TARGET])
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
        if selector == preflight._selector("step_duration()"):
            return encode(["uint256"], [1])
        if selector == preflight._selector("vault_relayer()"):
            assert address == COW_ADAPTER
            return encode(["address"], [VAULT_RELAYER])
        if selector == preflight._selector("settlement()"):
            assert address == COW_ADAPTER
            return encode(["address"], [SETTLEMENT])
        if selector == preflight._selector("domain_separator()"):
            # The adapter pinned the canonical domain at deploy; a settlement
            # answering something else is exactly the mismatch to catch.
            assert address == COW_ADAPTER
            return encode(["bytes32"], [DOMAIN_SEPARATOR])
        if selector == preflight._selector("registry()"):
            return encode(["address"], [REGISTRY])
        if selector == preflight._selector("is_executor_active(address)"):
            # Adapter state is registry-only: the burner has no executor view.
            assert address == REGISTRY
            return encode(["bool"], [True])
        if selector == preflight._selector("get_adapters()"):
            assert address == REGISTRY
            return encode(["address[]"], [[COW_ADAPTER, ADAPTER]])
        if selector == preflight._selector("get_adapter(address)"):
            assert address == REGISTRY
            (adapter,) = decode(["address"], data[4:])
            if adapter.lower() == COW_ADAPTER.lower():
                return encode(["address", "bool"], [VAULT_RELAYER, True])
            return encode(["address", "bool"], [EXECUTOR, True])
        raise AssertionError(f"unexpected eth_call to {address}: 0x{data.hex()}")


def _full_config() -> dict[str, Any]:
    return {
        "chainId": CHAIN_ID,
        "feeCollector": FEE_COLLECTOR,
        "target": TARGET,
        "targetDecimals": 18,
        "cowEnabled": True,
        "settlement": SETTLEMENT,
        "vaultRelayer": VAULT_RELAYER,
        "cowAdapter": COW_ADAPTER,
        "appData": APP_DATA,
        "start_total": 1,
        "floor_total": 1,
        "decay_factor_ray": preflight.RAY - 1,
        "step_duration": 1,
        "owner": OWNER,
        "emergencyOwner": EMERGENCY_OWNER,
        "burner": BURNER,
        "registry": REGISTRY,
        "adapters": [
            {"adapter": COW_ADAPTER, "executor": VAULT_RELAYER},
            {"adapter": ADAPTER, "executor": EXECUTOR},
        ],
        # Adapter code is pinned like any other contract: by name or address.
        "expectedCodeHashes": {ADAPTER: "0x" + FAKE_CODE_HASH.hex()},
    }


def test_full_preflight_preserves_existing_checks_and_adds_cancun_probe():
    rpc = FakeRpc()

    report = preflight.run_preflight(rpc, _full_config())

    assert not report.errors
    assert report.checks["evm.cancunOpcodes"] is True
    assert rpc.probe_code == preflight.CANCUN_PROBE_INIT_CODE
    assert report.checks["burner.interface.erc1271"] is True
    assert report.checks["cowAdapter.settlement"] == SETTLEMENT
    assert report.checks["cowAdapter.vaultRelayer"] == preflight.to_checksum_address(
        VAULT_RELAYER
    )
    assert report.checks["cowAdapter.domainSeparator"] is True
    assert report.checks["burner.registry"] == REGISTRY
    assert report.checks["burner.want"] == TARGET
    assert "burner.target" not in report.checks
    cow_label = f"adapter.{preflight.to_checksum_address(COW_ADAPTER)}"
    assert f"{cow_label}.enabled" not in report.checks
    assert report.checks[f"{cow_label}.active"] is True
    assert report.checks[f"{cow_label}.executor"] == preflight.to_checksum_address(
        VAULT_RELAYER
    )
    assert report.checks[f"{cow_label}.registered"] is True
    assert report.checks[f"{cow_label}.executorActive"] is True
    assert report.checks["registry.adapters"] == [
        preflight.to_checksum_address(COW_ADAPTER),
        preflight.to_checksum_address(ADAPTER),
    ]
    adapter_label = f"adapter.{preflight.to_checksum_address(ADAPTER)}"
    assert report.checks[f"{adapter_label}.active"] is True
    assert report.checks[f"{adapter_label}.executor"] == preflight.to_checksum_address(
        EXECUTOR
    )
    assert f"{adapter_label}.pinnedCodeHash" not in report.checks
    assert report.checks["codeHash.adapters[1].adapter"] == "0x" + FAKE_CODE_HASH.hex()
    assert report.checks[f"{adapter_label}.registered"] is True
    assert report.checks[f"{adapter_label}.executorActive"] is True
    assert not any("not in configuration" in warning for warning in report.warnings)


def test_full_preflight_check_keys_are_pinned():
    report = preflight.run_preflight(FakeRpc(), _full_config())
    assert not report.errors

    contracts = [
        "feeCollector",
        "target",
        "settlement",
        "vaultRelayer",
        "cowAdapter",
        "burner",
        "registry",
        "adapters[0].adapter",
        "adapters[1].adapter",
    ]
    expected = {"chainId", "evm.cancunOpcodes"}
    expected |= {f"code.{name}" for name in contracts}
    expected |= {f"codeHash.{name}" for name in contracts}
    expected |= {
        "feeCollector.target",
        "feeCollector.owner",
        "feeCollector.emergencyOwner",
        "target.decimals",
        "curve.activeSteps",
        "curve.activeEndPrice",
        "cowAdapter.settlement",
        "cowAdapter.vaultRelayer",
        "settlement.vaultRelayer",
        "cowAdapter.appData",
        "cowAdapter.domainSeparator",
        "burner.interface.erc165",
        "burner.interface.burner",
        "burner.feeCollector",
        "burner.want",
        "burner.start_total",
        "burner.floor_total",
        "burner.decay_factor_ray",
        "burner.step_duration",
        "burner.interface.erc1271",
        "burner.registry",
        "registry.adapters",
    }
    for adapter in (COW_ADAPTER, ADAPTER):
        label = f"adapter.{preflight.to_checksum_address(adapter)}"
        expected |= {
            f"{label}.registered",
            f"{label}.executor",
            f"{label}.active",
            f"{label}.executorActive",
        }
    assert set(report.checks) == expected


def test_adapter_checks_read_the_registry_without_a_burner():
    rpc = FakeRpc()
    config = _full_config()
    del config["burner"]

    report = preflight.run_preflight(rpc, config)

    assert not report.errors
    assert BURNER not in rpc.read_addresses
    assert REGISTRY in rpc.read_addresses
    adapter_label = f"adapter.{preflight.to_checksum_address(ADAPTER)}"
    assert report.checks[f"{adapter_label}.active"] is True
    assert report.checks[f"{adapter_label}.executorActive"] is True


def test_cow_domain_separator_mismatch_is_an_error():
    report = preflight.run_preflight(
        FakeRpc(settlement_domain_separator=bytes.fromhex("33" * 32)),
        _full_config(),
    )

    assert any(
        error.startswith("cowAdapter.domainSeparator:") for error in report.errors
    )


def test_cow_adapter_must_be_a_registered_adapter():
    config = _full_config()
    config["adapters"] = config["adapters"][1:]
    with pytest.raises(ValueError, match="cowAdapter must be listed in adapters"):
        preflight.validate_config(config)


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
    assert SETTLEMENT not in rpc.read_addresses
    assert COW_ADAPTER not in rpc.read_addresses


def test_lifecycle_calldata_covers_adapters_and_executor_sync():
    calls = preflight.lifecycle_calldata(
        _full_config(), BURNER, VAULT_RELAYER, [SELL_TOKEN]
    )

    # The CoW adapter is set, activated and disabled in the registry like any
    # other adapter (set before activate); only the allowance sync targets the
    # burner.
    assert [call["function"] for call in calls["configuration"]] == [
        "set_adapter(address,address)",
        "activate_adapter(address)",
        "set_adapter(address,address)",
        "activate_adapter(address)",
    ]
    assert calls["configuration"][0]["data"] == preflight.encode_call(
        "set_adapter(address,address)",
        ["address", "address"],
        [
            preflight.to_checksum_address(COW_ADAPTER),
            preflight.to_checksum_address(VAULT_RELAYER),
        ],
    )
    assert calls["configuration"][1]["data"] == preflight.encode_call(
        "activate_adapter(address)", ["address"], [preflight.to_checksum_address(COW_ADAPTER)]
    )
    assert calls["configuration"][2]["data"] == preflight.encode_call(
        "set_adapter(address,address)",
        ["address", "address"],
        [preflight.to_checksum_address(ADAPTER), preflight.to_checksum_address(EXECUTOR)],
    )
    assert calls["configuration"][3]["data"] == preflight.encode_call(
        "activate_adapter(address)", ["address"], [preflight.to_checksum_address(ADAPTER)]
    )
    assert [call["function"] for call in calls["emergency"]] == [
        "disable_adapter(address)",
        "disable_adapter(address)",
    ]
    assert calls["emergency"][0]["data"] == preflight.encode_call(
        "disable_adapter(address)", ["address"], [preflight.to_checksum_address(COW_ADAPTER)]
    )
    assert [call["function"] for call in calls["permissionless"]] == [
        "sync_executor_approvals(address,address[])",
    ]
    assert all(call["to"] == REGISTRY for call in calls["configuration"])
    assert all(call["to"] == REGISTRY for call in calls["emergency"])
    assert all(call["to"] == BURNER for call in calls["permissionless"])
    assert calls["permissionless"][0]["data"] == preflight.encode_call(
        "sync_executor_approvals(address,address[])",
        ["address", "address[]"],
        [preflight.to_checksum_address(VAULT_RELAYER), [
            preflight.to_checksum_address(SELL_TOKEN)
        ]],
    )


def test_lifecycle_calldata_requires_burner_executor_and_tokens_together():
    match = "--burner, --executor and at least one --token"
    with pytest.raises(ValueError, match=match):
        preflight.lifecycle_calldata(_full_config(), BURNER, VAULT_RELAYER, [])
    with pytest.raises(ValueError, match=match):
        preflight.lifecycle_calldata(_full_config(), BURNER, None, [SELL_TOKEN])
    with pytest.raises(ValueError, match=match):
        preflight.lifecycle_calldata(_full_config(), None, VAULT_RELAYER, [SELL_TOKEN])

    # Registry calldata alone needs no burner: only the sync leg targets it.
    calls = preflight.lifecycle_calldata(_full_config(), None, None, [])
    assert calls["permissionless"] == []
    assert len(calls["configuration"]) == 4
