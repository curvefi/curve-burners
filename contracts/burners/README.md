Burners maintain exchanging coins into _target_ and needed actions at the time of collect (like registering a new coin).  
Note: latest StableSwap and CryptoSwap implementations send fees automatically


## XYZBurner
Basically a template Burner, that allows to collect coins with associated payout.


## DutchAuctionBurner

During `FeeCollector`'s COLLECT phase, each token's full burner balance is
snapshotted as the lot for the upcoming calendar EXCHANGE week. The snapshot
fixes `initial_amount`; the curve (`start_total`, `floor_total`,
`step_duration`) is read live, the active window is the EXCHANGE frame read
from the FeeCollector calendar, and partial fills do not resize the lot or
restart its curve. `lots(token)` is the plain record `(staged_at,
initial_amount)` and `window(token)` its window (`window(token, timestamp)`
answers for a hypothetical staging time; a never-staged token reads
`(0, 0)`). Every window is
`auction_length` long (the EXCHANGE frame length for this burner); the
calendar hook only places its start, so a per-token slot inside the frame
would be a burner-only change.

While the lot is active, its total target-token price follows a discrete
exponential decay: equal time buys the same percentage drop, so
`100 000 -> 1` passes `10 000 -> 1 000 -> 100 -> 10` at each successive fifth
of the decay.

```text
decay_steps  = (auction_length - 1) // step_duration
step         = (timestamp - start) // step_duration
total_price  = start_total                         if step == 0
             = floor_total                         if step >= decay_steps
             = exp(log_start - log_drop * step / decay_steps)   otherwise,
               clamped into [floor_total, start_total]
payment      = ceil(total_price * amount / initial_amount)
```

`log_start = ln(start_total)` and `log_drop = ln(start_total / floor_total)`
(WAD fixed point) are prepared internally at deploy and on every resync;
the public parameters determine them, so any quote can be reproduced
off-chain with the same arithmetic (`scripts/dutch_auction_curve.py` is the
bit-exact Python mirror). The endpoints are exact by explicit branches: the price is
`start_total` during the first step and `floor_total` from the last active
step on (23:59 for a one-day frame with minute steps). `ln`/`exp` come from
snekmate's `math` module (the dependency pinned in `requirements.in`),
property-tested here against a high-precision reference.
`price(from)` is the corresponding upward-rounded 1e18-precision unit quote
(raw want per 1e18 raw units of `from`); `getAmountNeeded(from, amount)` is
the canonical exact raw-token payment quote.

