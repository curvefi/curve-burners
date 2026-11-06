"""OKX Intent Swap publisher: orders of the OKX DEX aggregator's intent mode
(Onchain OS), signed through the auction's ERC-1271 router
(``OKXIntentAdapter``).

Feed mode (``okx.post: true``, the default): every poll asks for an intent
quote with the auction as owner and the auction's receiver as receiver
(``GET /api/v6/dex/aggregator/quote?mode=intent&signingScheme=eip1271...``),
takes the order from ``signData.message`` as the API dictates it (appData,
commissionInfos, validTo, toTokenAmount — the client cannot set the signed
amounts), checks it against the lot (``toTokenAmount`` at or above the live
curve quote, else skipped: ``check_order`` would reject it), signs it as
``adapter ++ abi_encode(order)``, validates the pair through
``isValidSignature`` with the settlement's digest, and POSTs
``/api/v6/dex/aggregator/intent/create-order`` with ``signingScheme:
"eip1271"``. Every request carries the Onchain OS HMAC headers.

UNVERIFIED (``okx.owner_prefix``): on-chain the settlement reads the owner
from the first 20 bytes of an eip1271 signature and forwards the rest to
the owner's ``isValidSignature``; the solver SDK forwards the orderbook's
bytes verbatim. Whether the orderbook expects the submitter to send
``burner ++ adapter ++ abi_encode(order)`` (``owner_prefix: true``, the
default) or only ``adapter ++ abi_encode(order)`` and prefixes the owner
itself (``false``) has not been checked against the live API.

Standing-ask mode (``post: false``, or a chain without an intent chainIndex):
the ``order_for`` order (no commissions, validTo shortened to the keeper
TTL) is validated and logged; note that only allowlisted OKX solvers can
settle it, so this mode is a log, not a permissionless fallback.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

from keepers.auction_keeper.chain.abi import CallReverted
from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.chain.contracts import (
    OKX_ORDER_ABI,
    AdapterConfig,
    Auction,
    LotView,
    OKXIntentAdapterContract,
)
from keepers.auction_keeper.config import NetworkConfig, OKXSettings
from keepers.auction_keeper.rails import AdapterPublisher, PublishResult
from keepers.auction_keeper.rails.common import ApiError, JsonClient, hex_bytes, random_salt

log = logging.getLogger(__name__)

COMMISSION_TYPE = "CommissionInfo(uint256 feePercent,address referrerWalletAddress,uint256 flag)"
ORDER_TYPE_HASH = keccak(
    text=(
        "Order(address fromTokenAddress,address toTokenAddress,address owner,address receiver,"
        "uint256 fromTokenAmount,uint256 toTokenAmount,uint32 validTo,bytes32 appData,"
        "bytes32 swapMode,bool partiallyFillable,CommissionInfo[] commissionInfos)"
        + COMMISSION_TYPE
    )
)
COMMISSION_TYPE_HASH = keccak(text=COMMISSION_TYPE)
EXACT_IN = keccak(text="exactIn")
SIGNING_SCHEME = "eip1271"
QUOTE_PATH = "/api/v6/dex/aggregator/quote"
CREATE_ORDER_PATH = "/api/v6/dex/aggregator/intent/create-order"
# chainIndex of the chains the intent mode serves (the EVM chain id).
OKX_CHAIN_INDEXES: dict[int, str] = {1: "1", 56: "56", 196: "196", 8453: "8453", 42161: "42161"}


@dataclass(frozen=True)
class CommissionInfo:
    fee_percent: int
    referrer: str
    flag: int

    def as_tuple(self) -> tuple:
        return (self.fee_percent, self.referrer, self.flag)

    def as_json(self) -> dict[str, str]:
        return {
            "feePercent": str(self.fee_percent),
            "referrerWalletAddress": self.referrer,
            "flag": str(self.flag),
        }


@dataclass(frozen=True)
class OKXOrder:
    """OrderLib.Data as signed: ``signData.message`` field for field."""

    from_token: str
    to_token: str
    owner: str
    receiver: str
    from_amount: int
    to_amount: int
    valid_to: int
    app_data: bytes
    swap_mode: bytes
    partially_fillable: bool
    commissions: tuple[CommissionInfo, ...] = ()

    @classmethod
    def from_tuple(cls, values: tuple) -> OKXOrder:
        f, t, owner, receiver, fa, ta, valid_to, app_data, swap_mode, partial, commissions = values
        return cls(
            to_checksum_address(f),
            to_checksum_address(t),
            to_checksum_address(owner),
            to_checksum_address(receiver),
            fa,
            ta,
            valid_to,
            bytes(app_data),
            bytes(swap_mode),
            bool(partial),
            tuple(CommissionInfo(c[0], to_checksum_address(c[1]), c[2]) for c in commissions),
        )

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> OKXOrder:
        """The quote's ``signData.message`` (amounts, fees and flags as decimal strings)."""
        return cls(
            to_checksum_address(message["fromTokenAddress"]),
            to_checksum_address(message["toTokenAddress"]),
            to_checksum_address(message["owner"]),
            to_checksum_address(message["receiver"]),
            int(message["fromTokenAmount"]),
            int(message["toTokenAmount"]),
            int(message["validTo"]),
            _bytes32(message["appData"]),
            _bytes32(message["swapMode"]),
            bool(message["partiallyFillable"]),
            tuple(
                CommissionInfo(
                    int(c["feePercent"]),
                    to_checksum_address(c["referrerWalletAddress"]),
                    int(c["flag"]),
                )
                for c in message.get("commissionInfos") or []
            ),
        )

    def as_tuple(self) -> tuple:
        return (
            self.from_token,
            self.to_token,
            self.owner,
            self.receiver,
            self.from_amount,
            self.to_amount,
            self.valid_to,
            self.app_data,
            self.swap_mode,
            self.partially_fillable,
            [c.as_tuple() for c in self.commissions],
        )

    def with_valid_to(self, valid_to: int) -> OKXOrder:
        return replace(self, valid_to=valid_to)

    def encode(self) -> bytes:
        return encode([OKX_ORDER_ABI], [self.as_tuple()])

    def signature(self, adapter: str) -> bytes:
        """``adapter ++ abi_encode(order)``: what the auction's router validates."""
        return bytes.fromhex(adapter[2:]) + self.encode()

    def struct_hash(self) -> bytes:
        commission_hashes = b"".join(
            keccak(
                encode(
                    ["bytes32", "uint256", "address", "uint256"],
                    [COMMISSION_TYPE_HASH, *c.as_tuple()],
                )
            )
            for c in self.commissions
        )
        return keccak(
            encode(
                ["bytes32", "address", "address", "address", "address", "uint256", "uint256"]
                + ["uint32", "bytes32", "bytes32", "bool", "bytes32"],
                [
                    ORDER_TYPE_HASH,
                    self.from_token,
                    self.to_token,
                    self.owner,
                    self.receiver,
                    self.from_amount,
                    self.to_amount,
                    self.valid_to,
                    self.app_data,
                    self.swap_mode,
                    self.partially_fillable,
                    keccak(commission_hashes),
                ],
            )
        )

    def digest(self, domain_separator: bytes) -> bytes:
        """OrderLib.hash: the digest the settlement passes to the owner's isValidSignature."""
        return keccak(b"\x19\x01" + domain_separator + self.struct_hash())

    def uid(self, domain_separator: bytes) -> bytes:
        """OrderLib.packOrderUidParams: ``digest ++ owner ++ validTo``."""
        return (
            self.digest(domain_separator)
            + bytes.fromhex(self.owner[2:])
            + self.valid_to.to_bytes(4, "big")
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "fromTokenAddress": self.from_token,
            "toTokenAddress": self.to_token,
            "owner": self.owner,
            "receiver": self.receiver,
            "fromTokenAmount": str(self.from_amount),
            "toTokenAmount": str(self.to_amount),
            "validTo": self.valid_to,
            "appData": hex_bytes(self.app_data),
            "swapMode": hex_bytes(self.swap_mode),
            "partiallyFillable": self.partially_fillable,
            "commissionInfos": [c.as_json() for c in self.commissions],
        }


