"""Custom executions: take a lot natively, by a route or from your own want.

Built for scripted and agent-driven fills: import, describe what to take and
through which route, and call. Two rails:

* ``Executor.take_with_route`` — the ``AuctionTaker`` periphery takes the lot,
  runs the route (e.g. approve + swap on a DEX) in the auction callback, pays
  the quote from the proceeds, and forwards the profit. Needs no want up
  front, only gas.
* ``Executor.take_direct`` — ``take_with_limits`` paid from the sender's own
  want balance, with an explicit payment cap and deadline.

Both wait for the trigger (``not_before``, ``max_payment``) by polling the
node, simulate the call, then send it.

.. code-block:: python

    from auction_keeper import connect, approve_call, contract_call

    ex = connect("config.yaml", "ethereum")        # uses the network's keeper key
    lot = ex.lot(AUCTION, CRV)                      # live snapshot: available, amount_needed
    route = [
        approve_call(CRV, ROUTER, lot.available),
        contract_call(ROUTER, "exchange(address[11],uint256[5][5],uint256,uint256,address[5],address)",
                      path, params, lot.available, min_out, pools, ex.taker.address),
    ]
    ex.take_with_route(TakePlan(AUCTION, CRV, route=route, min_profit=0),
                       max_payment=min_out, not_before=lot.start + 3600)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eth_abi import encode
from eth_utils import to_checksum_address

from keepers.auction_keeper.chain.abi import MAX_UINT256, CallReverted, Function, selector
from keepers.auction_keeper.chain.client import ChainClient, TxResult, Web3Client
from keepers.auction_keeper.chain.contracts import Auction, AuctionTaker, Erc20, LotView
from keepers.auction_keeper.config import KeeperConfig, NetworkConfig, load_config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Call:
    """One raw route step executed by the AuctionTaker with itself as sender."""

    target: str
    data: bytes

    def as_tuple(self) -> tuple[str, bytes]:
        return to_checksum_address(self.target), self.data


def contract_call(target: str, signature: str, *args: Any) -> Call:
    """``Call`` for ``signature`` (``"swap(address,uint256)"``) with ABI-encoded ``args``."""
    return Call(to_checksum_address(target), Function(signature).encode(*args))


def approve_call(token: str, spender: str, amount: int = MAX_UINT256) -> Call:
    return contract_call(token, "approve(address,uint256)", to_checksum_address(spender), amount)


def raw_call(target: str, data: bytes | str) -> Call:
    """``Call`` from ready calldata, e.g. an aggregator API's ``tx.data``."""
    if isinstance(data, str):
        data = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    return Call(to_checksum_address(target), data)


CURVE_ROUTER_EXCHANGE = "exchange(address[11],uint256[5][5],uint256,uint256,address[5],address)"


def curve_router_exchange(
    router: str,
    route: list[str],
    swap_params: list[list[int]],
    amount: int,
    min_dy: int,
    receiver: str,
    pools: list[str] | None = None,
) -> Call:
    """Curve Router NG ``exchange``; ``route``/``swap_params``/``pools`` are padded to 11/5/5."""
    zero = "0x" + "00" * 20
    route = [to_checksum_address(a) for a in route] + [zero] * (11 - len(route))
    params = [list(p) + [0] * (5 - len(p)) for p in swap_params]
    params += [[0] * 5] * (5 - len(params))
    pools = [to_checksum_address(p) for p in (pools or [])] + [zero] * (5 - len(pools or []))
    return Call(
        to_checksum_address(router),
        selector(CURVE_ROUTER_EXCHANGE)
        + encode(
            ["address[11]", "uint256[5][5]", "uint256", "uint256", "address[5]", "address"],
            [route, params, amount, min_dy, pools, to_checksum_address(receiver)],
        ),
    )


@dataclass
class TakePlan:
    auction: str
    token: str
    max_amount: int = MAX_UINT256
    route: list[Call] = field(default_factory=list)
    min_profit: int = 0
    # Receiver of the want profit and unspent lot tokens; the sender by default.
    profit_receiver: str | None = None


class LotEnded(RuntimeError):
    pass


class WaitTimeout(TimeoutError):
    pass


