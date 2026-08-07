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
fixes `initial_amount`, `start_total`, and `floor_total`; the active window is
the epoch's calendar frame (`epoch_bounds`), and partial fills do not resize
the lot or restart its curve.

The auction core module itself is calendar-agnostic and stores no time
bounds: it tracks lots by opaque epoch numbers supplied through the importing
contract's `_auction_epoch` hook, and every window or elapsed-time computation
goes through the `_epoch_bounds(epoch)` hook. The weekly schedule above is
this burner's choice — it numbers epochs by the EXCHANGE frame's calendar week
and derives each epoch's window arithmetically from frame offsets pinned at
deployment; independent contracts (the watchtower handler, the resolver,
keepers) read the same windows from the public `epoch_bounds(epoch)` view.
Router approvals and the ERC-1271 envelope dispatcher live in a separate
`adapters` module wired to the core through burner-level hooks.

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

Native settlement exposes a minimal selector subset compatible with Yearn
Auction — `want`, `available`, `price`, `getAmountNeeded`, and `take` —
including the optional atomic taker callback. Yearn's overloads and the
`isActive`/`kicked`/`auctions` views are absent, and `price` is a WAD unit
quote over raw token amounts rather than Yearn's decimal-normalized price, so
Yearn tooling must quote through `getAmountNeeded` and cannot be assumed to
work unchanged. `take_with_limits` additionally binds inclusion to a deadline,
expected week, minimum amount, and maximum payment.

CoW integration starts unconfigured and disabled. The owner calls
`configure_cow(settlement, composable_cow, handler)` while disabled and then
`enable_cow`; the vault relayer and EIP-712 domain separator are read from
the settlement on-chain. The execution rail is direct, Yearn-auction style:
anyone may publish a canonical GPv2 order for an active lot to the CoW
orderbook (signing scheme eip1271, signature = the abi-encoded order), and
the burner validates it at settlement purely against live lot economics — no
registration required. Watchtower automation is a separate concern: the
standalone `CowWatchtowerHandler` contract implements the ComposableCoW
generator interface by reading the burner's public views, while the burner
only registers conditional orders pointing at it during COLLECT (bumping a
generation per reconfiguration so discovery re-creates orders). The
ComposableCoW wrapper is transport, never authority: watchtower-published
signatures are unwrapped and pass through the same economic checks.

Router allowances are refcounted, not budgeted: while a router (the CoW Vault
Relayer or Permit2) is referenced by an enabled rail, staging drives the
token's allowance to max; once the last reference is released the target drops
to zero. The approval pass inside `burn` is best-effort — a token whose
`approve` fails keeps its lot and native `take` path, and the permissionless
`sync_router_approvals` retries the grant or clears retired routers. Emergency
procedure: `disable_cow` (and `disable_adapter`) only flip the rail off and
release the router reference — allowances are cleared by the separate
permissionless sync, and the contract cannot enumerate staged tokens on-chain.
The emergency owner is a multisig, so the approved runbook batches the disable
with `sync_router_approvals(router, tokens)` in one transaction (no allowance
window); `scripts/emergency_cow_disable.py` builds that calldata bundle, and
anyone can sync stragglers afterwards. Because a
lot's snapshot no longer caps the allowance, tokens donated after the snapshot
sell at or above the curve price in favor of the FeeCollector. There is no
shared budget across rails — a CoW fill is invisible to the contract and only
lowers the balance, so cumulative sales per lot are path-dependent and can
exceed the snapshot when donations restore the balance; every extra unit still
clears at or above the curve price into the FeeCollector.

The payment token (`want`, aliased `target()`) mirrors `fee_collector.target()`
but only follows it deliberately. If the FeeCollector migrates its target, the
burner freezes in place — staging reverts and every fill path (native take and
ERC-1271 validation) goes inactive — until the owner calls
`resync_target(start_total, floor_total, decay_factor, step_duration)`, which
re-reads the target from the FeeCollector (it is never a parameter) and
revalidates the full curve against the EXCHANGE frame exactly like the
constructor. Because lots snapshot their curve in the old denomination, a
target change fences out every lot of the current epoch (`reconfigured_epoch`);
trading resumes with the next epoch's staging, and the old target itself
becomes regular sellable inventory. Calling `resync_target` with an unchanged
target is a plain curve retune: live lots keep their snapshots and only future
stagings pick up the new parameters.

Beyond the embedded ComposableCoW flow, external settlement rails plug in
through the `AdapterRegistry`: the owner enables an adapter id on the burner,
and the always-on ERC-1271 dispatcher validates versioned envelope signatures
against the registry's pinned validator code hash plus the live lot state.
The registry stays authoritative: the burner only caches the adapter's
resolved router, signatures are rejected while that cache disagrees with the
live config (e.g. after a registry version update changes the authorization
mode), and the permissionless `refresh_adapter_router` realigns the cache and
router refcounts. Either side — burner or registry — can disable an adapter
instantly (owner or emergency owner). Native permissionless settlement additionally has a
view-only `DutchAuctionResolver` that maps intent payloads onto `take`
calldata without holding funds or approvals.

This burner and its modules target the Cancun EVM. Deployment requires a
Cancun-compatible chain because Vyper's global nonreentrancy lock uses
transient-storage opcodes.
