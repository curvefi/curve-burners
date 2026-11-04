"""The auction's ``LotStaged`` log scan: token discovery through ``eth_getLogs``."""

from __future__ import annotations

from eth_utils import keccak

from keepers.auction_keeper.chain.abi import topic_address
from keepers.auction_keeper.chain.client import ChainClient, Log

# LotStaged(address indexed token, uint256 initial_amount, uint256 start_total,
#           uint256 floor_total, uint256 start, uint256 end)
LOT_STAGED_TOPIC = keccak(text="LotStaged(address,uint256,uint256,uint256,uint256,uint256)")


def lot_staged_token(entry: Log) -> str:
    return topic_address(entry.topics[1])


def staged_tokens(
    client: ChainClient, auction: str, from_block: int, to_block: int, chunk: int = 50_000
) -> set[str]:
    """Tokens with a LotStaged event in [from_block, to_block], scanned in
    ``chunk``-block ranges (public RPCs cap the range of one eth_getLogs)."""
    tokens: set[str] = set()
    start = from_block
    while start <= to_block:
        stop = min(start + chunk - 1, to_block)
        for entry in client.get_logs(auction, [LOT_STAGED_TOPIC], start, stop):
            tokens.add(lot_staged_token(entry))
        start = stop + 1
    return tokens


__all__ = ["LOT_STAGED_TOPIC", "lot_staged_token", "staged_tokens"]
