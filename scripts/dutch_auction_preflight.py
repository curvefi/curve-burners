"""Read-only DutchAuctionBurner deployment checks and governance calldata.

This utility never sends a transaction. It validates one chain configuration
against JSON-RPC and emits the mutable CoW lifecycle calls in their required
order: ``configure_cow`` followed by ``enable_cow``.

The JSON object follows the implementation-requirements manifest fields:
``chainId``, ``feeCollector``, ``target``, ``targetDecimals``, ``cowEnabled``,
``composableCow``, ``settlement``, ``vaultRelayer``, and ``appData``. Curve
calibration checks run when ``defaultX``, ``floor``, ``decayFactorRay``, and
``stepDuration`` are all present. ``owner``, ``emergencyOwner``, ``burner``,
``cowOrderValidity``, and ``expectedCodeHashes`` enable stricter post-deploy
checks without requiring a repository-wide chain manifest.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from eth_abi import decode, encode
from eth_utils import is_address, keccak, to_checksum_address


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ERC165_INTERFACE_ID = bytes.fromhex("01ffc9a7")
BURNER_INTERFACE_ID = bytes.fromhex("a3b5e311")
CONDITIONAL_ORDER_INTERFACE_ID = bytes.fromhex("b8296fc4")
ERC1271_INTERFACE_ID = bytes.fromhex("1626ba7e")
RAY = 10**27

# Creation code writes 42 to transient storage, reads it back, copies the word
# with MCOPY, and returns it. Unsupported Cancun opcodes make the eth_call fail;
# incorrect opcode semantics produce a result other than the sentinel.
CANCUN_PROBE_INIT_CODE = bytes.fromhex(
    "602a60005d60005c6000526020600060205e60206020f3"
)
CANCUN_PROBE_RESULT = (42).to_bytes(32, "big")


class PreflightError(RuntimeError):
    """Raised when an RPC call needed for deployment validation fails."""


class RpcClient:
    def __init__(self, url: str, timeout: float = 20.0):
        self.url = url
        self.timeout = timeout
        self.request_id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self.request_id += 1
        response = requests.post(
            self.url,
            json={
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": method,
                "params": params,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise PreflightError(f"{method} failed: {payload['error']}")
        return payload["result"]

    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def latest_timestamp(self) -> int:
        block = self.call("eth_getBlockByNumber", ["latest", False])
        return int(block["timestamp"], 16)

    def code(self, address: str) -> bytes:
        return bytes.fromhex(self.call("eth_getCode", [address, "latest"])[2:])

    def eth_call(self, address: str, data: bytes) -> bytes:
        result = self.call(
            "eth_call",
            [{"to": address, "data": "0x" + data.hex()}, "latest"],
        )
        return bytes.fromhex(result[2:])

    def eth_create_call(self, init_code: bytes) -> bytes:
        result = self.call(
            "eth_call",
            [{"data": "0x" + init_code.hex()}, "latest"],
        )
        return bytes.fromhex(result[2:])


@dataclass
class Report:
    checks: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def require_equal(self, name: str, actual: Any, expected: Any) -> None:
        self.checks[name] = actual
        if actual != expected:
            self.errors.append(f"{name}: expected {expected}, got {actual}")

    def require(self, name: str, condition: bool, detail: str) -> None:
        self.checks[name] = condition
        if not condition:
            self.errors.append(f"{name}: {detail}")


def _address(value: Any, name: str, *, allow_zero: bool = False) -> str:
    if not isinstance(value, str) or not is_address(value):
        raise ValueError(f"{name} is not an EVM address")
    address = to_checksum_address(value)
    if not allow_zero and address == ZERO_ADDRESS:
        raise ValueError(f"{name} must be nonzero")
    return address


def _bytes32(value: Any, name: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 66:
        raise ValueError(f"{name} must be a 32-byte hex value")
    try:
        return bytes.fromhex(value[2:])
    except ValueError as exc:
        raise ValueError(f"{name} must be hex encoded") from exc


def _integer(value: Any, name: str) -> int:
    try:
        parsed = int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{name} must not be negative")
    return parsed


def _selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def encode_call(signature: str, argument_types: list[str], arguments: list[Any]) -> str:
    return "0x" + (_selector(signature) + encode(argument_types, arguments)).hex()


def _read(
    rpc: RpcClient,
    address: str,
    signature: str,
    output_types: list[str],
    argument_types: list[str] | None = None,
    arguments: list[Any] | None = None,
) -> tuple[Any, ...]:
    argument_types = argument_types or []
    arguments = arguments or []
    raw = rpc.eth_call(
        address,
        _selector(signature) + encode(argument_types, arguments),
    )
    if not raw:
        raise PreflightError(f"{address} returned no data for {signature}")
    return decode(output_types, raw)


def _read_address(rpc: RpcClient, address: str, signature: str) -> str:
    return to_checksum_address(_read(rpc, address, signature, ["address"])[0])


def _read_uint(rpc: RpcClient, address: str, signature: str) -> int:
    return _read(rpc, address, signature, ["uint256"])[0]


def _read_bool(rpc: RpcClient, address: str, signature: str) -> bool:
    return _read(rpc, address, signature, ["bool"])[0]


def _read_bytes32(rpc: RpcClient, address: str, signature: str) -> str:
    return "0x" + _read(rpc, address, signature, ["bytes32"])[0].hex()


def _supports_interface(rpc: RpcClient, address: str, interface_id: bytes) -> bool:
    return _read(
        rpc,
        address,
        "supportsInterface(bytes4)",
        ["bool"],
        ["bytes4"],
        [interface_id],
    )[0]


def _mul_div_up(a: int, b: int, denominator: int) -> int:
    return (a * b + denominator - 1) // denominator


def _ray_pow_up(base_ray: int, exponent: int) -> int:
    result = RAY
    factor = base_ray
    while exponent:
        if exponent & 1:
            result = _mul_div_up(result, factor, RAY)
        exponent >>= 1
        if exponent:
            factor = _mul_div_up(factor, factor, RAY)
    return result


def _total_price_at_step(start_total: int, floor_total: int, factor: int, steps: int) -> int:
    decayed = _mul_div_up(start_total, _ray_pow_up(factor, steps), RAY)
    return max(floor_total, decayed)


def _expected_code_hash(config: dict[str, Any], name: str, address: str) -> str | None:
    expected = config.get("expectedCodeHashes", {})
    value = expected.get(name, expected.get(address, expected.get(address.lower())))
    if value is None:
        return None
    return "0x" + _bytes32(value, f"expectedCodeHashes.{name}").hex()


def _check_code(
    rpc: RpcClient,
    report: Report,
    config: dict[str, Any],
    name: str,
    address: str,
) -> None:
    code = rpc.code(address)
    report.require(f"code.{name}", bool(code), f"no code at {address}")
    code_hash = "0x" + keccak(code).hex() if code else None
    report.checks[f"codeHash.{name}"] = code_hash
    expected_hash = _expected_code_hash(config, name, address)
    if expected_hash is None:
        report.warnings.append(f"no expected code hash configured for {name}")
    elif code_hash != expected_hash:
        report.errors.append(
            f"codeHash.{name}: expected {expected_hash}, got {code_hash}"
        )


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(config)
    normalized["chainId"] = _integer(config["chainId"], "chainId")
    normalized["feeCollector"] = _address(config["feeCollector"], "feeCollector")
    normalized["target"] = _address(config["target"], "target")
    normalized["targetDecimals"] = _integer(
        config["targetDecimals"], "targetDecimals"
    )
    if not isinstance(config["cowEnabled"], bool):
        raise ValueError("cowEnabled must be a boolean")
    normalized["cowEnabled"] = config["cowEnabled"]
    normalized["appData"] = "0x" + _bytes32(config["appData"], "appData").hex()

    for name in (
        "defaultX",
        "floor",
        "decayFactorRay",
        "stepDuration",
        "cowOrderValidity",
    ):
        if name in config:
            normalized[name] = _integer(config[name], name)

    if "defaultX" in normalized and normalized["defaultX"] == 0:
        raise ValueError("defaultX must be positive")
    if "floor" in normalized:
        if normalized["floor"] == 0:
            raise ValueError("floor must be positive")
        if "defaultX" in normalized and normalized["floor"] > normalized["defaultX"]:
            raise ValueError("floor must not exceed defaultX")
    if "decayFactorRay" in normalized and not (
        RAY // 2 <= normalized["decayFactorRay"] < RAY
    ):
        raise ValueError("decayFactorRay must be at least RAY/2 and below RAY")
    for name in ("stepDuration", "cowOrderValidity"):
        if name in normalized and normalized[name] == 0:
            raise ValueError(f"{name} must be positive")

    for name in ("owner", "emergencyOwner", "burner"):
        if config.get(name):
            normalized[name] = _address(config[name], name)

    cow_names = ("composableCow", "settlement", "vaultRelayer")
    if normalized["cowEnabled"]:
        for name in cow_names:
            normalized[name] = _address(config[name], name)
    else:
        for name in cow_names:
            normalized[name] = _address(
                config.get(name, ZERO_ADDRESS), name, allow_zero=True
            )
    return normalized


def run_preflight(rpc: RpcClient, config: dict[str, Any]) -> Report:
    config = validate_config(config)
    report = Report()
    report.require_equal("chainId", rpc.chain_id(), config["chainId"])

    required_contracts = {
        "feeCollector": config["feeCollector"],
        "target": config["target"],
    }
    if config["cowEnabled"]:
        required_contracts.update(
            {
                "composableCow": config["composableCow"],
                "settlement": config["settlement"],
                "vaultRelayer": config["vaultRelayer"],
            }
        )
    if config.get("burner"):
        required_contracts["burner"] = config["burner"]

    for name, address in required_contracts.items():
        try:
            _check_code(rpc, report, config, name, address)
        except (PreflightError, requests.RequestException) as exc:
            report.errors.append(f"code.{name}: {exc}")

    try:
        probe_result = rpc.eth_create_call(CANCUN_PROBE_INIT_CODE)
        report.require(
            "evm.cancunOpcodes",
            probe_result == CANCUN_PROBE_RESULT,
            "TSTORE/TLOAD/MCOPY creation probe returned "
            f"0x{probe_result.hex()}, expected 0x{CANCUN_PROBE_RESULT.hex()}",
        )
    except (PreflightError, requests.RequestException) as exc:
        report.require(
            "evm.cancunOpcodes",
            False,
            f"TSTORE/TLOAD/MCOPY creation probe failed: {exc}",
        )

    fee_collector = config["feeCollector"]
    target = config["target"]
    try:
        report.require_equal(
            "feeCollector.target",
            _read_address(rpc, fee_collector, "target()"),
            target,
        )
        owner = _read_address(rpc, fee_collector, "owner()")
        emergency_owner = _read_address(rpc, fee_collector, "emergency_owner()")
        report.checks["feeCollector.owner"] = owner
        report.checks["feeCollector.emergencyOwner"] = emergency_owner
        if config.get("owner"):
            report.require_equal("feeCollector.owner", owner, config["owner"])
        else:
            report.warnings.append("owner is not pinned in configuration")
        if config.get("emergencyOwner"):
            report.require_equal(
                "feeCollector.emergencyOwner",
                emergency_owner,
                config["emergencyOwner"],
            )
        else:
            report.warnings.append("emergencyOwner is not pinned in configuration")
    except (PreflightError, requests.RequestException) as exc:
        report.errors.append(f"FeeCollector interface: {exc}")

    try:
        report.require_equal(
            "target.decimals",
            _read_uint(rpc, target, "decimals()"),
            config["targetDecimals"],
        )
    except (PreflightError, requests.RequestException) as exc:
        report.errors.append(f"target interface: {exc}")

    curve_fields = {"defaultX", "floor", "decayFactorRay", "stepDuration"}
    if curve_fields <= config.keys():
        try:
            timestamp = rpc.latest_timestamp()
            exchange_start, exchange_end = _read(
                rpc,
                fee_collector,
                "epoch_time_frame(uint256,uint256)",
                ["uint256", "uint256"],
                ["uint256", "uint256"],
                [4, timestamp],
            )
            report.require(
                "curve.exchangeFrame",
                exchange_end > exchange_start,
                "empty or reversed EXCHANGE frame",
            )
            if exchange_end <= exchange_start:
                raise PreflightError("empty or reversed EXCHANGE frame")
            active_elapsed = exchange_end - exchange_start - 1
            active_steps = active_elapsed // config["stepDuration"]
            report.checks["curve.activeSteps"] = active_steps
            report.require(
                "curve.activeStepBound",
                active_steps <= 100_000,
                f"{active_steps} active steps exceeds 100000",
            )
            end_price = _total_price_at_step(
                config["defaultX"],
                config["floor"],
                config["decayFactorRay"],
                active_steps,
            )
            report.require_equal("curve.activeEndPrice", end_price, config["floor"])
        except (PreflightError, requests.RequestException) as exc:
            report.errors.append(f"curve calibration: {exc}")

    if config["cowEnabled"]:
        try:
            composable_cow_domain_separator = _read_bytes32(
                rpc,
                config["composableCow"],
                "domainSeparator()",
            )
            settlement_domain_separator = _read_bytes32(
                rpc,
                config["settlement"],
                "domainSeparator()",
            )
            report.require(
                "composableCow.domainSeparator",
                composable_cow_domain_separator != "0x" + "00" * 32
                and composable_cow_domain_separator == settlement_domain_separator,
                "ComposableCoW domain separator "
                f"{composable_cow_domain_separator} must be nonzero and exactly equal "
                f"Settlement {settlement_domain_separator}",
            )
        except (PreflightError, requests.RequestException) as exc:
            report.errors.append(f"CoW domain separator interface: {exc}")

        try:
            report.require_equal(
                "settlement.vaultRelayer",
                _read_address(rpc, config["settlement"], "vaultRelayer()"),
                config["vaultRelayer"],
            )
        except (PreflightError, requests.RequestException) as exc:
            report.errors.append(f"Settlement interface: {exc}")

    burner = config.get("burner")
    if burner:
        try:
            report.require(
                "burner.interface.erc165",
                _supports_interface(rpc, burner, ERC165_INTERFACE_ID),
                "ERC-165 interface missing",
            )
            report.require(
                "burner.interface.burner",
                _supports_interface(rpc, burner, BURNER_INTERFACE_ID),
                "Curve Burner interface missing",
            )
            report.require_equal(
                "burner.feeCollector",
                _read_address(rpc, burner, "fee_collector()"),
                fee_collector,
            )
            report.require_equal(
                "burner.target",
                _read_address(rpc, burner, "target()"),
                target,
            )
            report.require_equal(
                "burner.appData",
                _read_bytes32(rpc, burner, "app_data()"),
                config["appData"],
            )
            getter_config = {
                "defaultX": "start_total()",
                "floor": "floor_total()",
                "decayFactorRay": "decay_factor_ray()",
                "stepDuration": "step_duration()",
                "cowOrderValidity": "cow_order_validity()",
            }
            for config_name, getter in getter_config.items():
                if config_name in config:
                    report.require_equal(
                        f"burner.{config_name}",
                        _read_uint(rpc, burner, getter),
                        config[config_name],
                    )
            cow_enabled = _read_bool(rpc, burner, "cow_enabled()")
            report.require_equal("burner.cowEnabled", cow_enabled, config["cowEnabled"])
            report.require_equal(
                "burner.interface.conditionalOrder",
                _supports_interface(rpc, burner, CONDITIONAL_ORDER_INTERFACE_ID),
                cow_enabled,
            )
            report.require_equal(
                "burner.interface.erc1271",
                _supports_interface(rpc, burner, ERC1271_INTERFACE_ID),
                cow_enabled,
            )
            report.require_equal(
                "burner.composableCow",
                _read_address(rpc, burner, "composable_cow()"),
                config["composableCow"],
            )
            report.require_equal(
                "burner.vaultRelayer",
                _read_address(rpc, burner, "vault_relayer()"),
                config["vaultRelayer"],
            )
            generation = _read_uint(rpc, burner, "cow_generation()")
            report.checks["burner.cowGeneration"] = generation
            if cow_enabled and generation == 0:
                report.errors.append("burner.cowGeneration: enabled with zero generation")
        except (PreflightError, requests.RequestException) as exc:
            report.errors.append(f"DutchAuctionBurner interface: {exc}")

    return report


def lifecycle_calldata(
    config: dict[str, Any],
    burner: str,
    retired_relayer: str | None,
    tokens: list[str],
) -> dict[str, Any]:
    config = validate_config(config)
    burner = _address(burner, "burner")
    calls: dict[str, list[dict[str, str]]] = {
        "configuration": [],
        "emergency": [
            {
                "to": burner,
                "function": "disable_cow()",
                "data": encode_call("disable_cow()", [], []),
            }
        ],
    }

    if config["cowEnabled"]:
        calls["configuration"] = [
            {
                "to": burner,
                "function": "configure_cow(address,address)",
                "data": encode_call(
                    "configure_cow(address,address)",
                    ["address", "address"],
                    [config["composableCow"], config["vaultRelayer"]],
                ),
            },
            {
                "to": burner,
                "function": "enable_cow()",
                "data": encode_call("enable_cow()", [], []),
            },
        ]

    if retired_relayer or tokens:
        if not retired_relayer or not tokens:
            raise ValueError("--retired-relayer and at least one --token are required together")
        relayer = _address(retired_relayer, "retired relayer")
        normalized_tokens = [_address(token, "token") for token in tokens]
        calls["emergency"].append(
            {
                "to": burner,
                "function": "revoke_cow_allowances(address[],address)",
                "data": encode_call(
                    "revoke_cow_allowances(address[],address)",
                    ["address[]", "address"],
                    [normalized_tokens, relayer],
                ),
            }
        )
    return calls


def _load_config(path: str) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as config_file:
        value = json.load(config_file)
    if not isinstance(value, dict):
        raise ValueError("configuration root must be an object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="single-chain JSON configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight", help="run read-only JSON-RPC checks")
    preflight.add_argument("--rpc-url", help="defaults to RPC_URL from .env/environment")
    preflight.add_argument("--timeout", type=float, default=20.0)

    calldata = subparsers.add_parser("calldata", help="emit lifecycle and emergency calldata")
    calldata.add_argument("--burner", required=True)
    calldata.add_argument("--retired-relayer")
    calldata.add_argument("--token", action="append", default=[])
    return parser


def main() -> int:
    load_dotenv()
    args = _parser().parse_args()
    config = _load_config(args.config)

    if args.command == "calldata":
        print(
            json.dumps(
                lifecycle_calldata(
                    config,
                    args.burner,
                    args.retired_relayer,
                    args.token,
                ),
                indent=2,
            )
        )
        return 0

    rpc_url = args.rpc_url or os.environ.get("RPC_URL")
    if not rpc_url:
        raise ValueError("--rpc-url or RPC_URL is required")
    report = run_preflight(RpcClient(rpc_url, args.timeout), config)
    print(json.dumps(report.__dict__, indent=2, default=str))
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
