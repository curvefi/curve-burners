"""Minimal ABI helpers: function selectors, call encoding, revert decoding.

The keeper talks to a handful of known contracts, so hand-written function
signatures with eth_abi are enough; no generated ABI JSON is needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
MAX_UINT256 = 2**256 - 1
WAD = 10**18


def split_types(types: str) -> list[str]:
    """Split a comma-separated ABI type list at the top level: ``(a,b)[],c``."""
    out: list[str] = []
    depth = 0
    current = ""
    for char in types:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            out.append(current)
            current = ""
        else:
            current += char
    if current:
        out.append(current)
    return out


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


@dataclass(frozen=True)
class Function:
    """A function signature plus its output types: ``Function("f(uint256)", ("address",))``."""

    signature: str
    outputs: tuple[str, ...] = ()

    @property
    def inputs(self) -> list[str]:
        return split_types(self.signature[self.signature.index("(") + 1 : -1])

    @property
    def selector(self) -> bytes:
        return selector(self.signature)

    def encode(self, *args: Any) -> bytes:
        inputs = self.inputs
        if len(inputs) != len(args):
            raise ValueError(f"{self.signature} takes {len(inputs)} arguments, got {len(args)}")
        return self.selector + (encode(inputs, list(args)) if inputs else b"")

    def decode(self, data: bytes) -> tuple[Any, ...]:
        return decode(list(self.outputs), data)


def address_word(address: str) -> bytes:
    """An address left-padded to 32 bytes (a log topic)."""
    return bytes(12) + bytes.fromhex(address[2:])


def topic_address(topic: bytes) -> str:
    return to_checksum_address("0x" + topic[-20:].hex())


# Revert decoding


_PANIC_SELECTOR = selector("Panic(uint256)")
_ERROR_SELECTOR = selector("Error(string)")

# Typed errors raised along the keeper's call paths; selector -> signature.
KNOWN_ERRORS: dict[bytes, str] = {
    selector(signature): signature
    for signature in (
        # dutch_auction core
        "BadBuyToken()",
        "BadReceiver()",
        "LotInactive()",
        "NothingAvailable()",
        "BadSellAmount()",
        "BadValidTo()",
        "BadBuyAmount()",
        "AmountExceedsAvailable()",
        "ZeroReceiver()",
        "Deadline()",
        "InsufficientAmount()",
        "ExcessivePayment()",
        "WantNotSellable()",
        # CoW adapter and gpv2; the other adapters reuse OrderNotValid(reason)
        # for shape failures.
        "OrderNotValid(string)",
        "NothingToSell()",
        # 0x deployer/registry, through ZeroExSettlerAdapter (fail closed)
        "NoInstance()",
        "ERC721NonexistentToken(uint256)",
        # 1inch adapters (order_for)
        "BadExpiration()",
        # AuctionTaker
        "ZeroAuction()",
        "OnlyActiveAuction()",
        "ProfitShortfall()",
        # DutchAuctionBurner
        "TargetChanged()",
        "OnlyFeeCollector()",
    )
}


class CallReverted(Exception):
    """An eth_call or transaction simulation reverted; ``data`` is the raw revert payload."""

    def __init__(self, data: bytes, message: str | None = None):
        self.data = bytes(data)
        super().__init__(message or describe_revert(self.data))


def describe_revert(data: bytes) -> str:
    """A readable name for a revert payload: typed error, Error(string), Panic, or hex."""
    if not data:
        return "reverted without data"
    head, tail = data[:4], data[4:]
    if head == _ERROR_SELECTOR:
        try:
            return f"Error({decode(['string'], tail)[0]!r})"
        except Exception:  # noqa: BLE001 - malformed payload, fall through to hex
            pass
    if head == _PANIC_SELECTOR and len(tail) == 32:
        return f"Panic({int.from_bytes(tail, 'big')})"
    signature = KNOWN_ERRORS.get(head)
    if signature is not None:
        name = signature[: signature.index("(")]
        types = split_types(signature[signature.index("(") + 1 : -1])
        if not types:
            return signature
        try:
            values = decode(types, tail)
            return f"{name}({', '.join(repr(v) for v in values)})"
        except Exception:  # noqa: BLE001
            return signature
    return "0x" + data.hex()
