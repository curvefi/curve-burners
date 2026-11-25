"""The priority fee of a take: a share of what the take earns.

A take competes with other takers for the same lot in the same block, and
the block builder orders them by tip. The node's suggested tip says what an
ordinary transaction pays, which next to a contested lot is nothing. So the
tip of a take is sized by the take itself (``fees`` section):

    budget  = profit_share * profit, converted from want to the native token
    tip     = (budget - gas * base_fee) / gas          (never below zero)
    tip     = max(priority_floor, min(priority_cap, tip))

``profit`` is ``route_dy - payment`` of the chunk, ``gas`` the estimate of
the take. The base fee is paid whatever the tip, so it comes out of the
budget first: a wider margin buys a higher tip, and a take whose share of
the profit does not even cover the base fee tips nothing. The keeper thus
never spends more on gas than ``profit_share`` of what it earns, unless the
owner sets a floor, which is paid regardless. With no price for the native
token (none configured, no source knows it) the tip is the node's
suggestion, inside the same floor and cap.

Pure arithmetic: integers in, wei out.
"""

from __future__ import annotations

from keepers.auction_keeper.config import FeeSettings

GWEI = 10**9


def take_priority_fee(
    settings: FeeSettings,
    *,
    profit: int,
    want_unit: int,
    native_price: float | None,
    gas: int,
    base_fee: int,
    suggested: int,
) -> int:
    """The tip per gas (wei) of a take. ``profit`` in want wei, ``want_unit``
    10**decimals of want, ``native_price`` want tokens per native token,
    ``suggested`` the node's own tip."""
    floor = int(settings.priority_floor * GWEI)
    cap = max(floor, int(settings.priority_cap * GWEI))
    if native_price is None or native_price <= 0 or gas <= 0:
        tip = suggested
    else:
        budget = int(settings.profit_share * profit / want_unit / native_price * 10**18)
        tip = max(0, budget - gas * base_fee) // gas
    return max(floor, min(cap, tip))


__all__ = ["GWEI", "take_priority_fee"]
