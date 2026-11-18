"""Electric router: routes and calldata from ``michwill/electric-router``.

The electric router solves the whole pool graph as a resistor network and
settles the result through its ``ElectricRouter`` contract: splits across
pools, LP deposits and withdrawals, vault and wrapper legs, each leg bounded
by its own minimum rate. Route finding is off-chain, so the keeper asks the
``erouter`` command line for a route at the exact amount and gets ready
calldata for ``ElectricRouter.execute`` back::

    erouter route --chain fraxtal --from TOKEN --to WANT --amount-wei N \\
        --calldata --receiver AUCTION_TAKER --json OUT.json

The tool runs as a subprocess on purpose: it is AGPL-licensed, pins its own
dependencies (numpy 2, its own compiler for the dev tools) and changes fast.
The keeper reads only the JSON report, and treats the call in it as opaque
bytes, so leg kinds added there (Uniswap pools, say) need no change here.
The route is safe to execute blind: the AuctionTaker's take reverts unless
the swap leaves at least the auction's payment in want.

Differences from the Curve router NG rail (``keepers.auction_keeper.routing.curve_router``):

- a route is priced for one amount; another amount is another ``erouter``
  run (seconds, not a ``get_dy``), so quotes are cached for ``cache`` seconds
  and the chunk search uses the price impact the tool reports instead of a
  bisection;
- the per-leg minimum rates in the calldata are derived at the quote's
  block; a take that lands after the pools moved reverts in simulation and
  is quoted again next poll.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from eth_utils import is_address, to_checksum_address

from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.execute import Call, approve_call, raw_call

log = logging.getLogger(__name__)

# erouter chain names by chain id; override with native.electric.chain.
CHAINS: dict[int, str] = {
    1: "ethereum",
    10: "optimism",
    56: "bsc",
    100: "gnosis",
    137: "polygon",
    143: "monad",
    146: "sonic",
    196: "xlayer",
    239: "tac",
    252: "fraxtal",
    8453: "base",
    9745: "plasma",
    42161: "arbitrum",
    42220: "celo",
    43114: "avalanche",
}

# AuctionTaker.MAX_CALL_DATA: a longer route step fails to decode in the
# take callback, so such a quote is dropped here.
TAKER_MAX_CALL_DATA = 1888
# Chunk search: re-quotes after the first, and the undershoot applied to the
# proportional step (impact grows faster than linearly on a curved pool).
CHUNK_STEPS = 3
CHUNK_SAFETY = 0.9
# The tool's exit code for "no route" and "token not in the pool universe",
# explained on stdout; everything else non-zero is a failure of the tool.
EXIT_NO_ROUTE = 2
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class ElectricRouterError(RuntimeError):
    """The ``erouter`` tool failed, or its report cannot be trusted."""


@dataclass(frozen=True)
class ElectricRoute:
    """One ``erouter`` answer: a route for exactly ``amount`` and its call."""

    token: str  # lowercase
    want: str  # lowercase
    amount: int
    dy: int  # want the chain says the route pays at the quote's block
    guaranteed: int  # want the per-leg minimum rates alone promise
    impact: float | None  # price impact as a fraction; None when not measured
    router: str | None  # ElectricRouter.execute target; None for a quote-only run
    data: bytes  # calldata for the router; empty for a quote-only run
    pools: tuple[str, ...]
    block: int

    def __str__(self) -> str:
        return f"electric router, {len(self.pools)} leg(s) at block {self.block}"


Runner = Callable[..., Any]


class ElectricRouter:
    """Quotes and swap calls from the ``erouter`` command line."""

    def __init__(
        self,
        chain: str,
        *,
        taker: str | None = None,
        command: Sequence[str] = ("erouter",),
        rpc_url: str | None = None,
        router: str | None = None,
        slippage_bp: float | None = None,
        max_legs: int = 16,
        min_tvl: float | None = None,
        timeout: int = 180,
        cache: int = 30,
        run: Runner = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.chain = chain
        # The receiver of the swap and the sender of the call: the AuctionTaker.
        # Without one the router only quotes (the ``electric`` price source).
        self.taker = to_checksum_address(taker) if taker else None
        self.command = tuple(command)
        self.rpc_url = rpc_url
        # When set, a report naming another ElectricRouter address is refused.
        self.router = to_checksum_address(router) if router else None
        self.slippage_bp = slippage_bp
        self.max_legs = max_legs
        self.min_tvl = min_tvl
        self.timeout = timeout
        self.cache = cache
        self._run = run
        self._clock = clock
        self._quotes: dict[tuple[str, str, int], tuple[float, ElectricRoute | None]] = {}

    # The tool

    def _argv(self, token: str, want: str, amount: int, report: str) -> list[str]:
        argv = [
            *self.command,
            "route",
            "--chain",
            self.chain,
            "--from",
            token,
            "--to",
            want,
            "--amount-wei",
            str(amount),
            "--max-legs",
            str(self.max_legs),
            "--json",
            report,
            "--ascii",
            "--no-color",
        ]
        if self.taker is not None:
            argv += ["--calldata", "needed", "--receiver", self.taker]
            if self.slippage_bp is not None:
                argv += ["--slippage-bp", str(self.slippage_bp)]
        if self.rpc_url:
            argv += ["--rpc-url", self.rpc_url]
        if self.min_tvl is not None:
            argv += ["--min-tvl", str(self.min_tvl)]
        return argv

    def _report(self, token: str, want: str, amount: int) -> dict[str, Any] | None:
        """The tool's JSON report; None when it found nothing to route."""
        with tempfile.TemporaryDirectory(prefix="erouter-") as tmp:
            path = os.path.join(tmp, "route.json")
            try:
                done = self._run(
                    self._argv(token, want, amount, path),
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
            except FileNotFoundError as exc:
                raise ElectricRouterError(
                    f"{self.command[0]!r} not found; install electric-router "
                    "or set native.electric.command"
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise ElectricRouterError(f"erouter timed out after {self.timeout}s") from exc
            if not os.path.exists(path):
                # No report: either nothing routes between the tokens or the
                # tool broke.
                said = _last_line(done.stdout)
                if done.returncode == 0 or (done.returncode == EXIT_NO_ROUTE and said):
                    log.info("erouter %s -> %s: %s", token, want, said or "no report")
                    return None
                raise ElectricRouterError(
                    f"erouter exited {done.returncode}: {said or _last_line(done.stderr)}"
                )
            with open(path) as handle:
                return json.load(handle)

    def _parse(
        self, payload: dict[str, Any], token: str, want: str, amount: int
    ) -> ElectricRoute | None:
        result = payload.get("result") or {}
        block = int((payload.get("chain") or {}).get("block") or 0)
        impact_bp = (result.get("price_impact") or {}).get("bp")
        impact = max(float(impact_bp), 0.0) / 1e4 if impact_bp is not None else None
        # Only an output the chain confirmed: the solver's own model is good
        # enough to choose pools and no better.
        if not result.get("verified") or not result.get("verified_out"):
            log.warning("erouter %s -> %s: output not verified on-chain; dropped", token, want)
            return None
        dy = int(result["verified_out"])
        if self.taker is None:
            return ElectricRoute(token, want, amount, dy, 0, impact, None, b"", (), block)

        call = payload.get("call")
        if not call:
            log.warning("erouter %s -> %s: no call in the report; dropped", token, want)
            return None
        # The report must answer the question asked: the call is executed
        # as-is, so a different amount, pair or receiver is a different trade.
        asked = (amount, token, want, self.taker.lower())
        told = (
            int(call["amount_in"]),
            str(call["token_in"]).lower(),
            str(call["token_out"]).lower(),
            str(call["receiver"]).lower(),
        )
        if asked != told:
            raise ElectricRouterError(f"erouter answered {told}, asked {asked}")
        target = str(call["to"])
        if not is_address(target):
            raise ElectricRouterError(f"erouter names a bad router address {target!r}")
        target = to_checksum_address(target)
        if self.router is not None and target != self.router:
            raise ElectricRouterError(
                f"erouter targets {target}, native.electric.router pins {self.router}"
            )
        data = bytes.fromhex(str(call["calldata"]).removeprefix("0x"))
        if len(data) > TAKER_MAX_CALL_DATA:
            log.warning(
                "erouter %s -> %s: %s bytes of calldata, the AuctionTaker takes %s; "
                "lower native.electric.max_legs",
                token,
                want,
                len(data),
                TAKER_MAX_CALL_DATA,
            )
            return None
        if call.get("unbounded_legs"):
            log.info(
                "erouter %s -> %s: leg(s) %s carry no minimum rate",
                token,
                want,
                call["unbounded_legs"],
            )
        return ElectricRoute(
            token,
            want,
            amount,
            dy,
            int(call.get("guaranteed_out") or 0),
            impact,
            target,
            data,
            tuple(to_checksum_address(pool) for pool in call.get("pools") or ()),
            block,
        )

    # Quotes

    def quote(self, token: str, want: str, amount: int) -> ElectricRoute | None:
        """The route for exactly ``amount``; None when nothing routes."""
        token, want = token.lower(), want.lower()
        if token == want or amount <= 0:
            return None
        key = (token, want, amount)
        cached = self._quotes.get(key)
        now = self._clock()
        if cached is not None and now - cached[0] < self.cache:
            return cached[1]
        payload = self._report(token, want, amount)
        route = self._parse(payload, token, want, amount) if payload else None
        now = self._clock()
        self._quotes = {k: v for k, v in self._quotes.items() if now - v[0] < self.cache}
        self._quotes[key] = (now, route)
        return route

    def best_route(self, token: str, want: str, amount: int) -> tuple[ElectricRoute, int] | None:
        route = self.quote(token, want, amount)
        return (route, route.dy) if route is not None and route.dy > 0 else None

    def chunk(
        self, route: ElectricRoute, available: int, max_impact: float
    ) -> tuple[ElectricRoute, int, int, float] | None:
        """The route for the largest amount up to ``available`` whose reported
        price impact stays under ``max_impact``: (route, amount, dy, impact).
        Impact is close to linear in size, so each step scales the amount by
        the overshoot; None when no step gets under the limit."""
        for _ in range(CHUNK_STEPS + 1):
            if route.impact is None or route.impact <= max_impact:
                return route, route.amount, route.dy, route.impact or 0.0
            amount = int(route.amount * max_impact / route.impact * CHUNK_SAFETY)
            smaller = self.quote(route.token, route.want, amount)
            if smaller is None or smaller.dy <= 0:
                return None
            route = smaller
        return None

    def swap_calls(
        self, route: ElectricRoute, amount: int, min_dy: int, receiver: str
    ) -> list[Call]:
        """The AuctionTaker's route steps: approve the router, run the call.

        ``min_dy`` is not written into the call: the tool's calldata is used
        byte for byte, every leg carries its own minimum rate, and the take
        reverts unless the swap leaves the auction's payment on the taker."""
        if route.router is None or not route.data:
            raise ElectricRouterError("a quote-only route has no call")
        if amount != route.amount:
            raise ElectricRouterError(f"route is for {route.amount}, asked to swap {amount}")
        if self.taker is None or to_checksum_address(receiver) != self.taker:
            raise ElectricRouterError(f"route pays {self.taker}, asked to pay {receiver}")
        return [
            approve_call(route.token, route.router, amount),
            raw_call(route.router, route.data),
        ]


def _last_line(text: str | None) -> str:
    lines = [line.strip() for line in _ANSI.sub("", text or "").splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


# One instance per distinct setup: the native rail and the ``electric`` price
# source of a network then share the quote cache instead of each paying for
# an ``erouter`` run.
_ROUTERS: dict[tuple[Any, ...], ElectricRouter] = {}


def electric_router_for(config: Any, client: ChainClient) -> ElectricRouter:
    """An ``ElectricRouter`` from a network config's ``native.electric``
    section (the chain name defaults by chain id)."""
    from keepers.auction_keeper.config import ConfigError

    settings = config.native.electric
    chain = settings.chain or CHAINS.get(client.chain_id)
    if chain is None:
        raise ConfigError(
            f"{config.name}: no erouter chain name for chain {client.chain_id}; "
            "set native.electric.chain"
        )
    key = (chain, config.auction_taker, settings)
    if key not in _ROUTERS:
        _ROUTERS[key] = ElectricRouter(
            chain,
            taker=config.auction_taker,
            command=settings.command,
            rpc_url=settings.rpc_url,
            router=settings.router,
            slippage_bp=settings.slippage_bp,
            max_legs=settings.max_legs,
            min_tvl=settings.min_tvl,
            timeout=settings.timeout,
            cache=settings.cache,
        )
    return _ROUTERS[key]


__all__ = [
    "CHAINS",
    "TAKER_MAX_CALL_DATA",
    "ElectricRoute",
    "ElectricRouter",
    "ElectricRouterError",
    "electric_router_for",
]
