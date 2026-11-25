"""The auction's logs: everything the local model (``core.model``) is fed by.

One ``eth_getLogs`` over the auction address per range answers all of them,
decoded here into typed events in chain order:

* ``LotStaged``: a lot's snapshot amount and window;
* ``Taken``: what a fill left of a lot (``remaining_balance``);
* ``EconomicsSet`` / ``ReceiverSet``: the curve, the want token or the
  receiver changed, so whatever was read once has to be read again.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_abi import decode
from eth_utils import keccak

from keepers.auction_keeper.chain.abi import topic_address
from keepers.auction_keeper.chain.client import ChainClient, Log

# LotStaged(address indexed token, uint256 initial_amount, uint256 start_total,
#           uint256 floor_total, uint256 start, uint256 end)
LOT_STAGED_TOPIC = keccak(text="LotStaged(address,uint256,uint256,uint256,uint256,uint256)")
# Taken(address indexed token, address indexed caller, address receiver,
#       uint256 amount_out, uint256 payment, uint256 remaining_balance)
TAKEN_TOPIC = keccak(text="Taken(address,address,address,uint256,uint256,uint256)")
# EconomicsSet(address indexed want, uint256 start_total, uint256 floor_total, uint256 step_duration)
ECONOMICS_SET_TOPIC = keccak(text="EconomicsSet(address,uint256,uint256,uint256)")
# ReceiverSet(address indexed receiver)
RECEIVER_SET_TOPIC = keccak(text="ReceiverSet(address)")


@dataclass(frozen=True)
class LotStaged:
    block: int
    token: str
    initial_amount: int
    start_total: int
    floor_total: int
    start: int
    end: int


@dataclass(frozen=True)
class Taken:
    block: int
    token: str
    amount_out: int
    payment: int
    remaining_balance: int


@dataclass(frozen=True)
class SettingsChanged:
    """``EconomicsSet`` or ``ReceiverSet``. ``economics`` fences out every lot
    staged up to this block (the contract's ``configured_at``)."""

    block: int
    economics: bool


AuctionEvent = LotStaged | Taken | SettingsChanged


def decode_event(entry: Log) -> AuctionEvent | None:
    """The typed event of a log of the auction; None for any other log."""
    if not entry.topics:
        return None
    topic = entry.topics[0]
    if topic == LOT_STAGED_TOPIC:
        initial, start_total, floor_total, start, end = decode(["uint256"] * 5, entry.data)
        return LotStaged(
            entry.block_number,
            topic_address(entry.topics[1]),
            initial,
            start_total,
            floor_total,
            start,
            end,
        )
    if topic == TAKEN_TOPIC:
        _, amount_out, payment, remaining = decode(
            ["address", "uint256", "uint256", "uint256"], entry.data
        )
        return Taken(
            entry.block_number, topic_address(entry.topics[1]), amount_out, payment, remaining
        )
    if topic in (ECONOMICS_SET_TOPIC, RECEIVER_SET_TOPIC):
        return SettingsChanged(entry.block_number, topic == ECONOMICS_SET_TOPIC)
    return None


def auction_events(
    client: ChainClient, auction: str, from_block: int, to_block: int
) -> list[AuctionEvent]:
    """The auction's events in [from_block, to_block] in chain order: one
    ``eth_getLogs`` (the caller keeps the range within what the RPC serves).
    No topic filter: the auction logs little else, and one request covers
    every event kind."""
    entries = client.get_logs(auction, [], from_block, to_block)
    return [event for entry in entries if (event := decode_event(entry)) is not None]


__all__ = [
    "ECONOMICS_SET_TOPIC",
    "LOT_STAGED_TOPIC",
    "RECEIVER_SET_TOPIC",
    "TAKEN_TOPIC",
    "AuctionEvent",
    "LotStaged",
    "SettingsChanged",
    "Taken",
    "auction_events",
    "decode_event",
]
