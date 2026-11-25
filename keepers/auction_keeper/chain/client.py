"""JSON-RPC access behind one small protocol so the keeper logic stays testable.

``Web3Client`` is the production implementation; tests drive the same code
through a Titanoboa-backed client (see tests/keeper/conftest.py).
"""

from __future__ import annotations

import logging
import queue
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from eth_utils import to_checksum_address

from keepers.auction_keeper.chain.abi import CallReverted
from keepers.auction_keeper.limits import RpcMeter

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


@dataclass(frozen=True)
class GasQuote:
    """What a transaction would cost right now: the gas estimate, the base
    fee per gas of the latest block and the node's suggested tip (wei)."""

    gas: int
    base_fee: int
    tip: int


class ChainClient(Protocol):
    """What the keeper needs from a node: reads, logs, and (optionally) sending."""

    @property
    def chain_id(self) -> int: ...

    @property
    def sender(self) -> str | None:
        """Address transactions are sent from; None for a read-only client."""
        ...

    def call(
        self, to: str, data: bytes, *, sender: str | None = None, at: int | None = None
    ) -> bytes:
        """eth_call at the latest block; raises CallReverted on revert. With
        ``at``, the call runs as the next block would see it: on the latest
        state with that block timestamp (or the node's pending block)."""
        ...

    def latest_block(self) -> Block: ...

    def get_logs(
        self, address: str, topics: Sequence[bytes | None], from_block: int, to_block: int
    ) -> list[Log]: ...

    def send(
        self,
        to: str,
        data: bytes,
        *,
        value: int = 0,
        at: int | None = None,
        receipt_timeout: float | None = None,
        priority_fee: PriorityFee | None = None,
    ) -> TxResult:
        """Sign, send, and wait for a transaction; raises on failure
        (``NotIncluded`` when no receipt came within ``receipt_timeout``).
        ``at`` is the block timestamp the gas estimate runs with;
        ``priority_fee(gas, base_fee, suggested)`` answers the tip per gas in
        wei (default: the node's suggestion). A client that has ``gas_quote``
        also takes ``gas``: the estimate made by it, not asked for again."""
        ...


# (estimated gas, base fee per gas, the node's suggested tip) -> tip per gas, wei.
PriorityFee = Callable[[int, int, int], int]


class NotIncluded(RuntimeError):
    """A sent transaction had no receipt in time: dropped by a private relay
    (they do not include a transaction that would revert), replaced, or
    still waiting. Its nonce is used again by the next send."""

    def __init__(self, tx_hash: str, reason: str):
        self.tx_hash = tx_hash
        super().__init__(f"transaction {tx_hash} not included: {reason}")


# How a call is run "as the next block": a block-timestamp override on the
# latest state, the node's pending block, or (neither served) the latest one.
AT_MODES = ("override", "pending", "latest")
# JSON-RPC errors that mean the endpoint does not take the request's shape.
_UNSUPPORTED_CODES = (-32600, -32601, -32602)


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


@dataclass
class _Lane:
    """One sending account and its nonce count. A lane carries one
    transaction at a time, from signing to receipt."""

    account: Any
    # One past the highest nonce this process saw included: the read node
    # may lag behind it, and never sees what went to a private relay.
    nonce_floor: int = 0


