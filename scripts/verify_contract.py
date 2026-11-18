"""Verify a deployed Vyper contract on an Etherscan-family explorer (v2 API) or Blockscout.

    python scripts/verify_contract.py --chain-id 137 --address 0x... \
        --contract contracts/hooks/CCTPBridger.vy --args address,uint32 0x9daF... 0

The standard-JSON input is produced by the compiler the contract's pragma
names (``vyper -f solc_json``; 0.3.x has no such output, so single-file
contracts are wrapped by hand). Compiler binaries: the venv's vyper for the
application line, ``~/.vvm/vyper-<version>`` for pinned legacy versions.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from eth_abi import encode
from eth_utils import to_checksum_address

ROOT = Path(__file__).resolve().parents[1]
ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"
SOURCIFY = "https://sourcify.dev/server"
BLOCKSCOUT = {
    137: "https://polygon.blockscout.com",
    1: "https://eth.blockscout.com",
    100: "https://gnosis.blockscout.com",
}


def pragma_version(source: Path) -> str:
    text = source.read_text()
    match = re.search(r"^#\s*(?:pragma version|@version)\s*[=^~]*\s*([0-9][^\s]*)", text, re.M)
    if not match:
        raise SystemExit(f"{source}: no version pragma")
    return match.group(1)


def compiler_binary(version: str) -> list[str]:
    venv = ROOT / ".venv" / "bin" / "vyper"
    if venv.exists():
        current = subprocess.run([str(venv), "--version"], capture_output=True, text=True).stdout
        if current.startswith(version):
            return [str(venv)]
    vvm = Path.home() / ".vvm" / f"vyper-{version}"
    if vvm.exists():
        return [str(vvm)]
    raise SystemExit(f"no vyper {version} binary (venv or ~/.vvm)")


def standard_json(contract: Path, version: str) -> dict[str, Any]:
    rel = contract.resolve().relative_to(ROOT).as_posix()
    if version.startswith("0.3."):
        return {
            "language": "Vyper",
            "sources": {rel: {"content": contract.read_text()}},
            "settings": {"outputSelection": {rel: ["*"]}},
        }
    out = subprocess.run(
        [*compiler_binary(version), "-f", "solc_json", rel],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit(out.stderr[-2000:])
    payload = json.loads(out.stdout)
    # Site-packages modules (snekmate) come out under a relative ``0/1/2/...``
    # path: republish them under their import name and keep the search path
    # list to the project root.
    sources: dict[str, Any] = {}
    for path, entry in payload["sources"].items():
        if "site-packages/" in path:
            path = path.split("site-packages/", 1)[1]
        sources[path] = entry
    payload["sources"] = sources
    settings = payload.setdefault("settings", {})
    settings["search_paths"] = ["."]
    settings["outputSelection"] = {rel: ["*"]}
    settings.pop("venom_flags", None)
    # Explorers do not always honor the compiler pragmas of the main file, so
    # spell them out as settings (same bytecode locally either way).
    text = contract.read_text()
    if match := re.search(r"^#\s*pragma\s+optimize\s+(\w+)", text, re.M):
        settings["optimize"] = match.group(1)
    if match := re.search(r"^#\s*pragma\s+evm-version\s+(\w+)", text, re.M):
        settings["evmVersion"] = match.group(1)
    if re.search(r"^#\s*pragma\s+experimental-codegen", text, re.M):
        settings["experimentalCodegen"] = True
    # Explorers accept only the standard top-level keys (vyper adds "integrity").
    return {key: payload[key] for key in ("language", "sources", "settings")}


def constructor_args(types: str | None, values: list[str]) -> str:
    if not types:
        return ""
    type_list = [t.strip() for t in types.split(",") if t.strip()]
    if len(type_list) != len(values):
        raise SystemExit("constructor types and values differ in length")
    parsed = []
    for abi_type, value in zip(type_list, values):
        if abi_type == "address":
            parsed.append(to_checksum_address(value))
        elif abi_type.startswith(("uint", "int")):
            parsed.append(int(value, 0))
        elif abi_type == "bool":
            parsed.append(value.lower() in ("1", "true"))
        elif abi_type.startswith("bytes"):
            parsed.append(bytes.fromhex(value[2:] if value.startswith("0x") else value))
        else:
            parsed.append(value)
    return encode(type_list, parsed).hex()


def verify_etherscan(
    chain_id: int, address: str, contract: Path, version: str, args_hex: str
) -> bool:
    load_dotenv(Path.home() / ".env", override=False)
    key = os.environ["ETHERSCAN_API_KEY"]
    rel = contract.resolve().relative_to(ROOT).as_posix()
    payload = standard_json(contract, version)
    response = requests.post(
        ETHERSCAN_V2,
        params={"chainid": chain_id},
        data={
            "apikey": key,
            "module": "contract",
            "action": "verifysourcecode",
            "codeformat": "vyper-json",
            "sourceCode": json.dumps(payload),
            "contractaddress": address,
            "contractname": f"{rel}:{contract.stem}",
            "compilerversion": f"vyper:{version}",
            "constructorArguements": args_hex,
            "licenseType": "3",
            "optimizationUsed": "0",
            "runs": "200",
        },
        timeout=60,
    )
    body = response.json()
    print(f"etherscan submit: {body}")
    if body.get("status") != "1":
        return False
    guid = body["result"]
    for _ in range(30):
        time.sleep(5)
        status = requests.get(
            ETHERSCAN_V2,
            params={
                "chainid": chain_id,
                "apikey": key,
                "module": "contract",
                "action": "checkverifystatus",
                "guid": guid,
            },
            timeout=60,
        ).json()
        print(f"etherscan status: {status.get('result')}")
        if status.get("result") != "Pending in queue":
            return "Pass" in str(status.get("result"))
    return False


def verify_blockscout(
    chain_id: int, address: str, contract: Path, version: str, args_hex: str
) -> bool:
    base = BLOCKSCOUT.get(chain_id)
    if base is None:
        print("blockscout: no instance configured for this chain")
        return False
    cfg = requests.get(f"{base}/api/v2/smart-contracts/verification/config", timeout=30).json()
    available = [v for v in cfg.get("vyper_compiler_versions", []) if v.startswith(f"v{version}")]
    if not available:
        print(f"blockscout: vyper {version} not offered")
        return False
    payload = standard_json(contract, version)
    files = {"files[0]": ("input.json", json.dumps(payload), "application/json")}
    data = {"compiler_version": available[0], "license_type": "mit"}
    if args_hex:
        data["constructor_args"] = "0x" + args_hex
    response = requests.post(
        f"{base}/api/v2/smart-contracts/{address}/verification/via/vyper-standard-input",
        files=files,
        data=data,
        timeout=60,
    )
    print(f"blockscout submit: {response.status_code} {response.text[:200]}")
    if response.status_code != 200:
        return False
    for _ in range(30):
        time.sleep(5)
        info = requests.get(f"{base}/api/v2/smart-contracts/{address}", timeout=30).json()
        if info.get("is_verified"):
            print("blockscout: verified")
            return True
    print("blockscout: not verified yet; check the explorer")
    return False


def verify_sourcify(chain_id: int, address: str, contract: Path, version: str) -> bool:
    """Sourcify fetches any released vyper binary (betas included) and pushes
    matches on to Etherscan; constructor args are read from the creation tx."""
    rel = contract.resolve().relative_to(ROOT).as_posix()
    commit = subprocess.run(
        [*compiler_binary(version), "--version"], capture_output=True, text=True
    ).stdout.strip()
    response = requests.post(
        f"{SOURCIFY}/v2/verify/{chain_id}/{address}",
        json={
            "stdJsonInput": standard_json(contract, version),
            "compilerVersion": commit or version,
            "contractIdentifier": f"{rel}:{contract.stem}",
        },
        timeout=120,
    )
    print(f"sourcify submit: {response.status_code} {response.text[:200]}")
    if response.status_code != 202:
        return False
    job = response.json()["verificationId"]
    for _ in range(40):
        time.sleep(5)
        status = requests.get(f"{SOURCIFY}/v2/verify/{job}", timeout=30).json()
        if status.get("isJobCompleted"):
            match = status.get("contract", {}).get("match")
            error = status.get("error")
            print(f"sourcify: match={match} error={(error or {}).get('message', '')[:200]}")
            return match is not None
    print("sourcify: job still running; check later")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--chain-id", type=int, required=True)
    parser.add_argument("--address", required=True)
    parser.add_argument("--contract", required=True, help="path to the .vy file")
    parser.add_argument("--args", help="constructor ABI types, comma separated", default=None)
    parser.add_argument("values", nargs="*", help="constructor values in order")
    parser.add_argument("--args-hex", help="ABI-encoded constructor args (hex), instead of --args")
    parser.add_argument(
        "--explorer", choices=["sourcify", "etherscan", "blockscout", "all"], default="all"
    )
    args = parser.parse_args(argv)

    contract = Path(args.contract)
    version = pragma_version(contract)
    args_hex = (args.args_hex or "").removeprefix("0x") or constructor_args(args.args, args.values)
    address = to_checksum_address(args.address)
    print(f"{contract} @ {address}: vyper {version}, constructor args 0x{args_hex}")
    ok = False
    # Sourcify first: it is the only route for the 0.5.x beta compiler, and a
    # Sourcify match is forwarded to Etherscan automatically.
    if args.explorer in ("sourcify", "all"):
        ok = verify_sourcify(args.chain_id, address, contract, version)
    if not ok and args.explorer in ("etherscan", "all"):
        ok = verify_etherscan(args.chain_id, address, contract, version, args_hex)
    if not ok and args.explorer in ("blockscout", "all"):
        ok = verify_blockscout(args.chain_id, address, contract, version, args_hex)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
