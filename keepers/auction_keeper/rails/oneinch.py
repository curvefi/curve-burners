"""1inch publishers: Limit Order Protocol v4 orders signed through the auction's
ERC-1271 router, in the three formats the 1inch adapters accept.

* ``OneInchPublisher`` (``OneInchAdapter``): plain orders without an
  extension. The public orderbook relays only FeeTaker orders, so by default
  (``oneinch.post: false``) the order and its signature are logged and
  recorded for direct on-chain fills (``fillContractOrder``); with ``post:
  true`` they are POSTed to ``oneinch.orderbook_url`` (a private feed).
* ``OneInchFeeTakerPublisher`` (``OneInchFeeTakerAdapter``): the orderbook's
  ``fee-info`` fees, whitelist and FeeTaker address are encoded into the
  standard FeeTaker extension (the bytes ``@1inch/limit-order-sdk``'s
  ``FeeTakerExtension.build()`` produces), the adapter's ``order_for`` builds
  the order over it, and the pair is POSTed to the orderbook.
* ``OneInchFusionPublisher`` (``OneInchFusionAdapter``): a Fusion quote with
  a custom preset (end amount = the curve quote, start amount above it) is
  encoded into the Fusion extension (``@1inch/fusion-sdk``'s
  ``FusionExtension.build()`` bytes), ``order_for`` builds the order, and the
  pair is submitted to the Fusion relayer with the ``quoteId``.

All three: the order's expiration is shortened to the keeper TTL (never past
the lot end) so the standing ask is reposted lower, with a fresh salt, as the
curve decays; the signature is ``adapter ++ abi_encode(order, extension)``;
validity is checked through the auction's ``isValidSignature`` (the router
itself swallows contract-signature reverts into ``BadSignature``); the
executor whose allowance is checked is the protocol (AggregationRouterV6).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

from keepers.auction_keeper.chain.abi import MAX_UINT256, ZERO_ADDRESS, CallReverted
from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.chain.contracts import (
    ONEINCH_ORDER_ABI,
    AdapterConfig,
    Auction,
    LotView,
    OneInchAdapterContract,
    OneInchFeeTakerAdapterContract,
    OneInchFusionAdapterContract,
)
from keepers.auction_keeper.config import NetworkConfig, OneInchSettings
from keepers.auction_keeper.rails import AdapterPublisher, PublishResult
from keepers.auction_keeper.rails.common import (
    ApiError,
    JsonClient,
    bearer,
    hex_bytes,
    random_salt,
)

log = logging.getLogger(__name__)

# OrderLib._LIMIT_ORDER_TYPEHASH
ORDER_TYPE_HASH = keccak(
    text=(
        "Order(uint256 salt,address maker,address receiver,address makerAsset,"
        "address takerAsset,uint256 makingAmount,uint256 takingAmount,uint256 makerTraits)"
    )
)
ORDER_FIELD_TYPES = ["uint256", "address", "address", "address", "address"] + ["uint256"] * 3
# MakerTraitsLib
EXPIRATION_OFFSET = 80
EXPIRATION_MASK = 2**40 - 1
MASK160 = 2**160 - 1
# The SDK's default order expiration delay after a Fusion auction ends.
FUSION_EXPIRATION_DELAY = 12
# Shortest resolver auction worth quoting (seconds).
FUSION_MIN_AUCTION = 30
BPS = 10_000


@dataclass(frozen=True)
class OneInchOrder:
    """IOrderMixin.Order with clean address fields."""

    salt: int
    maker: str
    receiver: str
    maker_asset: str
    taker_asset: str
    making_amount: int
    taking_amount: int
    maker_traits: int

    @classmethod
    def from_tuple(cls, values: tuple) -> OneInchOrder:
        salt, maker, receiver, maker_asset, taker_asset, making, taking, traits = values
        return cls(
            salt,
            to_checksum_address(maker),
            to_checksum_address(receiver),
            to_checksum_address(maker_asset),
            to_checksum_address(taker_asset),
            making,
            taking,
            traits,
        )

    def as_tuple(self) -> tuple:
        return (
            self.salt,
            self.maker,
            self.receiver,
            self.maker_asset,
            self.taker_asset,
            self.making_amount,
            self.taking_amount,
            self.maker_traits,
        )

    @property
    def expiration(self) -> int:
        return (self.maker_traits >> EXPIRATION_OFFSET) & EXPIRATION_MASK

    def with_expiration(self, timestamp: int) -> OneInchOrder:
        traits = self.maker_traits & ~(EXPIRATION_MASK << EXPIRATION_OFFSET)
        return replace(
            self, maker_traits=traits | ((timestamp & EXPIRATION_MASK) << EXPIRATION_OFFSET)
        )

    def encode(self, extension: bytes = b"") -> bytes:
        """``abi_encode(order, extension)``: the bare payload the adapters validate."""
        return encode([ONEINCH_ORDER_ABI, "bytes"], [self.as_tuple(), extension])

    def signature(self, adapter: str, extension: bytes = b"") -> bytes:
        return bytes.fromhex(adapter[2:]) + self.encode(extension)

    def digest(self, domain_separator: bytes) -> bytes:
        """OrderLib.hash: the order hash the protocol asks the maker to sign."""
        struct_hash = keccak(
            encode(["bytes32", *ORDER_FIELD_TYPES], [ORDER_TYPE_HASH, *self.as_tuple()])
        )
        return keccak(b"\x19\x01" + domain_separator + struct_hash)

    def api_data(self) -> dict[str, str]:
        """``LimitOrderV4Struct`` as the 1inch APIs take it (decimal strings)."""
        return {
            "salt": str(self.salt),
            "maker": self.maker,
            "receiver": self.receiver,
            "makerAsset": self.maker_asset,
            "takerAsset": self.taker_asset,
            "makingAmount": str(self.making_amount),
            "takingAmount": str(self.taking_amount),
            "makerTraits": str(self.maker_traits),
        }


# Extension encoders: byte-identical to the SDK builders (FeeTakerExtension,
# FusionExtension) as checked against their vectors in tests/burners.


def build_extension(fields: list[bytes]) -> bytes:
    """ExtensionLib layout: a word of eight cumulative uint32 field ends, then
    the fields (makerAssetSuffix, takerAssetSuffix, makingAmountData,
    takingAmountData, predicate, makerPermit, preInteraction, postInteraction)."""
    assert len(fields) == 8
    offsets, end = 0, 0
    for index, field in enumerate(fields):
        end += len(field)
        offsets |= end << (32 * index)
    return offsets.to_bytes(32, "big") + b"".join(fields)


def _address_bytes(address: str) -> bytes:
    return bytes.fromhex(address[2:])


def _address_half(address: str) -> bytes:
    return _address_bytes(address)[-10:]


def encode_fees(
    integrator_fee: int, integrator_share: int, resolver_fee: int, whitelist_discount: int
) -> bytes:
    """AmountGetterWithFee fee data: fees in 1e5, share and discount numerator in 1e2."""
    return (
        integrator_fee.to_bytes(2, "big")
        + integrator_share.to_bytes(1, "big")
        + resolver_fee.to_bytes(2, "big")
        + whitelist_discount.to_bytes(1, "big")
    )


def encode_whitelist(addresses: list[str]) -> bytes:
    return len(addresses).to_bytes(1, "big") + b"".join(_address_half(a) for a in addresses)


def _post_header(integrator: str, protocol: str, custom_receiver: str | None) -> bytes:
    flags = b"\x01" if custom_receiver else b"\x00"
    receiver = _address_bytes(custom_receiver) if custom_receiver else b""
    return flags + _address_bytes(integrator) + _address_bytes(protocol) + receiver


def fee_taker_extension(
    fee_taker: str,
    fees: bytes,
    whitelist: list[str],
    integrator: str,
    protocol: str,
    custom_receiver: str,
) -> bytes:
    """``FeeTakerExtension.new(fee_taker, fees, whitelist, {customReceiver}).build().encode()``."""
    getter = _address_bytes(fee_taker) + fees + encode_whitelist(whitelist)
    post = (
        _address_bytes(fee_taker)
        + _post_header(integrator, protocol, custom_receiver)
        + fees
        + encode_whitelist(whitelist)
    )
    return build_extension([b"", b"", getter, getter, b"", b"", b"", post])


def encode_auction_details(
    start_time: int,
    duration: int,
    initial_rate_bump: int,
    points: list[tuple[int, int]],
    gas_bump_estimate: int = 0,
    gas_price_estimate: int = 0,
) -> bytes:
    """AuctionDetails.encode: 3 + 4 + 4 + 3 + 3 + 1 bytes, (3 + 2)[N] points."""
    return (
        gas_bump_estimate.to_bytes(3, "big")
        + gas_price_estimate.to_bytes(4, "big")
        + start_time.to_bytes(4, "big")
        + duration.to_bytes(3, "big")
        + initial_rate_bump.to_bytes(3, "big")
        + len(points).to_bytes(1, "big")
        + b"".join(bump.to_bytes(3, "big") + delay.to_bytes(2, "big") for bump, delay in points)
    )


def encode_post_whitelist(allowed_time: int, entries: list[tuple[str, int]]) -> bytes:
    """Whitelist.encode: uint32 resolving start, uint8 size, (bytes10, uint16 delay)[N]."""
    return (
        allowed_time.to_bytes(4, "big")
        + len(entries).to_bytes(1, "big")
        + b"".join(_address_half(a) + delay.to_bytes(2, "big") for a, delay in entries)
    )


def encode_surplus(estimated_taking_amount: int, protocol_fee_percent: int) -> bytes:
    return estimated_taking_amount.to_bytes(32, "big") + protocol_fee_percent.to_bytes(1, "big")


def fusion_extension(
    settlement: str,
    auction: bytes,
    fees: bytes,
    whitelist: list[tuple[str, int]],
    allowed_time: int,
    integrator: str,
    protocol: str,
    custom_receiver: str,
    surplus: bytes,
) -> bytes:
    """``FusionExtension(settlement, auction, whitelist, surplus, {customReceiver, fees}).build()``."""
    getter = (
        _address_bytes(settlement)
        + auction
        + fees
        + encode_whitelist([address for address, _ in whitelist])
    )
    post = (
        _address_bytes(settlement)
        + _post_header(integrator, protocol, custom_receiver)
        + fees
        + encode_post_whitelist(allowed_time, whitelist)
        + surplus
    )
    return build_extension([b"", b"", getter, getter, b"", b"", b"", post])


def whitelist_delays(resolving_start: int, entries: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """``Whitelist.new(resolvingStartTime, [{address, allowFrom}])``: entries
    sorted by allowFrom (clipped to the start) with cumulative delays."""
    clipped = sorted(
        ((address, max(allow_from, resolving_start)) for address, allow_from in entries),
        key=lambda item: item[1],
    )
    out, elapsed = [], 0
    for address, allow_from in clipped:
        delay = allow_from - resolving_start - elapsed
        elapsed += delay
        out.append((address, delay))
    return out


def track_code(source: str | None) -> int:
    """The 4-byte track code the SDKs inject into salt bits [224, 256) from the
    API's ``source``: a 0x-prefixed 4- or 32-byte hex is used as is (its first
    4 bytes), anything else is keccak-hashed (the API returns bare hex)."""
    if not source:
        return 0
    if source.startswith("0x") and len(source) in (10, 66):
        try:
            return int(source[:10], 16)
        except ValueError:
            pass
    return int.from_bytes(keccak(text=source)[:4], "big")


def publisher_salt(source: str | None) -> int:
    """A 96-bit publisher salt whose high 32 bits carry the track code: bound
    above the extension hash it lands in salt bits [224, 256) like
    ``injectTrackCode``."""
    return (track_code(source) << 64) | random_salt(64)


def fee_info_extension(info: dict[str, Any], custom_receiver: str) -> bytes:
    """The FeeTaker extension ``Sdk.createOrder`` builds from a ``fee-info``
    answer: resolver fee and whitelist discount, the integrator fee when the
    answer carries one (bps, receiver and a whole-number share), the
    promotees of the whitelist, and the auction's receiver as custom receiver."""
    integrator_bps = int(info.get("integratorFeeBps") or 0)
    integrator_receiver = info.get("integratorFeeReceiver") or ZERO_ADDRESS
    share = info.get("integratorFeeSharePercent")
    if integrator_bps and (share is None or integrator_receiver == ZERO_ADDRESS):
        integrator_bps = 0  # malformed answer: the SDK falls back to a zero integrator fee
    if not integrator_bps:
        integrator_receiver, share = ZERO_ADDRESS, 0
    fees = encode_fees(
        integrator_bps * 10,
        int(share or 0),
        int(info["feeBps"]) * 10,
        100 - int(info.get("whitelistDiscountPercent") or 0),
    )
    whitelist = [to_checksum_address(a) for a in (info.get("whitelist") or {}).values()]
    return fee_taker_extension(
        to_checksum_address(info["extensionAddress"]),
        fees,
        whitelist,
        to_checksum_address(integrator_receiver),
        to_checksum_address(info["protocolFeeReceiver"]),
        custom_receiver,
    )


