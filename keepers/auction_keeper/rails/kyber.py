"""KyberSwap DSLO publisher: Double Signature Limit Orders signed through
the auction's ERC-1271 router (``KyberDsloAdapter``).

Feed mode (``kyber.post: true``, the default): the KyberSwap maker API's
two-step flow. ``POST /write/api/v1/orders/sign-message`` with the lot
(maker = the auction, receiver = the auction's receiver, making amount =
available, taking amount = the live curve quote, ``expiredAt`` = the keeper
TTL bound, never past the lot end) returns the EIP-712 message to sign —
the API dictates ``salt``, ``feeConfig``, the linear getters and the
``and([nonceEquals, timestampBelow])`` predicate; the publisher checks the
echoed fields against the lot and the adapter's ``describe`` (expiry
inside the window), signs ``adapter ++ abi_encode(order)``, validates the
pair through ``isValidSignature`` with the protocol's digest, and ``POST
/write/api/v1/orders`` sends the sign-message body plus ``salt`` and
``signature``. No API key is needed; an optional ``X-Client-Id`` selects
the fee recipient packed into ``feeConfig`` and is sent to both calls.

Fills carry a second, operator signature issued per fill by KyberSwap, so
distribution runs through the orderbook; the maker signature is checked on
the first fill only (an emergency disable needs the allowance sync).

Standing-ask mode (``post: false``): the ``order_for`` order (no fee, any
taker, ``timestampBelow`` predicate at the TTL bound) is validated and
logged for direct fills — but every fill still needs KyberSwap's operator
co-signature, so this is a log rather than a permissionless fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

from keepers.auction_keeper.chain.abi import ZERO_ADDRESS, CallReverted
from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.chain.contracts import (
    DSLO_ORDER_ABI,
    AdapterConfig,
    Auction,
    KyberDsloAdapterContract,
    LotView,
)
from keepers.auction_keeper.config import KyberSettings, NetworkConfig
from keepers.auction_keeper.rails import AdapterPublisher, PublishResult
from keepers.auction_keeper.rails.common import ApiError, JsonClient, hex_bytes, random_salt

log = logging.getLogger(__name__)

ORDER_TYPE_HASH = keccak(
    text=(
        "Order(uint256 salt,address makerAsset,address takerAsset,address maker,address receiver,"
        "address allowedSender,uint256 makingAmount,uint256 takingAmount,uint256 feeConfig,"
        "bytes makerAssetData,bytes takerAssetData,bytes getMakerAmount,bytes getTakerAmount,"
        "bytes predicate,bytes interaction)"
    )
)
STATIC_FIELD_TYPES = ["uint256", "address", "address", "address", "address", "address"] + [
    "uint256"
] * 3
GET_MAKER_AMOUNT = keccak(text="getMakerAmount(uint256,uint256,uint256)")[:4]
GET_TAKER_AMOUNT = keccak(text="getTakerAmount(uint256,uint256,uint256)")[:4]
TIMESTAMP_BELOW = keccak(text="timestampBelow(uint256)")[:4]
SIGN_MESSAGE_PATH = "/write/api/v1/orders/sign-message"
ORDERS_PATH = "/write/api/v1/orders"


def timestamp_below(time: int) -> bytes:
    """PredicateHelper.timestampBelow(t): fillable while block.timestamp < t."""
    return TIMESTAMP_BELOW + encode(["uint256"], [time])


def linear_getters(making_amount: int, taking_amount: int) -> tuple[bytes, bytes]:
    """AmountCalculator's linear getters over the order's own amounts."""
    args = encode(["uint256", "uint256"], [making_amount, taking_amount])
    return GET_MAKER_AMOUNT + args, GET_TAKER_AMOUNT + args


def _hex(value: str) -> bytes:
    return bytes.fromhex(value[2:] if value.startswith("0x") else value)


@dataclass(frozen=True)
class DsloOrder:
    """IDSOrderMixin.Order: the sign-message ``message`` field for field."""

    salt: int
    maker_asset: str
    taker_asset: str
    maker: str
    receiver: str
    allowed_sender: str
    making_amount: int
    taking_amount: int
    fee_config: int
    maker_asset_data: bytes = b""
    taker_asset_data: bytes = b""
    get_maker_amount: bytes = b""
    get_taker_amount: bytes = b""
    predicate: bytes = b""
    interaction: bytes = b""

    @classmethod
    def from_tuple(cls, values: tuple) -> DsloOrder:
        salt, ma, ta, maker, receiver, sender, making, taking, fee, *data = values
        return cls(
            salt,
            to_checksum_address(ma),
            to_checksum_address(ta),
            to_checksum_address(maker),
            to_checksum_address(receiver),
            to_checksum_address(sender),
            making,
            taking,
            fee,
            *(bytes(d) for d in data),
        )

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> DsloOrder:
        return cls(
            int(message["salt"]),
            to_checksum_address(message["makerAsset"]),
            to_checksum_address(message["takerAsset"]),
            to_checksum_address(message["maker"]),
            to_checksum_address(message.get("receiver") or ZERO_ADDRESS),
            to_checksum_address(message.get("allowedSender") or ZERO_ADDRESS),
            int(message["makingAmount"]),
            int(message["takingAmount"]),
            int(message["feeConfig"]),
            _hex(message.get("makerAssetData") or "0x"),
            _hex(message.get("takerAssetData") or "0x"),
            _hex(message.get("getMakerAmount") or "0x"),
            _hex(message.get("getTakerAmount") or "0x"),
            _hex(message.get("predicate") or "0x"),
            _hex(message.get("interaction") or "0x"),
        )

    def as_tuple(self) -> tuple:
        return (
            self.salt,
            self.maker_asset,
            self.taker_asset,
            self.maker,
            self.receiver,
            self.allowed_sender,
            self.making_amount,
            self.taking_amount,
            self.fee_config,
            self.maker_asset_data,
            self.taker_asset_data,
            self.get_maker_amount,
            self.get_taker_amount,
            self.predicate,
            self.interaction,
        )

    def with_expiry(self, valid_to: int) -> DsloOrder:
        """A bare ``timestampBelow`` predicate whose last fillable second is ``valid_to``."""
        return replace(self, predicate=timestamp_below(valid_to + 1))

    def encode(self) -> bytes:
        return encode([DSLO_ORDER_ABI], [self.as_tuple()])

    def signature(self, adapter: str) -> bytes:
        return bytes.fromhex(adapter[2:]) + self.encode()

    def digest(self, domain_separator: bytes) -> bytes:
        """DSOrderMixin.hashOrder: static fields in place, bytes fields by their hash."""
        fields = self.as_tuple()
        struct_hash = keccak(
            encode(
                ["bytes32", *STATIC_FIELD_TYPES, *["bytes32"] * 6],
                [ORDER_TYPE_HASH, *fields[:9], *[keccak(field) for field in fields[9:]]],
            )
        )
        return keccak(b"\x19\x01" + domain_separator + struct_hash)

    def as_json(self) -> dict[str, Any]:
        return {
            "salt": str(self.salt),
            "makerAsset": self.maker_asset,
            "takerAsset": self.taker_asset,
            "maker": self.maker,
            "receiver": self.receiver,
            "allowedSender": self.allowed_sender,
            "makingAmount": str(self.making_amount),
            "takingAmount": str(self.taking_amount),
            "feeConfig": str(self.fee_config),
            "makerAssetData": hex_bytes(self.maker_asset_data),
            "takerAssetData": hex_bytes(self.taker_asset_data),
            "getMakerAmount": hex_bytes(self.get_maker_amount),
            "getTakerAmount": hex_bytes(self.get_taker_amount),
            "predicate": hex_bytes(self.predicate),
            "interaction": hex_bytes(self.interaction),
        }


class KyberApi:
    """The maker API (docs.kyberswap.com, Limit Order API > Maker API): no key,
    an optional ``X-Client-Id``, a ``{code, message, data}`` envelope."""

    def __init__(self, http: Any, settings: KyberSettings):
        self.http = http
        self.settings = settings

    def _headers(self) -> dict[str, str]:
        return {"X-Client-Id": self.settings.client_id} if self.settings.client_id else {}

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.settings.api_url.rstrip('/')}{path}"
        payload = self.http.request("POST", url, json=body, headers=self._headers())
        if not isinstance(payload, dict) or str(payload.get("code", 0)) != "0":
            raise ApiError(200, payload, url)
        data = payload.get("data")
        return data if isinstance(data, dict) else {"raw": payload}

    def sign_message(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /write/api/v1/orders/sign-message`` -> ``{domain, types, primaryType, message}``."""
        return self._post(SIGN_MESSAGE_PATH, body)

    def create_order(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /write/api/v1/orders``: the sign-message body plus ``salt`` and ``signature``."""
        return self._post(ORDERS_PATH, body)


class KyberDsloPublisher(AdapterPublisher):
    version = "KyberDsloAdapter"

    def __init__(
        self,
        client: ChainClient,
        adapter: AdapterConfig,
        network: NetworkConfig,
        *,
        http: Any = None,
    ):
        super().__init__(client, adapter, network)
        self.settings = network.kyber
        self.contract = KyberDsloAdapterContract(client, adapter.adapter)
        self.protocol = self.contract.protocol()
        self.domain_separator = self.contract.domain_separator()
        self.api = KyberApi(http or JsonClient(), self.settings)
        if adapter.executor != self.protocol:
            log.warning(
                "KyberDsloAdapter %s: registry executor %s differs from the protocol %s",
                adapter.adapter,
                adapter.executor,
                self.protocol,
            )

    @classmethod
    def enabled(cls, network: NetworkConfig) -> bool:
        return network.kyber.enabled

    def publish(
        self, auction: Auction, lot: LotView, *, dry_run: bool = False
    ) -> PublishResult | None:
        if not lot.active:
            return None
        valid_to = self.valid_to(lot)
        if valid_to <= lot.timestamp:
            log.info("%s: lot window closes before an order could be valid", lot.token)
            return None
        if self.settings.post:
            return self.publish_api(auction, lot, valid_to, dry_run=dry_run)
        return self.publish_standing(auction, lot, valid_to, dry_run=dry_run)

    # Feed mode

    def order_body(self, auction: Auction, lot: LotView, valid_to: int) -> dict[str, Any]:
        return {
            "chainId": str(self.client.chain_id),
            "makerAsset": lot.token,
            "takerAsset": lot.want,
            "maker": auction.address,
            "receiver": lot.receiver,
            "makingAmount": str(lot.available),
            "takingAmount": str(lot.amount_needed),
            "expiredAt": valid_to,
        }

    def check_message(
        self, auction: Auction, lot: LotView, order: DsloOrder, domain: dict, valid_to: int
    ) -> str | None:
        """Why the sign-message order cannot be signed for this lot, or None."""
        verifying = domain.get("verifyingContract")
        if not verifying or to_checksum_address(verifying) != self.protocol:
            return f"verifyingContract {verifying} is not the adapter's protocol {self.protocol}"
        if str(domain.get("chainId")) != str(self.client.chain_id):
            return f"domain chainId {domain.get('chainId')} is not this chain"
        if order.maker != auction.address:
            return f"maker {order.maker} is not the auction"
        if order.receiver != lot.receiver:
            return f"receiver {order.receiver} is not the auction's receiver {lot.receiver}"
        if order.maker_asset != lot.token or order.taker_asset != lot.want:
            return f"assets {order.maker_asset} -> {order.taker_asset} are not the lot pair"
        if order.making_amount != lot.available or order.taking_amount < lot.amount_needed:
            return (
                f"amounts {order.making_amount} -> {order.taking_amount} differ from the lot "
                f"({lot.available} -> {lot.amount_needed})"
            )
        try:
            _receiver, _fee_to, _fee, _taker_fee, expiry = self.contract.describe(order.as_tuple())
        except CallReverted as exc:
            return f"adapter rejects the order shape: {exc}"
        # timestampBelow(expiredAt) is strict: the API's expiry lands one second
        # before the requested bound, never past it.
        if expiry > valid_to:
            return f"predicate expiry {expiry} is past the requested {valid_to}"
        if expiry <= lot.timestamp:
            return f"predicate expiry {expiry} already passed"
        return None

    def publish_api(
        self, auction: Auction, lot: LotView, valid_to: int, *, dry_run: bool
    ) -> PublishResult | None:
        body = self.order_body(auction, lot, valid_to)
        try:
            signing = self.api.sign_message(body)
        except ApiError as exc:
            log.warning("%s: Kyber sign-message failed: %s", lot.token, exc)
            return None
        message = signing.get("message")
        if not message:
            log.warning("%s: Kyber sign-message carries no message: %s", lot.token, signing)
            return None
        try:
            order = DsloOrder.from_message(message)
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("%s: malformed sign-message %s: %s", lot.token, message, exc)
            return None
        reason = self.check_message(auction, lot, order, signing.get("domain") or {}, valid_to)
        if reason is not None:
            log.warning("%s: Kyber order skipped: %s", lot.token, reason)
            return None
        signature = order.signature(self.address)
        digest = order.digest(self.domain_separator)
        reason = self.validate_signature(auction, digest, signature)
        if reason is not None:
            log.warning("%s: Kyber order rejected by the auction router: %s", lot.token, reason)
            return None
        if not self.ensure_allowance(auction, lot, self.protocol, dry_run=dry_run):
            return None
        # Record the last fillable second the predicate actually allows.
        valid_to = self.contract.describe(order.as_tuple())[4]
        submit = {**body, "salt": str(order.salt), "signature": hex_bytes(signature)}
        uid = hex_bytes(digest)
        details: dict[str, Any] = {
            "orderHash": uid,
            "order": order.as_json(),
            "signature": submit["signature"],
        }
        summary = (
            f"{lot.token} sells {order.making_amount} for >= {order.taking_amount}, "
            f"expires {valid_to}, feeConfig {order.fee_config}"
        )
        if dry_run:
            log.info("dry run: would post Kyber order %s (%s)", uid, summary)
        else:
            try:
                response = self.api.create_order(submit)
            except ApiError as exc:
                log.warning("%s: Kyber orders POST rejected %s: %s", lot.token, uid, exc)
                return None
            details["response"] = response
            log.info(
                "posted Kyber order %s (api id %s): %s",
                uid,
                response.get("id") or response.get("orderId"),
                summary,
            )
        return PublishResult(
            uid=uid,
            valid_to=valid_to,
            sell_amount=order.making_amount,
            buy_amount=order.taking_amount,
            details=details,
        )

    # Standing-ask mode

    def publish_standing(
        self, auction: Auction, lot: LotView, valid_to: int, *, dry_run: bool
    ) -> PublishResult | None:
        try:
            built, _ = self.contract.order_for(auction.address, lot.token, random_salt(96))
        except CallReverted as exc:
            log.warning("%s: order_for rejected the lot: %s", lot.token, exc)
            return None
        order = DsloOrder.from_tuple(built).with_expiry(valid_to)
        signature = order.signature(self.address)
        digest = order.digest(self.domain_separator)
        reason = self.validate_signature(auction, digest, signature)
        if reason is not None:
            log.warning("%s: Kyber order rejected by the auction router: %s", lot.token, reason)
            return None
        if not self.ensure_allowance(auction, lot, self.protocol, dry_run=dry_run):
            return None
        uid = hex_bytes(digest)
        details: dict[str, Any] = {
            "orderHash": uid,
            "order": order.as_json(),
            "signature": hex_bytes(signature),
        }
        summary = f"{lot.token} sells {order.making_amount} for >= {order.taking_amount}"
        if dry_run:
            log.info("dry run: would publish Kyber standing ask %s (%s)", uid, summary)
        else:
            log.info(
                "Kyber standing ask %s built, not posted (%s; fills need the operator "
                "co-signature): fillOrderTo(order=%s, orderSignature=%s)",
                uid,
                summary,
                details["order"],
                details["signature"],
            )
        return PublishResult(
            uid=uid,
            valid_to=valid_to,
            sell_amount=order.making_amount,
            buy_amount=order.taking_amount,
            details=details,
        )


__all__ = ["DsloOrder", "KyberApi", "KyberDsloPublisher", "linear_getters", "timestamp_below"]