class Web3Client:
    """``ChainClient`` over web3.py. A private key enables ``send``.

    Several keys make as many sending lanes: ``send`` takes the account that
    has waited longest (so they rotate) and holds it until the receipt, and
    as many transactions as there are accounts can be in flight at once from
    different threads. Each account counts its own nonces, so a transaction
    a relay never includes holds up its own lane only. ``sender`` is the
    first account.

    ``send_rpc`` lists endpoints used only to submit transactions (private
    relays and builders such as Flashbots Protect or MEV Blocker): the same
    signed transaction goes to all of them at once and one acceptance is
    enough. Everything else, the nonce, the gas estimate and the receipt
    included, stays on ``rpc_url``.
    """

    # Reads may be made from several threads at once (the native rail quotes
    # lots in parallel); a client without the attribute is used from one.
    concurrent = True

    def __init__(
        self,
        rpc_url: str,
        private_key: str | Sequence[str] | None = None,
        *,
        rpc_rate: float = 0.0,
        send_rpc: Sequence[str] | str | None = None,
        send_timeout: float = 5.0,
        timeout: float = 30.0,
        gas_margin: float = 1.25,
        receipt_timeout: float = 300.0,
        receipt_poll: float = 2.0,
    ):
        from web3 import HTTPProvider, Web3
        from web3.middleware import ExtraDataToPOAMiddleware

        self.w3 = Web3(HTTPProvider(rpc_url, request_kwargs={"timeout": timeout}))
        # Every request to the node is counted, and spaced out with a rate set.
        self.meter = RpcMeter(rpc_rate)
        request = self.w3.provider.make_request

        def metered(method: Any, params: Any) -> Any:
            self.meter.note(str(method))
            return request(method, params)

        self.w3.provider.make_request = metered  # type: ignore[method-assign]
        # Harmless on non-PoA chains; required on several L2s/sidechains.
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self.send_rpc = (send_rpc,) if isinstance(send_rpc, str) else tuple(send_rpc or ())
        self.send_timeout = send_timeout
        self._chain_id = self.w3.eth.chain_id
        keys = [private_key] if isinstance(private_key, str) else list(private_key or ())
        self._set_accounts([self.w3.eth.account.from_key(key) for key in keys])
        self.gas_margin = gas_margin
        self.receipt_timeout = receipt_timeout
        self.receipt_poll = receipt_poll
        # The best AT_MODES entry the endpoint serves, per method.
        self._at_mode: dict[str, int] = {}

    def _set_accounts(self, accounts: Sequence[Any]) -> None:
        if len({account.address for account in accounts}) != len(accounts):
            raise ValueError("the same sending key is listed twice")
        self._lanes = [_Lane(account) for account in accounts]
        # The lanes no transaction is in; first in, first out, so they rotate.
        self._free: queue.Queue[_Lane] = queue.Queue()
        for lane in self._lanes:
            self._free.put(lane)

    @property
    def chain_id(self) -> int:
        return self._chain_id

    @property
    def sender(self) -> str | None:
        return self._lanes[0].account.address if self._lanes else None

    @property
    def senders(self) -> list[str]:
        """Every sending account: as many transactions can be in flight at once."""
        return [lane.account.address for lane in self._lanes]

    def balance(self, address: str) -> int:
        return int(self.w3.eth.get_balance(to_checksum_address(address)))

    def _estimate(self, tx: dict[str, Any], at: int | None) -> int:
        from web3.exceptions import ContractLogicError

        if at is not None:
            return int(self._request_at("eth_estimateGas", tx, at), 16)
        try:
            return int(self.w3.eth.estimate_gas(tx))
        except ContractLogicError as exc:
            raise CallReverted(_revert_payload(exc)) from exc

    def gas_quote(self, to: str, data: bytes, *, value: int = 0, at: int | None = None) -> GasQuote:
        """The gas a transaction of the main account would use (``at``: with
        the next block's clock) and the fees it would pay; raises
        ``CallReverted`` when it would revert. A chain without a base fee
        answers its gas price as the base fee."""
        if not self._lanes:
            raise RuntimeError("read-only client: no private key configured")
        tx = {
            "from": self._lanes[0].account.address,
            "to": to_checksum_address(to),
            "data": data,
            "value": value,
        }
        gas = self._estimate(tx, at)
        base_fee = self.w3.eth.get_block("latest").get("baseFeePerGas")
        if base_fee is None:
            return GasQuote(gas, int(self.w3.eth.gas_price), 0)
        return GasQuote(gas, int(base_fee), int(self.w3.eth.max_priority_fee))

    def call(
        self, to: str, data: bytes, *, sender: str | None = None, at: int | None = None
    ) -> bytes:
        from web3.exceptions import ContractLogicError

        tx: dict[str, Any] = {"to": to_checksum_address(to), "data": data}
        if sender:
            tx["from"] = to_checksum_address(sender)
        if at is not None:
            return bytes.fromhex(self._request_at("eth_call", tx, at)[2:])
        try:
            return bytes(retry_read(lambda: self.w3.eth.call(tx)))
        except ContractLogicError as exc:
            raise CallReverted(_revert_payload(exc)) from exc

    def _request_at(self, method: str, tx: dict[str, Any], at: int) -> str:
        """``eth_call`` / ``eth_estimateGas`` for the block after the latest
        one, in the best of AT_MODES the endpoint serves (remembered)."""
        body = {
            key: ("0x" + value.hex() if isinstance(value, bytes) else value)
            for key, value in tx.items()
            if key in ("from", "to", "data")
        }
        if tx.get("value"):
            body["value"] = hex(tx["value"])
        while True:
            mode = AT_MODES[self._at_mode.get(method, 0)]
            if mode == "override":
                params: list[Any] = [body, "latest", {}, {"time": hex(at)}]
            else:
                params = [body, mode]
            answer = retry_read(lambda p=params: self.w3.provider.make_request(method, p))
            error = answer.get("error")
            if error is None:
                return answer["result"]
            if not isinstance(error, dict):
                raise RuntimeError(f"{method}: {error}")
            message = str(error.get("message", ""))
            payload = error.get("data")
            if error.get("code") == 3 or "revert" in message.lower():
                raw = payload[2:] if isinstance(payload, str) and payload.startswith("0x") else ""
                raise CallReverted(bytes.fromhex(raw))
            if error.get("code") in _UNSUPPORTED_CODES and mode != "latest":
                log.warning(
                    "%s: endpoint refuses %s mode (%s); falling back", method, mode, message
                )
                self._at_mode[method] = self._at_mode.get(method, 0) + 1
                continue
            raise RuntimeError(f"{method}: {message}")

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

    def send(
        self,
        to: str,
        data: bytes,
        *,
        value: int = 0,
        at: int | None = None,
        receipt_timeout: float | None = None,
        priority_fee: PriorityFee | None = None,
        gas: int | None = None,
    ) -> TxResult:
        if not self._lanes:
            raise RuntimeError("read-only client: no private key configured")
        lane = self._free.get()  # waits while every account has a transaction in flight
        try:
            return self._send_from(
                lane,
                to,
                data,
                value=value,
                at=at,
                receipt_timeout=receipt_timeout,
                priority_fee=priority_fee,
                gas=gas,
            )
        finally:
            self._free.put(lane)

    def _send_from(
        self,
        lane: _Lane,
        to: str,
        data: bytes,
        *,
        value: int,
        at: int | None,
        receipt_timeout: float | None,
        priority_fee: PriorityFee | None,
        gas: int | None = None,
    ) -> TxResult:
        account = lane.account
        sender = account.address
        # A transaction given to a private relay is in no public pending
        # pool, so the node's count is topped up with what was seen included
        # here. One that was never included leaves its nonce to the next send.
        nonce = max(self.w3.eth.get_transaction_count(sender, "pending"), lane.nonce_floor)
        tx: dict[str, Any] = {
            "from": sender,
            "to": to_checksum_address(to),
            "data": data,
            "value": value,
            "chainId": self._chain_id,
            "nonce": nonce,
        }
        if gas is None:
            gas = self._estimate(tx, at)
        tx["gas"] = int(gas * self.gas_margin)
        tx.update(self._fees(gas, priority_fee))
        signed = account.sign_transaction(tx)
        if self.send_rpc:
            tx_hash = submit_raw(self.send_rpc, signed.raw_transaction, self.send_timeout)
        else:
            tx_hash = bytes(self.w3.eth.send_raw_transaction(signed.raw_transaction))
        log.info("sent %s (%s nonce %s)", tx_hash.hex(), sender, nonce)
        receipt = self._wait_receipt(
            lane,
            tx_hash,
            nonce,
            self.receipt_timeout if receipt_timeout is None else receipt_timeout,
        )
        lane.nonce_floor = max(lane.nonce_floor, nonce + 1)
        result = TxResult(
            tx_hash=tx_hash.hex(),
            block_number=int(receipt["blockNumber"]),
            gas_used=int(receipt["gasUsed"]),
            status=int(receipt["status"]),
        )
        if not result.ok:
            raise RuntimeError(f"transaction {result.tx_hash} reverted on-chain")
        return result

    def _wait_receipt(self, lane: _Lane, tx_hash: Any, nonce: int, timeout: float) -> Any:
        """The receipt, polled on the read node. Gives up with ``NotIncluded``
        after ``timeout`` seconds, or as soon as the account's mined nonce
        has passed this one without it (replaced or sent twice)."""
        from web3.exceptions import TransactionNotFound

        sender = lane.account.address
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.w3.eth.get_transaction_receipt(tx_hash)
            except TransactionNotFound:
                pass
            if self.w3.eth.get_transaction_count(sender, "latest") > nonce:
                try:  # mined between the two reads
                    return self.w3.eth.get_transaction_receipt(tx_hash)
                except TransactionNotFound:
                    lane.nonce_floor = max(lane.nonce_floor, nonce + 1)
                    raise NotIncluded(tx_hash.hex(), "its nonce was used by another") from None
            if time.monotonic() >= deadline:
                raise NotIncluded(tx_hash.hex(), f"no receipt in {timeout:.0f}s")
            time.sleep(self.receipt_poll)

    def _fees(self, gas: int = 0, priority_fee: PriorityFee | None = None) -> dict[str, int]:
        latest = self.w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas")
        if base_fee is None:
            return {"gasPrice": int(self.w3.eth.gas_price)}
        priority = int(self.w3.eth.max_priority_fee)
        if priority_fee is not None:
            priority = priority_fee(gas, int(base_fee), priority)
        return {"maxPriorityFeePerGas": priority, "maxFeePerGas": 2 * int(base_fee) + priority}


