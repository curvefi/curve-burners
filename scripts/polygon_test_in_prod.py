"""Polygon test-in-prod of the Dutch auction stack with CoW settlement.

Deploys the real DutchAuctionBurner, AdapterRegistry, and CowAdapter against a
FeeCollectorMock whose calendar (period, epoch bounds), target, and fee are
set here, then drives the test: collect (stage lots), status, recover.
Publishing runs separately through the keeper (auction_keeper, config written
by ``deploy``).

    python scripts/polygon_test_in_prod.py rehearse            # whole flow on a fork, nothing sent
    python scripts/polygon_test_in_prod.py deploy              # live: deploy + write config
    python scripts/polygon_test_in_prod.py status              # epochs, lots, balances
    python scripts/polygon_test_in_prod.py collect             # live, inside COLLECT: stage lots
    python -m keepers.auction_keeper run --config keepers/auction_keeper/configs/config.polygon-test.yaml
    python scripts/polygon_test_in_prod.py recover             # pull every balance back to the EOA

The EOA is read from ``POLYGON_TEST_KEEPER_KEY`` (in ~/.env); it is the owner
of every contract, the proceeds go to the mock, and ``recover`` returns them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import boa
from dotenv import load_dotenv
from eth_account import Account
from eth_utils import keccak

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from keepers.auction_keeper.chain.abi import WAD  # noqa: E402
from keepers.auction_keeper.chain.boa_client import BoaClient  # noqa: E402

CHAIN = "polygon"
CHAIN_ID = 137
KEY_ENV = "POLYGON_TEST_KEEPER_KEY"
RPC_ENV = "DRPC_API_KEY"
RPC_TEMPLATE = "https://lb.drpc.org/ogrpc?network=polygon&dkey=${DRPC_API_KEY}"

# Tokens (Polygon PoS).
FRXUSD = "0x80Eede496655FB9047dd39d9f418d5483ED600df"  # Frax frxUSD (LayerZero OFT)
USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # native USDC (CCTP), the fallback target
CRV = "0x172370d5Cd63279eFa6d502DAB29171933a610AF"
WETH = "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619"
TARGET = FRXUSD
SELL_TOKENS = [USDC]  # ALTER: tokens the test auctions off (sent to the mock)
SELL_DECIMALS = {USDC: 6, CRV: 18, WETH: 18}

# CoW Protocol, deterministic across chains.
COW_SETTLEMENT = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"
COW_VAULT_RELAYER = "0xC92E8bdf79f0507f65a392b0ab4667716BFE0110"
APP_DATA_DOCUMENT = '{"appCode":"Curve Dutch auction test","metadata":{},"version":"1.3.0"}'
APP_DATA = keccak(text=APP_DATA_DOCUMENT)

# Calendar: 3-hour periods; SLEEP 15 min, COLLECT 30 min, EXCHANGE 2 h, FORWARD 15 min.
PERIOD = 3 * 3600
BOUNDS = (15 * 60, 45 * 60, 165 * 60)
EXCHANGE_LENGTH = BOUNDS[2] - BOUNDS[1]
# Curve: a full lot opens at START_TOTAL target and decays to FLOOR_TOTAL by the
# end of EXCHANGE in STEP_DURATION steps. Sized for a lot of 5 USDC (~5 target):
# the ask crosses the market about 40 minutes into the window.
START_TOTAL = 12 * WAD  # ALTER: ~2.5x the lot's market value, in target units
FLOOR_TOTAL = 1 * WAD  # ALTER: well below market
STEP_DURATION = 60
COLLECT_MAX_FEE = WAD // 100  # 1% COLLECT incentive to exercise the payout

# Keeper settings written into the config.
POLL_INTERVAL = 30
ORDER_TTL = 120

ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENTS_PATH = ROOT / "deployments.polygon-test.json"
CONFIG_PATH = ROOT / "keepers" / "auction_keeper" / "configs" / "config.polygon-test.yaml"

EPOCH_NAMES = {1: "SLEEP", 2: "COLLECT", 4: "EXCHANGE", 8: "FORWARD"}
ETH_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"


def rpc_url() -> str:
    load_dotenv(Path.home() / ".env", override=False)
    key = os.environ.get(RPC_ENV)
    if not key:
        raise SystemExit(f"{RPC_ENV} is not set in ~/.env")
    return RPC_TEMPLATE.replace("${DRPC_API_KEY}", key)


def account() -> Account:
    load_dotenv(Path.home() / ".env", override=False)
    key = os.environ.get(KEY_ENV)
    if not key:
        raise SystemExit(f"{KEY_ENV} is not set in ~/.env")
    return Account.from_key(key)


def setup(fork: bool) -> str:
    """Attach boa to Polygon (fork or live) and return the EOA address."""
    url = rpc_url()
    if fork:
        boa.fork(url, block_identifier="latest")  # drpc has no "safe" tag
        eoa = account().address if os.environ.get(KEY_ENV) else boa.env.generate_address("eoa")
        boa.env.eoa = eoa
        boa.env.set_balance(eoa, 1000 * WAD)
        return eoa
    boa.set_network_env(url)
    signer = account()
    boa.env.add_account(signer)
    boa.env.eoa = signer.address
    return signer.address


def erc20(address: str):
    return boa.load_partial("contracts/testing/ERC20Mock.vy").at(address)


def now() -> int:
    return boa.env.evm.patch.timestamp


# Deploy


def deploy(eoa: str, *, start_time: int | None = None) -> dict:
    gas = 0

    def used() -> int:
        return boa.env.get_gas_used()

    start_time = now() - 1 if start_time is None else start_time
    mock = boa.load(
        "contracts/testing/dutch_auction/FeeCollectorMock.vy",
        TARGET,
        eoa,
        eoa,
        start_time,
        PERIOD,
        list(BOUNDS),
    )
    gas += used()
    registry = boa.load("contracts/burners/adapters/AdapterRegistry.vy", mock.address)
    gas += used()
    cow_adapter = boa.load("contracts/burners/adapters/cow/CowAdapter.vy", COW_SETTLEMENT, APP_DATA)
    gas += used()
    assert cow_adapter.vault_relayer() == COW_VAULT_RELAYER
    burner = boa.load(
        "contracts/burners/DutchAuctionBurner.vy",
        mock.address,
        START_TOTAL,
        FLOOR_TOTAL,
        STEP_DURATION,
        registry.address,
    )
    gas += used()
    assert burner.auction_length() == EXCHANGE_LENGTH
    mock.set_burner(burner.address)
    gas += used()
    mock.set_max_fee(2, COLLECT_MAX_FEE)
    gas += used()
    registry.set_adapter(cow_adapter.address, COW_VAULT_RELAYER)
    gas += used()
    registry.activate_adapter(cow_adapter.address)
    gas += used()

    deployments = {
        "chain": CHAIN,
        "chainId": CHAIN_ID,
        "owner": eoa,
        "FeeCollectorMock": mock.address,
        "AdapterRegistry": registry.address,
        "CowAdapter": cow_adapter.address,
        "DutchAuctionBurner": burner.address,
        "target": TARGET,
        "sellTokens": SELL_TOKENS,
        "calendar": {"start_time": start_time, "period": PERIOD, "bounds": list(BOUNDS)},
        "curve": {
            "start_total": START_TOTAL,
            "floor_total": FLOOR_TOTAL,
            "step_duration": STEP_DURATION,
        },
        "appData": "0x" + APP_DATA.hex(),
        "appDataDocument": APP_DATA_DOCUMENT,
        "deployBlock": boa.env.evm.patch.block_number,
        "gasUsed": gas,
    }
    for name in ("FeeCollectorMock", "AdapterRegistry", "CowAdapter", "DutchAuctionBurner"):
        print(f"{name}: {deployments[name]}")
    print(f"gas used: {gas}")
    return deployments


def write_outputs(deployments: dict) -> None:
    DEPLOYMENTS_PATH.write_text(json.dumps(deployments, indent=2) + "\n")
    CONFIG_PATH.write_text(
        f"""# Generated by scripts/polygon_test_in_prod.py deploy; test-in-prod on Polygon.
