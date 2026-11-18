"""CoW Protocol publisher: GPv2 orders signed through the auction's ERC-1271 router.

An order sells a lot's available amount for the live quote, receiver = the
auction's proceeds receiver, and is posted with signing scheme ``eip1271``,
``from`` = the auction, signature ``cow_adapter ++ abi_encode(order)`` (the
template ``CowAdapter.order_for`` documents). The order is built here rather
than through ``order_for`` so the keeper can shorten ``validTo`` to its TTL:
a short validity keeps the standing ask close to the decaying curve and bounds
the open orders per owner.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests
from eth_abi import encode
from eth_utils import keccak

from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.chain.contracts import (
    AdapterConfig,
    Auction,
    CowAdapterContract,
    LotView,
)
from keepers.auction_keeper.config import NetworkConfig
from keepers.auction_keeper.rails import AdapterPublisher, PublishResult

log = logging.getLogger(__name__)

SELL_KIND = keccak(text="sell")
ERC20_BALANCE = keccak(text="erc20")
ORDER_TYPE_HASH = keccak(
    text=(
        "Order(address sellToken,address buyToken,address receiver,uint256 sellAmount,"
        "uint256 buyAmount,uint32 validTo,bytes32 appData,uint256 feeAmount,string kind,"
        "bool partiallyFillable,string sellTokenBalance,string buyTokenBalance)"
    )
)
ORDER_FIELD_TYPES = [
    "address",
    "address",
    "address",
    "uint256",
    "uint256",
    "uint32",
    "bytes32",
    "uint256",
    "bytes32",
    "bool",
    "bytes32",
    "bytes32",
]

# Public orderbook base URLs by chain id; override with cow.orderbook_url.
ORDERBOOK_URLS: dict[int, str] = {
    1: "https://api.cow.fi/mainnet",
    100: "https://api.cow.fi/xdai",
    137: "https://api.cow.fi/polygon",
    8453: "https://api.cow.fi/base",
    42161: "https://api.cow.fi/arbitrum_one",
    43114: "https://api.cow.fi/avalanche",
    56: "https://api.cow.fi/bnb",
    11155111: "https://api.cow.fi/sepolia",
}


@dataclass(frozen=True)
class GPv2Order:
    sell_token: str
    buy_token: str
    receiver: str
    sell_amount: int
    buy_amount: int
    valid_to: int
    app_data: bytes
    fee_amount: int = 0
    kind: bytes = SELL_KIND
    partially_fillable: bool = True
    sell_token_balance: bytes = ERC20_BALANCE
    buy_token_balance: bytes = ERC20_BALANCE

    def as_tuple(self) -> tuple:
        return (
            self.sell_token,
            self.buy_token,
            self.receiver,
            self.sell_amount,
            self.buy_amount,
            self.valid_to,
            self.app_data,
            self.fee_amount,
            self.kind,
            self.partially_fillable,
            self.sell_token_balance,
            self.buy_token_balance,
        )

    def encode(self) -> bytes:
        """``abi_encode(GPv2Order)``: the bare payload the CowAdapter validates."""
        return encode(ORDER_FIELD_TYPES, list(self.as_tuple()))

    def digest(self, domain_separator: bytes) -> bytes:
        struct_hash = keccak(
            encode(["bytes32", *ORDER_FIELD_TYPES], [ORDER_TYPE_HASH, *self.as_tuple()])
        )
        return keccak(b"\x19\x01" + domain_separator + struct_hash)

    def uid(self, domain_separator: bytes, owner: str) -> str:
        """The orderbook order id: ``digest ++ owner ++ validTo``."""
        return (
            "0x"
            + (
                self.digest(domain_separator)
                + bytes.fromhex(owner[2:])
                + self.valid_to.to_bytes(4, "big")
            ).hex()
        )

    def signature(self, adapter: str) -> bytes:
        """``adapter ++ abi_encode(order)``: the eip1271 signature the router expects."""
        return bytes.fromhex(adapter[2:]) + self.encode()

    def api_body(self, owner: str, signature: bytes) -> dict[str, Any]:
        return {
            "sellToken": self.sell_token,
            "buyToken": self.buy_token,
            "receiver": self.receiver,
            "sellAmount": str(self.sell_amount),
            "buyAmount": str(self.buy_amount),
            "validTo": self.valid_to,
            "appData": "0x" + self.app_data.hex(),
            "feeAmount": str(self.fee_amount),
            "kind": "sell",
            "partiallyFillable": self.partially_fillable,
            "sellTokenBalance": "erc20",
            "buyTokenBalance": "erc20",
            "signingScheme": "eip1271",
            "signature": "0x" + signature.hex(),
            "from": owner,
        }


class OrderbookError(RuntimeError):
    def __init__(self, status: int, error_type: str, description: str):
        self.status = status
        self.error_type = error_type
        self.description = description
        super().__init__(f"orderbook {status} {error_type}: {description}")


class Orderbook:
    """The CoW orderbook REST API, the few calls the keeper needs."""

    def __init__(
        self, base_url: str, *, session: requests.Session | None = None, timeout: float = 30.0
    ):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout

    @classmethod
    def for_chain(cls, chain_id: int, url: str | None = None) -> Orderbook:
        base = url or ORDERBOOK_URLS.get(chain_id)
        if base is None:
            raise ValueError(
                f"no default CoW orderbook for chain {chain_id}; set cow.orderbook_url"
            )
        return cls(base)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.session.request(
                method, f"{self.base_url}/api/v1{path}", timeout=self.timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise OrderbookError(0, "Transport", str(exc)) from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            raise OrderbookError(
                response.status_code,
                str(payload.get("errorType", "Unknown")),
                str(payload.get("description", response.text[:200])),
            )
        if not response.content:
            return None
        return response.json()

    def post_order(self, body: dict[str, Any]) -> str:
        return str(self._request("POST", "/orders", json=body))

    def get_order(self, uid: str) -> dict[str, Any]:
        return self._request("GET", f"/orders/{uid}")

    def register_app_data(self, app_data_hash: bytes, document: str) -> None:
        self._request("PUT", f"/app_data/0x{app_data_hash.hex()}", json={"fullAppData": document})


class CowPublisher(AdapterPublisher):
    version = "CowAdapter"

    def __init__(
        self,
        client: ChainClient,
        adapter: AdapterConfig,
        network: NetworkConfig,
        *,
        orderbook: Orderbook | None = None,
    ):
        super().__init__(client, adapter, network)
        self.contract = CowAdapterContract(client, adapter.adapter)
        self.app_data = self.contract.app_data()
        self.domain_separator = self.contract.domain_separator()
        self.vault_relayer = self.contract.vault_relayer()
        self.orderbook = orderbook or Orderbook.for_chain(
            client.chain_id, network.cow.orderbook_url
        )
        self._app_data_registered = False
        if adapter.executor != self.vault_relayer:
            log.warning(
                "CowAdapter %s: registry executor %s differs from vault relayer %s",
                adapter.adapter,
                adapter.executor,
                self.vault_relayer,
            )

    # Order construction

    def build_order(self, lot: LotView, valid_to: int) -> GPv2Order:
        return GPv2Order(
            sell_token=lot.token,
            buy_token=lot.want,
            receiver=lot.receiver,
            sell_amount=lot.available,
            buy_amount=lot.amount_needed,
            valid_to=valid_to,
            app_data=self.app_data,
        )

    def validate(self, auction: Auction, order: GPv2Order) -> str | None:
        """Run the auction's router on the order; None when valid, else the reason."""
        return self.validate_signature(
            auction, order.digest(self.domain_separator), order.signature(self.address)
        )

    # Publishing

    def publish(
        self, auction: Auction, lot: LotView, *, dry_run: bool = False
    ) -> PublishResult | None:
        if not lot.active:
            return None
        valid_to = self.valid_to(lot)
        if valid_to <= lot.timestamp:
            log.info("%s: lot window closes before an order could be valid", lot.token)
            return None
        order = self.build_order(lot, valid_to)
        reason = self.validate(auction, order)
        if reason is not None:
            log.warning("%s: order rejected by the auction router: %s", lot.token, reason)
            return None
        if not self.ensure_allowance(auction, lot, self.vault_relayer, dry_run=dry_run):
            return None

        signature = order.signature(self.address)
        body = order.api_body(auction.address, signature)
        uid = order.uid(self.domain_separator, auction.address)
        if dry_run:
            log.info(
                "dry run: would post %s selling %s for %s", uid, lot.available, lot.amount_needed
            )
        else:
            self._register_app_data()
            posted = self.orderbook.post_order(body)
            if posted.lower() != uid.lower():
                log.warning("orderbook uid %s differs from the computed %s", posted, uid)
                uid = posted
            log.info(
                "posted %s: %s sells %s for >= %s", uid, lot.token, lot.available, lot.amount_needed
            )
        return PublishResult(
            uid=uid,
            valid_to=valid_to,
            sell_amount=lot.available,
            buy_amount=lot.amount_needed,
            details={"order": body},
        )

    def _register_app_data(self) -> None:
        document = self.network.cow.app_data_document
        if self._app_data_registered or document is None:
            return
        if keccak(text=document) != self.app_data:
            raise ValueError(
                "cow.app_data_document hashes to "
                f"0x{keccak(text=document).hex()}, the adapter expects 0x{self.app_data.hex()}"
            )
        self.orderbook.register_app_data(self.app_data, document)
        self._app_data_registered = True