def fusion_quote_extension(
    quote: dict[str, Any],
    preset: dict[str, Any],
    *,
    now: int,
    auction_start: int,
    taking_amount: int,
    custom_receiver: str,
) -> bytes:
    """The Fusion extension ``quote.createFusionOrder({receiver, preset})``
    builds: the preset's auction details, the quote's resolvers whitelisted
    from now (an exclusive resolver from the start, the others from the
    auction start), the quote's fees, and the surplus block (market amount
    and surplus fee when the market beats the taking amount, else no fee)."""
    fee = quote.get("fee") or {}
    resolver_bps = int(fee.get("bps") or 0)
    discount = int(fee.get("whitelistDiscountPercent") or 0)
    protocol_receiver = to_checksum_address(fee.get("receiver") or ZERO_ADDRESS)
    integrator_bps = int(quote.get("integratorFee") or 0)
    integrator_receiver = quote.get("integratorFeeReceiver") or ZERO_ADDRESS
    integrator_share = int(quote.get("integratorFeeShare") or 0)
    if integrator_bps and integrator_receiver == ZERO_ADDRESS:
        integrator_bps = 0
    if not integrator_bps:
        integrator_receiver, integrator_share = ZERO_ADDRESS, 0
    surplus_fee = int(quote.get("surplusFee") or 0)
    has_fees = bool(resolver_bps or integrator_bps or surplus_fee > 0)
    fees = encode_fees(
        integrator_bps * 10,
        integrator_share,
        resolver_bps * 10,
        100 - discount if has_fees else 100,
    )
    if not has_fees:
        protocol_receiver = ZERO_ADDRESS

    exclusive = preset.get("exclusiveResolver")
    exclusive = to_checksum_address(exclusive) if exclusive else None
    entries = []
    for resolver in quote.get("whitelist") or []:
        resolver = to_checksum_address(resolver)
        allow_from = 0 if exclusive is None or resolver == exclusive else auction_start
        entries.append((resolver, allow_from))
    whitelist = whitelist_delays(now, entries)

    auction = encode_auction_details(
        auction_start,
        int(preset["auctionDuration"]),
        int(preset["initialRateBump"]),
        [(int(p["coefficient"]), int(p["delay"])) for p in preset.get("points") or []],
        int((preset.get("gasCost") or {}).get("gasBumpEstimate") or 0),
        int((preset.get("gasCost") or {}).get("gasPriceEstimate") or 0),
    )
    market = int(quote.get("marketAmount") or 0)
    surplus = (
        encode_surplus(market, surplus_fee)
        if market > taking_amount
        else encode_surplus(MAX_UINT256, 0)
    )
    return fusion_extension(
        to_checksum_address(quote["settlementAddress"]),
        auction,
        fees,
        whitelist,
        now,
        to_checksum_address(integrator_receiver),
        protocol_receiver,
        custom_receiver,
        surplus,
    )


