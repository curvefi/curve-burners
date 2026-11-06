"""Test-in-prod of CCTPBridger: USDC from Polygon to Ethereum through Circle CCTP v1.

    python scripts/polygon_cctp_bridge_test.py rehearse            # Polygon fork: deploy + bridge
    python scripts/polygon_cctp_bridge_test.py deploy              # live: deploy CCTPBridger (Polygon)
    python scripts/polygon_cctp_bridge_test.py bridge [--amount 2] # live: approve + bridge(), records the message
    python scripts/polygon_cctp_bridge_test.py attest              # poll Circle for the attestation
    python scripts/polygon_cctp_bridge_test.py receive             # live: receiveMessage on Ethereum, mints USDC

The EOA in ``POLYGON_TEST_KEEPER_KEY`` plays the Hooker (it approves the
bridger and calls bridge) and is the mint recipient on Ethereum. State is kept
in deployments.polygon-test.json under "cctp".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from eth_abi import decode
from eth_utils import keccak, to_checksum_address

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from keepers.auction_keeper.chain.abi import MAX_UINT256, Function  # noqa: E402
from keepers.auction_keeper.chain.client import Web3Client  # noqa: E402
from keepers.auction_keeper.chain.contracts import Erc20  # noqa: E402

KEY_ENV = "POLYGON_TEST_KEEPER_KEY"
ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENTS_PATH = ROOT / "deployments.polygon-test.json"

# Circle CCTP v1 (https://developers.circle.com/stablecoins/evm-smart-contracts).
POLYGON_DOMAIN = 7
ETHEREUM_DOMAIN = 0
POLYGON_TOKEN_MESSENGER = to_checksum_address("0x9daf8c91aefae50b9c0e69629d3f6ca40ca3b3fe")
POLYGON_MESSAGE_TRANSMITTER = to_checksum_address("0xf3be9355363857f3e001be68856a2f96b4c39ba9")
ETHEREUM_MESSAGE_TRANSMITTER = to_checksum_address("0x0a992d191deec32afe36203ad87d7d289a738f81")
POLYGON_USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
ETHEREUM_USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
IRIS_API = "https://iris-api.circle.com"

MESSAGE_SENT_TOPIC = keccak(text="MessageSent(bytes)")
RECEIVE_MESSAGE = Function("receiveMessage(bytes,bytes)", ("bool",))
BRIDGE = Function("bridge(address,address,uint256,uint256)", ("uint256",))
USED_NONCES = Function("usedNonces(bytes32)", ("uint256",))


def rpc(network: str) -> str:
    load_dotenv(Path.home() / ".env", override=False)
    return f"https://lb.drpc.org/ogrpc?network={network}&dkey={os.environ['DRPC_API_KEY']}"


def client(network: str, *, signing: bool = True) -> Web3Client:
    load_dotenv(Path.home() / ".env", override=False)
    return Web3Client(rpc(network), os.environ[KEY_ENV] if signing else None)


def load_state() -> dict[str, Any]:
    return json.loads(DEPLOYMENTS_PATH.read_text()) if DEPLOYMENTS_PATH.exists() else {}


def save_state(state: dict[str, Any]) -> None:
    DEPLOYMENTS_PATH.write_text(json.dumps(state, indent=2) + "\n")


# Steps


def deploy_live() -> str:
    import boa
    from eth_account import Account

    load_dotenv(Path.home() / ".env", override=False)
    boa.set_network_env(rpc("polygon"))
    signer = Account.from_key(os.environ[KEY_ENV])
    boa.env.add_account(signer)
    boa.env.eoa = signer.address
    bridger = boa.load("contracts/hooks/CCTPBridger.vy", POLYGON_TOKEN_MESSENGER, ETHEREUM_DOMAIN)
    print(f"CCTPBridger: {bridger.address} (gas {boa.env.get_gas_used()})")
    state = load_state()
    state["CCTPBridger"] = bridger.address
    save_state(state)
    return bridger.address


def message_from_receipt(c: Web3Client, tx_hash: str) -> bytes:
    receipt = c.w3.eth.get_transaction_receipt(tx_hash)
    for entry in receipt["logs"]:
        if (
            to_checksum_address(entry["address"]) == POLYGON_MESSAGE_TRANSMITTER
            and bytes(entry["topics"][0]) == MESSAGE_SENT_TOPIC
        ):
            return decode(["bytes"], bytes(entry["data"]))[0]
    raise RuntimeError("MessageSent not found in the receipt")


def bridge_live(amount_usdc: float) -> None:
    state = load_state()
    bridger = state["CCTPBridger"]
    c = client("polygon")
    usdc = Erc20(c, POLYGON_USDC)
    amount = int(amount_usdc * 10**6)
    balance = usdc.balance_of(c.sender)
    assert balance >= amount, f"EOA holds {balance / 1e6} USDC, needs {amount_usdc}"
    if usdc.allowance(c.sender, bridger) < amount:
        print("approve:", c.send(POLYGON_USDC, usdc.approve_calldata(bridger, MAX_UINT256)).tx_hash)
    # Simulate first: the return value is the bridged amount.
    bridged = BRIDGE.decode(
        c.call(bridger, BRIDGE.encode(POLYGON_USDC, c.sender, amount, 0), sender=c.sender)
    )[0]
    print(f"simulation: bridge() would send {bridged / 1e6} USDC to {c.sender} on Ethereum")
    result = c.send(bridger, BRIDGE.encode(POLYGON_USDC, c.sender, amount, 0))
    message = message_from_receipt(c, result.tx_hash)
    message_hash = "0x" + keccak(message).hex()
    print(f"bridge tx {result.tx_hash} (block {result.block_number}); messageHash {message_hash}")
    print(f"EOA USDC on Polygon now {usdc.balance_of(c.sender) / 1e6}")
    state["cctp"] = {
        "txHash": result.tx_hash,
        "amount": bridged,
        "message": "0x" + message.hex(),
        "messageHash": message_hash,
        "recipient": c.sender,
    }
    save_state(state)


def attest(poll: int = 30, timeout: int = 3600) -> str:
    state = load_state()
    message_hash = state["cctp"]["messageHash"]
    started = time.monotonic()
    while True:
        r = requests.get(f"{IRIS_API}/attestations/{message_hash}", timeout=30)
        payload = r.json() if r.content else {}
        status = payload.get("status", f"http {r.status_code}")
        print(f"attestation {status}")
        if status == "complete":
            state["cctp"]["attestation"] = payload["attestation"]
            save_state(state)
            return payload["attestation"]
        if time.monotonic() - started > timeout:
            raise SystemExit("attestation not ready; rerun `attest` later")
        time.sleep(poll)


def receive_live() -> None:
    state = load_state()
    message = bytes.fromhex(state["cctp"]["message"][2:])
    attestation = bytes.fromhex(state["cctp"]["attestation"][2:])
    c = client("ethereum")
    usdc = Erc20(c, ETHEREUM_USDC)
    before = usdc.balance_of(state["cctp"]["recipient"])
    print(f"ETH for gas: {c.w3.eth.get_balance(c.sender) / 1e18}")
    ok = RECEIVE_MESSAGE.decode(
        c.call(
            ETHEREUM_MESSAGE_TRANSMITTER,
            RECEIVE_MESSAGE.encode(message, attestation),
            sender=c.sender,
        )
    )[0]
    assert ok, "receiveMessage simulation returned false"
    result = c.send(ETHEREUM_MESSAGE_TRANSMITTER, RECEIVE_MESSAGE.encode(message, attestation))
    after = usdc.balance_of(state["cctp"]["recipient"])
    print(
        f"receiveMessage tx {result.tx_hash} (block {result.block_number}): minted {(after - before) / 1e6} USDC on Ethereum"
    )
    state["cctp"]["receiveTxHash"] = result.tx_hash
    save_state(state)


# Fork rehearsal

USDC_HOLDER = "0xA4D94019934D8333Ef880ABFFbF2FDd611C762BD"  # Aave aPolUSDCn


def rehearse() -> None:
    import boa

    boa.fork(rpc("polygon"), block_identifier="latest")
    eoa = boa.env.generate_address("eoa")
    boa.env.eoa = eoa
    boa.env.set_balance(eoa, 10**20)
    usdc = boa.load_partial("contracts/testing/ERC20Mock.vy").at(POLYGON_USDC)
    with boa.env.prank(USDC_HOLDER):
        usdc.transfer(eoa, 5 * 10**6)

    bridger = boa.load("contracts/hooks/CCTPBridger.vy", POLYGON_TOKEN_MESSENGER, ETHEREUM_DOMAIN)
    print(f"CCTPBridger on fork: {bridger.address}, deploy gas {boa.env.get_gas_used()}")
    assert bridger.cost() == 0 and bridger.check(eoa)
    usdc.approve(bridger.address, 2**256 - 1)
    supply_before = usdc.totalSupply()
    sent = bridger.bridge(POLYGON_USDC, eoa, 2 * 10**6, 0)
    print(
        f"bridge(): sent {sent / 1e6} USDC; EOA left {usdc.balanceOf(eoa) / 1e6}; bridger holds {usdc.balanceOf(bridger.address)}; supply burned {(supply_before - usdc.totalSupply()) / 1e6}"
    )
    assert sent == 2 * 10**6 and usdc.balanceOf(bridger.address) == 0
    # Below the threshold: nothing moves.
    assert bridger.bridge(POLYGON_USDC, eoa, 10**6, 5 * 10**6) == 0
    # Whole balance with max_value, swept together with a donation.
    usdc.transfer(bridger.address, 10**5)
    sent = bridger.bridge(POLYGON_USDC, eoa, 2**256 - 1, 0)
    print(
        f"bridge(max): sent {sent / 1e6} USDC (2.9 own + 0.1 donated), EOA left {usdc.balanceOf(eoa)}"
    )
    assert sent == 3 * 10**6


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["rehearse", "deploy", "bridge", "attest", "receive"])
    parser.add_argument("--amount", type=float, default=2.0, help="USDC to bridge (bridge)")
    args = parser.parse_args(argv)
    if args.command == "rehearse":
        rehearse()
    elif args.command == "deploy":
        deploy_live()
    elif args.command == "bridge":
        bridge_live(args.amount)
    elif args.command == "attest":
        attest()
    elif args.command == "receive":
        receive_live()
    return 0


if __name__ == "__main__":
    sys.exit(main())
