"""Fraxtal production deployment: FeeCollector + Hooker (LayerZero forward) + DutchAuctionBurner.

    python scripts/deploy_fraxtal.py --fork      # rehearsal on a local fork, nothing sent
    python scripts/deploy_fraxtal.py             # live, signs with POLYGON_TEST_KEEPER_KEY

No CoW on Fraxtal: the AdapterRegistry stays empty and lots settle through the
native take() (the AuctionTaker periphery is deployed for route fills). The
FORWARD duty hook sends frxUSD to the Ethereum treasury through LZOFTBridger
over the Frax OFT hub; the keeper passes the LayerZero fee as value and its
own address as the refund receiver (appended to the hook data).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import boa
from dotenv import load_dotenv
from eth_account import Account
from eth_utils import to_checksum_address

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

chain = "fraxtal"
CHAIN_ID = 252
KEY_ENV = "POLYGON_TEST_KEEPER_KEY"  # ALTER: deployer key env var (same EOA on every chain)

TARGET = to_checksum_address("0xfc00000000000000000000000000000000000001")  # frxUSD
TARGET_DECIMALS = 18
WETH = to_checksum_address(
    "0xfc00000000000000000000000000000000000002"
)  # WFRAX: FRAX is the gas token
ADMIN = "0x71F718D3e4d1449D1502A6A7595eb84eBcCB1683"  # ALTER: owner after setup
EMERGENCY_ADMIN = "0x6d447e544D01a59cb0774763bf15526574CffFeD"

ONE = 10**18
MAX_FEE = ONE // 1000  # 0.1% COLLECT and FORWARD keeper fee
MIN_EXCHANGE_AMOUNT = 5 * 10**TARGET_DECIMALS  # auction floor: 5 frxUSD
MIN_BRIDGE_AMOUNT = 100 * 10**TARGET_DECIMALS  # bridge only >= 100 frxUSD (LZ fee ~3 FRAX)
ETHEREUM_FEE_DESTINATION = (
    "0x6508eF65b0Bd57eaBD0f1D52685A70433B2d290B"  # Curve treasury on Ethereum
)

START_TOTAL = 100_000 * 10**TARGET_DECIMALS
STEP_DURATION = 30

# Frax frxUSD OFT hub (OFTAdapter over frxUSD): peers the Ethereum lockbox.
FRXUSD_OFT_HUB = to_checksum_address("0x96a394058e2b84a89bac9667b19661ed003cf5d4")
ETHEREUM_EID = 30101
LZ_QUOTE_AMOUNT = 1000 * ONE
LZ_EXTRA_OPTIONS = b""  # the hub enforces lzReceive gas toward Ethereum

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
FRXUSD_HOLDER = to_checksum_address(
    "0xeE454138083b9B9714cac3c7cF12560248d76D6B"
)  # fork-only: frxUSD/FXB2029 Curve pool
EMPTY_COMPENSATION = (0, (0, 0, 0), 0, 0, False)
EMPTY_HOOK_INPUT = (0, 0, b"")

# Pieces an earlier live run already deployed: checked, then reused
# (DEPLOY_FRESH=1 ignores them, e.g. on a fork).
EXISTING = {
    "LZOFTBridger": "0xa833a94f70A492CB4Ae289415Cbae130c1Bec3BD",  # 2026-10-23, bridge test passed
    "FeeCollector": None,
    "Hooker": None,
    "AdapterRegistry": None,
    "DutchAuctionBurner": None,
    "AuctionTaker": None,
}

DEPLOYMENTS_PATH = ROOT / "deployments.json"
KEEPER_CONFIG_PATH = ROOT / "keepers" / "auction_keeper" / "configs" / "config.fraxtal.yaml"


def rpc_url() -> str:
    load_dotenv(Path.home() / ".env", override=False)
    return f"https://lb.drpc.org/ogrpc?network=fraxtal&dkey={os.environ['DRPC_API_KEY']}"


def existing(name: str, path: str):
    address = EXISTING.get(name)
    if address and os.environ.get("DEPLOY_FRESH") is None:
        print(f"{name}: reusing {address}")
        return boa.load_partial(path).at(address)
    return None


def gas() -> int:
    return boa.env.get_gas_used()


def deploy(fork: bool) -> dict:
    total = 0
    fee_collector = existing("FeeCollector", "contracts/FeeCollector.vy")
    if fee_collector is None:
        fee_collector = boa.load(
            "contracts/FeeCollector.vy", TARGET, WETH, boa.env.eoa, EMERGENCY_ADMIN
        )
        total += gas()
    print(f"FeeCollector: {fee_collector.address}")
    assert fee_collector.target() == TARGET and fee_collector.owner() == boa.env.eoa
    assert fee_collector.emergency_owner() == EMERGENCY_ADMIN
    if fee_collector.max_fee(2) != MAX_FEE:
        fee_collector.set_max_fee(2, MAX_FEE)  # COLLECT
    if fee_collector.max_fee(8) != MAX_FEE:
        fee_collector.set_max_fee(8, MAX_FEE)  # FORWARD
    total += gas()

    bridger, hooker = deploy_hooks(fee_collector)
    total += gas()
    print(f"Hooker: {hooker.address}")
    if fee_collector.hooker() != hooker.address:
        fee_collector.set_hooker(hooker)
    total += gas()

    registry = existing("AdapterRegistry", "contracts/burners/adapters/AdapterRegistry.vy")
    if registry is None:
        registry = boa.load("contracts/burners/adapters/AdapterRegistry.vy", fee_collector.address)
        total += gas()
    print(f"AdapterRegistry: {registry.address} (empty: native settlement only)")
    assert registry.role_source() == fee_collector.address and registry.get_adapters() == []

    burner = existing("DutchAuctionBurner", "contracts/burners/DutchAuctionBurner.vy")
    if burner is None:
        burner = boa.load(
            "contracts/burners/DutchAuctionBurner.vy",
            fee_collector.address,
            START_TOTAL,
            MIN_EXCHANGE_AMOUNT,
            STEP_DURATION,
            registry.address,
        )
        total += gas()
    print(f"DutchAuctionBurner: {burner.address}")
    assert burner.fee_collector() == fee_collector.address
    assert burner.registry() == registry.address
    assert burner.auction_length() == 24 * 3600

    taker = existing("AuctionTaker", "contracts/burners/auction/periphery/AuctionTaker.vy")
    if taker is None:
        taker = boa.load("contracts/burners/auction/periphery/AuctionTaker.vy")
        total += gas()
    print(f"AuctionTaker: {taker.address}")

    if fee_collector.burner() != burner.address:
        fee_collector.set_burner(burner)
    fee_collector.set_killed([(ZERO_ADDRESS, 0)])  # burner and hooker are set: open every epoch
    total += gas()
    assert fee_collector.burner() == burner.address and fee_collector.hooker() == hooker.address

    deployment = {
        "FeeCollector": fee_collector.address,
        "Hooker": hooker.address,
        "Burner": burner.address,
        "AdapterRegistry": registry.address,
        "AuctionTaker": taker.address,
        "Bridger": bridger.address,
        "target": TARGET,
        "deployBlock": boa.env.evm.patch.block_number,
    }
    print(f"gas used: {total}")
    if fork:
        rehearse_forward(fee_collector, hooker, bridger)
    else:
        write_outputs(deployment)

    fee_collector.set_owner(ADMIN)
    assert fee_collector.owner() == ADMIN
    print(f"owner handed to {ADMIN}")
    return deployment


def bridge_foreplay(bridger) -> bytes:
    """bridge(token, treasury, max, min, _refund) without the refund word: the
    keeper appends its own address through the hook input data."""
    calldata = bridger.bridge.prepare_calldata(
        TARGET, ETHEREUM_FEE_DESTINATION, 2**256 - 1, MIN_BRIDGE_AMOUNT, ZERO_ADDRESS
    )
    return calldata[:-32]


def deploy_hooks(fee_collector):
    bridger = existing("LZOFTBridger", "contracts/hooks/LZOFTBridger.vy")
    if bridger is None:
        bridger = boa.load(
            "contracts/hooks/LZOFTBridger.vy",
            FRXUSD_OFT_HUB,
            ETHEREUM_EID,
            LZ_QUOTE_AMOUNT,
            LZ_EXTRA_OPTIONS,
        )
    assert bridger.TOKEN() == TARGET and bridger.DST_EID() == ETHEREUM_EID
    print(f"LZOFTBridger: {bridger.address}; cost() = {bridger.cost() / ONE} FRAX")

    target = boa.load_partial("contracts/testing/ERC20Mock.vy").at(TARGET)
    initial_oth = [
        (
            TARGET,
            target.approve.prepare_calldata(bridger.address, 2**256 - 1),
            EMPTY_COMPENSATION,
            False,
        )
    ]
    initial_oth_inputs = [EMPTY_HOOK_INPUT]
    initial_hooks = [(bridger.address, bridge_foreplay(bridger), EMPTY_COMPENSATION, True)]
    hooker = existing("Hooker", "contracts/hooks/Hooker.vy")
    if hooker is None:
        hooker = boa.load(
            "contracts/hooks/Hooker.vy",
            fee_collector,
            initial_oth,
            initial_oth_inputs,
            initial_hooks,
        )
    assert hooker.fee_collector() == fee_collector.address
    assert target.allowance(hooker.address, bridger.address) == 2**256 - 1
    hook = hooker.hooks(0)
    assert hook.to == bridger.address and hook.duty
    assert bytes(hook.foreplay) == initial_hooks[0][1], "bridge hook calldata differs"
    return bridger, hooker


def rehearse_forward(fee_collector, hooker, bridger) -> None:
    """Fork only: a FORWARD run through Hooker -> LZOFTBridger -> OFT hub."""
    from eth_abi import encode

    target = boa.load_partial("contracts/testing/ERC20Mock.vy").at(TARGET)
    keeper = boa.env.generate_address("keeper")
    boa.env.set_balance(keeper, 50 * ONE)
    with boa.env.prank(FRXUSD_HOLDER):
        target.transfer(fee_collector.address, 250 * ONE)
    # Native coin sent to the FeeCollector is wrapped into WFRAX by burn(ETH_ADDRESS).
    boa.env.set_balance(boa.env.eoa, boa.env.get_balance(boa.env.eoa) + ONE)
    boa.env.raw_call(fee_collector.address, value=ONE, data=b"")
    fee_collector.burn("0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE")
    wrapped = boa.load_partial("contracts/testing/ERC20Mock.vy").at(WETH)
    assert wrapped.balanceOf(fee_collector.address) == ONE, "WFRAX wrapping"

    start, _ = fee_collector.epoch_time_frame(8)
    now = boa.env.evm.patch.timestamp
    boa.env.time_travel(seconds=(start if start > now else start + 7 * 24 * 3600) - now + 60)
    cost = bridger.cost()
    hook_inputs = [(0, cost, encode(["address"], [keeper]))]
    with boa.env.prank(keeper):
        fee_collector.forward(hook_inputs, keeper, value=cost)
    print(
        f"forward rehearsal: FeeCollector frxUSD left {target.balanceOf(fee_collector.address) / ONE}, "
        f"hooker {target.balanceOf(hooker.address) / ONE}, keeper fee {target.balanceOf(keeper) / ONE}, "
        f"bridger holds {target.balanceOf(bridger.address)} / {boa.env.get_balance(bridger.address)}"
    )
    # Dust below the OFT's shared decimals (1e-6 frxUSD) stays on the Hooker for the next run.
    assert target.balanceOf(hooker.address) < 10**12 and target.balanceOf(bridger.address) == 0


def write_outputs(deployment: dict) -> None:
    deployments = json.loads(DEPLOYMENTS_PATH.read_text()) if DEPLOYMENTS_PATH.exists() else {}
    deployments[chain] = {k: v for k, v in deployment.items() if k not in ("target", "deployBlock")}
    DEPLOYMENTS_PATH.write_text(json.dumps(deployments, indent=2) + "\n")
    KEEPER_CONFIG_PATH.write_text(
        f"""# Fraxtal production keeper (generated by scripts/deploy_fraxtal.py).