class Executor:
    def __init__(
        self,
        client: ChainClient,
        *,
        taker: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client
        self.taker = AuctionTaker(client, taker) if taker else None
        self.sleep = sleep

    # Reads

    def auction(self, address: str) -> Auction:
        return Auction(self.client, address)

    def lot(self, auction: str, token: str) -> LotView:
        return self.auction(auction).lot_view(token)

    def payment_for(self, auction: str, token: str, amount: int) -> int:
        """Want owed now for ``amount`` of ``token`` (0 for an inactive lot)."""
        lot = self.lot(auction, token)
        if not lot.active:
            return 0
        return self.auction(auction).get_amount_needed(token, min(amount, lot.available))

    # Triggers

    def wait_for(
        self,
        auction: str,
        token: str,
        *,
        max_amount: int = MAX_UINT256,
        max_payment: int | None = None,
        not_before: int | None = None,
        poll: float = 15.0,
        timeout: float | None = None,
    ) -> LotView:
        """Block until the lot is live, ``not_before`` has passed, and the quote
        for ``min(max_amount, available)`` is at most ``max_payment``.

        Raises LotEnded once the window closes and WaitTimeout after ``timeout``
        seconds of waiting.
        """
        contract = self.auction(auction)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            lot = contract.lot_view(token)
            now = lot.timestamp
            if lot.end and now >= lot.end:
                raise LotEnded(f"{token}: window ended at {lot.end}")
            ready = lot.active and (not_before is None or now >= not_before)
            if ready and max_payment is not None:
                amount = min(max_amount, lot.available)
                payment = contract.get_amount_needed(token, amount)
                ready = payment <= max_payment
                log.info("%s: payment %s for %s (cap %s)", token, payment, amount, max_payment)
            if ready:
                return lot
            if deadline is not None and time.monotonic() >= deadline:
                raise WaitTimeout(f"{token}: trigger not reached within {timeout}s")
            self.sleep(poll)

    # Executions

    def take_with_route(
        self,
        plan: TakePlan,
        *,
        max_payment: int | None = None,
        not_before: int | None = None,
        poll: float = 15.0,
        timeout: float | None = None,
        dry_run: bool = False,
    ) -> TxResult | None:
        """Take through the AuctionTaker with ``plan.route`` paying the quote.

        Waits for the trigger, simulates, and sends; ``dry_run`` stops after a
        successful simulation and returns None.
        """
        if self.taker is None:
            raise RuntimeError("no AuctionTaker configured (auction_taker in config)")
        sender = self._sender()
        lot = self.wait_for(
            plan.auction,
            plan.token,
            max_amount=plan.max_amount,
            max_payment=max_payment,
            not_before=not_before,
            poll=poll,
            timeout=timeout,
        )
        calldata = self.taker.take_with_route_calldata(
            to_checksum_address(plan.auction),
            lot.token,
            plan.max_amount,
            plan.min_profit,
            to_checksum_address(plan.profit_receiver or sender),
            [call.as_tuple() for call in plan.route],
        )
        profit = self._simulate(self.taker.address, calldata, sender)
        log.info(
            "%s: route simulation ok, profit %s", lot.token, int.from_bytes(profit[:32], "big")
        )
        if dry_run:
            return None
        return self.client.send(self.taker.address, calldata)

    def take_direct(
        self,
        auction: str,
        token: str,
        *,
        max_payment: int,
        max_amount: int = MAX_UINT256,
        min_amount: int = 0,
        receiver: str | None = None,
        deadline: int | None = None,
        not_before: int | None = None,
        approve: bool = True,
        poll: float = 15.0,
        timeout: float | None = None,
        dry_run: bool = False,
    ) -> TxResult | None:
        """``take_with_limits`` paid from the sender's want balance.

        ``max_payment`` doubles as the wait trigger and the on-chain cap; the
        deadline defaults to the lot window's last second.
        """
        sender = self._sender()
        contract = self.auction(auction)
        lot = self.wait_for(
            auction,
            token,
            max_amount=max_amount,
            max_payment=max_payment,
            not_before=not_before,
            poll=poll,
            timeout=timeout,
        )
        want = Erc20(self.client, lot.want)
        if approve and want.allowance(sender, contract.address) < max_payment:
            if dry_run:
                log.info("dry run: would approve %s want to %s", max_payment, contract.address)
            else:
                self.client.send(want.address, want.approve_calldata(contract.address, max_payment))
        calldata = contract.take_with_limits_calldata(
            lot.token,
            max_amount,
            min_amount,
            max_payment,
            to_checksum_address(receiver or sender),
            deadline if deadline is not None else lot.end - 1,
        )
        if not (approve and dry_run):
            self._simulate(contract.address, calldata, sender)
        if dry_run:
            return None
        return self.client.send(contract.address, calldata)

    # Helpers

    def _sender(self) -> str:
        if self.client.sender is None:
            raise RuntimeError("executions need a keeper key (keeper_key_env in config)")
        return self.client.sender

    def _simulate(self, to: str, calldata: bytes, sender: str) -> bytes:
        try:
            return self.client.call(to, calldata, sender=sender)
        except CallReverted as exc:
            raise CallReverted(exc.data, f"simulation reverted: {exc}") from exc


def connect(
    config: KeeperConfig | NetworkConfig | str | Path,
    network: str | None = None,
    *,
    private_key: str | None = None,
    taker: str | None = None,
) -> Executor:
    """An ``Executor`` for one configured network.

    ``config`` is a config path, a loaded ``KeeperConfig`` (then ``network``
    names the entry), or a ``NetworkConfig``. The key comes from the network's
    ``keeper_key_env`` unless ``private_key`` is given.
    """
    if isinstance(config, str | Path):
        config = load_config(config)
    if isinstance(config, KeeperConfig):
        if network is None:
            raise ValueError("network name required with a KeeperConfig")
        config = config.network(network)
    client = Web3Client(config.rpc, private_key or config.private_key)
    return Executor(client, taker=taker or config.auction_taker)