# API


class OneInchApi:
    """The Developer Portal endpoints the publishers use (orderbook and Fusion)."""

    def __init__(self, http: Any, settings: OneInchSettings, chain_id: int):
        self.http = http
        self.settings = settings
        self.chain_id = chain_id

    def _headers(self) -> dict[str, str]:
        return bearer(self.settings.api_key)

    def fee_info(
        self, maker_asset: str, taker_asset: str, maker_amount: int, taker_amount: int
    ) -> dict[str, Any]:
        """``GET /orderbook/v4.0/{chain}/fee-info``: the FeeTaker the orderbook relays
        through, its fees, fee receivers, resolver whitelist and track source."""
        return self.http.request(
            "GET",
            f"{self.settings.orderbook_url.rstrip('/')}/{self.chain_id}/fee-info",
            params={
                "makerAsset": maker_asset,
                "takerAsset": taker_asset,
                "makerAmount": str(maker_amount),
                "takerAmount": str(taker_amount),
            },
            headers=self._headers(),
        )

    def submit_order(self, order_hash: str, signature: bytes, data: dict[str, str]) -> Any:
        """``POST /orderbook/v4.0/{chain}`` with the SDK's ``submitOrder`` body."""
        return self.http.request(
            "POST",
            f"{self.settings.orderbook_url.rstrip('/')}/{self.chain_id}",
            json={"orderHash": order_hash, "signature": hex_bytes(signature), "data": data},
            headers=self._headers(),
        )

    def fusion_quote(
        self,
        from_token: str,
        to_token: str,
        amount: int,
        wallet: str,
        custom_preset: dict[str, Any],
    ) -> dict[str, Any]:
        """``POST /fusion/quoter/v2.0/{chain}/quote/receive`` with a custom preset
        (``QuoterApi.getQuoteWithCustomPreset``: the request params in the query,
        the preset in the body)."""
        return self.http.request(
            "POST",
            f"{self.settings.fusion_url.rstrip('/')}/quoter/v2.0/{self.chain_id}/quote/receive",
            params={
                "fromTokenAddress": from_token,
                "toTokenAddress": to_token,
                "amount": str(amount),
                "walletAddress": wallet,
                "surplus": "true",
            },
            json={"customPreset": custom_preset},
            headers=self._headers(),
        )

    def fusion_submit(
        self, order: dict[str, str], signature: bytes, quote_id: str, extension: bytes
    ) -> Any:
        """``POST /fusion/relayer/v2.0/{chain}/order/submit`` (``RelayerApi.submit``)."""
        return self.http.request(
            "POST",
            f"{self.settings.fusion_url.rstrip('/')}/relayer/v2.0/{self.chain_id}/order/submit",
            json={
                "order": order,
                "signature": hex_bytes(signature),
                "quoteId": quote_id,
                "extension": hex_bytes(extension),
            },
            headers=self._headers(),
        )


