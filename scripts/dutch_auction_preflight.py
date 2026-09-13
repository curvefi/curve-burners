"""Read-only DutchAuctionBurner deployment checks and governance calldata.

This utility never sends a transaction. It validates one chain configuration
against JSON-RPC and emits the mutable lifecycle calls in their required
order. Adapter calldata targets the registry (``set_adapter`` then
``activate_adapter`` per adapter; ``disable_adapter`` for emergencies);
``sync_executor_approvals`` targets the burner and is permissionless.

The JSON object follows the implementation-requirements manifest fields:
``chainId``, ``feeCollector``, ``target``, ``targetDecimals``, ``cowEnabled``,
``settlement``, ``vaultRelayer``, ``cowAdapter``, and ``appData``. The CoW
adapter must also appear in ``adapters`` (adapter = cowAdapter, executor =
vaultRelayer): the generic adapter checks cover its registry entry, activation
flag, and executor activity. Curve checks run when ``start_total``,
``floor_total``, and ``step_duration`` are all present: the curve the burner
prepares on-chain from them and the FeeCollector EXCHANGE frame
is recomputed with the bit-exact mirror in ``dutch_auction_curve.py`` and
the burner's ``auction_length`` is checked against the frame.
``owner``, ``emergencyOwner``, ``burner``, and ``expectedCodeHashes`` (keyed
by contract name or address) enable stricter post-deploy checks without
requiring a repository-wide chain manifest. ``registry`` and ``adapters``
(``[{"adapter", "executor"}]``) pin the adapter surface: each entry is checked
against the registry listing (``get_adapters``) and config (executor and
active flag), its code (hash pinned through ``expectedCodeHashes``), and the
registry's ``is_executor_active`` answer for its executor.
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

try:  # run as a script from scripts/ or imported as scripts.dutch_auction_preflight
    from scripts import dutch_auction_curve as curve
except ImportError:  # pragma: no cover
    import dutch_auction_curve as curve


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ERC165_INTERFACE_ID = bytes.fromhex("01ffc9a7")
BURNER_INTERFACE_ID = bytes.fromhex("a3b5e311")
ERC1271_INTERFACE_ID = bytes.fromhex("1626ba7e")
WAD = 10**18

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
        "start_total",
        "floor_total",
        "step_duration",
    ):
        if name in config:
            normalized[name] = _integer(config[name], name)

    if "start_total" in normalized and normalized["start_total"] == 0:
        raise ValueError("start_total must be positive")
    if "floor_total" in normalized:
        if normalized["floor_total"] == 0:
            raise ValueError("floor_total must be positive")
        if "start_total" in normalized and normalized["floor_total"] > normalized["start_total"]:
            raise ValueError("floor_total must not exceed start_total")
    if "step_duration" in normalized and normalized["step_duration"] == 0:
        raise ValueError("step_duration must be positive")

    for name in ("owner", "emergencyOwner", "burner", "registry"):
        if config.get(name):
            normalized[name] = _address(config[name], name)

    adapters = config.get("adapters", [])
    if not isinstance(adapters, list):
        raise ValueError("adapters must be a list")
    normalized_adapters: list[dict[str, str]] = []
    for index, adapter in enumerate(adapters):
        if not isinstance(adapter, dict):
            raise ValueError(f"adapters[{index}] must be an object")
        entry = {
            "adapter": _address(
                adapter.get("adapter"), f"adapters[{index}].adapter"
            ),
            "executor": _address(
                adapter.get("executor"), f"adapters[{index}].executor"
            ),
        }
        normalized_adapters.append(entry)
    if len({entry["adapter"] for entry in normalized_adapters}) != len(normalized_adapters):
        raise ValueError("adapters must have unique adapter addresses")
    if normalized_adapters and not normalized.get("registry"):
        raise ValueError("adapters require registry")
    normalized["adapters"] = normalized_adapters

    cow_names = ("settlement", "vaultRelayer", "cowAdapter")
    if normalized["cowEnabled"]:
        for name in cow_names:
            normalized[name] = _address(config[name], name)
        if normalized["cowAdapter"] not in {a["adapter"] for a in normalized_adapters}:
            raise ValueError("cowAdapter must be listed in adapters")
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
                "settlement": config["settlement"],
                "vaultRelayer": config["vaultRelayer"],
                "cowAdapter": config["cowAdapter"],
            }
        )
    if config.get("burner"):
        required_contracts["burner"] = config["burner"]
    if config.get("registry"):
        required_contracts["registry"] = config["registry"]
    for index, adapter in enumerate(config["adapters"]):
        required_contracts[f"adapters[{index}].adapter"] = adapter["adapter"]

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

    curve_fields = {"start_total", "floor_total", "step_duration"}
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
            if exchange_end <= exchange_start:
                raise PreflightError("empty or reversed EXCHANGE frame")
            auction_length = exchange_end - exchange_start
            steps = curve.decay_steps(auction_length, config["step_duration"])
            if steps == 0:
                raise PreflightError("step_duration exceeds the EXCHANGE frame")
            log_start, log_drop = curve.curve_logs(
                config["start_total"], config["floor_total"]
            )
            report.checks["curve.decaySteps"] = steps
            report.checks["curve.logStart"] = log_start
            report.checks["curve.logDrop"] = log_drop
            if config.get("burner"):
                # The burner derives the same curve from these parameters and
                # its auction_length (the EXCHANGE frame length at deploy).
                report.require_equal(
                    "burner.auction_length",
                    _read_uint(rpc, config["burner"], "auction_length()"),
                    auction_length,
                )
            # Sanity: the curve sits at the floor by the last active second.
            end_price = curve.total_price(
                config["start_total"],
                config["floor_total"],
                log_start,
                log_drop,
                steps,
                auction_length - 1,
                config["step_duration"],
            )
            report.require_equal("curve.activeEndPrice", end_price, config["floor_total"])
        except (PreflightError, requests.RequestException) as exc:
            report.errors.append(f"curve calibration: {exc}")

    if config["cowEnabled"]:
        try:
            cow_adapter = config["cowAdapter"]
            settlement_domain_separator = _read_bytes32(
                rpc, config["settlement"], "domainSeparator()"
            )
            report.require_equal(
                "cowAdapter.settlement",
                _read_address(rpc, cow_adapter, "settlement()"),
                config["settlement"],
            )
            report.require_equal(
                "cowAdapter.vaultRelayer",
                _read_address(rpc, cow_adapter, "vault_relayer()"),
                config["vaultRelayer"],
            )
            report.require_equal(
                "settlement.vaultRelayer",
                _read_address(rpc, config["settlement"], "vaultRelayer()"),
                config["vaultRelayer"],
            )
            report.require_equal(
                "cowAdapter.appData",
                _read_bytes32(rpc, cow_adapter, "app_data()"),
                config["appData"],
            )
            report.require(
                "cowAdapter.domainSeparator",
                settlement_domain_separator != "0x" + "00" * 32
                and _read_bytes32(rpc, cow_adapter, "domain_separator()")
                == settlement_domain_separator,
                "CowAdapter domain separator must be nonzero and equal the Settlement's",
            )
        except (PreflightError, requests.RequestException) as exc:
            report.errors.append(f"CowAdapter interface: {exc}")

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
            # The payment token lives in the core as `want`; it mirrors the
            # FeeCollector target only through the owner's resync_target.
            report.require_equal(
                "burner.want",
                _read_address(rpc, burner, "want()"),
                target,
            )
            getter_config = {
                "start_total": "start_total()",
                "floor_total": "floor_total()",
                "step_duration": "step_duration()",
            }
            for config_name, getter in getter_config.items():
                if config_name in config:
                    report.require_equal(
                        f"burner.{config_name}",
                        _read_uint(rpc, burner, getter),
                        config[config_name],
                    )
            # The ERC-1271 router is the settlement entry for every adapter.
            report.require(
                "burner.interface.erc1271",
                _supports_interface(rpc, burner, ERC1271_INTERFACE_ID),
                "ERC-1271 interface missing",
            )
            report.require_equal(
                "burner.registry",
                _read_address(rpc, burner, "registry()"),
                config.get("registry", ZERO_ADDRESS),
            )
        except (PreflightError, requests.RequestException) as exc:
            report.errors.append(f"DutchAuctionBurner interface: {exc}")

    # Adapter state is registry-only: the burner routes an adapter iff
    # registry.get_adapter(adapter).active and approves an executor iff
    # registry.is_executor_active(executor). validate_config guarantees a
    # registry whenever adapters are configured.
    registry = config.get("registry")
    registered: set[str] | None = None
    if config["adapters"]:
        try:
            listed = [
                to_checksum_address(address)
                for address in _read(
                    rpc, registry, "get_adapters()", ["address[]"]
                )[0]
            ]
            report.checks["registry.adapters"] = listed
            registered = set(listed)
            configured = {adapter["adapter"] for adapter in config["adapters"]}
            for address in listed:
                if address not in configured:
                    report.warnings.append(
                        f"registry lists adapter {address} not in configuration"
                    )
        except (PreflightError, requests.RequestException) as exc:
            report.errors.append(f"registry.adapters: {exc}")
    for adapter in config["adapters"]:
        label = f"adapter.{adapter['adapter']}"
        try:
            if registered is not None:
                report.require(
                    f"{label}.registered",
                    adapter["adapter"] in registered,
                    "adapter missing from registry.get_adapters()",
                )
            adapter_config = _read(
                rpc,
                registry,
                "get_adapter(address)",
                ["address", "bool"],
                ["address"],
                [adapter["adapter"]],
            )
            executor = to_checksum_address(adapter_config[0])
            report.require_equal(f"{label}.executor", executor, adapter["executor"])
            report.require(
                f"{label}.active",
                adapter_config[1],
                "adapter not active in the registry",
            )
            executor_active = _read(
                rpc,
                registry,
                "is_executor_active(address)",
                ["bool"],
                ["address"],
                [executor],
            )[0]
            report.require(
                f"{label}.executorActive",
                executor_active,
                "active adapter's executor is not active in the registry",
            )
        except (PreflightError, requests.RequestException) as exc:
            report.errors.append(f"{label}: {exc}")

    return report


def lifecycle_calldata(
    config: dict[str, Any],
    burner: str | None,
    executor: str | None,
    tokens: list[str],
) -> dict[str, Any]:
    config = validate_config(config)
    calls: dict[str, list[dict[str, str]]] = {
        "configuration": [],
        "emergency": [],
        "permissionless": [],
    }

    # Governance calldata targets the registry: the burner holds no adapter
    # state. Entries are registered with set_adapter (repointable while
    # inactive) and go live in a separate activate_adapter step. An emergency disable is batched with the burner's
    # sync_executor_approvals below, since the registry never touches
    # allowances.
    registry = config.get("registry")

    def _registry_call(function: str, types: list[str], arguments: list[Any]) -> dict[str, str]:
        signature = f"{function}({','.join(types)})"
        return {
            "to": registry,
            "function": signature,
            "data": encode_call(signature, types, arguments),
        }

    for adapter in config["adapters"]:
        calls["configuration"].append(
            _registry_call(
                "set_adapter", ["address", "address"], [adapter["adapter"], adapter["executor"]]
            )
        )
        calls["configuration"].append(
            _registry_call("activate_adapter", ["address"], [adapter["adapter"]])
        )
        calls["emergency"].append(
            _registry_call("disable_adapter", ["address"], [adapter["adapter"]])
        )

    if burner or executor or tokens:
        if not burner or not executor or not tokens:
            raise ValueError(
                "--burner, --executor and at least one --token are required together"
            )
        burner = _address(burner, "burner")
        executor_address = _address(executor, "executor")
        normalized_tokens = [_address(token, "token") for token in tokens]
        # The target allowance (0 or max) is derived from the registry's
        # on-chain is_executor_active answer, so this call carries no
        # privilege and needs no gating.
        calls["permissionless"].append(
            {
                "to": burner,
                "function": "sync_executor_approvals(address,address[])",
                "data": encode_call(
                    "sync_executor_approvals(address,address[])",
                    ["address", "address[]"],
                    [executor_address, normalized_tokens],
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
    calldata.add_argument("--burner", help="target of permissionless sync_executor_approvals")
    calldata.add_argument("--executor", help="executor for permissionless sync_executor_approvals")
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
                    args.executor,
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
