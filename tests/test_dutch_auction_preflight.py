from __future__ import annotations

from typing import Any

from eth_abi import encode

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
APP_DATA = "0x" + "11" * 32
DOMAIN_SEPARATOR = bytes.fromhex("22" * 32)


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
        return b"\x60\x00"

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
            return encode(["bool"], [True])
        if selector == preflight._selector("fee_collector()"):
            return encode(["address"], [FEE_COLLECTOR])
        if selector == preflight._selector("app_data()"):
            return encode(["bytes32"], [bytes.fromhex(APP_DATA[2:])])
        if selector == preflight._selector("start_total()"):
            return encode(["uint256"], [1])
        if selector == preflight._selector("floor_total()"):
            return encode(["uint256"], [1])
        if selector == preflight._selector("decay_factor_ray()"):
            return encode(["uint256"], [preflight.RAY - 1])
        if selector in {
            preflight._selector("step_duration()"),
            preflight._selector("cow_order_validity()"),
            preflight._selector("cow_generation()"),
        }:
            return encode(["uint256"], [1])
        if selector == preflight._selector("cow_enabled()"):
            return encode(["bool"], [True])
        if selector == preflight._selector("composable_cow()"):
            return encode(["address"], [COMPOSABLE_COW])
        if selector == preflight._selector("vault_relayer()"):
            return encode(["address"], [VAULT_RELAYER])
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
        "appData": APP_DATA,
        "defaultX": 1,
        "floor": 1,
        "decayFactorRay": preflight.RAY - 1,
        "stepDuration": 1,
        "cowOrderValidity": 1,
        "owner": OWNER,
        "emergencyOwner": EMERGENCY_OWNER,
        "burner": BURNER,
    }


def test_full_preflight_preserves_existing_checks_and_adds_cancun_probe():
    rpc = FakeRpc()

    report = preflight.run_preflight(rpc, _full_config())

    assert not report.errors
    assert len(report.checks) == 40
    assert report.checks["evm.cancunOpcodes"] is True
    assert rpc.probe_code == preflight.CANCUN_PROBE_INIT_CODE


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
