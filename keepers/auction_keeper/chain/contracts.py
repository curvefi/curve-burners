"""Typed readers over the Dutch auction contract surface.

Every reader is a thin wrapper around ``ChainClient.call`` with the function
signatures of the contracts in ``contracts/``: the auction core (as exported by
``DutchAuctionBurner``), the ``AdapterRegistry``, the ``CowAdapter``, the
``AuctionTaker`` periphery, and plain ERC-20s.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_utils import to_checksum_address

from keepers.auction_keeper.chain.abi import MAX_UINT256, CallReverted, Function, address_word
from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.chain.events import LOT_STAGED_TOPIC, lot_staged_token, staged_tokens

ERC1271_MAGIC_VALUE = bytes.fromhex("1626ba7e")
ERC1271_INVALID = bytes.fromhex("ffffffff")


@dataclass(frozen=True)
class Lot:
    staged_at: int
    initial_amount: int


@dataclass(frozen=True)
class LotView:
    """A same-block snapshot of one token's lot, as the keeper reasons about it."""

    auction: str
    token: str
    want: str
    receiver: str
    timestamp: int
    start: int
    end: int
    initial_amount: int
    available: int
    # Want owed for the whole available amount right now (0 when inactive).
    amount_needed: int

    @property
    def active(self) -> bool:
        return self.available > 0

    def __str__(self) -> str:
        return (
            f"lot {self.token} @ {self.auction}: available={self.available} "
            f"needed={self.amount_needed} window=[{self.start}, {self.end})"
        )


class Contract:
    def __init__(self, client: ChainClient, address: str):
        self.client = client
        self.address = to_checksum_address(address)

    def _read(self, fn: Function, *args: Any, sender: str | None = None) -> tuple[Any, ...]:
        return fn.decode(self.client.call(self.address, fn.encode(*args), sender=sender))

    def _read_one(self, fn: Function, *args: Any) -> Any:
        return self._read(fn, *args)[0]


class Erc20(Contract):
    BALANCE_OF = Function("balanceOf(address)", ("uint256",))
    ALLOWANCE = Function("allowance(address,address)", ("uint256",))
    DECIMALS = Function("decimals()", ("uint8",))
    SYMBOL = Function("symbol()", ("string",))
    APPROVE = Function("approve(address,uint256)")

    def balance_of(self, owner: str) -> int:
        return self._read_one(self.BALANCE_OF, owner)

    def allowance(self, owner: str, spender: str) -> int:
        return self._read_one(self.ALLOWANCE, owner, spender)

    def decimals(self) -> int:
        return self._read_one(self.DECIMALS)

    def symbol(self) -> str:
        try:
            return self._read_one(self.SYMBOL)
        except Exception:  # noqa: BLE001 - bytes32 symbols and the like
            return self.address[:10]

    def approve_calldata(self, spender: str, amount: int) -> bytes:
        return self.APPROVE.encode(spender, amount)


