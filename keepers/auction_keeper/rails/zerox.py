"""0x Settler publisher: gasless orders signed through the auction's ERC-1271
router (``ZeroExSettlerAdapter``), on either Settler flavor.

The signed object is the Permit2 ``PermitWitnessTransferFrom`` digest the
Settler asks the taker (the auction) for: the permit (lot token, amount,
unordered nonce, deadline), the Settler as spender, and the flavor's witness
(``SlippageAndActions`` for ``SettlerMetaTxn``, ``Slippage`` for
``SettlerIntent``). The signature is ``adapter ++ abi_encode(Order)`` where
``Order`` carries the permit, the slippage and, for a MetaTxn, the Settler's
hash of the action list; Permit2 forwards it verbatim to the auction.

Three ways to publish, picked from the pinned adapter's flavor and the
``zerox`` config section:

* **Gasless API** (MetaTxn adapter, ``post: true``, a key in
  ``api_key_env``): ``GET /gasless/quote`` with the auction as ``taker`` and
  the auction's receiver as ``recipient``; the answer's ``trade.eip712``
  message is checked against the lot (spender = the pinned Settler, Permit2
  domain, token pair, amount within the available amount, ``minAmountOut``
  at or above the live curve quote — a quote below it is skipped, since
  ``check_order`` would reject it — deadline inside the lot window), its
  ``trade.hash`` is compared with the adapter's digest, the order is
  validated on-chain and ``POST /gasless/submit`` sends the trade with a
  ``signatureType: 5`` (Raw) ``signatureBytes`` signature. Endpoints, the
  parameter and body shapes are from the 0x docs (``/api-reference/evm-ap-is/
  gasless/getquote``, ``.../submit``, ``/evm/gasless-api/introduction``);
  whether the backend accepts a plain ERC-1271 contract as ``taker`` and
  relays the 276-byte signature is unverified (no key was available; the
  quote endpoint documents a ``SMART_WALLET_NOT_SUPPORTED`` error).
* **Self-relay** (MetaTxn adapter without the API, or when a quote cannot
  be signed): the publisher authors the action list itself —
  ``METATXN_TRANSFER_FROM(settler, permit)`` then ``BASIC(token, 1e6 ppm,
  pool, offset, exchange(i, j, 0, 0))`` through the Curve pool configured for
  the token in ``zerox.routes`` — hashes it, builds the order through
  ``order_for``, validates it, and per ``zerox.relay`` does nothing
  (``none``), logs the ``executeMetaTxn`` calldata (``log``) or, in keeper
  mode, simulates and sends it from the keeper account (``self-actions``;
  relaying is permissionless but the signer may not relay its own order, so
  the keeper EOA, never the auction, is the sender). The Settler pays the
  recipient its whole buy-token balance, so the pool's surplus above the
  floor goes to the FeeCollector and the keeper only pays gas.
* **Standing ask** (Intent adapter): the ``order_for`` order without an
  action hash, validated and logged for 0x's allow-listed solvers, who route
  and relay it themselves (``getSolvers()``); the Gasless API serves
  ``settler_metatransaction`` trades only, so there is no feed to post to.

Orders are fill-or-kill and Permit2 burns the nonce, so every publish uses a
fresh random nonce; the record expires with the permit deadline (the keeper
TTL, never past the lot end).
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
    ZEROX_ORDER_ABI,
    AdapterConfig,
    Auction,
    LotView,
    ZeroExDeployerRegistry,
    ZeroExSettlerAdapterContract,
    ZeroExSettlerContract,
)
from keepers.auction_keeper.config import CurveRoute, NetworkConfig, ZeroExSettings
from keepers.auction_keeper.rails import AdapterPublisher, PublishResult
from keepers.auction_keeper.rails.common import ApiError, JsonClient, hex_bytes, random_salt

log = logging.getLogger(__name__)

# EIP-712 types (adapters/zerox/settler.vy): the Settler's witnesses and
# Permit2's witness permit, sub-types in alphabetical order.
SLIPPAGE_AND_ACTIONS_TYPE = (
    "SlippageAndActions(address recipient,address buyToken,uint256 minAmountOut,bytes[] actions)"
)
SLIPPAGE_TYPE = "Slippage(address recipient,address buyToken,uint256 minAmountOut)"
TOKEN_PERMISSIONS_TYPE = "TokenPermissions(address token,uint256 amount)"
PERMIT_WITNESS_STUB = (
    "PermitWitnessTransferFrom(TokenPermissions permitted,address spender,uint256 nonce,"
    "uint256 deadline,"
)
SLIPPAGE_AND_ACTIONS_TYPE_HASH = keccak(text=SLIPPAGE_AND_ACTIONS_TYPE)
SLIPPAGE_TYPE_HASH = keccak(text=SLIPPAGE_TYPE)
TOKEN_PERMISSIONS_TYPE_HASH = keccak(text=TOKEN_PERMISSIONS_TYPE)
PERMIT_WITNESS_METATXN_TYPE_HASH = keccak(
    text=PERMIT_WITNESS_STUB
    + "SlippageAndActions slippageAndActions)"
    + SLIPPAGE_AND_ACTIONS_TYPE
    + TOKEN_PERMISSIONS_TYPE
)
PERMIT_WITNESS_INTENT_TYPE_HASH = keccak(
    text=PERMIT_WITNESS_STUB + "Slippage slippage)" + SLIPPAGE_TYPE + TOKEN_PERMISSIONS_TYPE
)

# Gasless API v2 (docs.0x.org/api-reference/evm-ap-is/gasless/{getquote,submit}).
QUOTE_PATH = "/gasless/quote"
SUBMIT_PATH = "/gasless/submit"
API_VERSION = "v2"
TRADE_TYPE = "settler_metatransaction"
PRIMARY_TYPE = "PermitWitnessTransferFrom"
# submit's signature discriminator: 5 = Raw, `signatureBytes` only ("for
# smart contract wallets (e.g., ERC-1271) that return opaque signature bytes").
RAW_SIGNATURE_TYPE = 5

# ISettlerActions selectors (0x-settler src/ISettlerActions.sol).
METATXN_TRANSFER_FROM = keccak(
    text="METATXN_TRANSFER_FROM(address,((address,uint256),uint256,uint256))"
)[:4]
BASIC = keccak(text="BASIC(address,uint256,address,uint256,bytes)")[:4]
# BASIC's proportion of the Settler's sell-token balance, in ppm (its `ppm`
# parameter): sell everything the first action pulled.
BASIS = 1_000_000
# Curve pool `exchange(int128 i, int128 j, uint256 dx, uint256 min_dy)`; BASIC
# patches the sell amount into the call data at `offset`, the third word (dx).
CURVE_EXCHANGE = keccak(text="exchange(int128,int128,uint256,uint256)")[:4]
EXCHANGE_AMOUNT_OFFSET = 4 + 2 * 32
ZID = bytes(32)


@dataclass(frozen=True)
class ZeroExOrder:
    """The signed order: the Permit2 permit, the Settler's AllowedSlippage and
    the action-list hash (zero for a SettlerIntent)."""

    token: str
    amount: int
    nonce: int
    deadline: int
    recipient: str
    buy_token: str
    min_amount_out: int
    actions_hash: bytes = bytes(32)

    @classmethod
    def from_tuple(cls, values: tuple) -> ZeroExOrder:
        (token, amount, nonce, deadline), (recipient, buy_token, min_out), actions_hash = values
        return cls(
            to_checksum_address(token),
            amount,
            nonce,
            deadline,
            to_checksum_address(recipient),
            to_checksum_address(buy_token),
            min_out,
            bytes(actions_hash),
        )

    @classmethod
    def from_message(cls, message: dict[str, Any], actions_hash: bytes) -> ZeroExOrder:
        """The quote's ``trade.eip712.message`` (amounts and the nonce as decimal strings)."""
        permitted = message["permitted"]
        slippage = message["slippageAndActions"]
        return cls(
            to_checksum_address(permitted["token"]),
            int(permitted["amount"]),
            int(message["nonce"]),
            int(message["deadline"]),
            to_checksum_address(slippage["recipient"]),
            to_checksum_address(slippage["buyToken"]),
            int(slippage["minAmountOut"]),
            actions_hash,
        )

    @property
    def permit(self) -> tuple[str, int, int, int]:
        return (self.token, self.amount, self.nonce, self.deadline)

    @property
    def slippage(self) -> tuple[str, str, int]:
        return (self.recipient, self.buy_token, self.min_amount_out)

    def as_tuple(self) -> tuple:
        return (self.permit, self.slippage, self.actions_hash)

    def with_deadline(self, deadline: int) -> ZeroExOrder:
        return replace(self, deadline=deadline)

    def encode(self) -> bytes:
        return encode([ZEROX_ORDER_ABI], [self.as_tuple()])

    def signature(self, adapter: str) -> bytes:
        return bytes.fromhex(adapter[2:]) + self.encode()

    def witness_hash(self, intent: bool) -> bytes:
        """SettlerIntent's _hashSlippage or SettlerMetaTxn's _hashActionsAndSlippage."""
        if intent:
            return keccak(
                encode(
                    ["bytes32", "address", "address", "uint256"],
                    [SLIPPAGE_TYPE_HASH, *self.slippage],
                )
            )
        return keccak(
            encode(
                ["bytes32", "address", "address", "uint256", "bytes32"],
                [SLIPPAGE_AND_ACTIONS_TYPE_HASH, *self.slippage, self.actions_hash],
            )
        )

    def permit_digest(self, spender: str, domain_separator: bytes, intent: bool) -> bytes:
        """The Permit2 digest the auction's isValidSignature receives for this order."""
        permitted_hash = keccak(
            encode(
                ["bytes32", "address", "uint256"],
                [TOKEN_PERMISSIONS_TYPE_HASH, self.token, self.amount],
            )
        )
        type_hash = PERMIT_WITNESS_INTENT_TYPE_HASH if intent else PERMIT_WITNESS_METATXN_TYPE_HASH
        struct_hash = keccak(
            encode(
                ["bytes32", "bytes32", "address", "uint256", "uint256", "bytes32"],
                [
                    type_hash,
                    permitted_hash,
                    spender,
                    self.nonce,
                    self.deadline,
                    self.witness_hash(intent),
                ],
            )
        )
        return keccak(b"\x19\x01" + domain_separator + struct_hash)

    def as_json(self) -> dict[str, Any]:
        return {
            "permit": {
                "token": self.token,
                "amount": str(self.amount),
                "nonce": str(self.nonce),
                "deadline": self.deadline,
            },
            "slippage": {
                "recipient": self.recipient,
                "buyToken": self.buy_token,
                "minAmountOut": str(self.min_amount_out),
            },
            "actionsHash": hex_bytes(self.actions_hash),
        }


