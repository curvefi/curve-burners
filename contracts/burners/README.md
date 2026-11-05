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
in `adapters/cow/gpv2.vy` and the JAM order type hash and partnerInfo layout
in `adapters/bebop/jam.vy` are keccak256-derived. Peers (`CowAdapter`,
`BebopJamAdapter`, the resolver) read the burner through `IDutchAuction`
alone.

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

### Bebop rail: `BebopJamAdapter`

Bebop JAM v2 (bebop-dex/bebop-jam-contracts, deployed as `JamSettlement` at
`0xbeb0b0623f66bE8cE162EbDfA2ec543A522F4ea6` with its `JamBalanceManager` at
`0xC5a350853E4e36b73EB0C24aaA4b8816C9A3579a` on every chain Bebop supports
but zkSync Era, Ethereum and Polygon included; the verified source is the
same on both, solc 0.8.27, and the API's `settlementAddress` and
`approvalTarget` name them) is the next rail, built the same way. JAM is a
solver auction: a taker (here the burner, the one selling) signs a
`JamOrder`, the winning solver settles it on-chain. Only JAM is covered;
Bebop's RFQ-M/RFQA `BebopSettlement` (`0xbbbbbBB520d69a9775E85b458C58c648259FAD5F`,
market-maker orders settled through Permit2 witnesses) is a separate
contract with its own order types and is not implemented. Economic
principle, verified against the settlement source: **the signed `buyAmount`
is the floor the FeeCollector is paid for the signed `sellAmount`**. Every
JAM order is fill-or-kill: `settle` pulls the whole `sellAmount` from the
taker through the balance manager, runs the solver's interactions, and
hands the receiver the settlement's whole balance of the buy token after
partner and protocol fees, requiring it to be at least `buyAmount`
(`InvalidOutputBalance` otherwise); `settleInternal` (a maker paying from
its own balance) and `settleBatch` enforce the same floor after fees; the
nonce is burnt on the fill, so no order settles twice and there is no
partial-fill state to reason about. Fees (`partnerInfo`) therefore only
reduce the solver's surplus above the floor and pass through.