# No settlement adapters here: lots are filled natively (Executor.take_with_route
# through the AuctionTaker, or take_with_limits); the loop only reports lots.
poll_interval: 60
order_ttl: 300
state_path: auction_keeper_state.fraxtal.json
networks:
  fraxtal:
    rpc: https://lb.drpc.org/ogrpc?network=fraxtal&dkey=${{DRPC_API_KEY}}
    auctions:
      - "{deployment["Burner"]}"
    start_block: {deployment["deployBlock"]}
    log_chunk: 50  # drpc free plan caps eth_getLogs ranges
    mode: offline
    auction_taker: "{deployment["AuctionTaker"]}"
    # keeper_key_env: POLYGON_TEST_KEEPER_KEY
"""
    )
    print(f"wrote {DEPLOYMENTS_PATH.name} and {KEEPER_CONFIG_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    fork = "--fork" in sys.argv[1:]
    url = rpc_url()
    if fork:
        boa.fork(url, block_identifier="latest")
        boa.env.eoa = (
            Account.from_key(os.environ[KEY_ENV]).address
            if os.environ.get(KEY_ENV)
            else boa.env.generate_address("deployer")
        )
        boa.env.set_balance(boa.env.eoa, 100 * ONE)
    else:
        boa.set_network_env(url)
        signer = Account.from_key(os.environ[KEY_ENV])
        boa.env.add_account(signer)
        boa.env.eoa = signer.address
    print(f"deployer {boa.env.eoa} on {chain}{' (fork)' if fork else ''}")
    deploy(fork)
    print("All set!")