# Publishers


class OneInchPublisher(AdapterPublisher):
    """Plain LOP v4 orders over ``OneInchAdapter.order_for``."""

    version = "OneInchAdapter"
    contract_class: type[OneInchAdapterContract] = OneInchAdapterContract

    def __init__(
        self,
        client: ChainClient,
        adapter: AdapterConfig,
        network: NetworkConfig,
        *,
        http: Any = None,
    ):
        super().__init__(client, adapter, network)
        self.settings = network.oneinch
        self.contract = self.contract_class(client, adapter.adapter)
        self.protocol = self.contract.limit_order_protocol()
        self.domain_separator = self.contract.domain_separator()
        self.api = OneInchApi(http or JsonClient(), self.settings, client.chain_id)
        if adapter.executor != self.protocol:
            log.warning(
                "%s %s: registry executor %s differs from the protocol %s",
                self.version,
                adapter.adapter,
                adapter.executor,
                self.protocol,
            )

    @classmethod
    def enabled(cls, network: NetworkConfig) -> bool:
        return network.oneinch.enabled

    def publish(
        self, auction: Auction, lot: LotView, *, dry_run: bool = False
    ) -> PublishResult | None:
        if not lot.active:
            return None
        valid_to = self.valid_to(lot)
        if valid_to <= lot.timestamp:
            log.info("%s: lot window closes before an order could be valid", lot.token)
            return None
        try:
            built, _ = self.contract.order_for(auction.address, lot.token, random_salt(96))
        except CallReverted as exc:
            log.warning("%s: order_for rejected the lot: %s", lot.token, exc)
            return None
        order = OneInchOrder.from_tuple(built).with_expiration(valid_to)
        if self.settings.post:
            return self.finish(
                auction,
                lot,
                order,
                b"",
                dry_run=dry_run,
                post=lambda h, s: self.api.submit_order(
                    h, s, {**order.api_data(), "extension": "0x"}
                ),
                feed="orderbook",
            )
        return self.finish(auction, lot, order, b"", dry_run=dry_run, post=None, feed=None)

    def finish(
        self,
        auction: Auction,
        lot: LotView,
        order: OneInchOrder,
        extension: bytes,
        *,
        dry_run: bool,
        post: Callable[[str, bytes], Any] | None,
        feed: str | None,
    ) -> PublishResult | None:
        """Validate, check the allowance, then post (or log for direct fills)."""
        signature = order.signature(self.address, extension)
        order_hash = hex_bytes(order.digest(self.domain_separator))
        reason = self.validate_signature(auction, order.digest(self.domain_separator), signature)
        if reason is not None:
            log.warning(
                "%s: %s order rejected by the auction router: %s", lot.token, self.version, reason
            )
            return None
        if not self.ensure_allowance(auction, lot, self.protocol, dry_run=dry_run):
            return None
        details: dict[str, Any] = {
            "orderHash": order_hash,
            "order": order.api_data(),
            "extension": hex_bytes(extension),
            "signature": hex_bytes(signature),
        }
        summary = f"{lot.token} sells {order.making_amount} for >= {order.taking_amount}"
        if dry_run:
            log.info("dry run: would publish %s %s (%s)", self.version, order_hash, summary)
        elif post is None:
            log.info(
                "%s order %s built for direct fills, not posted (%s); "
                "fillContractOrder(order=%s, signature=%s)",
                self.version,
                order_hash,
                summary,
                details["order"],
                details["signature"],
            )
        else:
            try:
                details["response"] = post(order_hash, signature)
            except ApiError as exc:
                log.warning(
                    "%s: %s %s rejected %s: %s", lot.token, self.version, feed, order_hash, exc
                )
                return None
            log.info("posted %s %s to the 1inch %s: %s", self.version, order_hash, feed, summary)
        return PublishResult(
            uid=order_hash,
            valid_to=order.expiration,
            sell_amount=order.making_amount,
            buy_amount=order.taking_amount,
            details=details,
        )