`adapters/bebop/jam.vy` holds the signed order type (the twelve EIP-712
fields with `hooksHash` last — exactly the `toSign` object the JAM API
returns; the protocol's unsigned `usingPermit2` flag is not part of it),
the type hash, the domain (name `JamSettlement`, version `2`), the
hashStruct with arrays hashed as packed words (`JamOrderLib.hash`), the
protocol's calldata shape, and the `partnerInfo` packing of `JamPartner`
(`[partner address | partner fee bps | protocol fee bps]`, fees below 100%,
a partner address only with a partner fee). `BebopJamAdapter.vy` is
stateless and immutable and pinned to the chain's settlement: at deploy it
rebuilds the domain from the fixed name and version, the chain and the
settlement address, checks it against the settlement's `DOMAIN_SEPARATOR`
and its own `hashJamOrder` on a probe order (so a wrong address, a proxy or
fork with another domain, another protocol version or a different order
type fails the deployment), and reads the balance manager. The registry
entry is `adapter = BebopJamAdapter, executor = balance manager`: a fill
pulls the sell tokens with a plain `transferFrom` from the taker through the
balance manager, which is what `sync_executor_approvals(balance_manager,
tokens)` approves; the settlement itself must never be approved (Bebop's
own warning, and the balance manager only moves funds when the settlement
asks). A solver calls `settle(order, sig, interactions, hooksData,
balanceRecipient)` or `settleInternal(order, sig, filledAmounts, hooksData)`
with `sig = BebopJamAdapter ++ abi.encode(toSign)`; the settlement forwards
`sig` verbatim to the taker's `isValidSignature` with the EIP-712 digest
(`JamValidation.validateSignature` routes any taker with code to ERC-1271,
no length constraint), the burner strips the adapter prefix, and the adapter
recomputes the digest from the bare order and checks the shape: canonical
encoding, the burner as taker, no hooks (hooks are calls the settlement
makes on the taker's behalf), exactly one sell and one buy token, a nonzero
nonce, a `partnerInfo` the settlement accepts (`NonCanonical`,
`InvalidHash`, `BadTaker`, `BadHooks`, `BadTokenCount`, `ZeroNonce`,
`InvalidFeePercentage`, `InvalidPartnerAddress`), then hands the tuple to
the auction's `check_order` — buy token is the target, the receiver is the
FeeCollector, sell amount within the snapshot, buy amount at or above the
live quote, `expiry` no later than the lot end (the settlement's expiry is
exclusive, the auction's bound inclusive, so a fill stops one second
earlier; a limit order's infinite expiry fails the bound). The native-coin
placeholder is never a lot nor the payment token, so it fails there too.
`executor` and `exclusivityDeadline` pass through: they only restrict who
fills (the API sets the winning solver as exclusive executor until the
quote's expiry). Orders pulled through Permit2 never reach this check with a
matching digest: flipping the unsigned `usingPermit2` routes the signature
to Permit2's witness digest, which differs from the settlement's
(`InvalidHash`), and the burner grants Permit2 no allowance anyway. Shape
failures revert with `jam.OrderNotValid(reason)`; the auction's typed
`check_order` revert bubbles unchanged through the burner, and — unlike
1inch — through the settlement too: a Solidity high-level call lets the
callee's revert data through, so a solver's failed fill carries the typed
reason, while a non-magic answer (the router's `0xffffffff` for a disabled
adapter) is reported as the settlement's own `InvalidContractSignature`.
The settlement checks the contract signature on every fill, so disabling
the adapter stops every unfilled order at once; the allowance sync is
hygiene here, not the step that stops fills.

`BebopJamAdapter.order_for(burner, token, nonce, sell_amount)` returns the
publishable pair for a live lot: the whole available amount by default,
priced at the current block, to the FeeCollector, any executor, no
exclusivity, no fees, no hooks, expiring at the lot end, and the signature
`adapter ++ abi.encode(order)`. A published order is a standing ask like the
CoW one: the burner keeps accepting it as the curve decays below it, and a
publisher re-posts lower with a fresh nonce (the settlement tracks nonces
per taker, one bit each). Anyone may publish and anyone may fill on-chain
(`executor = 0`), so the rail works without the Bebop feed; `describe(partnerInfo)`
unpacks the fee field for publishers. Keeper flow through the feed: `GET
/jam/{chain}/v2/quote?sell_tokens=<lot>&buy_tokens=<target>&sell_amounts=<n>&taker_address=<burner>&receiver_address=<FeeCollector>&approval_type=Standard&gasless=true`
returns `toSign` (the order, executor = the winning solver, `hooksHash =
0`, `partnerInfo` as configured, expiry about a minute out); the keeper
checks `toSign.buyAmounts[0] >= getAmountNeeded(token, n)` (or just
`eth_call`s the burner's `isValidSignature` on the digest), then `POST
/jam/{chain}/v2/order` with `quote_id`, `signature = adapter ++
abi.encode(toSign)` and `sign_scheme = "EIP1271"`, and the solver settles.
Verified here against the live API without a key: a quote for a contract
taker returns `toSign`, and the order endpoint accepts a 692-byte signature
with the default scheme and with `EIP1271` (it rejected the probe on the
taker's token balance, before any signature check; `ERC1271` is not a
known scheme). Open question, not verifiable without inventory on a
contract taker: whether the backend eth_calls the taker's ERC-1271 before
relaying the order to the solver (the on-chain path needs nothing else).
The quote endpoint answered without an API key; the docs list bearer auth.
JAM quotes expire in about a minute and the solver fills right after the
signature arrives, so the feed path is "quote, check against the curve,
sign, done" — no standing orders, no re-posting; the fallback is the
permissionless `order_for` ask. Bebop is not deployed on Fraxtal, so this
rail is for Polygon and Ethereum.

Limitations: one token per side (the lot against the target; JAM's
many-to-many orders are rejected, `BadTokenCount`); hooks are rejected
outright; the arrays' decoding bound (`jam.MAX_TOKENS`, eight) only matters
for the library's vectors; fees are whatever the publisher or the API
encoded and never reduce what the FeeCollector gets below the signed
amount; the API-side acceptance of ERC-1271 signatures is the open
question above.

Emergency procedure is the CoW one with `executor = balance manager`:
`registry.disable_adapter(BebopJamAdapter)` batched with
`sync_executor_approvals(balance_manager, tokens)`
(`scripts/emergency_cow_disable.py` takes the executor as an argument);
the disable alone already stops every fill, as the mainnet fork test shows.

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
