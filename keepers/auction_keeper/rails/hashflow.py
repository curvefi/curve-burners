"""Hashflow RFQ-m publisher: market-maker quotes with the auction as trader,
signed through the auction's ERC-1271 router (``HashflowAdapter``) and
relayed to ``HashflowRouter.tradeRFQM`` by the keeper.

Every poll (``hashflow.post: true`` with a credential): ``POST
/taker/v3/rfq`` asks for a quote with ``trader = auction``, ``baseToken`` =
the lot, ``quoteToken`` = the auction's ``want()``, ``baseTokenAmount`` =
everything available (or ``hashflow.chunk``), ``rfqType: 1`` (RFQ-m; the
taker SDK's ``QuoteData.rfqType`` is ``0 | 1``, the request field is
ASSUMED: the public taker docs describe RFQ-t only). The answer's
``quoteData`` + ``signature`` (the maker's EIP-191 signature) is checked
against the lot: this chain, the auction as trader, the lot pair, the base
amount as requested and within the available amount, ``quoteTokenAmount``
at or above the curve quote for it (below: skipped, ``check_order`` would
reject it), ``quoteExpiry`` inside the lot window, and the maker's
signature recovering to the pool's enabled signer (``signerConfiguration``;
an RFQ-t quote, hashed differently, fails here). The trader signature is
``adapter ++ abi_encode(quote)``; the pair is validated through the
auction's ``isValidSignature`` (the router itself hides typed reasons behind
"Invalid signer."), the router's allowance is checked, and then:

* keeper mode: the keeper sends ``tradeRFQM(quote)`` itself — there is no
  public endpoint to hand a maker-signed RFQ-m quote back to Hashflow, and
  the router lets anyone relay (``IHashflowRouter``: "the 3rd party can be
  the market maker itself") — and calls the burner's permissionless
  ``push_target()`` right after, since the pool pays the trader (the
  burner), not the FeeCollector;
* offline mode: the calldata is logged for any relayer, and a ``want``
  balance on the burner is reported for someone to push.

Target already sitting on the burner (an earlier fill, a relayer's) is
pushed at the start of every poll in keeper mode. No standing-ask mode: a
quote lives seconds to minutes, every fill is one exact ``txid``, nothing
settles without a maker's signature. Without a credential the trader-side
template from ``order_for`` is logged and nothing is requested.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from eth_abi import encode
from eth_abi.exceptions import DecodingError
from eth_keys import keys
from eth_keys.exceptions import BadSignature
from eth_utils import keccak, to_checksum_address

from keepers.auction_keeper.chain.abi import CallReverted
from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.chain.contracts import (
    HASHFLOW_QUOTE_ABI,
    AdapterConfig,
    Auction,
    Erc20,
    HashflowAdapterContract,
    HashflowPool,
    HashflowRouter,
    LotView,
)
from keepers.auction_keeper.config import HashflowSettings, NetworkConfig
from keepers.auction_keeper.rails import AdapterPublisher, PublishResult
from keepers.auction_keeper.rails.common import ApiError, JsonClient, hex_bytes

log = logging.getLogger(__name__)

# HashflowRouter.QUOTE_TYPEHASH (adapters/hashflow/hashflow.vy).
QUOTE_TYPE_HASH = keccak(
    text=(
        "Quote(bytes32 txid,address trader,address pool,address externalAccount,"
        "address baseToken,address quoteToken,uint256 baseTokenAmount,"
        "uint256 quoteTokenAmount,uint256 quoteExpiry)"
    )
)
EIP191_PREFIX = b"\x19Ethereum Signed Message:\n32"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
RFQ_PATH = "/taker/v3/rfq"
# RFQ-m in the taker SDK's QuoteData.rfqType (0 = RFQ-t, 1 = RFQ-m). Sent
# with every rfq item; ASSUMED as a request field (undocumented).
RFQ_TYPE_RFQM = 1
MAKER_SIGNATURE_LEN = 65


@dataclass(frozen=True)
class HashflowQuote:
    """The trader-signed RFQ-m quote: the nine EIP-712 fields in type order."""

    txid: bytes
    trader: str
    pool: str
    external_account: str
    base_token: str
    quote_token: str
    base_amount: int
    quote_amount: int
    expiry: int

    @classmethod
    def from_tuple(cls, values: tuple) -> HashflowQuote:
        txid, trader, pool, external, base, quote, base_amount, quote_amount, expiry = values
        return cls(
            bytes(txid),
            to_checksum_address(trader),
            to_checksum_address(pool),
            to_checksum_address(external),
            to_checksum_address(base),
            to_checksum_address(quote),
            base_amount,
            quote_amount,
            expiry,
        )

    @classmethod
    def from_quote_data(cls, data: dict[str, Any]) -> HashflowQuote:
        """The API's ``quoteData`` (amounts as decimal strings, txid hex,
        ``externalAccount`` null for a pool paying from its own balance)."""
        return cls(
            _bytes32(data["txid"]),
            to_checksum_address(data["trader"]),
            to_checksum_address(data["pool"]),
            to_checksum_address(data.get("externalAccount") or ZERO_ADDRESS),
            to_checksum_address(data["baseToken"]),
            to_checksum_address(data["quoteToken"]),
            int(data["baseTokenAmount"]),
            int(data["quoteTokenAmount"]),
            int(data["quoteExpiry"]),
        )

    def as_tuple(self) -> tuple:
        return (
            self.txid,
            self.trader,
            self.pool,
            self.external_account,
            self.base_token,
            self.quote_token,
            self.base_amount,
            self.quote_amount,
            self.expiry,
        )

    def encode(self) -> bytes:
        return encode([HASHFLOW_QUOTE_ABI], [self.as_tuple()])

    def signature(self, adapter: str) -> bytes:
        """``adapter ++ abi_encode(quote)``: the takerSignature the router forwards."""
        return bytes.fromhex(adapter[2:]) + self.encode()

    def struct_hash(self) -> bytes:
        return keccak(
            encode(
                ["bytes32", "bytes32", "address", "address", "address", "address", "address"]
                + ["uint256"] * 3,
                [QUOTE_TYPE_HASH, *self.as_tuple()],
            )
        )

    def digest(self, domain_separator: bytes) -> bytes:
        """The EIP-712 digest the router asks the trader's isValidSignature about."""
        return keccak(b"\x19\x01" + domain_separator + self.struct_hash())

    def maker_digest(self, chain_id: int) -> bytes:
        """HashflowPool._hashQuoteRFQM: EIP-191 over the packed fields in the pool's order."""
        packed = b"".join(
            bytes.fromhex(address[2:])
            for address in (
                self.pool,
                self.external_account,
                self.trader,
                self.base_token,
                self.quote_token,
            )
        )
        packed += b"".join(
            value.to_bytes(32, "big")
            for value in (self.base_amount, self.quote_amount, self.expiry)
        )
        packed += self.txid + chain_id.to_bytes(32, "big")
        return keccak(EIP191_PREFIX + keccak(packed))

    def maker_signer(self, maker_signature: bytes, chain_id: int) -> str | None:
        """The address a 65-byte (r, s, v) maker signature recovers to; None if malformed."""
        if len(maker_signature) != MAKER_SIGNATURE_LEN:
            return None
        v = maker_signature[64]
        if v >= 27:
            v -= 27
        if v not in (0, 1):
            return None
        try:
            signature = keys.Signature(maker_signature[:64] + bytes([v]))
            return signature.recover_public_key_from_msg_hash(
                self.maker_digest(chain_id)
            ).to_checksum_address()
        except BadSignature:
            return None

    def calldata_tuple(self, taker_signature: bytes, maker_signature: bytes) -> tuple:
        """The quote as ``tradeRFQM`` takes it (RFQMQuote: calldata order plus signatures)."""
        return (
            self.pool,
            self.external_account,
            self.trader,
            self.base_token,
            self.quote_token,
            self.base_amount,
            self.quote_amount,
            self.expiry,
            self.txid,
            taker_signature,
            maker_signature,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "txid": hex_bytes(self.txid),
            "trader": self.trader,
            "pool": self.pool,
            "externalAccount": self.external_account,
            "baseToken": self.base_token,
            "quoteToken": self.quote_token,
            "baseTokenAmount": str(self.base_amount),
            "quoteTokenAmount": str(self.quote_amount),
            "quoteExpiry": self.expiry,
        }


