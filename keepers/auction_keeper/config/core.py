"""The network and keeper configuration: ``NetworkConfig`` (one entry per
network, assembled from the section parsers of this package), ``KeeperConfig``
and the YAML loading with ``${NAME}`` expansion."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from eth_utils import to_checksum_address

from keepers.auction_keeper.config.common import ConfigError, address, addresses, positive_int
from keepers.auction_keeper.config.native import NativeSettings, parse_native
from keepers.auction_keeper.config.pricing import (
    PricingSettings,
    TtlSettings,
    parse_pricing,
    parse_ttl,
)
from keepers.auction_keeper.config.rails import CowSettings, parse_cow

DEFAULT_POLL_INTERVAL = 60
DEFAULT_ORDER_TTL = 300
DEFAULT_STATE_PATH = "auction_keeper_state.json"
DEFAULT_LOG_CHUNK = 50_000
DEFAULT_MAX_OPEN_ORDERS = 8
# offline: never sends a transaction (reads the chain, posts to orderbooks);
# keeper: also sends the on-chain housekeeping (executor allowance syncs).
MODES = ("offline", "keeper")
DEFAULT_MODE = "offline"

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class TokenFilter:
    whitelist: frozenset[str] = frozenset()
    blacklist: frozenset[str] = frozenset()

    def allows(self, token: str) -> bool:
        token = to_checksum_address(token)
        if token in self.blacklist:
            return False
        return not self.whitelist or token in self.whitelist


@dataclass(frozen=True)
class NetworkConfig:
    name: str
    rpc: str
    auctions: tuple[str, ...]
    tokens: TokenFilter = TokenFilter()
    start_block: int | None = None
    poll_interval: int = DEFAULT_POLL_INTERVAL
    order_ttl: int = DEFAULT_ORDER_TTL
    log_chunk: int = DEFAULT_LOG_CHUNK
    mode: str = DEFAULT_MODE
    keeper_key_env: str | None = None
    pricing: PricingSettings = PricingSettings()
    ttl: TtlSettings = TtlSettings()
    max_open_orders: int = DEFAULT_MAX_OPEN_ORDERS
    native: NativeSettings = NativeSettings()
    cow: CowSettings = CowSettings()
    # Periphery for custom executions (optional).
    auction_taker: str | None = None

    @property
    def onchain(self) -> bool:
        """Whether the publishing loop may send transactions (keeper mode)."""
        return self.mode == "keeper"

    @property
    def private_key(self) -> str | None:
        return os.environ.get(self.keeper_key_env) if self.keeper_key_env else None


@dataclass(frozen=True)
class KeeperConfig:
    networks: dict[str, NetworkConfig] = field(default_factory=dict)
    state_path: Path = Path(DEFAULT_STATE_PATH)

    def network(self, name: str) -> NetworkConfig:
        try:
            return self.networks[name]
        except KeyError:
            raise ConfigError(f"unknown network {name!r}; have {sorted(self.networks)}") from None


def expand_env(value: Any) -> Any:
    """Replace ``${NAME}`` in strings (recursively through lists and dicts)."""
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(f"environment variable {name} is not set")
            return os.environ[name]

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    return value


def _mode(value: Any, name: str, default: str) -> str:
    if value is None:
        return default
    if value not in MODES:
        raise ConfigError(f"{name} must be one of {', '.join(MODES)}")
    return value


def _key_env(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} must be the name of an environment variable")
    return value


def parse_network(name: str, raw: dict[str, Any], defaults: dict[str, Any]) -> NetworkConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"network {name!r} must be a mapping")
    rpc = raw.get("rpc")
    if not isinstance(rpc, str) or not rpc:
        raise ConfigError(f"network {name!r}: rpc is required")
    auctions = addresses(raw.get("auctions"), f"network {name!r}: auctions")
    if not auctions:
        raise ConfigError(f"network {name!r}: at least one auction is required")
    if len(set(auctions)) != len(auctions):
        raise ConfigError(f"network {name!r}: auctions lists an address twice")

    tokens_raw = raw.get("tokens") or {}
    if not isinstance(tokens_raw, dict):
        raise ConfigError(f"network {name!r}: tokens must be a mapping")
    tokens = TokenFilter(
        whitelist=frozenset(addresses(tokens_raw.get("whitelist"), "tokens.whitelist")),
        blacklist=frozenset(addresses(tokens_raw.get("blacklist"), "tokens.blacklist")),
    )

    cow = parse_cow(raw, name)

    start_block = raw.get("start_block")
    if start_block is not None and (
        isinstance(start_block, bool) or not isinstance(start_block, int) or start_block < 0
    ):
        raise ConfigError(f"network {name!r}: start_block must be a non-negative integer")

    taker = raw.get("auction_taker")
    if taker is not None:
        taker = address(taker, f"network {name!r}: auction_taker")

    mode = _mode(raw.get("mode"), f"network {name!r}: mode", defaults["mode"])
    keeper_key_env = (
        _key_env(raw.get("keeper_key_env"), f"network {name!r}: keeper_key_env")
        or defaults["keeper_key_env"]
    )
    if mode == "keeper" and not keeper_key_env:
        raise ConfigError(f"network {name!r}: mode keeper needs keeper_key_env")
    native = parse_native(raw.get("native"), f"network {name!r}: native", defaults["native"])
    if native.enabled and taker is None:
        raise ConfigError(f"network {name!r}: native takes need auction_taker")

    return NetworkConfig(
        name=name,
        rpc=rpc,
        auctions=auctions,
        tokens=tokens,
        start_block=start_block,
        poll_interval=positive_int(
            raw.get("poll_interval"), "poll_interval", defaults["poll_interval"]
        ),
        order_ttl=positive_int(raw.get("order_ttl"), "order_ttl", defaults["order_ttl"]),
        log_chunk=positive_int(raw.get("log_chunk"), "log_chunk", defaults["log_chunk"]),
        mode=mode,
        keeper_key_env=keeper_key_env,
        pricing=parse_pricing(
            raw.get("pricing"), f"network {name!r}: pricing", defaults["pricing"]
        ),
        ttl=parse_ttl(raw.get("ttl"), f"network {name!r}: ttl", defaults["ttl"]),
        max_open_orders=positive_int(
            raw.get("max_open_orders"), "max_open_orders", defaults["max_open_orders"]
        ),
        native=native,
        cow=cow,
        auction_taker=taker,
    )


def parse_config(raw: dict[str, Any]) -> KeeperConfig:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a mapping")
    raw = expand_env(raw)
    defaults = {
        "poll_interval": positive_int(
            raw.get("poll_interval"), "poll_interval", DEFAULT_POLL_INTERVAL
        ),
        "order_ttl": positive_int(raw.get("order_ttl"), "order_ttl", DEFAULT_ORDER_TTL),
        "log_chunk": positive_int(raw.get("log_chunk"), "log_chunk", DEFAULT_LOG_CHUNK),
        "mode": _mode(raw.get("mode"), "mode", DEFAULT_MODE),
        "keeper_key_env": _key_env(raw.get("keeper_key_env"), "keeper_key_env"),
        "pricing": parse_pricing(raw.get("pricing"), "pricing", PricingSettings()),
        "ttl": parse_ttl(raw.get("ttl"), "ttl", TtlSettings()),
        "max_open_orders": positive_int(
            raw.get("max_open_orders"), "max_open_orders", DEFAULT_MAX_OPEN_ORDERS
        ),
        "native": parse_native(raw.get("native"), "native", NativeSettings()),
    }
    networks_raw = raw.get("networks")
    if not isinstance(networks_raw, dict) or not networks_raw:
        raise ConfigError("networks must be a non-empty mapping")
    networks = {
        name: parse_network(name, network_raw, defaults)
        for name, network_raw in networks_raw.items()
    }
    state_path = raw.get("state_path")
    if state_path is not None and (not isinstance(state_path, str) or not state_path):
        raise ConfigError("state_path must be a file path")
    return KeeperConfig(networks=networks, state_path=Path(state_path or DEFAULT_STATE_PATH))


def load_config(path: str | Path, *, dotenv: bool = True) -> KeeperConfig:
    if dotenv:
        load_dotenv(Path.home() / ".env", override=False)
        load_dotenv(override=False)
    with open(path) as handle:
        raw = yaml.safe_load(handle) or {}
    return parse_config(raw)


__all__ = [
    "DEFAULT_LOG_CHUNK",
    "DEFAULT_MAX_OPEN_ORDERS",
    "DEFAULT_MODE",
    "DEFAULT_ORDER_TTL",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_STATE_PATH",
    "MODES",
    "KeeperConfig",
    "NetworkConfig",
    "TokenFilter",
    "expand_env",
    "load_config",
    "parse_config",
    "parse_network",
]
