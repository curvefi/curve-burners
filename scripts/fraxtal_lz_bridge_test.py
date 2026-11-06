"""Test-in-prod of LZOFTBridger: frxUSD from Fraxtal to Ethereum through the Frax OFT hub.

    python scripts/fraxtal_lz_bridge_test.py rehearse            # Fraxtal fork: deploy + bridge
    python scripts/fraxtal_lz_bridge_test.py deploy              # live: deploy LZOFTBridger (Fraxtal)
    python scripts/fraxtal_lz_bridge_test.py bridge [--amount 1] # live: approve + bridge() with the quoted fee
    python scripts/fraxtal_lz_bridge_test.py status              # LayerZero Scan state of the last message

The EOA in ``POLYGON_TEST_KEEPER_KEY`` (same key on every chain) plays the
Hooker: it approves the bridger, calls bridge() with ``cost()`` worth of
native coin and names itself as the refund address and as the receiver on
Ethereum. State is kept in deployments.fraxtal-test.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from eth_utils import to_checksum_address

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from keepers.auction_keeper.chain.abi import MAX_UINT256, Function  # noqa: E402
from keepers.auction_keeper.chain.client import Web3Client  # noqa: E402
from keepers.auction_keeper.chain.contracts import Erc20  # noqa: E402

KEY_ENV = "POLYGON_TEST_KEEPER_KEY"
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "deployments.fraxtal-test.json"

CHAIN_ID = 252
FRXUSD = to_checksum_address("0xfc00000000000000000000000000000000000001")
# Frax's frxUSD OFT hub on Fraxtal (OFTAdapter over frxUSD): the only peer of
# every spoke, and the only contract peering the Ethereum lockbox.
FRXUSD_OFT_HUB = to_checksum_address("0x96a394058e2b84a89bac9667b19661ed003cf5d4")
ETHEREUM_EID = 30101
ETHEREUM_FRXUSD = to_checksum_address("0xcacd6fd266af91b8aed52accc382b4e165586e29")
QUOTE_AMOUNT = 1000 * 10**18
EXTRA_OPTIONS = b""  # the hub enforces 200k lzReceive gas toward Ethereum
LZ_SCAN = "https://scan.layerzero-api.com/v1/messages/tx"

BRIDGE = Function("bridge(address,address,uint256,uint256,address)", ("uint256",))
COST = Function("cost(uint256)", ("uint256",))


def rpc(network: str) -> str:
    load_dotenv(Path.home() / ".env", override=False)
    return f"https://lb.drpc.org/ogrpc?network={network}&dkey={os.environ['DRPC_API_KEY']}"


def client(network: str, *, signing: bool = True) -> Web3Client:
    load_dotenv(Path.home() / ".env", override=False)
    return Web3Client(rpc(network), os.environ[KEY_ENV] if signing else None, timeout=60)


def load_state() -> dict[str, Any]:
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def deploy_live() -> str:
    import boa
    from eth_account import Account

    load_dotenv(Path.home() / ".env", override=False)
    boa.set_network_env(rpc("fraxtal"))
    signer = Account.from_key(os.environ[KEY_ENV])
    boa.env.add_account(signer)
    boa.env.eoa = signer.address
    bridger = boa.load(
        "contracts/hooks/LZOFTBridger.vy", FRXUSD_OFT_HUB, ETHEREUM_EID, QUOTE_AMOUNT, EXTRA_OPTIONS
    )
    assert bridger.TOKEN() == FRXUSD
    print(f"LZOFTBridger: {bridger.address} (gas {boa.env.get_gas_used()})")
    state = load_state()
    state["LZOFTBridger"] = bridger.address
    save_state(state)
    return bridger.address


def bridge_live(amount_frxusd: float) -> None:
    state = load_state()
    bridger = state["LZOFTBridger"]
    c = client("fraxtal")
    frxusd = Erc20(c, FRXUSD)
    amount = int(amount_frxusd * 10**18)
    balance = frxusd.balance_of(c.sender)
    assert balance >= amount, f"EOA holds {balance / 1e18} frxUSD, needs {amount_frxusd}"
    fee = COST.decode(c.call(bridger, COST.encode(amount)))[0]
    native = c.w3.eth.get_balance(c.sender)
    print(f"LayerZero fee {fee / 1e18} native; EOA native {native / 1e18}")
    assert native > fee, "not enough native coin for the LayerZero fee"
    if frxusd.allowance(c.sender, bridger) < amount:
        print("approve:", c.send(FRXUSD, frxusd.approve_calldata(bridger, MAX_UINT256)).tx_hash)
    # The keeper's own address is the refund receiver, so any excess comes back.
    calldata = BRIDGE.encode(FRXUSD, c.sender, amount, 0, c.sender)
    sent = BRIDGE.decode(c.call(bridger, calldata, sender=c.sender))[0] if False else None  # noqa: F841
    result = c.send(bridger, calldata, value=fee + fee // 10)  # 10% headroom, refunded
    print(
        f"bridge tx {result.tx_hash} (block {result.block_number}); EOA frxUSD now {frxusd.balance_of(c.sender) / 1e18}, native {c.w3.eth.get_balance(c.sender) / 1e18}"
    )
    state["lz"] = {"txHash": result.tx_hash, "amount": amount, "recipient": c.sender}
    save_state(state)


def status() -> None:
    state = load_state()
    tx_hash = state["lz"]["txHash"]
    r = requests.get(
        f"{LZ_SCAN}/{tx_hash if tx_hash.startswith('0x') else '0x' + tx_hash}", timeout=30
    )
    print("layerzero scan:", r.status_code)
    for message in (r.json() if r.status_code == 200 else {}).get("data", []):
        print(
            "  status",
            message.get("status", {}).get("name"),
            "dst tx",
            (message.get("destination") or {}).get("tx", {}).get("txHash"),
        )
    e = client("ethereum", signing=False)
    print(
        "Ethereum frxUSD of recipient:",
        Erc20(e, ETHEREUM_FRXUSD).balance_of(state["lz"]["recipient"]) / 1e18,
    )


FRXUSD_HOLDER = to_checksum_address(
    "0xeE454138083b9B9714cac3c7cF12560248d76D6B"
)  # fork-only: frxUSD/FXB2029 Curve pool


def rehearse() -> None:
    import boa

    boa.fork(rpc("fraxtal"), block_identifier="latest")
    eoa = boa.env.generate_address("eoa")
    boa.env.eoa = eoa
    boa.env.set_balance(eoa, 100 * 10**18)
    frxusd = boa.load_partial("contracts/testing/ERC20Mock.vy").at(FRXUSD)
    with boa.env.prank(FRXUSD_HOLDER):
        frxusd.transfer(eoa, 5 * 10**18)

    bridger = boa.load(
        "contracts/hooks/LZOFTBridger.vy", FRXUSD_OFT_HUB, ETHEREUM_EID, QUOTE_AMOUNT, EXTRA_OPTIONS
    )
    print(f"LZOFTBridger on fork: {bridger.address}, deploy gas {boa.env.get_gas_used()}")
    fee = bridger.cost(10**18)
    print(f"cost(1 frxUSD -> Ethereum) = {fee / 1e18} native")
    frxusd.approve(bridger.address, 2**256 - 1)
    keeper = boa.env.generate_address("keeper")
    sent = bridger.bridge(FRXUSD, eoa, 10**18, 0, keeper, value=fee + 10**18)
    print(
        f"bridge(): sent {sent / 1e18} frxUSD, refund to keeper {boa.env.get_balance(keeper) / 1e18} native, bridger holds {boa.env.get_balance(bridger.address)} / {frxusd.balanceOf(bridger.address)}"
    )
    assert sent == 10**18 and boa.env.get_balance(keeper) == 10**18
    assert bridger.bridge(FRXUSD, eoa, 10**18, 5 * 10**18, keeper, value=fee) == 0
    assert boa.env.get_balance(keeper) == 10**18 + fee, "below-threshold value refunded"
    print("below threshold: value refunded to the keeper")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["rehearse", "deploy", "bridge", "status"])
    parser.add_argument("--amount", type=float, default=1.0, help="frxUSD to bridge (bridge)")
    args = parser.parse_args(argv)
    if args.command == "rehearse":
        rehearse()
    elif args.command == "deploy":
        deploy_live()
    elif args.command == "bridge":
        bridge_live(args.amount)
    elif args.command == "status":
        status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