poll_interval: {POLL_INTERVAL}
order_ttl: {ORDER_TTL}
state_path: auction_keeper_state.polygon-test.json
networks:
  polygon:
    rpc: {RPC_TEMPLATE}
    auctions:
      - "{deployments["DutchAuctionBurner"]}"
    start_block: {deployments["deployBlock"]}
    log_chunk: 50  # drpc free plan caps eth_getLogs ranges
    tokens:
      whitelist: {json.dumps(SELL_TOKENS)}
    mode: keeper
    keeper_key_env: {KEY_ENV}
    cow:
      app_data_document: '{APP_DATA_DOCUMENT}'
"""
    )
    print(f"wrote {DEPLOYMENTS_PATH.name} and {CONFIG_PATH.relative_to(ROOT)}")


def load_deployments() -> dict:
    if not DEPLOYMENTS_PATH.exists():
        raise SystemExit(f"{DEPLOYMENTS_PATH.name} missing; run deploy first")
    return json.loads(DEPLOYMENTS_PATH.read_text())


def contracts(deployments: dict) -> tuple:
    mock = boa.load_partial("contracts/testing/dutch_auction/FeeCollectorMock.vy").at(
        deployments["FeeCollectorMock"]
    )
    burner = boa.load_partial("contracts/burners/DutchAuctionBurner.vy").at(
        deployments["DutchAuctionBurner"]
    )
    return mock, burner


# Test flow


def status(deployments: dict) -> None:
    mock, burner = contracts(deployments)
    ts = now()
    epoch = mock.epoch()
    print(f"now {ts}: epoch {EPOCH_NAMES[epoch]}")
    for bit, name in EPOCH_NAMES.items():
        start, end = mock.epoch_time_frame(bit)
        print(f"  {name:8} [{start}, {end})  {'<- now' if start <= ts < end else ''}")
    target = erc20(TARGET)
    print(f"target {target.symbol()} on mock: {target.balanceOf(mock.address) / WAD}")
    for address in deployments["sellTokens"]:
        token = erc20(address)
        decimals = token.decimals()
        lot = burner.lots(address)
        start, end = burner.window(address)
        available = burner.available(address)
        needed = burner.getAmountNeeded(address) if available else 0
        print(
            f"{token.symbol()} {address}: mock {token.balanceOf(mock.address) / 10**decimals}, "
            f"burner {token.balanceOf(burner.address) / 10**decimals}, "
            f"lot initial {lot.initial_amount / 10**decimals} window [{start}, {end}) "
            f"available {available / 10**decimals} needed {needed / WAD} {target.symbol()}, "
            f"relayer allowance {token.allowance(burner.address, COW_VAULT_RELAYER) > 0}"
        )


def collect(deployments: dict, eoa: str) -> None:
    mock, burner = contracts(deployments)
    if mock.epoch() != 2:
        start, _ = mock.epoch_time_frame(2)
        if start <= now():
            start += PERIOD
        raise SystemExit(f"not in COLLECT; next COLLECT starts at {start} (in {start - now()} s)")
    tokens = sorted(deployments["sellTokens"], key=lambda a: int(a, 16))
    funded = [t for t in tokens if erc20(t).balanceOf(mock.address) > 0]
    if not funded:
        raise SystemExit("no sell token balance on the mock; send tokens to it first")
    mock.collect(funded, eoa)
    burner.sync_executor_approvals(COW_VAULT_RELAYER, funded)
    for address in funded:
        lot = burner.lots(address)
        print(f"staged {address}: initial {lot.initial_amount}, window {burner.window(address)}")


def recover(deployments: dict, eoa: str) -> None:
    mock, burner = contracts(deployments)
    tokens = list(deployments["sellTokens"])
    burner.recover(tokens)  # back to the mock (owner-only)
    burner.push_target()
    mock.recover([TARGET, *tokens, ETH_ADDRESS], eoa)
    print("recovered everything to", eoa)


# Fork rehearsal

HOLDERS = {  # fork-only: whales pranked to fund the rehearsal
    CRV: "0xc7c939A474CB10EB837894D1ed1a77C61B268Fa7",  # CRV/TRICRYPTO pool
    USDC: "0xA4D94019934D8333Ef880ABFFbF2FDd611C762BD",  # Aave aPolUSDCn
}
FRXUSD_HOLDER = "0xdCb72c163de84618417Bec9aeF7ae32B5336d70E"  # BRZ/frxUSD pool
REHEARSAL_TOKEN = SELL_TOKENS[0]
REHEARSAL_LOT = 5 * 10 ** SELL_DECIMALS[REHEARSAL_TOKEN]


def rehearse(eoa: str) -> None:
    from keepers.auction_keeper.chain.contracts import AdapterRegistry, Auction
    from keepers.auction_keeper.config import CowSettings, NetworkConfig, TokenFilter
    from keepers.auction_keeper.execute import Executor
    from keepers.auction_keeper.rails.cow import CowPublisher

    deployments = deploy(eoa)
    mock, burner = contracts(deployments)
    sell = erc20(REHEARSAL_TOKEN)
    unit = 10 ** SELL_DECIMALS[REHEARSAL_TOKEN]
    symbol = sell.symbol()
    frxusd = erc20(TARGET)
    with boa.env.prank(HOLDERS[REHEARSAL_TOKEN]):
        sell.transfer(mock.address, REHEARSAL_LOT)

    collect_start, _ = mock.epoch_time_frame(2)
    boa.env.time_travel(seconds=collect_start + 60 - now())
    collect(deployments, eoa)
    fee_paid = sell.balanceOf(eoa)
    print(f"COLLECT incentive paid to the keeper: {fee_paid / unit} {symbol}")

    exchange_start, exchange_end = mock.epoch_time_frame(4)
    boa.env.time_travel(seconds=exchange_start + 40 * 60 - now())
    status(deployments)

    client = BoaClient(sender=eoa)
    auction = Auction(client, burner.address)
    config = NetworkConfig(
        name="polygon-fork",
        rpc="fork",
        auctions=(burner.address,),
        tokens=TokenFilter(whitelist=frozenset([REHEARSAL_TOKEN])),
        poll_interval=POLL_INTERVAL,
        order_ttl=ORDER_TTL,
        mode="keeper",
        keeper_key_env=KEY_ENV,
        cow=CowSettings(app_data_document=APP_DATA_DOCUMENT),
    )
    (adapter,) = AdapterRegistry(client, auction.registry()).active_adapters()
    publisher = CowPublisher(client, adapter, config, orderbook=None)  # type: ignore[arg-type]
    lot = auction.lot_view(REHEARSAL_TOKEN)
    order = publisher.build_order(lot, publisher.valid_to(lot))
    reason = publisher.validate(auction, order)
    print(
        "CoW order:",
        json.dumps(order.api_body(burner.address, order.signature(adapter.adapter)), indent=1),
    )
    print("router verdict:", reason or "valid")
    assert reason is None, reason
    result = publisher.publish(auction, lot, dry_run=True)
    assert result is not None
    print(
        f"dry-run publish uid {result.uid}: sell {result.sell_amount / unit} {symbol} for >= {result.buy_amount / WAD} frxUSD"
    )

    # Settle natively from the EOA's own frxUSD, as a filler would.
    payment = burner.getAmountNeeded(REHEARSAL_TOKEN)
    with boa.env.prank(FRXUSD_HOLDER):
        frxusd.transfer(eoa, payment)
    executor = Executor(client, sleep=lambda s: boa.env.time_travel(seconds=int(s)))
    executor.take_direct(burner.address, REHEARSAL_TOKEN, max_payment=payment)
    print(
        f"native take: paid {payment / WAD} frxUSD, mock holds {frxusd.balanceOf(mock.address) / WAD} frxUSD, EOA holds {sell.balanceOf(eoa) / unit} {symbol}"
    )
    assert burner.available(REHEARSAL_TOKEN) == 0

    recover(deployments, eoa)
    print(
        f"EOA after recover: {frxusd.balanceOf(eoa) / WAD} frxUSD, {sell.balanceOf(eoa) / unit} {symbol}"
    )
    print(f"total deploy gas {deployments['gasUsed']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["rehearse", "deploy", "status", "collect", "recover"])
    parser.add_argument(
        "--fork", action="store_true", help="run against a local fork (nothing sent)"
    )
    args = parser.parse_args(argv)
    fork = args.fork or args.command == "rehearse"
    eoa = setup(fork)
    print(
        f"EOA {eoa} on {CHAIN}{' (fork)' if fork else ''}, block {boa.env.evm.patch.block_number}"
    )

    if args.command == "rehearse":
        rehearse(eoa)
    elif args.command == "deploy":
        deployments = deploy(eoa)
        if not fork:
            write_outputs(deployments)
    elif args.command == "status":
        status(load_deployments())
    elif args.command == "collect":
        collect(load_deployments(), eoa)
    elif args.command == "recover":
        recover(load_deployments(), eoa)
    return 0


if __name__ == "__main__":
    started = time.monotonic()
    code = main()
    print(f"done in {time.monotonic() - started:.0f}s")
    sys.exit(code)
