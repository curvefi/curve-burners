"""Velora Delta publisher: Portikus V2 orders signed through the auction's
ERC-1271 router (``VeloraDeltaAdapter``).

Feed mode (``velora.post: true``, the default): ``GET /v2/delta/prices`` for
the lot (sell side, the auction as ``userAddress``, the auction's receiver as
``beneficiary``) gives Velora's quoted output; the order is then built here
from the adapter's ``order_for`` (owner = the auction, beneficiary = the
FeeCollector, ``srcAmount`` = available, ``destAmount`` = the curve quote
grossed up for a configured partner fee, ``expectedAmount`` = the larger of
the quoted output and ``destAmount``, ``deadline`` = the keeper TTL bound
within the lot window, a random nonce, no permit, no metadata, no bridge) —
the API's own ``/v2/delta/orders/build`` wants an undocumented ``route``
object, so it is not used. The pair ``adapter ++ abi_encode(order)`` is
validated through ``isValidSignature`` under the ``Order`` (fill-or-kill) or
``FillableOrder`` digest per ``velora.partially_fillable``, then ``POST
/v2/delta/orders`` sends ``{chainId, order, signature, partner?,
partiallyFillable, type}`` (the SDK's ``DeltaOrderToPost``). UNVERIFIED: the
docs describe ``signature`` as a 64-byte ERC-2098 compact EOA signature; the
SDK comment restricts that to EOA signatures, but whether the API relays a
long ERC-1271 signature from a contract owner has not been exercised.
Settlement is by Velora's registered agents only.

Standing-ask mode (``post: false``): the same order, validated and logged;
only registered agents can settle, so this is a log, not a permissionless
fallback.
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
    DELTA_ORDER_ABI,
    AdapterConfig,
    Auction,
    Erc20,
    LotView,
    VeloraDeltaAdapterContract,
)
from keepers.auction_keeper.config import NetworkConfig, VeloraSettings
from keepers.auction_keeper.rails import AdapterPublisher, PublishResult
from keepers.auction_keeper.rails.common import ApiError, JsonClient, hex_bytes, random_salt

log = logging.getLogger(__name__)

BRIDGE_TYPE = (
    "Bridge(bytes4 protocolSelector,uint256 destinationChainId,address outputToken,"
    "int8 scalingFactor,bytes protocolData)"
)
ORDER_FIELDS = (
    "(address owner,address beneficiary,address srcToken,address destToken,"
    "uint256 srcAmount,uint256 destAmount,uint256 expectedAmount,uint256 deadline,"
    "uint8 kind,uint256 nonce,uint256 partnerAndFee,bytes permit,bytes metadata,Bridge bridge)"
)
ORDER_TYPE_HASH = keccak(text="Order" + ORDER_FIELDS + BRIDGE_TYPE)
FILLABLE_ORDER_TYPE_HASH = keccak(text="FillableOrder" + ORDER_FIELDS + BRIDGE_TYPE)
BRIDGE_TYPE_HASH = keccak(text=BRIDGE_TYPE)
KIND_SELL = 0
MAX_FEE_BPS = 200
BPS = 10_000
PRICES_PATH = "/v2/delta/prices"
ORDERS_PATH = "/v2/delta/orders"


@dataclass(frozen=True)
class Bridge:
    protocol_selector: bytes = bytes(4)
    destination_chain_id: int = 0
    output_token: str = ZERO_ADDRESS
    scaling_factor: int = 0
    protocol_data: bytes = b""

    def as_tuple(self) -> tuple:
        return (
            self.protocol_selector,
            self.destination_chain_id,
            self.output_token,
            self.scaling_factor,
            self.protocol_data,
        )

    def hash(self) -> bytes:
        return keccak(
            encode(
                ["bytes32", "bytes4", "uint256", "address", "int8", "bytes32"],
                [BRIDGE_TYPE_HASH, *self.as_tuple()[:4], keccak(self.protocol_data)],
            )
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "protocolSelector": hex_bytes(self.protocol_selector),
            "destinationChainId": self.destination_chain_id,
            "outputToken": self.output_token,
            "scalingFactor": self.scaling_factor,
            "protocolData": hex_bytes(self.protocol_data),
        }


@dataclass(frozen=True)
class DeltaOrder:
    """types/Order.sol: the fourteen signed fields."""

    owner: str
    beneficiary: str
    src_token: str
    dest_token: str
    src_amount: int
    dest_amount: int
    expected_amount: int
    deadline: int
    kind: int
    nonce: int
    partner_and_fee: int
    permit: bytes = b""
    metadata: bytes = b""
    bridge: Bridge = Bridge()

    @classmethod
    def from_tuple(cls, values: tuple) -> DeltaOrder:
        owner, beneficiary, src, dest, sa, da, ea, deadline, kind, nonce, paf, permit, meta, b = (
            values
        )
        return cls(
            to_checksum_address(owner),
            to_checksum_address(beneficiary),
            to_checksum_address(src),
            to_checksum_address(dest),
            sa,
            da,
            ea,
            deadline,
            kind,
            nonce,
            paf,
            bytes(permit),
            bytes(meta),
            Bridge(bytes(b[0]), b[1], to_checksum_address(b[2]), b[3], bytes(b[4])),
        )

    def as_tuple(self) -> tuple:
        return (
            self.owner,
            self.beneficiary,
            self.src_token,
            self.dest_token,
            self.src_amount,
            self.dest_amount,
            self.expected_amount,
            self.deadline,
            self.kind,
            self.nonce,
            self.partner_and_fee,
            self.permit,
            self.metadata,
            self.bridge.as_tuple(),
        )

    def encode(self) -> bytes:
        return encode([DELTA_ORDER_ABI], [self.as_tuple()])

    def signature(self, adapter: str) -> bytes:
        return bytes.fromhex(adapter[2:]) + self.encode()

    def digest(self, domain_separator: bytes, *, fillable: bool = False) -> bytes:
        """OrderHashLib.hash / FillableOrderHashLib.hash under the Portikus domain."""
        struct_hash = keccak(
            encode(
                ["bytes32"]
                + ["address"] * 4
                + ["uint256"] * 4
                + ["uint8", "uint256", "uint256", "bytes32", "bytes32", "bytes32"],
                [
                    FILLABLE_ORDER_TYPE_HASH if fillable else ORDER_TYPE_HASH,
                    *self.as_tuple()[:11],
                    keccak(self.permit),
                    keccak(self.metadata),
                    self.bridge.hash(),
                ],
            )
        )
        return keccak(b"\x19\x01" + domain_separator + struct_hash)

    def as_json(self) -> dict[str, Any]:
        """``DeltaAuctionOrder`` as the API takes it (amounts and nonce as strings)."""
        return {
            "owner": self.owner,
            "beneficiary": self.beneficiary,
            "srcToken": self.src_token,
            "destToken": self.dest_token,
            "srcAmount": str(self.src_amount),
            "destAmount": str(self.dest_amount),
            "expectedAmount": str(self.expected_amount),
            "deadline": self.deadline,
            "kind": self.kind,
            "nonce": str(self.nonce),
            "partnerAndFee": str(self.partner_and_fee),
            "permit": hex_bytes(self.permit),
            "metadata": hex_bytes(self.metadata),
            "bridge": self.bridge.as_json(),
        }


def pack_partner_and_fee(
    partner: str, fee_bps: int, *, takes_surplus: bool = False, cap_surplus: bool = True
) -> int:
    """FeeManagerLib packing: partner from bit 96, capSurplus bit 9,
    partnerTakesSurplus bit 8, fee bps in the low 8 bits (clamped to 2%)."""
    return (
        (int(partner, 16) << 96)
        | (int(cap_surplus) << 9)
        | (int(takes_surplus) << 8)
        | min(fee_bps, MAX_FEE_BPS)
    )


def gross_up(net_floor: int, fee_bps: int) -> int:
    """The destAmount whose partner-fee netting (``amount - amount * bps / 1e4``)
    still covers ``net_floor``."""
    if fee_bps == 0:
        return net_floor
    amount = -(-net_floor * BPS // (BPS - fee_bps))
    while amount - amount * fee_bps // BPS < net_floor:
        amount += 1
    # The smallest amount that still nets to the floor (the ceiling overshoots by one at times).
    while amount > net_floor and (amount - 1) - (amount - 1) * fee_bps // BPS >= net_floor:
        amount -= 1
    return amount


class VeloraApi:
    """``/v2/delta/prices`` and ``/v2/delta/orders`` (paths from the Velora SDK,
    ``getDeltaPrice`` and ``postDeltaOrder``); no key."""

    def __init__(self, http: Any, settings: VeloraSettings):
        self.http = http
        self.settings = settings

    def price(self, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.settings.api_url.rstrip('/')}{PRICES_PATH}"
        payload = self.http.request("GET", url, params=params)
        if not isinstance(payload, dict) or "route" not in payload:
            raise ApiError(200, payload, url)
        return payload

    def post_order(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.settings.api_url.rstrip('/')}{ORDERS_PATH}"
        payload = self.http.request("POST", url, json=body)
        if not isinstance(payload, dict) or "id" not in payload:
            raise ApiError(200, payload, url)
        return payload


class VeloraDeltaPublisher(AdapterPublisher):
    version = "VeloraDeltaAdapter"

    def __init__(
        self,
        client: ChainClient,
        adapter: AdapterConfig,
        network: NetworkConfig,
        *,
        http: Any = None,
    ):
        super().__init__(client, adapter, network)
        self.settings = network.velora
        self.contract = VeloraDeltaAdapterContract(client, adapter.adapter)
        self.settlement = self.contract.settlement()
        self.domain_separator = self.contract.domain_separator()
        self.api = VeloraApi(http or JsonClient(), self.settings)
        self._decimals: dict[str, int] = {}
        if adapter.executor != self.settlement:
            log.warning(
                "VeloraDeltaAdapter %s: registry executor %s differs from the Delta contract %s",
                adapter.adapter,
                adapter.executor,
                self.settlement,
            )

    @classmethod
    def enabled(cls, network: NetworkConfig) -> bool:
        return network.velora.enabled

    @property
    def partner_and_fee(self) -> int:
        if self.settings.partner_address and self.settings.partner_fee_bps:
            return pack_partner_and_fee(
                self.settings.partner_address, self.settings.partner_fee_bps
            )
        return 0

    def decimals(self, token: str) -> int:
        if token not in self._decimals:
            self._decimals[token] = Erc20(self.client, token).decimals()
        return self._decimals[token]

    def publish(
        self, auction: Auction, lot: LotView, *, dry_run: bool = False
    ) -> PublishResult | None:
        if not lot.active:
            return None
        valid_to = self.valid_to(lot)
        if valid_to <= lot.timestamp:
            log.info("%s: lot window closes before an order could be valid", lot.token)
            return None
        quoted: int | None = None
        price: dict[str, Any] | None = None
        if self.settings.post:
            try:
                price = self.api.price(self.price_params(auction, lot))
                quoted = quoted_output(price)
            except (ApiError, KeyError, TypeError, ValueError) as exc:
                log.warning("%s: Delta price failed: %s", lot.token, exc)
                return None
        order = self.build_order(auction, lot, valid_to, quoted)
        if order is None:
            return None
        signature = order.signature(self.address)
        digest = order.digest(self.domain_separator, fillable=self.settings.partially_fillable)
        reason = self.validate_signature(auction, digest, signature)
        if reason is not None:
            log.warning("%s: Delta order rejected by the auction router: %s", lot.token, reason)
            return None
        if not self.ensure_allowance(auction, lot, self.settlement, dry_run=dry_run):
            return None
        uid = hex_bytes(digest)
        details: dict[str, Any] = {
            "orderHash": uid,
            "order": order.as_json(),
            "signature": hex_bytes(signature),
            "partiallyFillable": self.settings.partially_fillable,
            "quoted": None if quoted is None else str(quoted),
        }
        summary = (
            f"{lot.token} sells {order.src_amount} for >= {order.dest_amount} "
            f"(expected {order.expected_amount}, curve {lot.amount_needed}), deadline {valid_to}"
        )
        if quoted is not None and quoted < lot.amount_needed:
            log.info(
                "%s: Delta quote %s is below the curve %s; posting the curve floor as a standing ask",
                lot.token,
                quoted,
                lot.amount_needed,
            )
        if dry_run:
            log.info("dry run: would publish Delta order %s (%s)", uid, summary)
        elif not self.settings.post:
            log.info(
                "Delta standing ask %s built, not posted (%s; registered agents settle): "
                "swapSettle(order=%s, signature=%s)",
                uid,
                summary,
                details["order"],
                details["signature"],
            )
        else:
            body = self.order_body(order, signature, price or {})
            try:
                response = self.api.post_order(body)
            except ApiError as exc:
                log.warning("%s: Delta orders POST rejected %s: %s", lot.token, uid, exc)
                return None
            details["response"] = response
            log.info("posted Delta order %s (api id %s): %s", uid, response.get("id"), summary)
        return PublishResult(
            uid=uid,
            valid_to=valid_to,
            sell_amount=order.src_amount,
            buy_amount=order.dest_amount,
            details=details,
        )

    def price_params(self, auction: Auction, lot: LotView) -> dict[str, Any]:
        params: dict[str, Any] = {
            "srcToken": lot.token,
            "destToken": lot.want,
            "amount": str(lot.available),
            "srcDecimals": self.decimals(lot.token),
            "destDecimals": self.decimals(lot.want),
            "chainId": self.client.chain_id,
            "side": "SELL",
            "userAddress": auction.address,
            "beneficiary": lot.receiver,
        }
        if self.settings.partner:
            params["partner"] = self.settings.partner
        return params

    def build_order(
        self, auction: Auction, lot: LotView, valid_to: int, quoted: int | None
    ) -> DeltaOrder | None:
        """``order_for`` with the deadline at the TTL bound, the partner fee
        packed and the floor grossed up for it, the expected amount at the
        quote when it beats the floor (surplus above it is shared)."""
        try:
            built, _ = self.contract.order_for(auction.address, lot.token, random_salt(256))
        except CallReverted as exc:
            log.warning("%s: order_for rejected the lot: %s", lot.token, exc)
            return None
        order = DeltaOrder.from_tuple(built)
        fee_bps = min(self.settings.partner_fee_bps, MAX_FEE_BPS) if self.partner_and_fee else 0
        dest_amount = gross_up(order.dest_amount, fee_bps)
        expected = max(dest_amount, quoted or 0)
        return replace(
            order,
            dest_amount=dest_amount,
            expected_amount=expected,
            deadline=valid_to,
            partner_and_fee=self.partner_and_fee,
        )

    def order_body(
        self, order: DeltaOrder, signature: bytes, price: dict[str, Any]
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "chainId": self.client.chain_id,
            "order": order.as_json(),
            "signature": hex_bytes(signature),
            "partiallyFillable": self.settings.partially_fillable,
            "type": self.settings.order_type,
        }
        if self.settings.partner:
            body["partner"] = self.settings.partner
        return body


def quoted_output(price: dict[str, Any]) -> int:
    """The quoted output of a same-chain Delta price: the destination step's
    output amount (the origin step's when the route has one step)."""
    route = price["route"]
    step = route.get("destination") or route["origin"]
    return int(step["output"]["amount"])


__all__ = [
    "Bridge",
    "DeltaOrder",
    "VeloraApi",
    "VeloraDeltaPublisher",
    "gross_up",
    "pack_partner_and_fee",
    "quoted_output",
]