def _post_raw(url: str, raw_hex: str, timeout: float) -> str | None:
    """Post a signed transaction to one endpoint; the refusal, None when taken."""
    import requests

    body = {"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": [raw_hex]}
    try:
        answer = requests.post(url, json=body, timeout=timeout).json()
    except (requests.RequestException, ValueError) as exc:
        return str(exc)
    error = answer.get("error") if isinstance(answer, dict) else answer
    if not error:
        return None
    message = str(error.get("message", error) if isinstance(error, dict) else error)
    # The endpoint has it already (a retry, or another relay passed it on).
    return None if "already known" in message.lower() else message


def submit_raw(
    urls: Sequence[str],
    raw: bytes,
    timeout: float,
    *,
    post: Callable[[str, str, float], str | None] = _post_raw,
) -> bytes:
    """Post the signed transaction ``raw`` to every endpoint at once and
    return its hash (keccak of the raw bytes). One acceptance is enough: an
    endpoint that refuses or does not answer in ``timeout`` seconds is logged
    and ignored; a ``RuntimeError`` only when none took it."""
    from concurrent.futures import ThreadPoolExecutor

    from eth_utils import keccak

    raw_hex = "0x" + bytes(raw).hex()
    with ThreadPoolExecutor(len(urls), thread_name_prefix="send") as pool:
        refusals = list(pool.map(lambda url: post(url, raw_hex, timeout), urls))
    for url, refusal in zip(urls, refusals, strict=True):
        if refusal is not None:
            log.warning("send endpoint %s refused the transaction: %s", _host(url), refusal)
    if all(refusal is not None for refusal in refusals):
        raise RuntimeError(f"no send endpoint accepted the transaction: {refusals}")
    return keccak(bytes(raw))


def _host(url: str) -> str:
    """The endpoint without its path and query (they may carry a key)."""
    from urllib.parse import urlsplit

    return urlsplit(url).netloc or url


def _revert_payload(exc: Exception) -> bytes:
    data = getattr(exc, "data", None)
    if isinstance(data, str) and data.startswith("0x"):
        return bytes.fromhex(data[2:])
    if isinstance(data, bytes):
        return data
    return b""
