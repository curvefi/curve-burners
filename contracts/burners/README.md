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
in `adapters/cow/gpv2.vy` and the 1inch LOP v4 type hash and traits layout
in `adapters/oneinch/oneinch.vy` are keccak256-derived (the FeeTaker and
Fusion extension layouts live in `adapters/oneinch/feetaker.vy` and
`fusion.vy`). Peers (`CowAdapter`, the three 1inch adapters, the resolver)
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

### 1inch rail: `OneInchAdapter`, `OneInchFeeTakerAdapter`, `OneInchFusionAdapter`

The 1inch Limit Order Protocol v4 (hosted by `AggregationRouterV6`,
`0x111111125421cA6dc452d289314280a0f8842A65` on every chain 1inch supports,
Ethereum and Polygon included, with identical verified source) is the second
rail, built the same way, and comes as a family of three adapters: the
plain-order `OneInchAdapter`, and two that accept the order formats the
1inch off-chain feeds carry, `OneInchFeeTakerAdapter` (orders with the
standard `FeeTaker` extension, the only format the public orderbook API
relays) and `OneInchFusionAdapter` (Fusion orders settled through 1inch's
`SimpleSettlement`, itself a `FeeTaker` with a resolver auction). 1inch was
chosen over the other candidates (OKX intent swap, Bebop JAM) because the
settlement contracts are open, verified and unchanged across chains, a
contract maker is a first-class citizen on-chain
(`fillContractOrder[Args]` forwards the signature bytes verbatim to the
maker's ERC-1271 `isValidSignature` with the EIP-712 order digest), and any
taker may fill a plain order permissionlessly, so the rail works without an
off-chain feed. All three share one economic principle: **the signed
`takingAmount` is the floor the FeeCollector is paid for `makingAmount`**.
Without amount getters the protocol prices every fill linearly from the
order amounts, rounding the taking amount up; the FeeTaker fee gross-up and
the Fusion rate bump only raise what the taker pays, and the custom
receiver gets at least the linear base in every branch and at every
rounding (see below), so each adapter validates the shape of its extension
and then hands the same tuple to the auction's `check_order` — taker asset
is the target, the resolved receiver is the FeeCollector, making amount
within the snapshot, taking amount at or above the live quote, the 40-bit
`makerTraits` expiration no later than the lot end (a never-expiring order
fails it) — exactly like a plain order. The bump curve is not replicated on
chain: it depends on `block.basefee` and only adds surplus.

Shared mechanics. Every 1inch adapter is stateless and immutable and pinned
to the chain's router: at deploy it rebuilds the EIP-712 domain from the
router's ERC-5267 `eip712Domain()` and cross-checks it against the router's
own `hashOrder` on a probe order, so a wrong address, a proxy with another
domain or a different order type fails the deployment. The registry entry
of each is `adapter = <adapter>, executor = router`, because a fill pulls
the maker asset with a plain `transferFrom` from the maker: the router is
what `sync_executor_approvals(router, tokens)` approves (orders with the
`USE_PERMIT2` trait are rejected, so Permit2 is never involved), and the
executor reference count keeps
the allowance while any of the three is active. A taker calls
`router.fillContractOrder(order, sig, amount, takerTraits)` (or
`fillContractOrderArgs` with the extension in `args`) with `sig = <adapter>
++ abi.encode(order, extension)`; the router forwards `sig` to the burner's
`isValidSignature` with the order digest, the burner strips the adapter
prefix, and the adapter recomputes the digest from the bare pair
(`adapters/oneinch/oneinch.vy` holds the order type, the type hash, the
`makerTraits` bit layout and the extension binding and field layout of
`OrderLib`/`ExtensionLib`) and runs the checks all three share — canonical
encoding, the burner as maker, neither `USE_PERMIT2` nor `UNWRAP_WETH`,
the extension bound to the order exactly as the protocol binds it (flag and
salt) and carrying no asset suffixes (`NonCanonical`, `InvalidHash`,
`BadMaker`, `BadMakerTraits`, `MissingOrderExtension`,
`UnexpectedOrderExtension`, `InvalidExtensionHash`, `BadExtensionOffsets`,
`BadAssetSuffix`). `OneInchAdapter` hosts these checks and the lot quoting
as internals, and the two fee-taking adapters import it as a module
(`initializes: base`, exporting its `limit_order_protocol` and
`domain_separator`), adding their extension policy on top. Shape failures
revert with `oneinch.OrderNotValid(reason)`; the auction's typed
`check_order` revert bubbles unchanged through the burner. The router
itself, however, swallows every revert of a contract signature check into
its own `BadSignature()`, so a publisher classifies an order by `eth_call`
against the burner's `isValidSignature`, not against a fill. Two protocol
facts shape every 1inch rail. The router checks a contract signature on
the first fill of an order only: a partially filled order keeps filling at
its signed unit price (at or above the curve at validation time, which
only decays) until it expires, without asking the burner again — fine
economically, but it means that disabling an adapter does not stop the
remainder of an already started order; only the allowance does. And a
published order is a standing ask like the CoW one: the burner keeps
accepting it as the curve decays below it, and a publisher re-posts lower
with a fresh salt (the router tracks fills per order hash). 1inch is not
deployed on Fraxtal, so this rail is for Polygon and Ethereum.

#### Plain orders: `OneInchAdapter`

`adapters/oneinch/OneInchAdapter.vy` accepts orders without amount getters
(predicates, maker permits and interactions only restrict or decorate a
fill and pass through; getters are `BadAmountGetter`), so the protocol
prices every fill linearly from the order amounts and the resolved receiver
(an empty one means the maker) must be the FeeCollector.
`OneInchAdapter.order_for(burner, token, salt, sell_amount)` returns the
publishable pair for a live lot: a plain order (no extension) for the whole
available amount by default, priced at the current block, open to every
taker, partially and multiply fillable, expiring at the lot end. Plain
orders are not what the 1inch off-chain feeds carry, so a publisher hands
them to takers directly (or through its own feed); the on-chain fill is
permissionless.

#### Orderbook orders: `OneInchFeeTakerAdapter`

The public 1inch orderbook (`POST /orderbook/v4.0/{chain}`, Developer
Portal key required) relays only orders carrying the standard FeeTaker
extension: `contracts/extensions/FeeTaker.sol` with
`AmountGetterWithFee.sol` in 1inch/limit-order-protocol, deployed at
`0xc0DFdB9E7a392c3dBBE7c6FBe8FBC1789C9FE05e` on Ethereum and Polygon (the
verified source is identical on both chains and matches master up to the
`orderHash` plumbing added in 2026-08, which does not touch the layout;
`GET /orderbook/v4.0/{chain}/fee-info` returns it as `extensionAddress`
together with the fees, the protocol fee receiver and the resolver
whitelist). `adapters/oneinch/feetaker.vy` pins the layout. How the
FeeTaker works: the amount getters gross the linear taking amount up by
`(1e5 + integratorFee + resolverFee) / 1e5` (rounded up) and scale the
making amount down by the same factor (rounded down), with the resolver
fee discounted for whitelisted takers; `postInteraction`, which the router
calls only when the `makerTraits` ask for it and which splits only when
`order.receiver` is the FeeTaker itself (else `InconsistentFee` for any
fee), keeps `floor(T * fee / denominator)` per fee for the integrator and
protocol receivers and sends the rest to the custom receiver (flag `0x01`,
20 bytes after the two fee receivers) or to the maker; and it refuses
takers that are neither whitelisted nor holders of the Resolver Access
Token (`0xAcce5500000f71A32B5E5514D1577E14b7aacC4a`): orderbook orders
are filled by 1inch resolvers, not by anyone. Receiver floor: for a fill
of making amount `m` the taker pays `T >= ceil(T0 * m / M0) * D / 1e5`
with `D = 1e5 + fees`, the receiver gets `T - floor(T * iF / D) - floor(T *
rF / D) >= T * 1e5 / D >= ceil(T0 * m / M0)`, the linear base, with no
wei-level leak (the base is an integer); taking-amount fills deliver fewer
maker tokens for the same payment. The floor holds only if the getters and
the post-interaction read the same fees for the same taker, so the adapter
requires identical fee bytes and whitelists on both sides.
`OneInchFeeTakerAdapter.vy` checks, after the shared ones: the order
receiver is the FeeTaker (`BadOrderReceiver`), the post-interaction flag is
set and the pre-interaction flag is not (`BadMakerTraits`), no
pre-interaction data (`BadPreInteraction`), both amount getters target the
pinned FeeTaker with identical extraData (`BadAmountGetter`), the
post-interaction targets it (`BadPostInteraction`), the fee data parses
with the getter's bounds and is the same on both sides (`BadFeeData`), the
whitelists parse and list the same takers (`BadWhitelist`), and neither
side has a tail (a tail of 20 bytes or more chains another amount getter
or `IPostInteraction`: `BadGetterTail`, `BadInteractionTail`). The custom
receiver (the maker when none is flagged) is handed to `check_order`,
which rejects anything but the FeeCollector. Fee values, fee receivers and
whitelists are not bounded: the API dictates them and they are the taker's
cost; `describe(extension)` returns them for publishers.

Keeper flow (orderbook): `GET fee-info` for the pair and amounts, build the
extension with the SDK (`@1inch/limit-order-sdk`:
`FeeTakerExtension.new(extensionAddress, fees, whitelist, {customReceiver:
FeeCollector}).build().encode()`, or `sdk.createOrder` with `receiver =
FeeCollector`; the Python encoders in `tests/burners/test_feetaker_module.py`
reproduce the SDK bytes), then
`OneInchFeeTakerAdapter.order_for(burner, token, salt, sell_amount,
extension)` returns the order (receiver = FeeTaker, amounts at the live
quote, partial and multiple fills, post-interaction call, expiring at the
lot end, salt bound like `LimitOrder.buildSalt`) and the signature
`adapter ++ abi.encode(order, extension)`; submit `POST
/orderbook/v4.0/{chain}` with `orderHash, signature, data{...order,
extension}`. The SDK's own order over the same extension validates too.

#### Fusion orders: `OneInchFusionAdapter`

Fusion orders are settled through `contracts/SimpleSettlement.sol` in
1inch/limit-order-settlement (`is FeeTaker`), deployed at
`0x2Ad5004c60e16E54d5007C80CE329Adde5B51Ef5` on Ethereum (as `Settlement`,
the same contract with a priority-fee cap on fills relative to
`block.basefee`) and Polygon (`SimpleSettlement`); the verified source is
identical to master up to the pragma on both chains, and the quote API
returns the address as `settlementAddress`. `adapters/oneinch/fusion.vy`
pins what it adds to the FeeTaker layout: the getter extraData opens with
the auction details (`bytes3 gasBumpEstimate, bytes4 gasPriceEstimate,
bytes4 auctionStartTime, bytes3 auctionDuration, bytes3 initialRateBump,
uint8 N, (bytes3 rateBump, bytes2 timeDelta)[N]`) before the FeeTaker fee
data and getter whitelist; the post-interaction reads the whitelist as
`bytes4 allowedTime, uint8 N, (bytes10 addressHalf, bytes2 timeDelta)[N]`
(the halves of the getter whitelist, with resolver timing) and then a
surplus block (`bytes32 estimatedTakingAmount, uint8 protocolSurplusFee` in
1e2). Economics: the getter applies `rateBump = max(auctionBump -
gasBump, 0)` after the fee gross-up, taking amount up and making amount
down by `(1e7 + rateBump) / 1e7`, so no fill pays less than the FeeTaker
amount; after the auction ends the bump is zero and the order stands at
its linear price until it expires. The surplus fee takes at most the whole
excess of the net taking amount over the estimate scaled to the fill
(rounded up), and the settlement requires the estimate to be at least the
order's taking amount (`InvalidEstimatedTakingAmount`), so the receiver
still gets at least the linear base. `OneInchFusionAdapter.vy` runs the
FeeTaker checks against the pinned settlement, with the post whitelist in
its Fusion shape, plus: the auction details parse (`BadAuctionDetails`),
the auction ends no later than the order expiration
(`AuctionPastExpiration`; afterwards the standing ask is the floor, which
is fine), the surplus block closes the post-interaction with a fee of at
most 100% (`BadSurplus`), and the estimate covers the order's taking amount
(`InvalidEstimatedTakingAmount`, mirrored so publishers get a typed
reason). Allowed times and delays are the settlement's concern (a fill
before them reverts `AllowedTimeViolation`), and on Ethereum so is the
priority-fee cap; `describe(extension)` returns the fees, receivers,
whitelist size, auction details and surplus block.