def hash_actions(actions: list[bytes]) -> bytes:
    """SettlerMetaTxn._hashArrayOfBytes: keccak of the packed keccaks of every action."""
    return keccak(b"".join(keccak(action) for action in actions))


def transfer_from_action(settler: str, permit: tuple[str, int, int, int]) -> bytes:
    """``METATXN_TRANSFER_FROM(recipient = the Settler, permit)``: the first
    action of a MetaTxn, pulling the permitted amount into the Settler."""
    token, amount, nonce, deadline = permit
    return METATXN_TRANSFER_FROM + encode(
        ["address", "((address,uint256),uint256,uint256)"],
        [settler, ((token, amount), nonce, deadline)],
    )


def curve_basic_action(route: CurveRoute) -> bytes:
    """``BASIC``: sell the Settler's whole balance of the route's token to the
    Curve pool; ``min_dy`` is 0 because the Settler's slippage check is the floor."""
    data = CURVE_EXCHANGE + encode(
        ["int128", "int128", "uint256", "uint256"], [route.i, route.j, 0, 0]
    )
    return BASIC + encode(
        ["address", "uint256", "address", "uint256", "bytes"],
        [route.token, BASIS, route.pool, EXCHANGE_AMOUNT_OFFSET, data],
    )


class ZeroExApi:
    def __init__(self, http: Any, settings: ZeroExSettings):
        self.http = http
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.api_key)

    def headers(self) -> dict[str, str]:
        return {"0x-api-key": self.settings.api_key or "", "0x-version": API_VERSION}

    def _url(self, path: str) -> str:
        return f"{self.settings.api_url.rstrip('/')}{path}"

    def quote(self, params: dict[str, str]) -> dict[str, Any]:
        """``GET /gasless/quote``; errors are HTTP 4xx/5xx ``{name, message, data}``."""
        payload = self.http.request(
            "GET", self._url(QUOTE_PATH), params=params, headers=self.headers()
        )
        if not isinstance(payload, dict):
            raise ApiError(200, payload, self._url(QUOTE_PATH))
        return payload

    def submit(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /gasless/submit`` with ``{chainId, trade{type, eip712, signature}}``."""
        payload = self.http.request(
            "POST", self._url(SUBMIT_PATH), json=body, headers=self.headers()
        )
        return payload if isinstance(payload, dict) else {"raw": payload}


# The adapter's typed reason once the 0x registry no longer lists its Settler,
# and the registry's own reverts the adapter lets bubble when the feature has
# no instance left (fail closed): both terminal for the adapter.
RETIRED = "SettlerRetired"
REGISTRY_REVERTS = ("NoInstance", "ERC721NonexistentToken")


class ZeroExSettlerPublisher(AdapterPublisher):
    version = "ZeroExSettlerAdapter"

    def __init__(
        self,
        client: ChainClient,
        adapter: AdapterConfig,
        network: NetworkConfig,
        *,
        http: Any = None,
    ):
        super().__init__(client, adapter, network)
        self.settings = network.zerox
        self.contract = ZeroExSettlerAdapterContract(client, adapter.adapter)
        self.settler = self.contract.settler()
        self.permit2 = self.contract.permit2()
        # The 0x deployer/registry the adapter pins: its Settler must stay the
        # feature's current or previous instance, else the adapter answers
        # OrderNotValid(SettlerRetired) and this publisher stops for good.
        self.registry = ZeroExDeployerRegistry(client, self.contract.deployer())
        self.feature = self.contract.feature()
        self.retired = False
        self.domain_separator = self.contract.domain_separator()
        pinned_intent = self.contract.intent()
        self.intent = (
            pinned_intent if self.settings.flavor is None else self.settings.flavor == "intent"
        )
        self.api = ZeroExApi(http or JsonClient(), self.settings)
        self.chain_id = self.settings.chain_id or client.chain_id
        self.routes = {route.token: route for route in self.settings.routes}
        if self.intent != pinned_intent:
            log.warning(
                "ZeroExSettlerAdapter %s: zerox.flavor overrides the pinned flavor (intent=%s); "
                "digests under the other witness fail on-chain",
                adapter.adapter,
                pinned_intent,
            )
        if adapter.executor != self.permit2:
            log.warning(
                "ZeroExSettlerAdapter %s: registry executor %s differs from Permit2 %s",
                adapter.adapter,
                adapter.executor,
                self.permit2,
            )
        if self.settings.post and not self.intent and not self.api.configured:
            log.warning(
                "zerox: no Gasless API key in %s; MetaTxn orders go the self-relay way (relay: %s)",
                self.settings.api_key_env,
                self.settings.relay,
            )

    @classmethod
    def enabled(cls, network: NetworkConfig) -> bool:
        return network.zerox.enabled

    @property
    def feed_ready(self) -> bool:
        return self.settings.post and self.api.configured and not self.intent

    def publish(
        self, auction: Auction, lot: LotView, *, dry_run: bool = False
    ) -> PublishResult | None:
        if not lot.active or self.retired:
            return None
        if not self.registry.lists(self.feature, self.settler):
            self.retire("the 0x registry no longer lists it (removed, or two newer deployments)")
            return None
        if self.intent:
            return self.publish_standing(auction, lot, dry_run=dry_run)
        if self.feed_ready:
            result = self.publish_quote(auction, lot, dry_run=dry_run)
            if result is not None or self.settings.relay == "none":
                return result
        return self.publish_self_relay(auction, lot, dry_run=dry_run)

    # 0x's pause switch

    def retire(self, reason: str) -> None:
        """Stop publishing for this adapter: the pinned Settler is retired in
        the 0x registry, a terminal state (a new adapter deployment follows
        the registry's current Settler). Logged once."""
        if not self.retired:
            log.error(
                "ZeroExSettlerAdapter %s: Settler %s is retired (%s); no more orders for this "
                "adapter until one pinned to the registry's current Settler is deployed",
                self.address,
                self.settler,
                reason,
            )
        self.retired = True

    def router_rejected(self, lot: LotView, reason: str, what: str = "order") -> None:
        """An isValidSignature rejection: SettlerRetired is terminal for the
        adapter (0x retired the Settler between the registry check and the
        validation), anything else (an economic reject, a stale quote) is
        this lot's and this poll's."""
        if RETIRED in reason or reason.startswith(REGISTRY_REVERTS):
            self.retire(f"the auction router answered {reason}")
        else:
            log.warning("%s: 0x %s rejected by the auction router: %s", lot.token, what, reason)

    # Gasless API

    def quote_params(self, auction: Auction, lot: LotView) -> dict[str, str]:
        params = {
            "chainId": str(self.chain_id),
            "sellToken": lot.token,
            "buyToken": lot.want,
            "sellAmount": str(lot.available),
            "taker": auction.address,
            "recipient": lot.receiver,
        }
        if self.settings.slippage_ppm is not None:
            params["slippagePpm"] = str(self.settings.slippage_ppm)
        return params

    def check_quote(
        self,
        auction: Auction,
        lot: LotView,
        order: ZeroExOrder,
        eip712: dict[str, Any],
        actions: list[bytes],
    ) -> str | None:
        """Why the quote's trade cannot be signed for this lot, or None."""
        if eip712.get("primaryType") != PRIMARY_TYPE:
            return f"primaryType {eip712.get('primaryType')} is not {PRIMARY_TYPE}"
        domain = eip712.get("domain") or {}
        verifying = domain.get("verifyingContract")
        if not verifying or to_checksum_address(verifying) != self.permit2:
            return f"verifyingContract {verifying} is not the adapter's Permit2 {self.permit2}"
        if str(domain.get("chainId")) != str(self.chain_id):
            return f"domain chainId {domain.get('chainId')} is not {self.chain_id}"
        spender = eip712.get("message", {}).get("spender")
        if not spender or to_checksum_address(spender) != self.settler:
            return f"spender {spender} is not the pinned Settler {self.settler}"
        if order.token != lot.token or order.buy_token != lot.want:
            return f"tokens {order.token} -> {order.buy_token} are not the lot pair"
        if order.recipient != lot.receiver:
            return f"recipient {order.recipient} is not the auction's receiver {lot.receiver}"
        if order.amount == 0 or order.amount > lot.available:
            return f"sell amount {order.amount} exceeds the available {lot.available}"
        needed = (
            lot.amount_needed
            if order.amount == lot.available
            else auction.get_amount_needed(lot.token, order.amount)
        )
        if order.min_amount_out < needed:
            return f"0x quote minAmountOut {order.min_amount_out} is below the curve quote {needed}"
        if order.deadline <= lot.timestamp:
            return "quote already expired"
        if order.deadline > lot.end:
            return f"deadline {order.deadline} is past the lot end {lot.end}"
        if not actions or actions[0][:4] != METATXN_TRANSFER_FROM:
            return "first action is not METATXN_TRANSFER_FROM"
        return None

    def publish_quote(
        self, auction: Auction, lot: LotView, *, dry_run: bool
    ) -> PublishResult | None:
        try:
            quote = self.api.quote(self.quote_params(auction, lot))
        except ApiError as exc:
            log.warning("%s: 0x gasless quote failed: %s", lot.token, exc)
            return None
        if not quote.get("liquidityAvailable", True):
            log.info("%s: 0x reports no liquidity (zid %s)", lot.token, quote.get("zid"))
            return None
        trade = quote.get("trade") or {}
        eip712 = trade.get("eip712") or {}
        message = eip712.get("message")
        if trade.get("type") != TRADE_TYPE or not message:
            log.warning("%s: 0x quote carries no %s trade: %s", lot.token, TRADE_TYPE, quote)
            return None
        try:
            actions = [bytes.fromhex(a[2:]) for a in message["slippageAndActions"]["actions"]]
            order = ZeroExOrder.from_message(message, hash_actions(actions))
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("%s: malformed trade.eip712.message %s: %s", lot.token, message, exc)
            return None
        reason = self.check_quote(auction, lot, order, eip712, actions)
        if reason is not None:
            log.info("%s: 0x quote %s skipped: %s", lot.token, quote.get("zid"), reason)
            return None
        digest = order.permit_digest(self.settler, self.domain_separator, False)
        trade_hash = trade.get("hash")
        if trade_hash and bytes.fromhex(trade_hash[2:]) != digest:
            log.warning(
                "%s: 0x trade hash %s differs from the adapter's digest %s; skipped",
                lot.token,
                trade_hash,
                hex_bytes(digest),
            )
            return None
        signature = order.signature(self.address)
        reason = self.validate_signature(auction, digest, signature)
        if reason is not None:
            self.router_rejected(lot, reason)
            return None
        if not self.ensure_allowance(auction, lot, self.permit2, dry_run=dry_run):
            return None
        body = {
            "chainId": self.chain_id,
            "trade": {
                "type": TRADE_TYPE,
                "eip712": eip712,
                "signature": {
                    "signatureType": RAW_SIGNATURE_TYPE,
                    "signatureBytes": hex_bytes(signature),
                },
            },
        }
        details: dict[str, Any] = {
            "mode": "gasless-api",
            "tradeHash": hex_bytes(digest),
            "zid": quote.get("zid"),
            "order": order.as_json(),
            "actions": [hex_bytes(a) for a in actions],
            "signature": hex_bytes(signature),
            "buyAmount": quote.get("buyAmount"),
            "minBuyAmount": quote.get("minBuyAmount"),
        }
        summary = (
            f"{lot.token} sells {order.amount} for >= {order.min_amount_out} "
            f"(curve {lot.amount_needed}), deadline {order.deadline}"
        )
        if dry_run:
            log.info(
                "dry run: would submit 0x gasless trade %s (%s)", details["tradeHash"], summary
            )
        else:
            try:
                details["response"] = self.api.submit(body)
            except ApiError as exc:
                log.warning("%s: 0x gasless submit rejected: %s", lot.token, exc)
                return None
            log.info(
                "submitted 0x gasless trade %s: %s; response %s",
                details["tradeHash"],
                summary,
                details["response"],
            )
        uid = (details.get("response") or {}).get("tradeHash") or details["tradeHash"]
        return PublishResult(
            uid=str(uid),
            valid_to=order.deadline,
            sell_amount=order.amount,
            buy_amount=order.min_amount_out,
            details=details,
        )

    # Self-relay (MetaTxn without the API)

    def build_self_relay(
        self, auction: Auction, lot: LotView, route: CurveRoute, valid_to: int
    ) -> tuple[ZeroExOrder, list[bytes]] | None:
        """The order and action list of a self-authored Curve-pool fill, or None."""
        permit = (lot.token, lot.available, random_salt(256), valid_to)
        actions = [transfer_from_action(self.settler, permit), curve_basic_action(route)]
        try:
            built, _ = self.contract.order_for(
                auction.address, lot.token, permit[2], 0, valid_to, hash_actions(actions)
            )
        except CallReverted as exc:
            log.warning("%s: order_for rejected the lot: %s", lot.token, exc)
            return None
        order = ZeroExOrder.from_tuple(built)
        if order.permit != permit:
            # The lot moved between the snapshot and the build: the signed
            # permit would not be the one the first action carries.
            log.info(
                "%s: lot changed while building (%s != %s); retry next poll",
                lot.token,
                order.permit,
                permit,
            )
            return None
        return order, actions

    def publish_self_relay(
        self, auction: Auction, lot: LotView, *, dry_run: bool
    ) -> PublishResult | None:
        relay = self.settings.relay
        if relay == "none":
            log.info(
                "%s: no 0x feed for this lot and zerox.relay is none; nothing published", lot.token
            )
            return None
        route = self.routes.get(lot.token)
        if route is None:
            log.info("%s: no Curve route in zerox.routes; nothing to relay", lot.token)
            return None
        valid_to = self.valid_to(lot)
        if valid_to <= lot.timestamp:
            log.info("%s: lot window closes before an order could be valid", lot.token)
            return None
        built = self.build_self_relay(auction, lot, route, valid_to)
        if built is None:
            return None
        order, actions = built
        signature = order.signature(self.address)
        digest = order.permit_digest(self.settler, self.domain_separator, False)
        reason = self.validate_signature(auction, digest, signature)
        if reason is not None:
            self.router_rejected(lot, reason)
            return None
        if not self.ensure_allowance(auction, lot, self.permit2, dry_run=dry_run):
            return None
        calldata = ZeroExSettlerContract(self.client, self.settler).execute_meta_txn_calldata(
            order.slippage, actions, ZID, auction.address, signature
        )
        details: dict[str, Any] = {
            "mode": "self-relay",
            "relay": relay,
            "tradeHash": hex_bytes(digest),
            "order": order.as_json(),
            "actions": [hex_bytes(a) for a in actions],
            "signature": hex_bytes(signature),
            "settler": self.settler,
            "calldata": hex_bytes(calldata),
        }
        summary = (
            f"{lot.token} sells {order.amount} for >= {order.min_amount_out} "
            f"through Curve pool {route.pool}, deadline {order.deadline}"
        )
        if dry_run:
            log.info("dry run: would relay 0x MetaTxn %s (%s)", details["tradeHash"], summary)
        elif relay == "self-actions" and self.network.onchain:
            if not self.relay_now(lot, calldata, details, summary):
                return None
        else:
            log.info(
                "0x MetaTxn %s built, not relayed (%s): send to %s from any account but the "
                "auction: %s",
                details["tradeHash"],
                summary,
                self.settler,
                details["calldata"],
            )
        return PublishResult(
            uid=details["tradeHash"],
            valid_to=order.deadline,
            sell_amount=order.amount,
            buy_amount=order.min_amount_out,
            details=details,
        )

    def relay_now(
        self, lot: LotView, calldata: bytes, details: dict[str, Any], summary: str
    ) -> bool:
        """Simulate then send ``executeMetaTxn`` from the keeper account."""
        sender = self.client.sender
        if sender is None:
            log.error("%s: keeper mode without a sending client; 0x order not relayed", lot.token)
            return False
        if sender == lot.auction:
            log.error("%s: the auction cannot relay its own 0x order", lot.token)
            return False
        try:
            self.client.call(self.settler, calldata, sender=sender)
            receipt = self.client.send(self.settler, calldata)
        except CallReverted as exc:
            log.warning("%s: executeMetaTxn reverted (%s): %s", lot.token, summary, exc)
            return False
        details["txHash"] = receipt.tx_hash
        log.info("relayed 0x MetaTxn %s in %s: %s", details["tradeHash"], receipt.tx_hash, summary)
        return True

    # Standing ask (Intent)

    def publish_standing(
        self, auction: Auction, lot: LotView, *, dry_run: bool
    ) -> PublishResult | None:
        valid_to = self.valid_to(lot)
        if valid_to <= lot.timestamp:
            log.info("%s: lot window closes before an order could be valid", lot.token)
            return None
        try:
            built, _ = self.contract.order_for(
                auction.address, lot.token, random_salt(256), 0, valid_to, bytes(32)
            )
        except CallReverted as exc:
            log.warning("%s: order_for rejected the lot: %s", lot.token, exc)
            return None
        order = ZeroExOrder.from_tuple(built)
        signature = order.signature(self.address)
        digest = order.permit_digest(self.settler, self.domain_separator, True)
        reason = self.validate_signature(auction, digest, signature)
        if reason is not None:
            self.router_rejected(lot, reason, "intent order")
            return None
        if not self.ensure_allowance(auction, lot, self.permit2, dry_run=dry_run):
            return None
        details: dict[str, Any] = {
            "mode": "intent",
            "tradeHash": hex_bytes(digest),
            "order": order.as_json(),
            "signature": hex_bytes(signature),
            "settler": self.settler,
        }
        summary = f"{lot.token} sells {order.amount} for >= {order.min_amount_out}, deadline {order.deadline}"
        if dry_run:
            log.info(
                "dry run: would publish 0x intent order %s (%s)", details["tradeHash"], summary
            )
        else:
            log.info(
                "0x SettlerIntent order %s built for solvers, not posted (%s): "
                "executeMetaTxn(slippage=%s, actions=<solver's>, zid, msgSender=%s, sig=%s) on %s",
                details["tradeHash"],
                summary,
                order.slippage,
                auction.address,
                details["signature"],
                self.settler,
            )
        return PublishResult(
            uid=details["tradeHash"],
            valid_to=order.deadline,
            sell_amount=order.amount,
            buy_amount=order.min_amount_out,
            details=details,
        )


__all__ = [
    "BASIC",
    "BASIS",
    "METATXN_TRANSFER_FROM",
    "QUOTE_PATH",
    "RAW_SIGNATURE_TYPE",
    "SUBMIT_PATH",
    "TRADE_TYPE",
    "ZeroExApi",
    "ZeroExOrder",
    "ZeroExSettlerPublisher",
    "curve_basic_action",
    "hash_actions",
    "transfer_from_action",
]
