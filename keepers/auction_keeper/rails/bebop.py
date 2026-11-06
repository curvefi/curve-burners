"""Bebop JAM publisher: ``JamOrder``s signed through the auction's ERC-1271
router (``BebopJamAdapter``).

Feed mode (``bebop.post: true``, the default): each poll asks the JAM API
for a gasless quote with the auction as taker and the auction's receiver as
receiver, checks the returned ``toSign`` order against the lot (our token
and payment token, the whole available amount, ``buyAmounts[0]`` at or above
the live curve quote — a quote below the curve cannot be signed, the
auction's ``check_order`` would reject it), signs it as ``adapter ++
abi_encode(toSign)``, validates the pair through ``isValidSignature``, and
POSTs ``{quote_id, signature, sign_scheme: "EIP1271"}``; the winning solver
settles right away. JAM quotes expire in about a minute, so the whole cycle
(quote, check, sign, post) happens within one poll and the recorded order
expires with the quote; the next poll quotes again while the lot is live.

Standing-ask mode (``post: false``, or no chain slug for the network): the
permissionless order from ``BebopJamAdapter.order_for`` (any executor, no
fees, expiring with the keeper TTL) is validated and logged for solvers that
read the log or the state file; anyone may fill it through ``settle``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

from keepers.auction_keeper.chain.abi import CallReverted
from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.chain.contracts import (
    JAM_ORDER_ABI,
    AdapterConfig,
    Auction,
    BebopJamAdapterContract,
    LotView,
)
from keepers.auction_keeper.config import BebopSettings, NetworkConfig
from keepers.auction_keeper.rails import AdapterPublisher, PublishResult
from keepers.auction_keeper.rails.common import ApiError, JsonClient, bearer, hex_bytes, random_salt

log = logging.getLogger(__name__)

# JamOrderLib.ORDER_TYPE_HASH
ORDER_TYPE_HASH = keccak(
    text=(
        "JamOrder(address taker,address receiver,uint256 expiry,uint256 exclusivityDeadline,"
        "uint256 nonce,address executor,uint256 partnerInfo,address[] sellTokens,"
        "address[] buyTokens,uint256[] sellAmounts,uint256[] buyAmounts,bytes32 hooksHash)"
    )
)
EMPTY_HOOKS_HASH = bytes(32)
SIGN_SCHEME = "EIP1271"
# API path slugs by chain id; `bebop.chain` overrides (e.g. robinhood).
BEBOP_CHAINS: dict[int, str] = {
    1: "ethereum",
    10: "optimism",
    56: "bsc",
    137: "polygon",
    999: "hyperevm",
    8453: "base",
    42161: "arbitrum",
    43114: "avalanche",
}


@dataclass(frozen=True)
class JamOrder:
    """The signed JamOrder: the twelve EIP-712 fields, ``toSign`` field for field."""

    taker: str
    receiver: str
    expiry: int
    exclusivity_deadline: int
    nonce: int
    executor: str
    partner_info: int
    sell_tokens: tuple[str, ...]
    buy_tokens: tuple[str, ...]
    sell_amounts: tuple[int, ...]
    buy_amounts: tuple[int, ...]
    hooks_hash: bytes = EMPTY_HOOKS_HASH

    @classmethod
    def from_tuple(cls, values: tuple) -> JamOrder:
        taker, receiver, expiry, deadline, nonce, executor, partner, st, bt, sa, ba, hooks = values
        return cls(
            to_checksum_address(taker),
            to_checksum_address(receiver),
            expiry,
            deadline,
            nonce,
            to_checksum_address(executor),
            partner,
            tuple(to_checksum_address(t) for t in st),
            tuple(to_checksum_address(t) for t in bt),
            tuple(sa),
            tuple(ba),
            bytes(hooks),
        )

    @classmethod
    def from_to_sign(cls, to_sign: dict[str, Any]) -> JamOrder:
        """The API's ``toSign`` object (amounts and nonce as decimal strings)."""
        return cls(
            to_checksum_address(to_sign["taker"]),
            to_checksum_address(to_sign["receiver"]),
            int(to_sign["expiry"]),
            int(to_sign["exclusivityDeadline"]),
            int(to_sign["nonce"]),
            to_checksum_address(to_sign["executor"]),
            int(to_sign["partnerInfo"]),
            tuple(to_checksum_address(t) for t in to_sign["sellTokens"]),
            tuple(to_checksum_address(t) for t in to_sign["buyTokens"]),
            tuple(int(a) for a in to_sign["sellAmounts"]),
            tuple(int(a) for a in to_sign["buyAmounts"]),
            bytes.fromhex(to_sign["hooksHash"][2:]),
        )

    def as_tuple(self) -> tuple:
        return (
            self.taker,
            self.receiver,
            self.expiry,
            self.exclusivity_deadline,
            self.nonce,
            self.executor,
            self.partner_info,
            list(self.sell_tokens),
            list(self.buy_tokens),
            list(self.sell_amounts),
            list(self.buy_amounts),
            self.hooks_hash,
        )

    def with_expiry(self, expiry: int) -> JamOrder:
        return replace(self, expiry=expiry)

    def encode(self) -> bytes:
        return encode([JAM_ORDER_ABI], [self.as_tuple()])

    def signature(self, adapter: str) -> bytes:
        return bytes.fromhex(adapter[2:]) + self.encode()

    def struct_hash(self) -> bytes:
        """JamOrderLib.hash: arrays hashed as their packed words."""

        def packed(values: tuple, abi_type: str) -> bytes:
            return keccak(b"".join(encode([abi_type], [v]) for v in values))

        return keccak(
            encode(
                ["bytes32", "address", "address", "uint256", "uint256", "uint256", "address"]
                + ["uint256"]
                + ["bytes32"] * 5,
                [
                    ORDER_TYPE_HASH,
                    self.taker,
                    self.receiver,
                    self.expiry,
                    self.exclusivity_deadline,
                    self.nonce,
                    self.executor,
                    self.partner_info,
                    packed(self.sell_tokens, "address"),
                    packed(self.buy_tokens, "address"),
                    packed(self.sell_amounts, "uint256"),
                    packed(self.buy_amounts, "uint256"),
                    self.hooks_hash,
                ],
            )
        )

    def digest(self, domain_separator: bytes) -> bytes:
        """The digest the settlement passes to the taker's isValidSignature."""
        return keccak(b"\x19\x01" + domain_separator + self.struct_hash())