Keeper flow (Fusion): `POST /fusion/quoter/v2.0/{chain}/quote/receive` with
`walletAddress = burner`, the pair and amount, and a `customPreset` whose
`auctionEndAmount` is our quote at post time and `auctionStartAmount` above
it (the auction must end by the lot end; the SDK adds a 12-second
expiration delay after it, so start it early enough); the quote carries
the settlement address (must equal the pinned one), the resolver whitelist,
the fees and the surplus estimate, and
`quote.createFusionOrder({receiver: FeeCollector, preset: custom})` builds
the order and `order.extension.encode()`; then either sign the SDK's order
as is (`adapter ++ abi.encode(order, extension)`; its traits and salt are
what the adapter expects) or feed the extension to
`OneInchFusionAdapter.order_for(burner, token, salt, sell_amount,
extension)` for an order priced at the current block, and submit `POST
/fusion/orders/v2.0/{chain}/order/submit` with `order, signature, quoteId,
extension`. Both feeds need a Developer Portal key. Open question, not
verifiable here (no key in this environment): whether the 1inch backends
accept a non-65-byte ERC-1271 signature from a contract maker; the known
datapoint is that Safe's 130-byte signatures work on Fusion.

Limitations shared by the two fee-taking rails: only whitelisted resolvers
and access-token holders can fill, so these rails are feeds for 1inch's
resolver network rather than permissionless venues; the extension decoding
bound (`oneinch.MAX_EXTENSION`, 2048 bytes) covers a Fusion extension with
a few dozen whitelisted resolvers; the taking tokens pass through the
extension contract within the fill transaction, whose verified layout the
adapters trust (each deploy probes its extension's `getTakingAmount` on a
known vector, so a FeeTaker pinned as a settlement or vice versa fails the
deployment); and fee receivers are whatever the publisher encoded — the
API dictates them for relayed orders, and they never reduce what the
FeeCollector gets below the linear base.

Emergency procedure is the CoW one with `executor = router`, for all three
adapters: `registry.disable_adapter(<adapter>)` batched with
`sync_executor_approvals(router, tokens)` (`scripts/emergency_cow_disable.py`
takes the executor as an argument). Here the allowance sync is not optional
hygiene but the step that actually stops fills of orders validated before
the disable (the first-fill-only signature check applies to all three), as
the mainnet fork tests demonstrate; and because the three share the router
as executor, the allowance only clears once the last of them is disabled.

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
