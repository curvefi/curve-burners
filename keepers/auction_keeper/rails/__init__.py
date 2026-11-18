"""Settlement adapters the keeper can publish to.

An adapter publisher turns a live lot into a signed order for one settlement
rail. Publishers are looked up by the adapter contract's ``ADAPTER_VERSION``
string, so a network config never lists adapter addresses: the keeper reads
the auction's ``AdapterRegistry`` and picks a publisher for every active
adapter it knows.

Every publisher follows the same shape: build the order from the live lot
quote (through the adapter's ``order_for`` or the rail's own feed), validate
it through the auction's ``isValidSignature`` so typed reverts are logged,
check the executor allowance (synced in keeper mode), post or log it, and
return a ``PublishResult`` the keeper records in the state file so the order
is reposted only before it expires, and never past the lot window.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from keepers.auction_keeper.chain.abi import CallReverted
from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.chain.contracts import (
    ERC1271_MAGIC_VALUE,
    AdapterConfig,
    Auction,
    Erc20,
    LotView,
)
from keepers.auction_keeper.config import NetworkConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublishResult:
    """What a publisher did for a lot: the order id it holds and its validity."""

    uid: str
    valid_to: int
    sell_amount: int
    buy_amount: int
    details: dict[str, Any]


class AdapterPublisher(ABC):
    #: The adapter contract's ADAPTER_VERSION this publisher serves.
    version: str

    def __init__(self, client: ChainClient, adapter: AdapterConfig, network: NetworkConfig):
        self.client = client
        self.adapter = adapter
        self.network = network
        # Set by the keeper: the schedule's validity for a lot (None: the uniform TTL).
        self.planner: Callable[[LotView], int | None] | None = None

    @property
    def address(self) -> str:
        return self.adapter.adapter

    @property
    def executor(self) -> str:
        return self.adapter.executor

    @classmethod
    def enabled(cls, network: NetworkConfig) -> bool:
        """Whether the network config switches this rail on (default: yes)."""
        return True

    @abstractmethod
    def publish(
        self, auction: Auction, lot: LotView, *, dry_run: bool = False
    ) -> PublishResult | None:
        """Publish an order for ``lot``; None when nothing was (or could be) published."""

    # Shared steps

    def valid_to(self, lot: LotView) -> int:
        """The order validity bound: the keeper's schedule for the lot when it
        has one (``keepers.auction_keeper.core.schedule``), else the uniform TTL; never
        past the lot window end."""
        planned = self.planner(lot) if self.planner is not None else None
        if planned is not None:
            return min(lot.end, planned)
        return min(lot.end, lot.timestamp + self.network.order_ttl)

    def validate_signature(self, auction: Auction, digest: bytes, signature: bytes) -> str | None:
        """Run the auction's router on (digest, signature); None when valid, else the reason."""
        try:
            answer = auction.is_valid_signature(digest, signature)
        except CallReverted as exc:
            return str(exc)
        if answer != ERC1271_MAGIC_VALUE:
            return f"router answered 0x{answer.hex()}"
        return None

    def ensure_allowance(
        self, auction: Auction, lot: LotView, executor: str, *, dry_run: bool
    ) -> bool:
        """Whether ``executor`` can pull the lot; keeper mode syncs it.

        Offline mode posts regardless: the order is valid, and the
        permissionless ``sync_executor_approvals`` can run from anywhere before
        a fill — the warning names the call.
        """
        token = Erc20(self.client, lot.token)
        if token.allowance(auction.address, executor) >= lot.available:
            return True
        if not self.network.onchain:
            log.warning(
                "%s: executor %s allowance missing (offline mode, posting anyway); "
                "run sync_executor_approvals(%s, [%s]) on %s before a fill",
                lot.token,
                executor,
                executor,
                lot.token,
                auction.address,
            )
            return True
        if dry_run:
            log.info("dry run: would sync allowance of %s to %s", lot.token, executor)
            return True
        if self.client.sender is None:
            log.error("%s: keeper mode without a sending client; allowance not synced", lot.token)
            return False
        log.info("syncing %s allowance for executor %s", lot.token, executor)
        self.client.send(
            auction.address, auction.sync_executor_approvals_calldata(executor, [lot.token])
        )
        return token.allowance(auction.address, executor) >= lot.available


def publisher_classes() -> dict[str, type[AdapterPublisher]]:
    """Every publisher by the ``ADAPTER_VERSION`` it serves. A new rail adds
    its class to the list."""
    from keepers.auction_keeper.rails.bebop import BebopJamPublisher
    from keepers.auction_keeper.rails.cow import CowPublisher
    from keepers.auction_keeper.rails.hashflow import HashflowPublisher
    from keepers.auction_keeper.rails.kyber import KyberDsloPublisher
    from keepers.auction_keeper.rails.okx import OKXIntentPublisher
    from keepers.auction_keeper.rails.oneinch import (
        OneInchFeeTakerPublisher,
        OneInchFusionPublisher,
        OneInchPublisher,
    )
    from keepers.auction_keeper.rails.uniswapx import UniswapXPublisher
    from keepers.auction_keeper.rails.velora import VeloraDeltaPublisher
    from keepers.auction_keeper.rails.zerox import ZeroExSettlerPublisher

    classes: list[type[AdapterPublisher]] = [
        CowPublisher,
        OKXIntentPublisher,
        KyberDsloPublisher,
        VeloraDeltaPublisher,
        ZeroExSettlerPublisher,
        HashflowPublisher,
        OneInchPublisher,
        OneInchFeeTakerPublisher,
        OneInchFusionPublisher,
        UniswapXPublisher,
        BebopJamPublisher,
    ]
    return {cls.version: cls for cls in classes}


__all__ = ["AdapterPublisher", "PublishResult", "publisher_classes"]
