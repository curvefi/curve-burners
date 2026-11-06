"""The settlement-rail sections: ``cow`` and, for every other rail, a section
named after it parsed by ``parse_rail`` into a ``RailSettings`` subclass (an
``api_key_env``, an ``enabled`` switch and ``post`` plus the rail's own
fields). A new rail adds its settings class here and its field to
``NetworkConfig`` (``config/core.py``)."""

from __future__ import annotations

import os
import typing
from dataclasses import dataclass, fields
from typing import Any

from keepers.auction_keeper.config.common import ConfigError, addresses


@dataclass(frozen=True)
class CowSettings:
    orderbook_url: str | None = None
    # The full appData JSON document whose keccak is the adapter's app_data();
    # registered with the orderbook once so hash-only orders are accepted.
    app_data_document: str | None = None


@dataclass(frozen=True)
class RailSettings:
    """What every non-CoW rail shares: an API key env var, an on/off switch,
    and whether orders are posted to the rail's feed or only built, validated
    and logged (``post: false``: the signed order is left for direct on-chain
    fills by whoever reads the log or the state file)."""

    api_key_env: str | None = None
    enabled: bool = True
    post: bool = True

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


@dataclass(frozen=True)
class OKXSettings(RailSettings):
    """OKX DEX aggregator intent mode (Onchain OS). Every endpoint, the quote
    included, needs an Onchain OS project key: the key, secret and passphrase
    sign each request (HMAC headers)."""

    api_key_env: str | None = "OKX_API_KEY"
    secret_key_env: str = "OKX_SECRET_KEY"
    passphrase_env: str = "OKX_PASSPHRASE"
    api_url: str = "https://web3.okx.com"
    # chainIndex in the API (the chain id on EVM chains); defaults by chain id
    # for the chains the intent mode lists (Ethereum, BSC, Arbitrum, Base, X Layer).
    chain_index: str | None = None
    # Quote parameters: slippagePercent (required by the intent mode; the
    # signed toTokenAmount is the quote after it) and the order expiration in
    # seconds (180 default, 300 max on the API side).
    slippage_percent: str = "0.1"
    expiration: int = 180
    # UNVERIFIED: whether the orderbook expects the submitter to send the
    # on-chain shape `burner ++ adapter ++ abi.encode(order)` (true, what the
    # settlement's Signing.recoverEip1271Signer reads and what the solver SDK
    # forwards verbatim) or only `adapter ++ abi.encode(order)` and prefixes
    # the owner itself (false). One keyed probe settles it; flip here.
    owner_prefix: bool = True

    @property
    def secret_key(self) -> str | None:
        return os.environ.get(self.secret_key_env)

    @property
    def passphrase(self) -> str | None:
        return os.environ.get(self.passphrase_env)


@dataclass(frozen=True)
class KyberSettings(RailSettings):
    """KyberSwap limit orders (DSLO): the maker API needs no key; an optional
    ``X-Client-Id`` selects the fee recipient the API packs into the signed
    ``feeConfig`` (sent to both calls)."""

    api_key_env: str | None = None
    api_url: str = "https://limit-order.kyberswap.com"
    client_id: str | None = None


@dataclass(frozen=True)
class VeloraSettings(RailSettings):
    """Velora (ex-ParaSwap) Delta: the public Delta API needs no key. The
    order is built here from the price answer and the adapter's
    ``order_for`` (the API's build endpoint wants an undocumented route
    object) and posted to ``/v2/delta/orders``."""

    api_key_env: str | None = None
    api_url: str = "https://api.paraswap.io"
    # Partner string sent with the price and the order (fee attribution).
    partner: str | None = None
    # A partner fee the order signs away (packed into partnerAndFee): the
    # recipient and bps; the signed destAmount is grossed up so the netted
    # floor still covers the curve quote. None: no partner fee.
    partner_address: str | None = None
    partner_fee_bps: int = 0
    # FillableOrder (pro-rata partial fills) instead of the fill-or-kill Order
    # primary type; the adapter accepts either digest.
    partially_fillable: bool = False
    # ASSUMED: the API's order `type` for a standing ask at the curve floor
    # (MARKET is the API default; LIMIT keeps the order open until the deadline).
    order_type: str = "LIMIT"


# What a MetaTxn publisher does when the Gasless API is not in play (``post``
# off, no key, or a quote it cannot sign): nothing, log the self-authored
# relay calldata, or (keeper mode) send it from the keeper account.
ZEROX_RELAY_MODES = ("none", "log", "self-actions")
ZEROX_FLAVORS = ("metatxn", "intent")


@dataclass(frozen=True)
class CurveRoute:
    """One Curve pool hop for a self-authored 0x action list: sell ``token``
    (coin ``i`` of ``pool``) for coin ``j`` (the auction's payment token)."""

    token: str
    pool: str
    i: int
    j: int

    @classmethod
    def parse(cls, raw: Any) -> CurveRoute:
        if not isinstance(raw, dict) or set(raw) != {"token", "pool", "i", "j"}:
            raise ConfigError("zerox.routes entries are {token, pool, i, j} mappings")
        (token,) = addresses([raw["token"]], "zerox.routes.token")
        (pool,) = addresses([raw["pool"]], "zerox.routes.pool")
        for name in ("i", "j"):
            if isinstance(raw[name], bool) or not isinstance(raw[name], int) or raw[name] < 0:
                raise ConfigError(f"zerox.routes.{name} must be a non-negative integer")
        return cls(token=token, pool=pool, i=raw["i"], j=raw["j"])