class OneInchFeeTakerPublisher(OneInchPublisher):
    """Orderbook orders: the ``fee-info`` FeeTaker extension over
    ``OneInchFeeTakerAdapter.order_for``, POSTed to the public orderbook."""

    version = "OneInchFeeTakerAdapter"
    contract_class = OneInchFeeTakerAdapterContract
    contract: OneInchFeeTakerAdapterContract

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.fee_taker = self.contract.fee_taker()

    def publish(
        self, auction: Auction, lot: LotView, *, dry_run: bool = False
    ) -> PublishResult | None:
        if not lot.active:
            return None
        valid_to = self.valid_to(lot)
        if valid_to <= lot.timestamp:
            log.info("%s: lot window closes before an order could be valid", lot.token)
            return None
        try:
            info = self.api.fee_info(lot.token, lot.want, lot.available, lot.amount_needed)
        except ApiError as exc:
            log.warning("%s: fee-info failed: %s", lot.token, exc)
            return None
        fee_taker = to_checksum_address(info["extensionAddress"])
        if fee_taker != self.fee_taker:
            log.warning(
                "%s: orderbook FeeTaker %s is not the adapter's %s; not publishing",
                lot.token,
                fee_taker,
                self.fee_taker,
            )
            return None
        extension = fee_info_extension(info, lot.receiver)
        order, _ = self.build(auction, lot, extension, publisher_salt(info.get("source")))
        if order is None:
            return None
        order = order.with_expiration(valid_to)
        return self.finish(
            auction,
            lot,
            order,
            extension,
            dry_run=dry_run,
            post=lambda h, s: self.api.submit_order(
                h, s, {**order.api_data(), "extension": hex_bytes(extension)}
            ),
            feed="orderbook",
        )

    def build(
        self, auction: Auction, lot: LotView, extension: bytes, salt: int
    ) -> tuple[OneInchOrder | None, tuple | None]:
        """``order_for`` over the extension plus ``describe`` for the log; typed
        shape failures (``OrderNotValid(reason)``) are logged, not raised."""
        try:
            built, _ = self.contract.order_for_extension(
                auction.address, lot.token, salt, 0, extension
            )
            described = self.contract.describe(extension)
        except CallReverted as exc:
            log.warning("%s: %s rejected the extension: %s", lot.token, self.version, exc)
            return None, None
        log.info("%s: %s extension %s", lot.token, self.version, self.describe_text(described))
        return OneInchOrder.from_tuple(built), described

    @staticmethod
    def describe_text(described: tuple) -> str:
        fees, receivers, size = described[:3]
        return (
            f"fees(integrator={fees[0]}/1e5 share={fees[1]}% resolver={fees[2]}/1e5 "
            f"discount_numerator={fees[3]}) receivers(integrator={receivers[0]} "
            f"protocol={receivers[1]} custom={receivers[2]}) whitelist={size}"
        )


