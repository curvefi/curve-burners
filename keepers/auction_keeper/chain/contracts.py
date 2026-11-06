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


# OKX Intent Swap order as signed (adapters/okx/intent_swap.vy): the eleven
# EIP-712 fields, commissions (feePercent, referrerWalletAddress, flag) last.
OKX_COMMISSION_ABI = "(uint256,address,uint256)"
OKX_ORDER_ABI = (
    "(address,address,address,address,uint256,uint256,uint32,bytes32,bytes32,bool,"
    f"{OKX_COMMISSION_ABI}[])"
)


class OKXIntentAdapterContract(AdapterContract):
    SETTLEMENT = Function("settlement()", ("address",))
    DOMAIN_SEPARATOR = Function("domain_separator()", ("bytes32",))
    TOKEN_APPROVE = Function("token_approve()", ("address",))
    ORDER_FOR = Function("order_for(address,address,uint256,bytes32)", (OKX_ORDER_ABI, "bytes"))
    DESCRIBE = Function("describe(bytes)", (OKX_ORDER_ABI, "bytes32", "bytes"))

    def settlement(self) -> str:
        return to_checksum_address(self._read_one(self.SETTLEMENT))

    def domain_separator(self) -> bytes:
        return bytes(self._read_one(self.DOMAIN_SEPARATOR))

    def token_approve(self) -> str:
        return to_checksum_address(self._read_one(self.TOKEN_APPROVE))

    def order_for(
        self, auction: str, token: str, sell_amount: int = 0, app_data: bytes = bytes(32)
    ) -> tuple[tuple, bytes]:
        order, signature = self._read(self.ORDER_FOR, auction, token, sell_amount, app_data)
        return tuple(order), bytes(signature)

    def describe(self, payload: bytes) -> tuple[tuple, bytes, bytes]:
        """(order, digest, uid) of a bare ``abi_encode(order)`` payload."""
        order, digest, uid = self._read(self.DESCRIBE, payload)
        return tuple(order), bytes(digest), bytes(uid)


# KyberSwap DSLO order (adapters/kyber/dslo.vy): nine static fields, then
# makerAssetData, takerAssetData, getMakerAmount, getTakerAmount, predicate,
# interaction.
DSLO_ORDER_ABI = (
    "(uint256,address,address,address,address,address,uint256,uint256,uint256,"
    "bytes,bytes,bytes,bytes,bytes,bytes)"
)


class KyberDsloAdapterContract(AdapterContract):
    PROTOCOL = Function("protocol()", ("address",))
    DOMAIN_SEPARATOR = Function("domain_separator()", ("bytes32",))
    ORDER_FOR = Function("order_for(address,address,uint256,uint256)", (DSLO_ORDER_ABI, "bytes"))
    DESCRIBE = Function(
        f"describe({DSLO_ORDER_ABI})", ("address", "address", "uint256", "bool", "uint256")
    )

    def protocol(self) -> str:
        return to_checksum_address(self._read_one(self.PROTOCOL))

    def domain_separator(self) -> bytes:
        return bytes(self._read_one(self.DOMAIN_SEPARATOR))

    def order_for(
        self, auction: str, token: str, salt: int, sell_amount: int = 0
    ) -> tuple[tuple, bytes]:
        order, signature = self._read(self.ORDER_FOR, auction, token, salt, sell_amount)
        return tuple(order), bytes(signature)

    def describe(self, order: tuple) -> tuple[str, str, int, bool, int]:
        """(receiver, fee recipient, fee percent 1e-5, taker-asset fee, last fillable timestamp)."""
        receiver, fee_recipient, fee_percent, taker_fee, valid_to = self._read(self.DESCRIBE, order)
        return (
            to_checksum_address(receiver),
            to_checksum_address(fee_recipient),
            fee_percent,
            taker_fee,
            valid_to,
        )


# Velora Delta order (adapters/velora/delta.vy): fourteen fields, the
# Bridge (protocolSelector, destinationChainId, outputToken, scalingFactor,
# protocolData) last.
DELTA_BRIDGE_ABI = "(bytes4,uint256,address,int8,bytes)"
DELTA_ORDER_ABI = (
    "(address,address,address,address,uint256,uint256,uint256,uint256,uint8,uint256,uint256,"
    f"bytes,bytes,{DELTA_BRIDGE_ABI})"
)


class VeloraDeltaAdapterContract(AdapterContract):
    SETTLEMENT = Function("settlement()", ("address",))
    DOMAIN_SEPARATOR = Function("domain_separator()", ("bytes32",))
    ORDER_FOR = Function("order_for(address,address,uint256,uint256)", (DELTA_ORDER_ABI, "bytes"))
    DESCRIBE = Function("describe(uint256)", ("address", "uint256", "bool", "bool"))

    def settlement(self) -> str:
        return to_checksum_address(self._read_one(self.SETTLEMENT))

    def domain_separator(self) -> bytes:
        return bytes(self._read_one(self.DOMAIN_SEPARATOR))

    def order_for(
        self, auction: str, token: str, nonce: int, sell_amount: int = 0
    ) -> tuple[tuple, bytes]:
        order, signature = self._read(self.ORDER_FOR, auction, token, nonce, sell_amount)
        return tuple(order), bytes(signature)

    def describe(self, partner_and_fee: int) -> tuple[str, int, bool, bool]:
        """(partner, fee bps, partner takes surplus, cap surplus) of a packed partnerAndFee."""
        partner, fee_bps, takes_surplus, cap_surplus = self._read(self.DESCRIBE, partner_and_fee)
        return to_checksum_address(partner), fee_bps, takes_surplus, cap_surplus