@dataclass(frozen=True)
class ZeroExSettings(RailSettings):
    """0x Settler gasless flows. With a Gasless API key (``post: true``) a
    SettlerMetaTxn adapter is served through ``/gasless/quote`` and
    ``/gasless/submit``; otherwise (or when a quote cannot be signed) the
    publisher authors its own action list through a configured Curve pool and
    relays it per ``relay``. A SettlerIntent adapter only logs standing asks
    for 0x's solvers (the API serves ``settler_metatransaction`` only)."""

    api_key_env: str | None = "ZEROX_API_KEY"
    api_url: str = "https://api.0x.org"
    # chainId sent to the API; defaults to the connected chain's id.
    chain_id: int | None = None
    # Override of the adapter's pinned flavor ("metatxn" | "intent"); the
    # default reads the adapter's intent() and is what validates on-chain.
    flavor: str | None = None
    relay: str = "log"
    # slippagePpm for the quote; None leaves the API default. Tighter slippage
    # raises the quote's minAmountOut (the signed floor) towards buyAmount.
    slippage_ppm: int | None = None
    # Curve pools for the self-authored action list, one per sell token.
    routes: tuple[CurveRoute, ...] = ()

    def __post_init__(self) -> None:
        if self.relay not in ZEROX_RELAY_MODES:
            raise ConfigError(f"zerox.relay must be one of {', '.join(ZEROX_RELAY_MODES)}")
        if self.flavor is not None and self.flavor not in ZEROX_FLAVORS:
            raise ConfigError(f"zerox.flavor must be one of {', '.join(ZEROX_FLAVORS)}")
        routes = tuple(
            route if isinstance(route, CurveRoute) else CurveRoute.parse(route)
            for route in self.routes
        )
        if len({route.token for route in routes}) != len(routes):
            raise ConfigError("zerox.routes lists a token twice")
        object.__setattr__(self, "routes", routes)


@dataclass(frozen=True)
class HashflowSettings(RailSettings):
    """Hashflow RFQ-m: every taker API call needs the credential Hashflow
    issues (``Authorization`` header). There is no standing-ask mode and no
    public submit endpoint for a maker-signed RFQ-m quote: ``post: true``
    requests quotes, and the keeper relays ``tradeRFQM`` itself in keeper
    mode (offline mode logs the calldata for any relayer)."""

    api_key_env: str | None = "HASHFLOW_API_KEY"
    api_url: str = "https://api.hashflow.com"
    # The `source` identity of the RFQ body: "api" for an API wallet trader
    # (the docs' term for a wallet quoting through the API), else the
    # aggregator name Hashflow registered.
    source: str = "api"
    # Base-token amount requested per quote; None asks for everything
    # available (an RFQ-m fill is exact, so this bounds the fill size).
    chunk: int | None = None


# Integer rail fields that may be zero (the rest must be positive).
_NON_NEGATIVE_FIELDS: set[str] = {"partner_fee_bps", "slippage_ppm"}


def parse_cow(raw: dict[str, Any], network: str) -> CowSettings:
    section = raw.get("cow") or {}
    if not isinstance(section, dict):
        raise ConfigError(f"network {network!r}: cow must be a mapping")
    unknown = set(section) - {"orderbook_url", "app_data_document"}
    if unknown:
        raise ConfigError(f"network {network!r}: cow: unknown setting(s) {sorted(unknown)}")
    for key, value in section.items():
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"network {network!r}: cow.{key} must be a string")
    return CowSettings(
        orderbook_url=section.get("orderbook_url"),
        app_data_document=section.get("app_data_document"),
    )


def parse_rail(raw: dict[str, Any], network: str, key: str, cls: type) -> Any:
    """Parse one rail section (``raw[key]``) into its ``RailSettings``
    subclass; the accepted type of every field is its annotation (``str``,
    ``str | None``, ``bool``, ``int``), unknown keys and wrong types are
    errors."""
    section = raw.get(key) or {}
    if not isinstance(section, dict):
        raise ConfigError(f"network {network!r}: {key} must be a mapping")
    allowed = {f.name for f in fields(cls)}
    hints = typing.get_type_hints(cls)
    values: dict[str, Any] = {}
    for field_name, value in section.items():
        if field_name not in allowed:
            raise ConfigError(f"network {network!r}: {key}.{field_name} is not a setting")
        hint = hints[field_name]
        if typing.get_origin(hint) in (tuple, list):
            types: tuple[type, ...] = (list, tuple)
        else:
            types = typing.get_args(hint) or (hint,)
        if isinstance(value, bool) and bool not in types or not isinstance(value, types):
            raise ConfigError(f"network {network!r}: {key}.{field_name} has the wrong type")
        if int in types and isinstance(value, int) and not isinstance(value, bool):
            if value < 0 or (value == 0 and field_name not in _NON_NEGATIVE_FIELDS):
                raise ConfigError(f"network {network!r}: {key}.{field_name} must be positive")
        values[field_name] = value
    return cls(**values)


__all__ = ["CowSettings", "RailSettings", "parse_cow", "parse_rail"]