def _bytes32(value: str) -> bytes:
    data = bytes.fromhex(value[2:] if value.startswith("0x") else value)
    if len(data) != 32:
        raise ValueError(f"not a bytes32: {value}")
    return data


def _hex_to_bytes(value: str) -> bytes:
    return bytes.fromhex(value[2:] if value.startswith("0x") else value)


class HashflowApi:
    """``POST {api_url}/taker/v3/rfq`` with the credential in ``Authorization``
    (docs.hashflow.com, taker "Getting Started - API v3"); the answer is
    ``{status, error?, rfqId, internalRfqIds?, quotes[{quoteData, signature,
    ...}]}`` and anything but ``status: "success"`` with a quote is an
    ApiError."""

    def __init__(self, http: Any, settings: HashflowSettings):
        self.http = http
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.api_key)

    def headers(self) -> dict[str, str]:
        return {"Authorization": self.settings.api_key or "", "Content-Type": "application/json"}

    def rfq(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.settings.api_url.rstrip('/')}{RFQ_PATH}"
        payload = self.http.request("POST", url, json=body, headers=self.headers())
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise ApiError(200, payload, url)
        quotes = payload.get("quotes") or []
        if not quotes or not isinstance(quotes[0], dict):
            raise ApiError(200, payload, url)
        return payload


