Burners maintain exchanging coins into _target_ and needed actions at the time of collect (like registering a new coin).  
Note: latest StableSwap and CryptoSwap implementations send fees automatically


## XYZBurner
Basically a template Burner, that allows to collect coins with associated payout.


## CowSwapBurner
Using `ComposableCow` to post orders into CowSwap.
Coins are priced via CowSwap solvers' internal auction.


## DutchAuctionBurner

During `FeeCollector`'s COLLECT phase, each token's full burner balance is
snapshotted as the lot for the upcoming calendar EXCHANGE week. The snapshot
fixes `initial_amount`, `start_total`, `floor_total`, and the auction time
frame; partial fills do not resize the lot or restart its curve.

While the lot is active, its total target-token price follows a discrete
geometric decay:

```text
steps       = floor((timestamp - start) / step_duration)
total_price = max(floor_total, ceil(start_total * decay_factor^steps))
payment     = ceil(total_price * amount / initial_amount)
```

The total price therefore starts at `start_total`, steps down toward the hard
`floor_total`, and becomes inactive at the EXCHANGE end. `price(from)` is the
corresponding upward-rounded WAD unit quote; `getAmountNeeded(from, amount)` is
the canonical exact raw-token payment quote.

Native settlement exposes the Yearn-compatible `want`, `available`, `price`,
`getAmountNeeded`, and `take` selectors, including the optional atomic taker
callback. `take_with_limits` additionally binds inclusion to a deadline,
expected week, minimum amount, and maximum payment.

CoW integration starts unconfigured and disabled. The owner calls
`configure_cow` while disabled and then `enable_cow`; each reconfiguration
increments the generation so stale registrations cannot validate. Tokens are
registered for the current generation during COLLECT. The Vault Relayer gets a
finite lot-sized allowance, and native fills reduce the same allowance that
CoW fills consume, making it a shared settlement budget. Retired relayer
allowances can be explicitly revoked.

This burner and its modules target the Cancun EVM. Deployment requires a
Cancun-compatible chain because Vyper's global nonreentrancy lock uses
transient-storage opcodes.