class Auction(Contract):
    """The IDutchAuction surface plus the adapters module exports of the burner."""

    WANT = Function("want()", ("address",))
    RECEIVER = Function("receiver()", ("address",))
    REGISTRY = Function("registry()", ("address",))
    AUCTION_LENGTH = Function("auction_length()", ("uint256",))
    LOTS = Function("lots(address)", ("uint256", "uint256"))
    WINDOW = Function("window(address,uint256)", ("uint256", "uint256"))
    AVAILABLE = Function("available(address)", ("uint256",))
    PRICE = Function("price(address,uint256)", ("uint256",))
    GET_AMOUNT_NEEDED = Function("getAmountNeeded(address,uint256,uint256)", ("uint256",))
    CHECK_ORDER = Function(
        "check_order(address,address,address,uint256,uint256,uint256)", ("bool",)
    )
    IS_VALID_SIGNATURE = Function("isValidSignature(bytes32,bytes)", ("bytes4",))
    SYNC_EXECUTOR_APPROVALS = Function("sync_executor_approvals(address,address[])")
    TAKE = Function("take(address,uint256,address,bytes)", ("uint256",))
    TAKE_WITH_LIMITS = Function(
        "take_with_limits(address,uint256,uint256,uint256,address,uint256,bytes)",
        ("uint256", "uint256"),
    )

    def want(self) -> str:
        return to_checksum_address(self._read_one(self.WANT))

    def receiver(self) -> str:
        return to_checksum_address(self._read_one(self.RECEIVER))

    def registry(self) -> str:
        return to_checksum_address(self._read_one(self.REGISTRY))

    def auction_length(self) -> int:
        return self._read_one(self.AUCTION_LENGTH)

    def lots(self, token: str) -> Lot:
        staged_at, initial_amount = self._read(self.LOTS, token)
        return Lot(staged_at=staged_at, initial_amount=initial_amount)

    def window(self, token: str, timestamp: int = 0) -> tuple[int, int]:
        start, end = self._read(self.WINDOW, token, timestamp)
        return start, end

    def available(self, token: str) -> int:
        return self._read_one(self.AVAILABLE, token)

    def price(self, token: str, timestamp: int | None = None) -> int:
        if timestamp is None:
            timestamp = self.client.latest_block().timestamp
        return self._read_one(self.PRICE, token, timestamp)

    def get_amount_needed(
        self, token: str, amount: int = MAX_UINT256, *, timestamp: int | None = None
    ) -> int:
        """Want owed for ``amount`` now, or projected at ``timestamp`` (same lot window)."""
        if timestamp is None:
            timestamp = self.client.latest_block().timestamp
        return self._read_one(self.GET_AMOUNT_NEEDED, token, amount, timestamp)

    def check_order(
        self,
        sell_token: str,
        buy_token: str,
        receiver: str,
        sell_amount: int,
        min_buy_amount: int,
        valid_to: int,
    ) -> bool:
        """True for a fillable order; raises CallReverted with the typed reason otherwise."""
        return self._read_one(
            self.CHECK_ORDER, sell_token, buy_token, receiver, sell_amount, min_buy_amount, valid_to
        )

    def is_valid_signature(self, digest: bytes, signature: bytes) -> bytes:
        """The router's answer: ERC1271_MAGIC_VALUE, ERC1271_INVALID, or a CallReverted."""
        return bytes(self._read_one(self.IS_VALID_SIGNATURE, digest, signature))

    def lot_view(self, token: str) -> LotView:
        """Snapshot a token's lot in a few reads; ``available`` is 0 for an inactive lot."""
        token = to_checksum_address(token)
        block = self.client.latest_block()
        lot = self.lots(token)
        start, end = self.window(token)
        available = self.available(token)
        needed = 0
        if available:
            needed = self._read_one(self.GET_AMOUNT_NEEDED, token, available, block.timestamp)
        return LotView(
            auction=self.address,
            token=token,
            want=self.want(),
            receiver=self.receiver(),
            timestamp=block.timestamp,
            start=start,
            end=end,
            initial_amount=lot.initial_amount,
            available=available,
            amount_needed=needed,
        )

    # Calldata builders (sent by whoever holds a key)

    def sync_executor_approvals_calldata(self, executor: str, tokens: list[str]) -> bytes:
        return self.SYNC_EXECUTOR_APPROVALS.encode(executor, tokens)

    def take_calldata(self, token: str, max_amount: int, receiver: str, data: bytes = b"") -> bytes:
        return self.TAKE.encode(token, max_amount, receiver, data)

    def take_with_limits_calldata(
        self,
        token: str,
        max_amount: int,
        min_amount: int,
        max_payment: int,
        receiver: str,
        deadline: int,
        data: bytes = b"",
    ) -> bytes:
        return self.TAKE_WITH_LIMITS.encode(
            token, max_amount, min_amount, max_payment, receiver, deadline, data
        )

    # Token discovery

    def staged_tokens(self, from_block: int, to_block: int, chunk: int = 50_000) -> set[str]:
        """Tokens with a LotStaged event in [from_block, to_block] (``chain.events``)."""
        return staged_tokens(self.client, self.address, from_block, to_block, chunk)


@dataclass(frozen=True)
class AdapterConfig:
    adapter: str
    executor: str
    active: bool