def _bytes32(value: str) -> bytes:
    data = bytes.fromhex(value[2:] if value.startswith("0x") else value)
    if len(data) != 32:
        raise ValueError(f"not a bytes32: {value}")
    return data


class OKXApi:
    """The Onchain OS DEX aggregator endpoints the publisher uses, with the
    HMAC headers every request needs: ``OK-ACCESS-SIGN = base64(HMAC-SHA256(
    timestamp + METHOD + requestPath(+ query) + body, secret))``, the
    timestamp in ISO 8601 UTC with milliseconds (``OK-ACCESS-TIMESTAMP``),
    plus ``OK-ACCESS-KEY`` and ``OK-ACCESS-PASSPHRASE``."""

    def __init__(self, http: Any, settings: OKXSettings):
        self.http = http
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.api_key and self.settings.secret_key and self.settings.passphrase)

    @staticmethod
    def timestamp() -> str:
        return (
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{datetime.now(UTC).microsecond // 1000:03d}Z"
        )

    def headers(self, method: str, path: str, query: str = "", body: str = "") -> dict[str, str]:
        timestamp = self.timestamp()
        prehash = timestamp + method.upper() + path + (f"?{query}" if query else "") + body
        digest = hmac.new(
            (self.settings.secret_key or "").encode(), prehash.encode(), hashlib.sha256
        ).digest()
        return {
            "OK-ACCESS-KEY": self.settings.api_key or "",
            "OK-ACCESS-SIGN": base64.b64encode(digest).decode(),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.settings.passphrase or "",
            "Content-Type": "application/json",
        }

    def _check(self, payload: Any, url: str) -> list[Any]:
        """The API envelope ``{code, data, msg}``; anything but code "0" is an error."""
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise ApiError(200, payload, url)
        data = payload.get("data")
        return data if isinstance(data, list) else [data]

    def quote(self, params: dict[str, str]) -> dict[str, Any]:
        query = urlencode(params)
        url = f"{self.settings.api_url.rstrip('/')}{QUOTE_PATH}"
        payload = self.http.request(
            "GET", url, params=params, headers=self.headers("GET", QUOTE_PATH, query)
        )
        (quote,) = self._check(payload, url)[:1] or [None]
        if not quote:
            raise ApiError(200, payload, url)
        return quote

    def create_order(self, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body, separators=(",", ":"))
        url = f"{self.settings.api_url.rstrip('/')}{CREATE_ORDER_PATH}"
        payload = self.http.request(
            "POST", url, data=data, headers=self.headers("POST", CREATE_ORDER_PATH, "", data)
        )
        entries = self._check(payload, url)
        return entries[0] if entries and isinstance(entries[0], dict) else {"raw": payload}


class OKXIntentPublisher(AdapterPublisher):
    version = "OKXIntentAdapter"

    def __init__(
        self,
        client: ChainClient,
        adapter: AdapterConfig,
        network: NetworkConfig,
        *,
        http: Any = None,
    ):
        super().__init__(client, adapter, network)
        self.settings = network.okx
        self.contract = OKXIntentAdapterContract(client, adapter.adapter)
        self.settlement = self.contract.settlement()
        self.token_approve = self.contract.token_approve()
        self.domain_separator = self.contract.domain_separator()
        self.api = OKXApi(http or JsonClient(), self.settings)
        self.chain_index = self.settings.chain_index or OKX_CHAIN_INDEXES.get(client.chain_id)
        if adapter.executor != self.token_approve:
            log.warning(
                "OKXIntentAdapter %s: registry executor %s differs from TokenApprove %s",
                adapter.adapter,
                adapter.executor,
                self.token_approve,
            )
        if self.settings.post and self.chain_index is None:
            log.warning(
                "okx: no intent chainIndex for chain id %s (set okx.chain_index); "
                "publishing standing asks only",
                client.chain_id,
            )
        if self.settings.post and not self.api.configured:
            log.warning(
                "okx: API key, secret or passphrase missing (%s, %s, %s); "
                "every endpoint needs them, publishing standing asks only",
                self.settings.api_key_env,
                self.settings.secret_key_env,
                self.settings.passphrase_env,
            )

    @classmethod
    def enabled(cls, network: NetworkConfig) -> bool:
        return network.okx.enabled

    @property
    def feed_ready(self) -> bool:
        return self.settings.post and self.chain_index is not None and self.api.configured

    def publish(
        self, auction: Auction, lot: LotView, *, dry_run: bool = False
    ) -> PublishResult | None:
        if not lot.active:
            return None
        if self.feed_ready:
            return self.publish_quote(auction, lot, dry_run=dry_run)
        return self.publish_standing(auction, lot, dry_run=dry_run)

    # Feed mode

    def quote_params(self, auction: Auction, lot: LotView) -> dict[str, str]:
        assert self.chain_index is not None
        return {
            "chainIndex": self.chain_index,
            "mode": "intent",
            "swapMode": "exactIn",
            "fromTokenAddress": lot.token,
            "toTokenAddress": lot.want,
            "amount": str(lot.available),
            "userWalletAddress": auction.address,
            "swapReceiverAddress": lot.receiver,
            "signingScheme": SIGNING_SCHEME,
            "slippagePercent": self.settings.slippage_percent,
            "expiration": str(self.settings.expiration),
        }

    def check_quote(
        self, auction: Auction, lot: LotView, order: OKXOrder, domain: dict
    ) -> str | None:
        """Why the quote's order cannot be signed for this lot, or None."""
        verifying = domain.get("verifyingContract")
        if not verifying or to_checksum_address(verifying) != self.settlement:
            return (
                f"verifyingContract {verifying} is not the adapter's settlement {self.settlement}"
            )
        if str(domain.get("chainId")) != str(self.client.chain_id):
            return f"domain chainId {domain.get('chainId')} is not this chain"
        if order.owner != auction.address:
            return f"owner {order.owner} is not the auction"
        if order.receiver != lot.receiver:
            return f"receiver {order.receiver} is not the auction's receiver {lot.receiver}"
        if order.from_token != lot.token or order.to_token != lot.want:
            return f"tokens {order.from_token} -> {order.to_token} are not the lot pair"
        if order.swap_mode != EXACT_IN or order.partially_fillable:
            return "not an exactIn fill-or-kill order"
        if order.from_amount == 0 or order.from_amount > lot.available:
            return f"sell amount {order.from_amount} exceeds the available {lot.available}"
        needed = (
            lot.amount_needed
            if order.from_amount == lot.available
            else auction.get_amount_needed(lot.token, order.from_amount)
        )
        if order.to_amount < needed:
            return f"OKX quote {order.to_amount} is below the curve quote {needed}"
        if order.valid_to <= lot.timestamp:
            return "quote already expired"
        if order.valid_to > lot.end:
            return f"validTo {order.valid_to} is past the lot end {lot.end}"
        return None

    def publish_quote(
        self, auction: Auction, lot: LotView, *, dry_run: bool
    ) -> PublishResult | None:
        try:
            quote = self.api.quote(self.quote_params(auction, lot))
        except ApiError as exc:
            log.warning("%s: OKX intent quote failed: %s", lot.token, exc)
            return None
        sign_data = quote.get("signData") or {}
        message = sign_data.get("message")
        quote_id = quote.get("quoteId")
        if not message or not quote_id:
            log.warning("%s: OKX quote carries no signData.message/quoteId: %s", lot.token, quote)
            return None
        try:
            order = OKXOrder.from_message(message)
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("%s: malformed signData.message %s: %s", lot.token, message, exc)
            return None
        reason = self.check_quote(auction, lot, order, sign_data.get("domain") or {})
        if reason is not None:
            log.info("%s: OKX quote %s skipped: %s", lot.token, quote_id, reason)
            return None
        signature = order.signature(self.address)
        digest = order.digest(self.domain_separator)
        reason = self.validate_signature(auction, digest, signature)
        if reason is not None:
            log.warning("%s: OKX order rejected by the auction router: %s", lot.token, reason)
            return None
        if not self.ensure_allowance(auction, lot, self.token_approve, dry_run=dry_run):
            return None
        posted = (
            bytes.fromhex(auction.address[2:]) if self.settings.owner_prefix else b""
        ) + signature
        body = {
            "chainIndex": self.chain_index,
            "fromTokenAddress": order.from_token,
            "toTokenAddress": order.to_token,
            "fromTokenAmount": str(order.from_amount),
            "toTokenAmount": str(order.to_amount),
            "userWalletAddress": order.owner,
            "swapReceiverAddress": order.receiver,
            "validTo": order.valid_to,
            "quoteId": str(quote_id),
            "appData": hex_bytes(order.app_data),
            "signingScheme": SIGNING_SCHEME,
            "signature": hex_bytes(posted),
            "commissionInfos": [c.as_json() for c in order.commissions],
        }
        uid = hex_bytes(order.uid(self.domain_separator))
        details: dict[str, Any] = {
            "quoteId": str(quote_id),
            "orderUid": uid,
            "order": order.as_json(),
            "signature": hex_bytes(signature),
            "ownerPrefix": self.settings.owner_prefix,
        }
        summary = (
            f"{lot.token} sells {order.from_amount} for >= {order.to_amount} "
            f"(curve {lot.amount_needed}), validTo {order.valid_to}"
        )
        if dry_run:
            log.info("dry run: would create OKX intent order for quote %s (%s)", quote_id, summary)
        else:
            try:
                response = self.api.create_order(body)
            except ApiError as exc:
                log.warning(
                    "%s: OKX create-order for quote %s rejected: %s", lot.token, quote_id, exc
                )
                return None
            details["response"] = response
            if response.get("orderUid"):
                uid = str(response["orderUid"])
            log.info("created OKX intent order %s for quote %s: %s", uid, quote_id, summary)
        return PublishResult(
            uid=uid,
            valid_to=order.valid_to,
            sell_amount=order.from_amount,
            buy_amount=order.to_amount,
            details=details,
        )

    # Standing-ask mode

    def publish_standing(
        self, auction: Auction, lot: LotView, *, dry_run: bool
    ) -> PublishResult | None:
        valid_to = self.valid_to(lot)
        if valid_to <= lot.timestamp:
            log.info("%s: lot window closes before an order could be valid", lot.token)
            return None
        app_data = random_salt(256).to_bytes(32, "big")
        try:
            built, _ = self.contract.order_for(auction.address, lot.token, 0, app_data)
        except CallReverted as exc:
            log.warning("%s: order_for rejected the lot: %s", lot.token, exc)
            return None
        order = OKXOrder.from_tuple(built).with_valid_to(valid_to)
        signature = order.signature(self.address)
        digest = order.digest(self.domain_separator)
        reason = self.validate_signature(auction, digest, signature)
        if reason is not None:
            log.warning("%s: OKX order rejected by the auction router: %s", lot.token, reason)
            return None
        if not self.ensure_allowance(auction, lot, self.token_approve, dry_run=dry_run):
            return None
        uid = hex_bytes(order.uid(self.domain_separator))
        details: dict[str, Any] = {
            "orderUid": uid,
            "order": order.as_json(),
            "signature": hex_bytes(signature),
        }
        summary = f"{lot.token} sells {order.from_amount} for >= {order.to_amount}"
        if dry_run:
            log.info("dry run: would publish OKX standing ask %s (%s)", uid, summary)
        else:
            log.info(
                "OKX standing ask %s built, not posted (%s; only allowlisted solvers settle): "
                "settle signature=%s ++ %s",
                uid,
                summary,
                auction.address,
                details["signature"],
            )
        return PublishResult(
            uid=uid,
            valid_to=order.valid_to,
            sell_amount=order.from_amount,
            buy_amount=order.to_amount,
            details=details,
        )


__all__ = ["OKX_CHAIN_INDEXES", "CommissionInfo", "OKXApi", "OKXIntentPublisher", "OKXOrder"]
