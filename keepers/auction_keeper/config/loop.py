"""The ``focus``, ``lookahead`` and ``fees`` sections: which lots a cycle works
on, which block it prices them for and what a take tips
(``keepers.auction_keeper.core.focus``, ``keepers.auction_keeper.core.keeper``,
``keepers.auction_keeper.core.fees``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from keepers.auction_keeper.config.common import ConfigError, non_negative, positive_int
from keepers.auction_keeper.limits import DEFAULT_COOLDOWN, DEFAULT_PER_MINUTE
from keepers.auction_keeper.logs import LogSettings

# Seconds between blocks by chain id (whole seconds: block timestamps are).
BLOCK_TIMES: dict[int, int] = {
    1: 12,
    10: 2,
    56: 1,
    100: 5,
    137: 2,
    250: 1,
    252: 2,
    8453: 2,
    42161: 1,
    43114: 2,
}


def non_negative_int(value: Any, name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class FocusSettings:
    """Which lots get real work (chain reads, quotes, orders, takes) in a
    cycle. Every lot is ranked from the local model each cycle for free; only
    the ones chosen here cost requests."""

    # Work every lot whose ask is at most this far above its highest market
    # estimate (0.25: within 25%). However many they are.
    threshold: float = 0.25
    # Plus this many of the lots nearest to their market, whatever the distance.
    nearest: int = 5
    # Plus this many lots no source has a price for, in rotation.
    blind: int = 2
    # At most this many lots a cycle, nearest first.
    max_lots: int = 40
    # Seconds an on-chain `available` read of a worked lot is reused.
    confirm: int = 60
    # Lots read back from the chain per cycle, in rotation over all of them:
    # catches what no log says (a kill switch, tokens moved without a take).
    audit: int = 2


@dataclass(frozen=True)
class LookaheadSettings:
    """Price lots for the block a transaction or an order will execute in,
    not for the last one mined."""

    # Blocks ahead of the latest one (0: price for the latest block).
    blocks: int = 1
    # Seconds ahead instead; unset: blocks x the chain's block time (BLOCK_TIMES).
    seconds: int | None = None
    # Also price adapter orders for that block (the orderbook must then
    # accept a signature that is valid one block from now).
    orders: bool = False

    def ahead(self, chain_id: int) -> int:
        if self.seconds is not None:
            return self.seconds
        return self.blocks * BLOCK_TIMES.get(chain_id, 0)


@dataclass(frozen=True)
class FeeSettings:
    """The priority fee of the keeper's takes (``core.fees``). Every other
    transaction (the allowance syncs) pays the node's suggested fee."""

    # Gwei per gas a take tips at least, whatever it earns (0: no floor).
    priority_floor: float = 0.0
    # Gwei per gas a take tips at most.
    priority_cap: float = 5.0
    # Share of the take's profit (route quote - payment) spent on gas, base
    # fee included; the rest of it is the tip.
    profit_share: float = 0.5
    # Want tokens per native token, to convert the profit; unset: asked from
    # the price sources (the wrapped native token), and without an answer
    # the tip is the node's suggestion within the floor and the cap.
    native_price: float | None = None


def parse_fees(raw: Any, name: str, default: FeeSettings) -> FeeSettings:
    raw = _section(raw, name, FeeSettings)
    if raw is None:
        return default
    share = non_negative(raw.get("profit_share"), f"{name}.profit_share", default.profit_share)
    if share > 1:
        raise ConfigError(f"{name}.profit_share must be a fraction in [0, 1]")
    price = raw.get("native_price", default.native_price)
    if price is not None:
        price = non_negative(price, f"{name}.native_price", 0.0)
        if not price:
            raise ConfigError(f"{name}.native_price must be positive")
    settings = FeeSettings(
        priority_floor=non_negative(
            raw.get("priority_floor"), f"{name}.priority_floor", default.priority_floor
        ),
        priority_cap=non_negative(
            raw.get("priority_cap"), f"{name}.priority_cap", default.priority_cap
        ),
        profit_share=share,
        native_price=price,
    )
    if settings.priority_floor > settings.priority_cap:
        raise ConfigError(f"{name}: priority_floor must not exceed priority_cap")
    return settings


