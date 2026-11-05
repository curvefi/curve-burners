"""What every config section shares: the error type and the primitive
validators the section parsers are built from."""

from __future__ import annotations

from typing import Any

from eth_utils import is_address, to_checksum_address


class ConfigError(ValueError):
    pass


def addresses(values: Any, name: str) -> tuple[str, ...]:
    """A list of addresses, checksummed; ``None`` is empty."""
    if values is None:
        return ()
    if not isinstance(values, list):
        raise ConfigError(f"{name} must be a list of addresses")
    out = []
    for value in values:
        if not isinstance(value, str) or not is_address(value):
            raise ConfigError(f"{name}: {value!r} is not an address")
        out.append(to_checksum_address(value))
    return tuple(out)


def address(value: Any, name: str) -> str:
    (checksummed,) = addresses([value], name)
    return checksummed


def positive_int(value: Any, name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def fraction(value: Any, name: str, default: float) -> float:
    """A number in [0, 1)."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value < 1:
        raise ConfigError(f"{name} must be a fraction in [0, 1)")
    return float(value)


def non_empty_string(value: Any, name: str, default: str | None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} must be a non-empty string")
    return value


__all__ = [
    "ConfigError",
    "address",
    "addresses",
    "fraction",
    "non_empty_string",
    "positive_int",
]