class OneInchFusionPublisher(OneInchFeeTakerPublisher):
    """Fusion orders: a custom-preset quote encoded into the Fusion extension
    over ``OneInchFusionAdapter.order_for``, submitted to the relayer."""

    version = "OneInchFusionAdapter"
    contract_class = OneInchFusionAdapterContract
    contract: OneInchFusionAdapterContract

    @property
    def settlement(self) -> str:
        return self.fee_taker

    def publish(
        self, auction: Auction, lot: LotView, *, dry_run: bool = False
    ) -> PublishResult | None:
        if not lot.active:
            return None
        valid_to = self.valid_to(lot)
        # The resolver auction plus the SDK's expiration delay must fit the TTL
        # and the lot window; the order expires right after the auction.
        duration = min(
            self.settings.fusion_auction_duration,
            valid_to - lot.timestamp - FUSION_EXPIRATION_DELAY,
        )
        if duration < FUSION_MIN_AUCTION:
            log.info("%s: not enough lot time left for a Fusion auction", lot.token)
            return None
        end_amount = lot.amount_needed
        start_amount = end_amount * (BPS + self.settings.fusion_start_premium_bps) // BPS
        try:
            quote = self.api.fusion_quote(
                lot.token,
                lot.want,
                lot.available,
                auction.address,
                {
                    "auctionDuration": duration,
                    "auctionStartAmount": str(start_amount),
                    "auctionEndAmount": str(end_amount),
                },
            )
        except ApiError as exc:
            log.warning("%s: Fusion quote failed: %s", lot.token, exc)
            return None
        settlement = to_checksum_address(quote.get("settlementAddress") or ZERO_ADDRESS)
        if settlement != self.settlement:
            log.warning(
                "%s: Fusion settlement %s is not the adapter's %s; not publishing",
                lot.token,
                settlement,
                self.settlement,
            )
            return None
        preset = (quote.get("presets") or {}).get("custom")
        quote_id = quote.get("quoteId")
        if not preset or not quote_id:
            log.warning("%s: Fusion quote carries no custom preset or quoteId", lot.token)
            return None
        auction_start = lot.timestamp + int(preset.get("startAuctionIn") or 0)
        expiration = auction_start + int(preset["auctionDuration"]) + FUSION_EXPIRATION_DELAY
        if expiration > lot.end:
            log.info("%s: the Fusion auction would end past the lot window", lot.token)
            return None
        extension = fusion_quote_extension(
            quote,
            preset,
            now=lot.timestamp,
            auction_start=auction_start,
            taking_amount=end_amount,
            custom_receiver=lot.receiver,
        )
        order, _ = self.build(auction, lot, extension, publisher_salt(quote.get("source")))
        if order is None:
            return None
        order = order.with_expiration(expiration)
        return self.finish(
            auction,
            lot,
            order,
            extension,
            dry_run=dry_run,
            post=lambda h, s: self.api.fusion_submit(order.api_data(), s, quote_id, extension),
            feed="Fusion relayer",
        )

    @staticmethod
    def describe_text(described: tuple) -> str:
        base = OneInchFeeTakerPublisher.describe_text(described)
        details, surplus = described[3], described[4]
        return (
            f"{base} auction(start={details[2]} duration={details[3]} "
            f"initial_bump={details[4]}/1e7 points={details[5]} gas_bump={details[0]}) "
            f"surplus(estimate={surplus[0]} fee={surplus[1]}%)"
        )


__all__ = [
    "OneInchApi",
    "OneInchFeeTakerPublisher",
    "OneInchFusionPublisher",
    "OneInchOrder",
    "OneInchPublisher",
    "fee_info_extension",
    "fusion_quote_extension",
    "publisher_salt",
    "track_code",
]
