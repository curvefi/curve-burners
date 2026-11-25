"""``python -m keepers.auction_keeper`` — run the publisher or print auction status."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace

from keepers.auction_keeper.chain.contracts import Erc20
from keepers.auction_keeper.config import KeeperConfig, NetworkConfig, load_config
from keepers.auction_keeper.core.keeper import NetworkKeeper, run_networks
from keepers.auction_keeper.core.state import KeeperState
from keepers.auction_keeper.limits import GATES
from keepers.auction_keeper.logs import LogSettings
from keepers.auction_keeper.logs import setup as setup_logging


def _prepare(args: argparse.Namespace) -> KeeperConfig:
    """Load the config and apply what is process-wide: the log and the HTTP budgets."""
    try:
        config = load_config(args.config)
    except Exception:
        setup_logging(LogSettings(), verbose=args.verbose)
        raise
    log = config.log
    if getattr(args, "log_file", None):
        log = replace(log, file=args.log_file)
    setup_logging(log, verbose=args.verbose)
    limits = config.limits
    GATES.configure(default=limits.http, hosts=limits.hosts, cooldown=limits.cooldown)
    return config


def _networks(config: KeeperConfig, names: Sequence[str]) -> list[NetworkConfig]:
    return [config.network(name) for name in names] if names else list(config.networks.values())


def cmd_run(args: argparse.Namespace) -> int:
    config = _prepare(args)
    state = KeeperState(None if args.dry_run else config.state_path)
    keepers = [
        NetworkKeeper(network, state, dry_run=args.dry_run)
        for network in _networks(config, args.network)
    ]
    run_networks(keepers, once=args.once)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = _prepare(args)
    state = KeeperState(config.state_path)
    for network in _networks(config, args.network):
        keeper = NetworkKeeper(network, state, dry_run=True)
        print(f"== {network.name} (chain {keeper.client.chain_id})")
        for auction in keeper.auctions:
            publishers = keeper.publishers_for(auction)
            # The book and the estimates the running keeper saved; nothing is priced here.
            seen = keeper.survey(auction, refresh=False)
            book = seen.book
            chosen = {item.token for item in seen.chosen}
            print(f"auction {auction.address}: want {book.want}")
            print("  adapters: " + ", ".join(f"{p.version}@{p.address}" for p in publishers))
            print(
                f"  {len(book.lots)} lots known, {len(seen.ranked)} live, {len(chosen)} in focus; "
                f"priced for timestamp {seen.ts} (latest block {seen.now})"
            )
            for item in seen.ranked:
                lot = book.view(item.token, seen.ts)
                if lot is None:
                    continue
                symbol = Erc20(keeper.client, item.token).symbol() if args.symbols else ""
                distance = (
                    "no estimate" if item.ratio is None else f"{item.ratio - 1:+.1%} to market"
                )
                mark = "*" if item.token in chosen else " "
                print(
                    f" {mark}{symbol:>8} {item.token}: available={lot.available} "
                    f"needed={lot.amount_needed} ({distance}) window=[{lot.start}, {lot.end})"
                )
                print(f"           schedule: {keeper.plan_for(book, lot)}")
                if keeper.native is not None and item.token in chosen:
                    quote = keeper.native.quote(auction, lot)
                    print(f"           native: {quote if quote else 'no route'}")
                for publisher in publishers:
                    record = state.order(
                        network.name, auction.address, publisher.address, item.token
                    )
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
    run.add_argument(
        "--log-file",
        help="write the log to this file, rotated at log.max_mb (default: log.file, else stderr)",
    )
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="print lots and recorded orders")
    status.add_argument("--config", required=True)
    status.add_argument("--network", action="append", default=[])
    status.add_argument(
        "--symbols", action="store_true", help="read each live lot's token symbol from the node"
    )
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