class BebopApi:
    def __init__(self, http: Any, settings: BebopSettings):
        self.http = http
        self.settings = settings

    def _url(self, chain: str, path: str) -> str:
        return f"{self.settings.api_url.rstrip('/')}/jam/{chain}/v2/{path}"

    def quote(self, chain: str, params: dict[str, str]) -> dict[str, Any]:
        """``GET /jam/{chain}/v2/quote``; API-level errors come back as HTTP 200
        ``{"error": {"errorCode", "message"}}`` and are raised as ApiError."""
        payload = self.http.request(
            "GET", self._url(chain, "quote"), params=params, headers=bearer(self.settings.api_key)
        )
        if not isinstance(payload, dict) or "error" in payload:
            raise ApiError(200, payload, self._url(chain, "quote"))
        return payload

    def order(self, chain: str, body: dict[str, Any]) -> Any:
        """``POST /jam/{chain}/v2/order`` with ``{quote_id, signature, sign_scheme}``."""
        payload = self.http.request(
            "POST", self._url(chain, "order"), json=body, headers=bearer(self.settings.api_key)
        )
        if isinstance(payload, dict) and "error" in payload:
            raise ApiError(200, payload, self._url(chain, "order"))
        return payload


class BebopJamPublisher(AdapterPublisher):
    version = "BebopJamAdapter"

    def __init__(
        self,
        client: ChainClient,
        adapter: AdapterConfig,
        network: NetworkConfig,
        *,
        http: Any = None,
    ):
        super().__init__(client, adapter, network)
        self.settings = network.bebop
        self.contract = BebopJamAdapterContract(client, adapter.adapter)
        self.settlement = self.contract.settlement()
        self.balance_manager = self.contract.balance_manager()
        self.domain_separator = self.contract.domain_separator()
        self.api = BebopApi(http or JsonClient(), self.settings)
        self.chain = self.settings.chain or BEBOP_CHAINS.get(client.chain_id)
        if adapter.executor != self.balance_manager:
            log.warning(
                "BebopJamAdapter %s: registry executor %s differs from the balance manager %s",
                adapter.adapter,
                adapter.executor,
                self.balance_manager,
            )
        if self.settings.post and self.chain is None:
            log.warning(
                "bebop: no API chain slug for chain id %s (set bebop.chain); "
                "publishing standing asks only",
                client.chain_id,
            )

    @classmethod
    def enabled(cls, network: NetworkConfig) -> bool:
        return network.bebop.enabled

    def publish(
        self, auction: Auction, lot: LotView, *, dry_run: bool = False
    ) -> PublishResult | None:
        if not lot.active:
            return None
        if self.settings.post and self.chain is not None:
            return self.publish_quote(auction, lot, dry_run=dry_run)
        return self.publish_standing(auction, lot, dry_run=dry_run)

    # Feed mode

    def quote_params(self, auction: Auction, lot: LotView) -> dict[str, str]:
        return {
            "sell_tokens": lot.token,
            "buy_tokens": lot.want,
            "sell_amounts": str(lot.available),
            "taker_address": auction.address,
            "receiver_address": lot.receiver,
            "approval_type": "Standard",
            "gasless": "true",
        }

    def check_quote(self, auction: Auction, lot: LotView, order: JamOrder) -> str | None:
        """Why the quote's ``toSign`` cannot be signed for this lot, or None."""
        if order.taker != auction.address:
            return f"taker {order.taker} is not the auction"
        if order.receiver != lot.receiver:
            return f"receiver {order.receiver} is not the auction's receiver {lot.receiver}"
        if order.sell_tokens != (lot.token,) or order.buy_tokens != (lot.want,):
            return f"tokens {order.sell_tokens} -> {order.buy_tokens} are not the lot pair"
        if len(order.sell_amounts) != 1 or len(order.buy_amounts) != 1:
            return "not a one-to-one order"
        if order.hooks_hash != EMPTY_HOOKS_HASH:
            return "order carries hooks"
        sell = order.sell_amounts[0]
        if sell == 0 or sell > lot.available:
            return f"sell amount {sell} exceeds the available {lot.available}"
        needed = (
            lot.amount_needed
            if sell == lot.available
            else auction.get_amount_needed(lot.token, sell)
        )
        if order.buy_amounts[0] < needed:
            return f"JAM quote {order.buy_amounts[0]} is below the curve quote {needed}"
        if order.expiry <= lot.timestamp:
            return "quote already expired"
        if order.expiry > lot.end:
            return f"quote expiry {order.expiry} is past the lot end {lot.end}"
        return None

    def publish_quote(
        self, auction: Auction, lot: LotView, *, dry_run: bool
    ) -> PublishResult | None:
        assert self.chain is not None
        try:
            quote = self.api.quote(self.chain, self.quote_params(auction, lot))
        except ApiError as exc:
            log.warning("%s: JAM quote failed: %s", lot.token, exc)
            return None
        to_sign = quote.get("toSign")
        quote_id = quote.get("quoteId")
        if not to_sign or not quote_id:
            log.warning("%s: JAM quote carries no toSign/quoteId: %s", lot.token, quote)
            return None
        settlement = to_checksum_address(quote.get("settlementAddress") or self.settlement)
        if settlement != self.settlement:
            log.warning(
                "%s: JAM settlement %s is not the adapter's %s",
                lot.token,
                settlement,
                self.settlement,
            )
            return None
        try:
            order = JamOrder.from_to_sign(to_sign)
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("%s: malformed toSign %s: %s", lot.token, to_sign, exc)
            return None
        reason = self.check_quote(auction, lot, order)
        if reason is not None:
            log.info("%s: JAM quote %s skipped: %s", lot.token, quote_id, reason)
            return None
        signature = order.signature(self.address)
        reason = self.validate_signature(auction, order.digest(self.domain_separator), signature)
        if reason is not None:
            log.warning("%s: JAM order rejected by the auction router: %s", lot.token, reason)
            return None
        if not self.ensure_allowance(auction, lot, self.balance_manager, dry_run=dry_run):
            return None
        body = {"quote_id": quote_id, "signature": hex_bytes(signature), "sign_scheme": SIGN_SCHEME}
        details: dict[str, Any] = {
            "quoteId": quote_id,
            "toSign": to_sign,
            "solver": quote.get("solver"),
            "signature": body["signature"],
        }
        summary = (
            f"{lot.token} sells {order.sell_amounts[0]} for >= {order.buy_amounts[0]} "
            f"(curve {lot.amount_needed}), expiry {order.expiry}"
        )
        if dry_run:
            log.info("dry run: would post JAM order for quote %s (%s)", quote_id, summary)
        else:
            try:
                details["response"] = self.api.order(self.chain, body)
            except ApiError as exc:
                log.warning("%s: JAM order %s rejected: %s", lot.token, quote_id, exc)
                return None
            log.info(
                "posted JAM order for quote %s: %s; response %s",
                quote_id,
                summary,
                details["response"],
            )
        return PublishResult(
            uid=str(quote_id),
            valid_to=order.expiry,
            sell_amount=order.sell_amounts[0],
            buy_amount=order.buy_amounts[0],
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
        try:
            built, _ = self.contract.order_for(auction.address, lot.token, random_salt(256))
        except CallReverted as exc:
            log.warning("%s: order_for rejected the lot: %s", lot.token, exc)
            return None
        order = JamOrder.from_tuple(built).with_expiry(valid_to)
        signature = order.signature(self.address)
        digest = order.digest(self.domain_separator)
        reason = self.validate_signature(auction, digest, signature)
        if reason is not None:
            log.warning("%s: JAM order rejected by the auction router: %s", lot.token, reason)
            return None
        if not self.ensure_allowance(auction, lot, self.balance_manager, dry_run=dry_run):
            return None
        order_hash = hex_bytes(digest)
        details: dict[str, Any] = {
            "orderHash": order_hash,
            "order": _json_order(order),
            "signature": hex_bytes(signature),
        }
        summary = f"{lot.token} sells {order.sell_amounts[0]} for >= {order.buy_amounts[0]}"
        if dry_run:
            log.info("dry run: would publish JAM standing ask %s (%s)", order_hash, summary)
        else:
            log.info(
                "JAM standing ask %s built for solvers, not posted (%s): settle(order=%s, sig=%s)",
                order_hash,
                summary,
                details["order"],
                details["signature"],
            )
        return PublishResult(
            uid=order_hash,
            valid_to=order.expiry,
            sell_amount=order.sell_amounts[0],
            buy_amount=order.buy_amounts[0],
            details=details,
        )


def _json_order(order: JamOrder) -> dict[str, Any]:
    return {
        "taker": order.taker,
        "receiver": order.receiver,
        "expiry": order.expiry,
        "exclusivityDeadline": order.exclusivity_deadline,
        "nonce": str(order.nonce),
        "executor": order.executor,
        "partnerInfo": str(order.partner_info),
        "sellTokens": list(order.sell_tokens),
        "buyTokens": list(order.buy_tokens),
        "sellAmounts": [str(a) for a in order.sell_amounts],
        "buyAmounts": [str(a) for a in order.buy_amounts],
        "hooksHash": hex_bytes(order.hooks_hash),
    }


__all__ = ["BEBOP_CHAINS", "BebopApi", "BebopJamPublisher", "JamOrder"]