# 0x Settler gasless order (adapters/zerox/settler.vy): the Permit2 permit
# (token, amount, nonce, deadline), the AllowedSlippage (recipient, buyToken,
# minAmountOut) and the Settler's hash of the signed action list.
ZEROX_PERMIT_ABI = "(address,uint256,uint256,uint256)"
ZEROX_SLIPPAGE_ABI = "(address,address,uint256)"
ZEROX_ORDER_ABI = f"({ZEROX_PERMIT_ABI},{ZEROX_SLIPPAGE_ABI},bytes32)"


class ZeroExSettlerAdapterContract(AdapterContract):
    SETTLER = Function("settler()", ("address",))
    INTENT = Function("intent()", ("bool",))
    PERMIT2 = Function("permit2()", ("address",))
    DEPLOYER = Function("deployer()", ("address",))
    FEATURE = Function("feature()", ("uint256",))
    DOMAIN_SEPARATOR = Function("domain_separator()", ("bytes32",))
    HASH_ACTIONS = Function("hash_actions(bytes[])", ("bytes32",))
    ORDER_FOR = Function(
        "order_for(address,address,uint256,uint256,uint256,bytes32)", (ZEROX_ORDER_ABI, "bytes")
    )
    DESCRIBE = Function("describe(bytes)", (ZEROX_ORDER_ABI, "bytes32"))

    def settler(self) -> str:
        return to_checksum_address(self._read_one(self.SETTLER))

    def intent(self) -> bool:
        """True for a SettlerIntent adapter (Slippage witness), False for SettlerMetaTxn."""
        return bool(self._read_one(self.INTENT))

    def permit2(self) -> str:
        return to_checksum_address(self._read_one(self.PERMIT2))

    def deployer(self) -> str:
        """The 0x Settler deployer/registry the adapter checks its Settler against."""
        return to_checksum_address(self._read_one(self.DEPLOYER))

    def feature(self) -> int:
        """The registry feature (ERC-721 token id) of the pinned flavor: 3 MetaTxn, 4 Intent."""
        return int(self._read_one(self.FEATURE))

    def domain_separator(self) -> bytes:
        """Permit2's EIP-712 domain separator, as pinned by the adapter."""
        return bytes(self._read_one(self.DOMAIN_SEPARATOR))

    def hash_actions(self, actions: list[bytes]) -> bytes:
        """The Settler's hash of an action list (the actions_hash of a MetaTxn order)."""
        return bytes(self._read_one(self.HASH_ACTIONS, actions))

    def order_for(
        self,
        auction: str,
        token: str,
        nonce: int,
        sell_amount: int = 0,
        deadline: int = 0,
        actions_hash: bytes = bytes(32),
    ) -> tuple[tuple, bytes]:
        order, signature = self._read(
            self.ORDER_FOR, auction, token, nonce, sell_amount, deadline, actions_hash
        )
        return tuple(order), bytes(signature)

    def describe(self, payload: bytes) -> tuple[tuple, bytes]:
        """(order, Permit2 digest) of a bare ``abi_encode(order)`` payload."""
        order, digest = self._read(self.DESCRIBE, payload)
        return tuple(order), bytes(digest)


class ZeroExDeployerRegistry(Contract):
    """The 0x Settler deployer/registry: whether a Settler is still its
    feature's current (``ownerOf``) or previous (``prev``) instance, 0x's
    ``requireGenuineSettler`` and the adapter's own liveness check. A
    reverting view (no instance left) counts as not listed."""

    OWNER_OF = Function("ownerOf(uint256)", ("address",))
    PREV = Function("prev(uint128)", ("address",))

    def owner_of(self, feature: int) -> str | None:
        try:
            return to_checksum_address(self._read_one(self.OWNER_OF, feature))
        except CallReverted:
            return None

    def prev(self, feature: int) -> str | None:
        try:
            return to_checksum_address(self._read_one(self.PREV, feature))
        except CallReverted:
            return None

    def lists(self, feature: int, settler: str) -> bool:
        """One call while the Settler is current, two once it is the previous one."""
        settler = to_checksum_address(settler)
        if self.owner_of(feature) == settler:
            return True
        return self.prev(feature) == settler


class ZeroExSettlerContract(Contract):
    """A 0x Settler (MetaTxn or Intent flavor): the relay entrypoint's calldata."""

    EXECUTE_META_TXN = Function(
        "executeMetaTxn((address,address,uint256),bytes[],bytes32,address,bytes)", ("bool",)
    )

    def execute_meta_txn_calldata(
        self,
        slippage: tuple[str, str, int],
        actions: list[bytes],
        zid: bytes,
        msg_sender: str,
        sig: bytes,
    ) -> bytes:
        return self.EXECUTE_META_TXN.encode(slippage, actions, zid, msg_sender, sig)


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
    "DELTA_ORDER_ABI",
    "DSLO_ORDER_ABI",
    "ERC1271_INVALID",
    "ERC1271_MAGIC_VALUE",
    "LOT_STAGED_TOPIC",
    "OKX_ORDER_ABI",
    "ZEROX_ORDER_ABI",
    "AdapterConfig",
    "AdapterContract",
    "AdapterRegistry",
    "Auction",
    "AuctionTaker",
    "CowAdapterContract",
    "Erc20",
    "KyberDsloAdapterContract",
    "Lot",
    "LotView",
    "OKXIntentAdapterContract",
    "VeloraDeltaAdapterContract",
    "ZeroExDeployerRegistry",
    "ZeroExSettlerAdapterContract",
    "ZeroExSettlerContract",
    "address_word",
    "lot_staged_token",
]