@dataclass(frozen=True)
class LimitSettings:
    """Request budgets (``keepers.auction_keeper.limits``), for the whole
    process: the HTTP APIs limit and ban by IP."""

    # Requests per minute to one HTTP host (0: no limit), and per-host values
    # on top of the built-in ones (api.cow.fi: 20).
    http: float = DEFAULT_PER_MINUTE
    hosts: dict[str, float] = field(default_factory=dict)
    # Seconds a host is left alone after a 403 or a 429 (doubling when it repeats).
    cooldown: float = DEFAULT_COOLDOWN
    # Requests per second to a network's node (0: no limit; they are counted either way).
    rpc: float = 0.0


def parse_limits(raw: Any, name: str = "limits") -> LimitSettings:
    default = LimitSettings()
    raw = _section(raw, name, LimitSettings)
    if raw is None:
        return default
    hosts = raw.get("hosts") or {}
    if not isinstance(hosts, dict) or not all(isinstance(host, str) for host in hosts):
        raise ConfigError(f"{name}.hosts must map host names to requests per minute")
    return LimitSettings(
        http=non_negative(raw.get("http"), f"{name}.http", default.http),
        hosts={
            host: non_negative(rate, f"{name}.hosts.{host}", 0.0) for host, rate in hosts.items()
        },
        cooldown=non_negative(raw.get("cooldown"), f"{name}.cooldown", default.cooldown),
        rpc=non_negative(raw.get("rpc"), f"{name}.rpc", default.rpc),
    )


def parse_log(raw: Any, name: str = "log") -> LogSettings:
    default = LogSettings()
    raw = _section(raw, name, LogSettings)
    if raw is None:
        return default
    file = raw.get("file", default.file)
    if file is not None and (not isinstance(file, str) or not file):
        raise ConfigError(f"{name}.file must be a file path")
    return LogSettings(
        file=file,
        max_mb=positive_int(raw.get("max_mb"), f"{name}.max_mb", default.max_mb),
        backups=non_negative_int(raw.get("backups"), f"{name}.backups", default.backups),
        repeat=non_negative_int(raw.get("repeat"), f"{name}.repeat", default.repeat),
    )


def _section(raw: Any, name: str, cls: type) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be a mapping")
    unknown = set(raw) - set(cls.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"{name}: unknown setting(s) {sorted(unknown)}")
    return raw


def parse_focus(raw: Any, name: str, default: FocusSettings) -> FocusSettings:
    raw = _section(raw, name, FocusSettings)
    if raw is None:
        return default
    settings = FocusSettings(
        threshold=non_negative(raw.get("threshold"), f"{name}.threshold", default.threshold),
        **{
            key: non_negative_int(raw.get(key), f"{name}.{key}", getattr(default, key))
            for key in ("nearest", "blind", "max_lots", "confirm", "audit")
        },
    )
    if settings.max_lots == 0:
        raise ConfigError(f"{name}.max_lots must be positive")
    return settings


def parse_lookahead(raw: Any, name: str, default: LookaheadSettings) -> LookaheadSettings:
    raw = _section(raw, name, LookaheadSettings)
    if raw is None:
        return default
    orders = raw.get("orders", default.orders)
    if not isinstance(orders, bool):
        raise ConfigError(f"{name}.orders must be a boolean")
    seconds = raw.get("seconds", default.seconds)
    if seconds is not None:
        seconds = non_negative_int(seconds, f"{name}.seconds", 0)
    return LookaheadSettings(
        blocks=non_negative_int(raw.get("blocks"), f"{name}.blocks", default.blocks),
        seconds=seconds,
        orders=orders,
    )


__all__ = [
    "BLOCK_TIMES",
    "FeeSettings",
    "FocusSettings",
    "LimitSettings",
    "LogSettings",
    "LookaheadSettings",
    "non_negative_int",
    "parse_fees",
    "parse_focus",
    "parse_limits",
    "parse_log",
    "parse_lookahead",
]
