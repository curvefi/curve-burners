"""Fixtures of the pure-Python keeper tests: no boa, no node, no HTTP.

Run them on their own, without the boa conftests above this directory:

    python -m pytest tests/keeper/pure --confcutdir=tests/keeper/pure

``FakeChain`` is a ``ChainClient`` that answers the few calls the keeper's
cycle makes from an in-memory auction, counts every request and keeps a
clock the test moves.
"""

from __future__ import annotations

from collections import Counter

import pytest
from eth_abi import encode
from eth_utils import keccak, to_checksum_address

from keepers.auction_keeper.chain.abi import address_word
from keepers.auction_keeper.chain.client import Block, Log
from keepers.auction_keeper.chain.contracts import AdapterRegistry, Auction, AuctionTaker, Erc20
from keepers.auction_keeper.chain.events import LOT_STAGED_TOPIC, TAKEN_TOPIC
from keepers.auction_keeper.core.model import Curve

WAD = 10**18
# The Ethereum burner's curve (0xc014...AbD4, read 2026-11-24).
START_TOTAL = 250_000 * WAD
FLOOR_TOTAL = 100 * WAD
STEP = 36
LENGTH = 86_400
WINDOW_START = 1_795_478_400

AUCTION = to_checksum_address("0x" + "a1" * 20)
WANT = to_checksum_address("0x" + "b2" * 20)
RECEIVER = to_checksum_address("0x" + "c3" * 20)
REGISTRY = to_checksum_address("0x" + "d4" * 20)


def token_address(index: int) -> str:
    return to_checksum_address(keccak(index.to_bytes(4, "big"))[-20:])


@pytest.fixture
def curve() -> Curve:
    return Curve.build(START_TOTAL, FLOOR_TOTAL, STEP, LENGTH)


class FakeChain:
    """An auction in memory behind the ``ChainClient`` protocol."""

    chain_id = 1
    sender = None

    def __init__(self, now: int = WINDOW_START + 600):
        self.number = 1000
        self.now = now
        self.logs: list[Log] = []
        # token -> [initial_amount, start, end, balance]
        self.lots: dict[str, list[int]] = {}
        self.requests: Counter[str] = Counter()
        self.calls_at: list[int | None] = []
        # The block timestamp each take simulation ran with.
        self.simulated: list[int | None] = []

    # Test controls

    def stage(self, token: str, amount: int, start: int = WINDOW_START, log: bool = True) -> None:
        self.lots[token] = [amount, start, start + LENGTH, amount]
        if log:
            self.logs.append(
                Log(
                    AUCTION,
                    (LOT_STAGED_TOPIC, address_word(token)),
                    encode(
                        ["uint256"] * 5, [amount, START_TOTAL, FLOOR_TOTAL, start, start + LENGTH]
                    ),
                    self.number,
                )
            )

    def take(self, token: str, remaining: int, log: bool = True) -> None:
        self.lots[token][3] = remaining
        self.mine()
        if log:
            self.logs.append(
                Log(
                    AUCTION,
                    (TAKEN_TOPIC, address_word(token), address_word(RECEIVER)),
                    encode(
                        ["address", "uint256", "uint256", "uint256"], [RECEIVER, 1, 1, remaining]
                    ),
                    self.number,
                )
            )

    def mine(self, seconds: int = 12) -> None:
        self.number += 1
        self.now += seconds

    # ChainClient

    def latest_block(self) -> Block:
        self.requests["eth_getBlockByNumber"] += 1
        return Block(self.number, self.now)

    def get_logs(self, address, topics, from_block, to_block):
        self.requests["eth_getLogs"] += 1
        return [entry for entry in self.logs if from_block <= entry.block_number <= to_block]

    def call(self, to, data, *, sender=None, at=None):
        self.requests["eth_call"] += 1
        self.calls_at.append(at)
        head, tail = bytes(data[:4]), bytes(data[4:])
        token = to_checksum_address(tail[12:32]) if len(tail) >= 32 else ""
        simple = {
            Auction.START_TOTAL.selector: ("uint256", START_TOTAL),
            Auction.FLOOR_TOTAL.selector: ("uint256", FLOOR_TOTAL),
            Auction.STEP_DURATION.selector: ("uint256", STEP),
            Auction.AUCTION_LENGTH.selector: ("uint256", LENGTH),
            Auction.WANT.selector: ("address", WANT),
            Auction.RECEIVER.selector: ("address", RECEIVER),
            Auction.REGISTRY.selector: ("address", REGISTRY),
            Erc20.DECIMALS.selector: ("uint8", 18),
            Erc20.ALLOWANCE.selector: ("uint256", 2**256 - 1),
            AdapterRegistry.GET_ADAPTERS.selector: ("address[]", []),
        }
        if head == AuctionTaker.TAKE_WITH_ROUTE.selector:
            self.simulated.append(at)
            return encode(["uint256"], [1])
        if head in simple:
            kind, value = simple[head]
            return encode([kind], [value])
        lot = self.lots.get(token)
        if head == Auction.LOTS.selector:
            return encode(["uint256", "uint256"], [lot[1] - 1, lot[0]] if lot else [0, 0])
        if head == Auction.WINDOW.selector:
            return encode(["uint256", "uint256"], lot[1:3] if lot else [0, 0])
        if head == Auction.AVAILABLE.selector:
            live = lot is not None and lot[1] <= self.now < lot[2]
            return encode(["uint256"], [min(lot[0], lot[3]) if live else 0])
        raise AssertionError(f"unexpected call {head.hex()} to {to}")

    def send(self, to, data, *, value=0, at=None, receipt_timeout=None):
        raise AssertionError("the pure tests never send")


@pytest.fixture(autouse=True)
def production_adapter_cache(monkeypatch):
    """Run with the suite above (boa), its conftest makes every keeper read
    the adapter registry each cycle; these tests count requests as deployed."""
    from keepers.auction_keeper.core.keeper import NetworkKeeper

    monkeypatch.setattr(NetworkKeeper, "adapters_ttl", 300.0)


@pytest.fixture
def chain() -> FakeChain:
    return FakeChain()
