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
fixes `initial_amount`; the curve (`start_total`, `floor_total`, decay, step)
is read live, the active window is the epoch's calendar frame
(`epoch_bounds`), and partial fills do not resize the lot or restart its curve.

The auction core module itself is calendar-agnostic and stores no time
bounds: it tracks lots by opaque epoch ids supplied through the importing
contract's `_auction_epoch` hook, and every window or elapsed-time computation
goes through the `_epoch_bounds(epoch)` hook. The weekly schedule above is
this burner's choice — an epoch is its EXCHANGE window's start timestamp, read
from the FeeCollector calendar; independent contracts (the resolver, keepers)
read the same windows from the public `epoch_bounds(epoch)` view.

While the lot is active, its total target-token price follows a discrete
geometric decay:

```text
steps       = floor((timestamp - start) / step_duration)
total_price = max(floor_total, start_total * decay_factor^steps)
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
minimum amount, and maximum payment.

### Settlement rails: native take and registry adapters

`DutchAuctionBurner.vy` is the auction core, the shared `roles` module (owner
and emergency owner read live from the FeeCollector) and the `adapters` module:
refcounted executor approvals, the local adapter set, and the prefix-based
ERC-1271 router. External settlement protocols plug in through the
`AdapterRegistry`: the owner registers a verifier contract (the adapter's
identity and routing key) with its executor (the protocol contract that pulls
sold tokens), activates it, and enables it on the burner. Staging touches no
allowances: after a collect the keeper calls the permissionless
`sync_executor_approvals(executor, tokens)`, which drives each token's
allowance to max while the executor is referenced by an enabled adapter and to
zero once it is not — the same call is the repair and cleanup path.
Both switches are live — the local set and the registry flag — so either side
kills a rail instantly (owner or emergency owner).

### Adapter signature format

Every ERC-1271 signature the burner accepts follows one template
(`adapter_types.vy`):

```text
signature = verifier_address (20 bytes) ++ payload
```

A protocol settles an order by calling the burner's `isValidSignature(hash,
signature)`. The burner reads the verifier address from the prefix, requires it
to be enabled locally and active in the registry, and forwards `payload` to
that verifier's `isValidSignature(hash, payload)` with a staticcall, letting
its reverts bubble up. The payload is whatever the verifier's protocol needs to
rebuild the digest it is asked about: the verifier recomputes the digest from
the payload, compares it with `hash`, and only then prices the order fields
with the burner's `check_order` view. Signature bytes therefore carry no
authority — they only transport the order to the verifier — and every fill is
priced against the live curve at settlement time. Bytes without a known prefix
select no adapter and are invalid. The template is transparent to the
protocols themselves: for a contract signer they forward the signature bytes
verbatim (GPv2Settlement after stripping the owner, Permit2 for UniswapX-style
orders, 1inch LOP), so a publisher simply prepends the verifier address when
composing the bytes.

### CoW rail: `CowAdapter`

CoW Protocol is one such adapter. `cow/CowAdapter.vy` is a stateless,
immutable verifier pinned to the chain's `GPv2Settlement` (domain separator
and vault relayer are read from it at deploy) and to the `appData` every order
must carry; its registry entry is `verifier = CowAdapter, executor = vault
relayer`. An order is published to the CoW orderbook with signing scheme
`eip1271`, `from = burner`, and signature `CowAdapter ++ abi.encode(order)`;
`CowAdapter.order_for(burner, token)` returns exactly that order and signature
for a live lot, priced at the current block. GPv2Settlement strips the owner,
the burner strips the adapter prefix, and the adapter validates the bare order — digest, appData, zero fee, sell kind,
partially fillable, ERC-20 balances — before delegating the economics to
`check_order` (buy token is the target, receiver is the FeeCollector, amounts
within the snapshot, `buyAmount` at or above the live quote, `validTo` inside
the lot window). Publication is permissionless: anyone may post such an order
(a keeper, a solver, a third party) and nothing weaker than the live curve ever
settles. There is no ComposableCoW registration: a publisher re-posts orders
as the curve decays (an earlier order stays valid as a standing ask above the
curve).

Emergency procedure: `disable_adapter(CowAdapter)` (owner or emergency owner)
stops routing at once but leaves the relayer allowances in place, and the
contract cannot enumerate staged tokens on-chain; the multisig runbook batches
the disable with `sync_executor_approvals(relayer, tokens)` in one transaction
(`scripts/emergency_cow_disable.py` builds the calldata), and anyone can sync
stragglers afterwards. Because a lot's snapshot never caps the allowance,
tokens donated after the snapshot sell at or above the curve price in favor of
the FeeCollector; a CoW fill is invisible to the contract and only lowers the
balance, so cumulative sales per lot are path-dependent and can exceed the
snapshot when donations restore the balance — every extra unit still clears at
or above the curve price into the FeeCollector.

### Payment token and resync

The payment token (`want`, aliased `target()`) mirrors `fee_collector.target()`
but only follows it deliberately. If the FeeCollector migrates its target, the
burner freezes in place — staging reverts and every fill path (native take and
ERC-1271 validation) goes inactive — until the owner calls
`resync_target(expected_target, start_total, floor_total, decay_factor,
step_duration)` during the SLEEP phase, which re-reads the target from the
FeeCollector (never a parameter), asserts it equals the one the parameters
were tuned for, and revalidates the full curve against the EXCHANGE frame
exactly like the constructor. A target change fences out every previous
epoch's lot (`reconfigured_epoch`); trading resumes with the next staging, and
the old target itself becomes regular sellable inventory. Calling
`resync_target` with an unchanged target is a plain curve retune that reprices
live lots immediately.

### Resolver

Native permissionless settlement additionally has a view-only
`DutchAuctionResolver` that maps intent payloads onto `take` calldata without
holding funds or approvals; fillers of other protocols use the auction as a
liquidity venue this way, with no allowance or signature involved.

This burner and its modules target the Cancun EVM. Deployment requires a
Cancun-compatible chain because Vyper's global nonreentrancy lock uses
transient-storage opcodes.
