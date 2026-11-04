"""``ChainClient`` over Titanoboa: tests and fork rehearsals drive the keeper
through the in-process EVM with the same code paths as production.

Logs are not indexed by boa, so ``logs`` is a list the caller fills with the
entries ``get_logs`` should answer with (token discovery through LotStaged).
"""

from __future__ import annotations

from collections.abc import Sequence

import boa
from eth.exceptions import Revert
from eth_utils import keccak, to_checksum_address

from keepers.auction_keeper.chain.abi import CallReverted
from keepers.auction_keeper.chain.client import Block, Log, TxResult


class BoaClient:
    def __init__(self, sender: str | None = None):
        self._sender = sender
        self.logs: list[Log] = []
        self.sent: list[tuple[str, bytes]] = []

    @property
    def chain_id(self) -> int:
        return boa.env.evm.patch.chain_id

    @property
    def sender(self) -> str | None:
        return self._sender

    def call(self, to: str, data: bytes, *, sender: str | None = None) -> bytes:
        try:
            result = boa.env.raw_call(to, sender=sender or self._sender, data=data, simulate=True)
        except Revert as exc:
            raise CallReverted(exc.args[0] if exc.args else b"") from exc
        return bytes(result.output)

    def latest_block(self) -> Block:
        return Block(number=boa.env.evm.patch.block_number, timestamp=boa.env.evm.patch.timestamp)

    def get_logs(
        self, address: str, topics: Sequence[bytes | None], from_block: int, to_block: int
    ) -> list[Log]:
        return [
            entry
            for entry in self.logs
            if entry.address == to_checksum_address(address)
            and from_block <= entry.block_number <= to_block
            and all(t is None or entry.topics[i] == t for i, t in enumerate(topics))
        ]

    def send(self, to: str, data: bytes, *, value: int = 0) -> TxResult:
        if self._sender is None:
            raise RuntimeError("read-only client")
        self.sent.append((to_checksum_address(to), data))
        try:
            boa.env.raw_call(to, sender=self._sender, data=data, value=value)
        except Revert as exc:
            raise CallReverted(exc.args[0] if exc.args else b"") from exc
        return TxResult(
            tx_hash="0x" + keccak(data + len(self.sent).to_bytes(4, "big")).hex(),
            block_number=boa.env.evm.patch.block_number,
            gas_used=0,
            status=1,
        )