class HashflowPublisher(AdapterPublisher):
    version = "HashflowAdapter"

    def __init__(
        self,
        client: ChainClient,
        adapter: AdapterConfig,
        network: NetworkConfig,
        *,
        http: Any = None,
    ):
        super().__init__(client, adapter, network)
        self.settings = network.hashflow
        self.contract = HashflowAdapterContract(client, adapter.adapter)
        self.router = HashflowRouter(client, self.contract.router())
        self.domain_separator = self.contract.domain_separator()
        self.api = HashflowApi(http or JsonClient(), self.settings)
        if adapter.executor != self.router.address:
            log.warning(
                "HashflowAdapter %s: registry executor %s differs from the router %s",
                adapter.adapter,
                adapter.executor,
                self.router.address,
            )
        if self.settings.post and not self.api.configured:
            log.warning(
                "hashflow: no credential in %s; every taker call needs one, "
                "logging quote-request templates only",
                self.settings.api_key_env,
            )

    @classmethod
    def enabled(cls, network: NetworkConfig) -> bool:
        return network.hashflow.enabled

    @property
    def feed_ready(self) -> bool:
        return self.settings.post and self.api.configured

    def publish(
        self, auction: Auction, lot: LotView, *, dry_run: bool = False
    ) -> PublishResult | None:
        if not lot.active:
            return None
        # Proceeds of earlier fills sit on the burner until pushed.
        self.push_target(auction, lot, dry_run=dry_run)
        if not self.feed_ready:
            return self.log_template(auction, lot)
        return self.publish_quote(auction, lot, dry_run=dry_run)

    # Proceeds

    def push_target(self, auction: Auction, lot: LotView, *, dry_run: bool) -> int:
        """Forward ``want`` held by the burner to the FeeCollector (keeper mode);
        offline, report it. Returns the amount moved (or waiting)."""
        held = Erc20(self.client, lot.want).balance_of(auction.address)
        if held == 0:
            return 0
        if not self.network.onchain:
            log.warning(
                "%s holds %s of %s (Hashflow fills pay the trader); run push_target() on it "
                "(offline mode; FeeCollector.forward() does it at the FORWARD step anyway)",
                auction.address,
                held,
                lot.want,
            )
            return held
        if dry_run:
            log.info(
                "dry run: would push_target() %s of %s from %s", held, lot.want, auction.address
            )
            return held
        if self.client.sender is None:
            log.error(
                "%s: keeper mode without a sending client; target not pushed", auction.address
            )
            return held
        try:
            result = self.client.send(auction.address, auction.push_target_calldata())
        except CallReverted as exc:
            log.warning("%s: push_target() reverted: %s", auction.address, exc)
            return held
        log.info(
            "pushed %s of %s from %s to the FeeCollector (%s)",
            held,
            lot.want,
            auction.address,
            result.tx_hash,
        )
        return held

    # Quote request

    def sell_amount(self, lot: LotView) -> int:
        chunk = self.settings.chunk
        return lot.available if not chunk else min(lot.available, chunk)

    def template(self, auction: Auction, lot: LotView) -> HashflowQuote | None:
        """The trader side of the quote request from ``order_for`` (typed reverts logged)."""
        try:
            built = self.contract.order_for(auction.address, lot.token, self.sell_amount(lot))
        except CallReverted as exc:
            log.warning("%s: order_for rejected the lot: %s", lot.token, exc)
            return None
        return HashflowQuote.from_tuple(built)

    def rfq_body(self, template: HashflowQuote) -> dict[str, Any]:
        chain = {"chainType": "evm", "chainId": self.client.chain_id}
        return {
            "source": self.settings.source,
            "baseChain": chain,
            "quoteChain": chain,
            "rfqs": [
                {
                    "baseToken": template.base_token,
                    "quoteToken": template.quote_token,
                    "baseTokenAmount": str(template.base_amount),
                    "trader": template.trader,
                    "rfqType": RFQ_TYPE_RFQM,
                }
            ],
        }

    def log_template(self, auction: Auction, lot: LotView) -> None:
        template = self.template(auction, lot)
        if template is not None:
            log.info(
                "%s: Hashflow RFQ-m quote request not sent (no credential or post: false): %s",
                lot.token,
                self.rfq_body(template),
            )
        return None

    # Quote checks

    def check_quote(
        self,
        auction: Auction,
        lot: LotView,
        template: HashflowQuote,
        quote: HashflowQuote,
        data: dict[str, Any],
        maker_signature: bytes,
    ) -> str | None:
        """Why the maker's quote cannot be signed and relayed for this lot, or None."""
        for key in ("baseChain", "quoteChain"):
            chain = data.get(key) or {}
            if chain and str(chain.get("chainId")) != str(self.client.chain_id):
                return f"{key} {chain} is not this chain"
        if "rfqType" in data and int(data["rfqType"]) != RFQ_TYPE_RFQM:
            return f"rfqType {data['rfqType']} is not RFQ-m"
        if quote.trader != auction.address:
            return f"trader {quote.trader} is not the auction"
        if quote.base_token != lot.token or quote.quote_token != lot.want:
            return f"tokens {quote.base_token} -> {quote.quote_token} are not the lot pair"
        if quote.pool == ZERO_ADDRESS:
            return "no pool"
        if quote.base_amount != template.base_amount or quote.base_amount > lot.available:
            return (
                f"base amount {quote.base_amount} is not the requested {template.base_amount} "
                f"(available {lot.available})"
            )
        needed = (
            template.quote_amount
            if quote.base_amount == template.base_amount
            else auction.get_amount_needed(lot.token, quote.base_amount)
        )
        if quote.quote_amount < needed:
            return f"Hashflow quote {quote.quote_amount} is below the curve quote {needed}"
        if quote.expiry < lot.timestamp:
            return "quote already expired"
        if quote.expiry > lot.end:
            return f"quoteExpiry {quote.expiry} is past the lot end {lot.end}"
        signer = quote.maker_signer(maker_signature, self.client.chain_id)
        if signer is None:
            return "malformed maker signature"
        try:
            pool_signer, enabled = HashflowPool(self.client, quote.pool).signer_configuration()
        except (CallReverted, DecodingError, ValueError) as exc:
            # A pool the factory never created (no code) answers nothing.
            return f"pool {quote.pool} signerConfiguration unreadable: {exc}"
        if not enabled:
            return f"pool {quote.pool} is disabled"
        if signer != pool_signer:
            return f"maker signature recovers to {signer}, the pool's signer is {pool_signer}"
        return None

    # Feed mode

    def publish_quote(
        self, auction: Auction, lot: LotView, *, dry_run: bool
    ) -> PublishResult | None:
        template = self.template(auction, lot)
        if template is None:
            return None
        body = self.rfq_body(template)
        try:
            answer = self.api.rfq(body)
        except ApiError as exc:
            log.warning("%s: Hashflow RFQ failed: %s", lot.token, exc)
            return None
        rfq_id = answer.get("rfqId")
        entry = answer["quotes"][0]
        data = entry.get("quoteData") or {}
        signature_hex = entry.get("signature")
        if not data or not signature_hex:
            log.warning("%s: Hashflow quote carries no quoteData/signature: %s", lot.token, entry)
            return None
        try:
            quote = HashflowQuote.from_quote_data(data)
            maker_signature = _hex_to_bytes(signature_hex)
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("%s: malformed quoteData %s: %s", lot.token, data, exc)
            return None
        reason = self.check_quote(auction, lot, template, quote, data, maker_signature)
        if reason is not None:
            log.info("%s: Hashflow quote %s skipped: %s", lot.token, hex_bytes(quote.txid), reason)
            return None
        taker_signature = quote.signature(self.address)
        reason = self.validate_signature(
            auction, quote.digest(self.domain_separator), taker_signature
        )
        if reason is not None:
            log.warning("%s: Hashflow quote rejected by the auction router: %s", lot.token, reason)
            return None
        if not self.ensure_allowance(auction, lot, self.router.address, dry_run=dry_run):
            return None
        calldata = self.router.trade_rfqm_calldata(
            quote.calldata_tuple(taker_signature, maker_signature)
        )
        txid = hex_bytes(quote.txid)
        details: dict[str, Any] = {
            "rfqId": rfq_id,
            "txid": txid,
            "quote": quote.as_json(),
            "takerSignature": hex_bytes(taker_signature),
            "makerSignature": hex_bytes(maker_signature),
            "router": self.router.address,
            "calldata": hex_bytes(calldata),
        }
        summary = (
            f"{lot.token} sells {quote.base_amount} for {quote.quote_amount} "
            f"(curve {template.quote_amount}), pool {quote.pool}, expiry {quote.expiry}"
        )
        if dry_run:
            log.info("dry run: would relay tradeRFQM for txid %s (%s)", txid, summary)
        elif self.network.onchain and self.client.sender is not None:
            try:
                result = self.client.send(self.router.address, calldata)
            except CallReverted as exc:
                log.warning("%s: tradeRFQM for txid %s reverted: %s", lot.token, txid, exc)
                return None
            details["relayed"] = result.tx_hash
            log.info("relayed tradeRFQM for txid %s: %s (%s)", txid, summary, result.tx_hash)
            details["pushed"] = self.push_target(auction, lot, dry_run=False)
        else:
            log.info(
                "Hashflow RFQ-m quote %s validated, not relayed (offline mode; %s): "
                "tradeRFQM on %s with calldata %s",
                txid,
                summary,
                self.router.address,
                details["calldata"],
            )
        return PublishResult(
            uid=txid,
            valid_to=quote.expiry,
            sell_amount=quote.base_amount,
            buy_amount=quote.quote_amount,
            details=details,
        )


__all__ = ["RFQ_PATH", "RFQ_TYPE_RFQM", "HashflowApi", "HashflowPublisher", "HashflowQuote"]
