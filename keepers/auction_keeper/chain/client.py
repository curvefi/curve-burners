"""JSON-RPC access behind one small protocol so the keeper logic stays testable.

``Web3Client`` is the production implementation; tests drive the same code
through a Titanoboa-backed client (see tests/keeper/conftest.py).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from eth_utils import to_checksum_address

from keepers.auction_keeper.chain.abi import CallReverted

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Block:
    number: int
    timestamp: int


@dataclass(frozen=True)
class Log:
    address: str
    topics: tuple[bytes, ...]
    data: bytes
    block_number: int


@dataclass(frozen=True)
class TxResult:
    tx_hash: str
    block_number: int
    gas_used: int
    status: int

    @property
    def ok(self) -> bool:
        return self.status == 1


class ChainClient(Protocol):
    """What the keeper needs from a node: reads, logs, and (optionally) sending."""

    @property
    def chain_id(self) -> int: ...

    @property
    def sender(self) -> str | None:
        """Address transactions are sent from; None for a read-only client."""
        ...

    def call(self, to: str, data: bytes, *, sender: str | None = None) -> bytes:
        """eth_call at the latest block; raises CallReverted on revert."""
        ...

    def latest_block(self) -> Block: ...

    def get_logs(
        self, address: str, topics: Sequence[bytes | None], from_block: int, to_block: int
    ) -> list[Log]: ...

    def send(self, to: str, data: bytes, *, value: int = 0) -> TxResult:
        """Sign, send, and wait for a transaction; raises on failure."""
        ...


# A read that died on the connection (an RPC closing a kept-alive socket, a
# reset under parallel quotes) is repeated this many times before it fails.
READ_RETRIES = 2
READ_RETRY_DELAY = 0.5


def retry_read[T](read: Callable[[], T], *, sleep: Callable[[float], None] = time.sleep) -> T:
    """``read()`` again after a connection error or a timeout. Only for reads:
    an answer the RPC did give (a revert, an error payload) is not retried."""
    from requests.exceptions import ConnectionError as ConnectionFailed
    from requests.exceptions import Timeout

    for attempt in range(READ_RETRIES + 1):
        try:
            return read()
        except (ConnectionFailed, Timeout) as exc:
            if attempt == READ_RETRIES:
                raise
            log.debug("rpc read failed (%s); retrying", exc)
            sleep(READ_RETRY_DELAY * (attempt + 1))
    raise AssertionError("unreachable")


class Web3Client:
    """``ChainClient`` over web3.py. A private key enables ``send``."""

    # Reads may be made from several threads at once (the native rail quotes
    # lots in parallel); a client without the attribute is used from one.
    concurrent = True

    def __init__(
        self,
        rpc_url: str,
        private_key: str | None = None,
        *,
        timeout: float = 30.0,
        gas_margin: float = 1.25,
        receipt_timeout: float = 300.0,
    ):
        from web3 import HTTPProvider, Web3
        from web3.middleware import ExtraDataToPOAMiddleware

        self.w3 = Web3(HTTPProvider(rpc_url, request_kwargs={"timeout": timeout}))
        # Harmless on non-PoA chains; required on several L2s/sidechains.
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self._chain_id = self.w3.eth.chain_id
        self._account = self.w3.eth.account.from_key(private_key) if private_key else None
        self.gas_margin = gas_margin
        self.receipt_timeout = receipt_timeout

    @property
    def chain_id(self) -> int:
        return self._chain_id

    @property
    def sender(self) -> str | None:
        return self._account.address if self._account else None

    def call(self, to: str, data: bytes, *, sender: str | None = None) -> bytes:
        from web3.exceptions import ContractLogicError

        tx: dict[str, Any] = {"to": to_checksum_address(to), "data": data}
        if sender:
            tx["from"] = to_checksum_address(sender)
        try:
            return bytes(retry_read(lambda: self.w3.eth.call(tx)))
        except ContractLogicError as exc:
            raise CallReverted(_revert_payload(exc)) from exc

    def latest_block(self) -> Block:
        block = retry_read(lambda: self.w3.eth.get_block("latest"))
        return Block(number=int(block["number"]), timestamp=int(block["timestamp"]))

    def get_logs(
        self, address: str, topics: Sequence[bytes | None], from_block: int, to_block: int
    ) -> list[Log]:
        query = {
            "address": to_checksum_address(address),
            "topics": [None if t is None else "0x" + t.hex() for t in topics],
            "fromBlock": from_block,
            "toBlock": to_block,
        }
        entries = retry_read(lambda: self.w3.eth.get_logs(query))
        return [
            Log(
                address=to_checksum_address(entry["address"]),
                topics=tuple(bytes(t) for t in entry["topics"]),
                data=bytes(entry["data"]),
                block_number=int(entry["blockNumber"]),
            )
            for entry in entries
        ]

    def send(self, to: str, data: bytes, *, value: int = 0) -> TxResult:
        if self._account is None:
            raise RuntimeError("read-only client: no private key configured")
        from web3.exceptions import ContractLogicError

        tx: dict[str, Any] = {
            "from": self._account.address,
            "to": to_checksum_address(to),
            "data": data,
            "value": value,
            "chainId": self._chain_id,
            "nonce": self.w3.eth.get_transaction_count(self._account.address, "pending"),
        }
        try:
            tx["gas"] = int(self.w3.eth.estimate_gas(tx) * self.gas_margin)
        except ContractLogicError as exc:
            raise CallReverted(_revert_payload(exc)) from exc
        tx.update(self._fees())
        signed = self._account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        log.info("sent %s", tx_hash.hex())
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.receipt_timeout)
        result = TxResult(
            tx_hash=tx_hash.hex(),
            block_number=int(receipt["blockNumber"]),
            gas_used=int(receipt["gasUsed"]),
            status=int(receipt["status"]),
        )
        if not result.ok:
            raise RuntimeError(f"transaction {result.tx_hash} reverted on-chain")
        return result

    def _fees(self) -> dict[str, int]:
        latest = self.w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas")
        if base_fee is None:
            return {"gasPrice": int(self.w3.eth.gas_price)}
        priority = int(self.w3.eth.max_priority_fee)
        return {"maxPriorityFeePerGas": priority, "maxFeePerGas": 2 * int(base_fee) + priority}


def _revert_payload(exc: Exception) -> bytes:
    data = getattr(exc, "data", None)
    if isinstance(data, str) and data.startswith("0x"):
        return bytes.fromhex(data[2:])
    if isinstance(data, bytes):
        return data
    return b""