The native surface (`interfaces/IDutchAuction.vyi`) is `want`, `receiver`,
the curve parameters, `auction_length`, `lots`, `window`, `available`, `price`, `getAmountNeeded`, `take`
(with the optional atomic taker callback), `take_with_limits` (inclusion
bound to a deadline, minimum amount, and maximum payment) and `check_order`.
Quotes and `take` carry Yearn Auction's selector names with every Yearn
overload; the Yearn-only views `isActive`, `auctionLength`, and `auctions`
live in the separate `auction/yearn_auction.vy` module
(`interfaces/IYearnAuction.vyi`), which the burner imports and exports and
which can be dropped without touching the core. `auctions().scaler = 1`
matches the raw 1e18-precision `price`, so Yearn's `amount * scaler * price /
1e18` reproduces `getAmountNeeded` up to rounding.

### Settlement rails: native take and registry adapters

`DutchAuctionBurner.vy` composes the auction core with the shared utility
modules: `roles` (the burner exposes only the owner, read live from the
FeeCollector; it has no emergency role of its own), `adapters` (the
prefix-based ERC-1271 router and the permissionless executor allowance sync)
and `recovery`. The burner holds no adapter state of its own — management
lives entirely in the `AdapterRegistry` (`adapters/AdapterRegistry.vy`),
which the burner reads live (the registry is a required deployment
dependency; an empty one means native settlement only — every signature is
invalid). External settlement
protocols plug in through it: the owner registers an adapter contract (any
rail selling inventory by its own rules; signatures prefixed with its address
reach it through the router) with its executor (the contract that pulls sold
tokens, often the adapter itself) via `set_adapter(adapter,
executor)` — an inactive adapter can be repointed at a new executor, and
`get_adapters()` lists every registered one — and switches it on with a
separate `activate_adapter(adapter)`, after which the public
`is_executor_active(executor)` answers true. Staging touches no allowances:
after a collect the keeper calls the burner's permissionless
`sync_executor_approvals(executor, tokens)`, which drives each token's
allowance to max while `registry.is_executor_active(executor)` holds and to
zero once it does not — the same call is the repair and cleanup path. There
is a single switch per adapter, the registry flag: `disable_adapter(adapter)`
(registry owner or emergency owner) clears it, releases the executor
reference, and kills the rail at once for every auction reading the registry;
only the owner can activate again.

Types and constants are shared through interfaces: the `Lot` record
lives in `interfaces/IDutchAuction.vyi`, Yearn's `AuctionInfo` in
`interfaces/IYearnAuction.vyi`, `AdapterConfig` in
`interfaces/IAdapterRegistry.vyi`, and the ERC-165 ids, the burner interface
id, and the ERC-1271 magic value in `utils/constants.vy`; the GPv2 constants
in `adapters/cow/gpv2.vy` are keccak256-derived. Peers (`CowAdapter`, the resolver)
read the burner through `IDutchAuction` alone.

### Adapter signature format

Every ERC-1271 signature the burner accepts follows one template (documented
in the `adapters` module):

```text
signature = adapter_address (20 bytes) ++ payload
```

A protocol settles an order by calling the burner's `isValidSignature(hash,
signature)`; the signature argument is unbounded bytes, so payload size is the
adapter's concern. The burner reads the adapter address from the prefix,
requires `registry.get_adapter(adapter).active`, and forwards `payload` to
that adapter's `isValidSignature(hash, payload)` with a staticcall, letting
its reverts bubble up. The payload is whatever the adapter's protocol needs to
rebuild the digest it is asked about: the adapter recomputes the digest from
the payload, compares it with `hash`, and only then prices the order fields
with the burner's `check_order` view. `check_order` returns `true` for a
fillable order and otherwise reverts with a typed auction error
(`BadBuyToken`, `BadReceiver`, `LotInactive`, `NothingAvailable`,
`BadSellAmount`, `BadValidTo`, `BadBuyAmount`), so an `eth_call` classifies
an order by error selector without decoding a reason string. Signature bytes
therefore carry no authority — they only transport the order to the adapter —
and every fill is priced against the live curve at settlement time. Bytes
without a known prefix select no adapter and are invalid. The template is
transparent to the protocols themselves: for a contract signer they forward
the signature bytes verbatim (GPv2Settlement after stripping the owner), so a
publisher simply prepends the adapter address when composing the bytes.

### CoW rail: `CowAdapter`

CoW Protocol is one such adapter. `adapters/cow/CowAdapter.vy` is a stateless, immutable
adapter pinned to the chain's `GPv2Settlement` (domain separator and vault
relayer are read from it at deploy) and to the `appData` every order must
carry; its registry entry is `adapter = CowAdapter, executor = vault relayer`.
An order is published to the CoW orderbook with signing scheme `eip1271`, `from
= burner`, and signature `CowAdapter ++ abi.encode(order)`;
`CowAdapter.order_for(burner, token, sell_amount)` returns exactly that
order and signature for a live lot, priced at the current block
(`sell_amount = 0` sells everything available). GPv2Settlement strips the
owner, the burner strips the adapter prefix, and the adapter validates the
bare order — digest, appData, zero fee, sell kind, partially fillable, ERC-20
balances — before delegating the economics to `check_order` (buy token is the
target, receiver is the FeeCollector, amounts within the snapshot, `buyAmount`
at or above the live quote, `validTo` no later than the lot end). Protocol
checks revert with `gpv2.OrderNotValid(reason)` (`NonCanonical`,
`InvalidHash`, `BadAppData`, `BadOrderFlags`, `BadBalanceMode`); the auction's
typed `check_order` revert bubbles unchanged, so a single `eth_call` against
`isValidSignature` tells a publisher exactly why an order is not fillable.
Publication is permissionless: anyone may post such an order (a keeper, a
solver, a third party) and nothing weaker than the live curve ever settles. A
publisher re-posts orders as the curve decays (an earlier order stays valid as
a standing ask above the curve).

Emergency procedure: `registry.disable_adapter(CowAdapter)` (owner or
emergency owner) stops routing at once but leaves the relayer allowances in
place, and the burner cannot enumerate staged tokens on-chain; the multisig
runbook batches the registry disable with the burner's
`sync_executor_approvals(relayer, tokens)` in one transaction
(`scripts/emergency_cow_disable.py <registry> <burner> <adapter> <executor>
<token>...` builds both `(target, calldata)` pairs), and anyone can sync
stragglers afterwards. `available` never exceeds the snapshot; a balance
restored by donations sells again along the same curve, always in the
FeeCollector's favor.

### Payment token and resync

The payment token (`want`, no `target()` alias) mirrors
`fee_collector.target()` but only follows it deliberately. If the FeeCollector
migrates its target, the
burner freezes in place — staging reverts and every fill path (native take and
ERC-1271 validation) goes inactive — until the owner calls
`resync_target(expected_target, start_total, floor_total, step_duration)`
during the SLEEP phase, which re-reads the target from the FeeCollector
(never a parameter), asserts it equals the one the parameters were tuned
for, and solves the curve against the EXCHANGE frame exactly like the
constructor. Every resync stales every lot staged up to its block (staging
counts from the next block); trading resumes with the next staging, and after
a target change the old
target itself becomes regular sellable inventory. Calling `resync_target`
with an unchanged target is a plain curve retune under the same fence: the
retuned curve applies from the next staging, never to a live lot. The
proceeds receiver is mutable in the core module for other importers; the
burner pins it to the FeeCollector and exposes no setter.

### Roles and recovery

The owner configures — `resync_target`, the registry's
`set_adapter`/`activate_adapter` — and is the only one who can
`recover(coins)`, which moves ERC-20 or native balances solely back to the
FeeCollector (an evacuation batches it with `FeeCollector.set_killed`, or a
permissionless collect in the same COLLECT frame restages the token). The
FeeCollector's emergency owner acts on the burner only through the
FeeCollector kill masks and `registry.disable_adapter`. `push_target` stays
permissionless and returns held target tokens to the FeeCollector.

### Periphery: resolver and taker

`auction/periphery/` holds two stateless helpers for the native rail.
`DutchAuctionResolver.vy` is a view-only intent resolver that maps intent
payloads onto `take` calldata without holding funds or approvals; fillers of
other protocols use the auction as a liquidity venue this way, with no
allowance or signature involved. `AuctionTaker.vy` is an example filler: it
takes a lot, runs a caller-supplied route (e.g. an aggregator swap of the lot
into `want`) inside the take callback, pays the quote from the proceeds, and
forwards the profit in one transaction, custodying nothing between calls.

This burner and its modules target the Cancun EVM. Deployment requires a
Cancun-compatible chain because Vyper's global nonreentrancy lock uses
transient-storage opcodes.