class AdapterRegistry(Contract):
    GET_ADAPTERS = Function("get_adapters()", ("address[]",))
    GET_ADAPTER = Function("get_adapter(address)", ("(address,bool)",))
    IS_EXECUTOR_ACTIVE = Function("is_executor_active(address)", ("bool",))

    def adapters(self) -> list[AdapterConfig]:
        out = []
        for adapter in self._read_one(self.GET_ADAPTERS):
            executor, active = self._read_one(self.GET_ADAPTER, adapter)
            out.append(
                AdapterConfig(
                    adapter=to_checksum_address(adapter),
                    executor=to_checksum_address(executor),
                    active=active,
                )
            )
        return out

    def active_adapters(self) -> list[AdapterConfig]:
        return [config for config in self.adapters() if config.active]

    def is_executor_active(self, executor: str) -> bool:
        return self._read_one(self.IS_EXECUTOR_ACTIVE, executor)


class AdapterContract(Contract):
    """What every registry adapter exposes to the keeper: its version string."""

    ADAPTER_VERSION = Function("ADAPTER_VERSION()", ("string",))

    def version(self) -> str | None:
        try:
            return self._read_one(self.ADAPTER_VERSION)
        except (CallReverted, ValueError):
            return None


class CowAdapterContract(AdapterContract):
    SETTLEMENT = Function("settlement()", ("address",))
    DOMAIN_SEPARATOR = Function("domain_separator()", ("bytes32",))
    VAULT_RELAYER = Function("vault_relayer()", ("address",))
    APP_DATA = Function("app_data()", ("bytes32",))

    def settlement(self) -> str:
        return to_checksum_address(self._read_one(self.SETTLEMENT))

    def domain_separator(self) -> bytes:
        return bytes(self._read_one(self.DOMAIN_SEPARATOR))

    def vault_relayer(self) -> str:
        return to_checksum_address(self._read_one(self.VAULT_RELAYER))

    def app_data(self) -> bytes:
        return bytes(self._read_one(self.APP_DATA))


# UniswapX ExclusiveDutchOrder (adapters/uniswapx/uniswapx.vy): info (reactor,
# swapper, nonce, deadline, validation contract, validation data), decay
# window, exclusivity, input, outputs.
UNISWAPX_ORDER_ABI = (
    "((address,address,uint256,uint256,address,bytes),uint256,uint256,address,uint256,"
    "(address,uint256,uint256),(address,uint256,uint256,address)[])"
)


class UniswapXAdapterContract(AdapterContract):
    REACTOR = Function("reactor()", ("address",))
    PERMIT2 = Function("permit2()", ("address",))
    DOMAIN_SEPARATOR = Function("domain_separator()", ("bytes32",))
    ORDER_FOR = Function(
        "order_for(address,address,uint256,uint256)", (UNISWAPX_ORDER_ABI, "bytes")
    )

    def reactor(self) -> str:
        return to_checksum_address(self._read_one(self.REACTOR))

    def permit2(self) -> str:
        return to_checksum_address(self._read_one(self.PERMIT2))

    def domain_separator(self) -> bytes:
        """Permit2's EIP-712 domain separator, as pinned by the adapter."""
        return bytes(self._read_one(self.DOMAIN_SEPARATOR))

    def order_for(
        self, auction: str, token: str, nonce: int, sell_amount: int = 0
    ) -> tuple[tuple, bytes]:
        order, signature = self._read(self.ORDER_FOR, auction, token, nonce, sell_amount)
        return tuple(order), bytes(signature)


class AuctionTaker(Contract):
    """``contracts/burners/auction/periphery/AuctionTaker.vy``: native take plus a route."""

    TAKE_WITH_ROUTE = Function(
        "take_with_route(address,address,uint256,uint256,address,(address,bytes)[])",
        ("uint256",),
    )

    def take_with_route_calldata(
        self,
        auction: str,
        token: str,
        max_amount: int,
        min_profit: int,
        profit_receiver: str,
        calls: list[tuple[str, bytes]],
    ) -> bytes:
        return self.TAKE_WITH_ROUTE.encode(
            auction, token, max_amount, min_profit, profit_receiver, calls
        )


__all__ = [
    "ERC1271_INVALID",
    "ERC1271_MAGIC_VALUE",
    "LOT_STAGED_TOPIC",
    "UNISWAPX_ORDER_ABI",
    "AdapterConfig",
    "AdapterContract",
    "AdapterRegistry",
    "Auction",
    "AuctionTaker",
    "CowAdapterContract",
    "Erc20",
    "Lot",
    "LotView",
    "UniswapXAdapterContract",
    "address_word",
    "lot_staged_token",
]
