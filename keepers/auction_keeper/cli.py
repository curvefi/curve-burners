"""``python -m keepers.auction_keeper`` — run the publisher or print auction status."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from keepers.auction_keeper.chain.contracts import Erc20
from keepers.auction_keeper.config import KeeperConfig, NetworkConfig, load_config
from keepers.auction_keeper.core.keeper import NetworkKeeper, run_networks
from keepers.auction_keeper.core.state import KeeperState


def _networks(config: KeeperConfig, names: Sequence[str]) -> list[NetworkConfig]:
    return [config.network(name) for name in names] if names else list(config.networks.values())


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state = KeeperState(None if args.dry_run else config.state_path)
    keepers = [
        NetworkKeeper(network, state, dry_run=args.dry_run)
        for network in _networks(config, args.network)
    ]
    run_networks(keepers, once=args.once)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state = KeeperState(config.state_path)
    for network in _networks(config, args.network):
        keeper = NetworkKeeper(network, state, dry_run=True)
        print(f"== {network.name} (chain {keeper.client.chain_id})")
        for auction in keeper.auctions:
            publishers = keeper.publishers_for(auction)
            print(f"auction {auction.address}: want {auction.want()}")
            print("  adapters: " + ", ".join(f"{p.version}@{p.address}" for p in publishers))
            for token in sorted(keeper.candidate_tokens(auction)):
                lot = auction.lot_view(token)
                symbol = Erc20(keeper.client, token).symbol()
                status = (
                    f"available={lot.available} needed={lot.amount_needed}"
                    if lot.active
                    else "inactive"
                )
                print(f"  {symbol:>8} {token}: {status} window=[{lot.start}, {lot.end})")
                if lot.active:
                    print(f"           schedule: {keeper.plan_for(auction, lot)}")
                    if keeper.native is not None:
                        quote = keeper.native.quote(auction, lot)
                        print(f"           native: {quote if quote else 'no route'}")
                for publisher in publishers:
                    record = state.order(network.name, auction.address, publisher.address, token)
                    if record is not None:
                        print(
                            f"           {publisher.version}: {record.uid} valid_to={record.valid_to}"
                        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auction_keeper", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="publish orders for live lots")
    run.add_argument("--config", required=True)
    run.add_argument(
        "--network", action="append", default=[], help="limit to a network (repeatable)"
    )
    run.add_argument("--once", action="store_true", help="one cycle per network, then exit")
    run.add_argument("--dry-run", action="store_true", help="build and validate, post nothing")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="print lots and recorded orders")
    status.add_argument("--config", required=True)
    status.add_argument("--network", action="append", default=[])
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
