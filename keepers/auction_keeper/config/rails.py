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

from keepers.auction_keeper.config.common import ConfigError


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


# Integer rail fields that may be zero (the rest must be positive).
_NON_NEGATIVE_FIELDS: set[str] = set()


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
        types = typing.get_args(hints[field_name]) or (hints[field_name],)
        if isinstance(value, bool) and bool not in types or not isinstance(value, types):
            raise ConfigError(f"network {network!r}: {key}.{field_name} has the wrong type")
        if int in types and not isinstance(value, bool):
            if value < 0 or (value == 0 and field_name not in _NON_NEGATIVE_FIELDS):
                raise ConfigError(f"network {network!r}: {key}.{field_name} must be positive")
        values[field_name] = value
    return cls(**values)


__all__ = ["CowSettings", "RailSettings", "parse_cow", "parse_rail"]
