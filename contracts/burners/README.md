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
in `adapters/cow/gpv2.vy`, the OKX order type hash, domain and commission
rules in `adapters/okx/intent_swap.vy` and the 0x Settler and Permit2 type
hashes in `adapters/zerox/settler.vy` are keccak256-derived. Peers
(`CowAdapter`, `OKXIntentAdapter`, `ZeroExSettlerAdapter`, the resolver)
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
an order by error selector without decoding a reason string. Its receiver
rule: the buy token must go to the auction's `receiver` (the FeeCollector)
or to the auction itself (see "Receiver on the auction" below); any other
receiver, the zero address included, is `BadReceiver`. Signature bytes
therefore carry no authority — they only transport the order to the adapter —
and every fill is priced against the live curve at settlement time. Bytes
without a known prefix select no adapter and are invalid. The template is
transparent to the protocols themselves: for a contract signer they forward
the signature bytes verbatim (GPv2Settlement after stripping the owner), so a
publisher simply prepends the adapter address when composing the bytes.

### Receiver on the auction

Some protocols credit the proceeds to the signer itself and carry no
receiver field (Hashflow RFQ-m is one: the pool pays `quote.trader`), so the
only account such an order can name is the burner. `check_order` therefore
accepts the auction itself as the order receiver next to the configured
`receiver`; nothing else changes, and the configured receiver can still
never be the auction (`_set_receiver`): native takes always pay out of the
auction, and the native `take` path does not know the second receiver.
Proceeds credited to the burner are not stranded. `push_target()` is
permissionless: it reads the burner's whole `want` balance, transfers it to
the FeeCollector in one `transfer` when it is nonzero, and returns the
amount moved (0 for an empty balance, never a revert). `FeeCollector.forward()`
calls `burner.push_target()` before reading its own balance, so target
credited to the burner during the EXCHANGE week reaches the FeeCollector by
the FORWARD step without any keeper action; a keeper may call `push_target()`
earlier at any time (the keeper of such a rail should do so right after a
fill, so the proceeds do not sit on the burner for the rest of the week).
Target held on the burner between the fill and the push touches no
accounting: the target is never a lot (`_stage_lot` rejects `want`, and the
FeeCollector keeps the target coin killed for COLLECT, so a collect never
pulls it), `available(want)`, `price(want)` and `getAmountNeeded(want)` read
0, and `check_order` rejects it as a sell token (`LotInactive`). `resync_target`
is unaffected: after a target migration the old target becomes regular
inventory and a leftover balance is snapshotted at the next collect like any
other token, which already held for the native rail's leftovers. Adapters
whose protocol has a receiver field keep publishing the FeeCollector
(`order_for` of CoW and the others), but an order naming the burner now
validates on them too.

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

### OKX rail: `OKXIntentAdapter`

OKX Intent Swap (the intent mode of the OKX DEX aggregator API, "Onchain
OS") is the next rail, built the same way. Its on-chain `Settlement` is a
GPv2 fork (verified Solidity 0.8.24 source on Etherscan, no proxy), deployed
at one address per calldata schema on Ethereum, Arbitrum, Base, BSC and X
Layer: the legacy schema at `0x25ED72C3f671b626810A6dB597DCFD50F215A423`
(`settle` selector `0xf1c95699`, where the volume settles) and the threshold
schema at `0xC75d2cE9d0A2C67b606a7B325c16E8a1B7C15a4A` (`0x462c3ed1`,
`version() == "v2.0.0-threshold-uncapped-surplus"`, solvers still being
onboarded). Both share the EIP-712 domain name `OKX Intent Swap`, version
`v1.0.0`, and the immutable `tokenApproveProxy`
`0x70cBb871E8f30Fc8Ce23609E9E0Ea87B6b222F58` whose `tokenApprove()` is the
OKX DEX `TokenApprove` `0x40aA958dd87FC8305b97f2BA922CDdCa374bcD7f` (Etherscan
"OKX: DEX Token Approval 1", verified): a fill calls
`TokenApproveProxy.claimTokens`, which the proxy's `allowedApprove` allowlist
gates and forwards to `TokenApprove.claimTokens`, a plain `transferFrom` from
the order owner. Neither Polygon nor Fraxtal is supported, so this rail is
for Ethereum (and the other four chains). Economic principle, verified
against the settlement source: **the signed `toTokenAmount` is the floor the
receiver is paid for the signed `fromTokenAmount`, after every fee**. Every
order is fill-or-kill: `TradeLib.extractFlags` rejects the partial-fill bit
(and the buy-order bit; only `exactIn` exists), the whole `fromTokenAmount`
is pulled from the owner (commissions flagged "ToC" straight to their
recipients, the rest to the settlement), the gross proceeds are
`netFrom * fromPrice / toPrice` at the solver's clearing prices, to-token
commissions and the solver fee come off them, and the payout to the receiver
must still cover `toTokenAmount` (`"toTokenAmount not respected"`); the
threshold schema's output-shortfall gate is capped by the same floor. The
order uid (`digest ++ owner ++ validTo`) is marked filled, so no order
settles twice. Surplus (whatever is left on the settlement after payouts) is
shared among the batch's participants, the from-side share going to the
owner (the burner, where it is inventory again) and the to-side share to the
receiver. `settle` is `onlySolver`: only KYB'd, owner-allowlisted solvers
fill, as with CoW, and there is no permissionless on-chain fill.

`adapters/okx/intent_swap.vy` holds the signed order type (the eleven
EIP-712 fields with `commissionInfos` last — exactly the quote API's
`signData.message`), the type hashes, the domain, the hashStruct with the
commission array hashed as its element hashes (`OrderLib.hash`), the uid
packing, the trade flag layout (`EIP1271_TRADE_FLAGS = 0x40`: scheme bits
5-6) and the commission rules of `FeeLib` (at most three entries, each
exactly one direction, a recipient for a paid entry, 3% total, paid entries
sharing one direction and one fund flow). `OKXIntentAdapter.vy` is
stateless and immutable and pinned to one settlement: at deploy it rebuilds
the domain from the fixed name and version, the chain and the settlement
address, checks it against the settlement's `domainSeparator` (so a wrong
address, a fork with another domain or another protocol version fails the
deployment), and follows `tokenApproveProxy().tokenApprove()` to the
approval target. The registry entry is `adapter = OKXIntentAdapter, executor
= TokenApprove` (`adapter.token_approve()`), what
`sync_executor_approvals(token_approve, tokens)` approves; one adapter per
settlement, the two sharing the executor (the registry refcounts it). A
solver settles a trade with `flags = 0x40` and `signature = burner ++
OKXIntentAdapter ++ abi.encode(order)`; `Signing.recoverEip1271Signer`
reads the owner from the first 20 bytes and forwards the rest verbatim to
the owner's `isValidSignature` with the EIP-712 digest (and requires the
signed `owner` field to be that owner), the burner strips the adapter
prefix, and the adapter recomputes the digest from the bare order and
checks the shape: canonical encoding, the burner as `owner`, the `exactIn`
mode and no partial-fill flag, commissions the settlement accepts
(`NonCanonical`, `InvalidHash`, `BadOwner`, `BadOrderFlags`,
`BadCommissionDirection`, `ZeroFeeRecipient`, `CommissionTooHigh`,
`MixedCommissionDirections`, `MixedCommissionFlows`), then hands the tuple
to the auction's `check_order` — buy token is the target, the receiver is
the FeeCollector (an empty `receiver` means the owner, as the protocol
resolves it, and fails there), sell amount within the snapshot, buy amount
at or above the live quote, `validTo` (inclusive on both sides) no later
than the lot end. The native-coin placeholder is a legal buy token on the
settlement but never the target, so it fails there too. `appData` and the
commissions themselves pass through: `appData` has no on-chain effect
(the API sets it per quote; `order_for` takes it as a salt), and every
commission or solver fee only lowers the solver's margin above the floor.
Shape failures revert with `intent_swap.OrderNotValid(reason)`; the
auction's typed `check_order` revert bubbles unchanged through the burner
and through the settlement (a Solidity high-level call lets the callee's
revert data through), while a non-magic answer (the router's `0xffffffff`
for a disabled adapter) is reported as the settlement's own `"invalid
eip1271 signature"`. The settlement checks the contract signature on every
fill, so disabling the adapter stops every unfilled order at once; the
allowance sync is hygiene here, not the step that stops fills.

`OKXIntentAdapter.order_for(burner, token, sell_amount, app_data)` returns
the publishable pair for a live lot: the whole available amount by default,
priced at the current block, to the FeeCollector, no commissions, expiring
at the lot end, and the signature `adapter ++ abi.encode(order)`. A
published order is a standing ask like the CoW one: the burner keeps
accepting it as the curve decays below it, and a publisher re-posts lower
(a different `toTokenAmount` is a new uid; equal fields need another
`appData`). `describe(payload)` decodes a bare payload into its order, its
digest and its uid, the key of the settlement's `filledAmount(uid)` and
`invalidateOrder(uid)` (owner-only; the burner has no such call, orders
simply expire). Keeper flow through the API (every endpoint requires an OKX
Onchain OS API key — `OK-ACCESS-KEY`, HMAC-signed requests — including the
quote): `GET /api/v6/dex/aggregator/quote?chainIndex=1&mode=intent&swapMode=exactIn&fromTokenAddress=<lot>&toTokenAddress=<target>&amount=<n>&userWalletAddress=<burner>&swapReceiverAddress=<FeeCollector>&signingScheme=eip1271&slippagePercent=<s>&expiration=<seconds>`
returns `quoteId` and `signData` (`domain.verifyingContract` names the
settlement the order is for, `message` is the order with OKX's
`commissionInfos` and `appData`); the keeper checks
`message.toTokenAmount >= getAmountNeeded(token, n)` (or `eth_call`s the
burner's `isValidSignature` on the digest), then `POST
/api/v6/dex/aggregator/intent/create-order` with the message fields,
`quoteId`, `signingScheme = "eip1271"` and `signature = adapter ++
abi.encode(message)`; the orderbook runs its auction and the winning solver
settles within the quote's expiration (180 s by default, 300 s at most).
Open questions, not verifiable without an API key: whether the orderbook
prefixes the owner to an `eip1271` signature itself or expects `burner ++
adapter ++ abi.encode(message)` from the submitter (the on-chain settle
calldata needs the owner prefix; the solver SDK passes the orderbook's
bytes verbatim), and whether it `eth_call`s the owner's ERC-1271 before
relaying an order (the docs say it does, and that a long signature is
fine). The quote's `verifyingContract` decides which deployed adapter to
use.

Limitations: one order per fill and no partial fills (the protocol's
choice); the API path needs an OKX API key and the solver set is OKX's
allowlist, so the rail has no permissionless fallback (CoW's is the
orderbook, 1inch's is any taker); `appData` is free; the executor is the
OKX DEX-wide `TokenApprove`, whose `claimTokens` any contract on the
unverified `TokenApproveProxy`'s allowlist (the two settlements, the OKX
DEX routers) may call for any owner it names — approving it is the same
trust every OKX DEX user extends, bounded here by the staged inventory, and
listing the adapter is the owner's review of it.

Emergency procedure is the CoW one with `executor = token_approve`:
`registry.disable_adapter(OKXIntentAdapter)` batched with
`sync_executor_approvals(token_approve, tokens)`
(`scripts/emergency_cow_disable.py` takes the executor as an argument); the
disable alone already stops every fill, as the mainnet fork test shows.

### Kyber rail: `KyberDsloAdapter`

KyberSwap's limit orders settle through `DSLOProtocol` ("Double Signature
Limit Order", `0xcab2FA2eeab7065B45CBcF6E3936dDE2506b4f6C` on every chain
KyberSwap serves — Ethereum, Polygon, Arbitrum, Base, BSC and more, with
identical verified source; not Fraxtal), a fork of the 1inch Limit Order
Protocol v2 with a packed `feeConfig` added to the order. It is the
busiest of the limit-order venues reviewed (about 8,700 `OrderFilled`
events on Ethereum in the 30 days to 2026-11-02, mostly through aggregator
routes; KyberSwap and OpenOcean/DODO were weighed in the same review, the
latter two rejected for private-taker and operator-only settlement). The
protocol is a first-class home for a contract maker: `fillOrderTo` hands
`signature.orderSignature` verbatim to the maker's ERC-1271
`isValidSignature` with the EIP-712 order digest (OpenZeppelin's
`SignatureChecker`, after a failed ECDSA recovery), pulls the maker asset
with a plain `transferFrom` from the maker, and anyone may call it. The
"double" signature is the catch: every fill also carries an operator
co-signature over `DSOrder(orderHash, opExpireTime)` from a KyberSwap-held
key, issued per fill by the KyberSwap API with a minute-scale expiry. It
is how gasless cancel works and it means distribution runs through
KyberSwap's orderbook and co-signing service — the same kind of gate as
1inch resolvers or Bebop solvers, outside the maker's signature and
irrelevant to what the maker receives. The API documents ERC-1271 maker
signatures as accepted (`POST /write/api/v1/orders`), but no live order
with a contract maker was observed, so the first production order is the
test of that path; the on-chain fill is proven on a mainnet fork.

`adapters/kyber/KyberDsloAdapter.vy` is stateless and immutable and pinned
to the chain's protocol: at deploy it rebuilds the EIP-712 domain
(`"Kyber DSLO Protocol"`, `"1"`, the chain id and the protocol address)
and checks it against the protocol's `DOMAIN_SEPARATOR()`, then the order
hashing against its `hashOrder` on a probe order. The registry entry is
`adapter = KyberDsloAdapter, executor = protocol`, because the protocol
itself pulls the sold tokens (`sync_executor_approvals(protocol, tokens)`).
A taker calls `fillOrderTo(params, amounts, sender)` with
`params.signature.orderSignature = KyberDsloAdapter ++ abi.encode(Order)`;
the burner strips the prefix and the adapter recomputes the digest from the
bare order (`adapters/kyber/dslo.vy` holds the order type, the type hash,
the `feeConfig` layout, the linear getters and the predicate shapes). The
economic principle is the 1inch one: **the signed `takingAmount` is the
floor the FeeCollector is paid for `makingAmount`**. The order carries no
amounts of its own at fill time — `getMakerAmount`/`getTakerAmount` are
calldata the protocol staticcalls on itself with the swap amount appended
— so the adapter pins each getter to the protocol's own linear calculator
over the order's own amounts (`floor(taking * M / T)` maker tokens for a
taking-amount fill, `ceil(making * T / M)` payment for a making-amount
fill, both in the maker's favour) or accepts it empty (exact-amount fills
only); anything else is `BadAmountGetter`. `feeConfig` passes through: a
taker-asset fee is paid on top by the taker, a maker-asset fee is carved
out of the taker's maker-asset receipt, and in both branches the maker
gives `makingAmount` and the FeeCollector receives the full `takingAmount`.
The order has no expiry field either: the expiry is the `timestampBelow(t)`
predicate (strict, so the last fillable second is `t - 1`), alone or as an
entry of the `and(address[], bytes[])` the KyberSwap API signs into every
order next to `nonceEquals(maker, nonce)` (the burner never advances its
protocol nonce, so the API's `nonceEquals(burner, 0)` always holds); the
adapter accepts exactly those shapes (`BadPredicate` otherwise, an empty
predicate included) and hands `t - 1` to `check_order` as the validity
bound. Asset data (appended to the `transferFrom` calldata) and the
interaction (a whitelisted post-fill callback) must be empty
(`BadAssetData`, `BadInteraction`); `salt` and `allowedSender` pass
through. The remaining checks are the shared ones — canonical encoding,
digest, the burner as maker (`NonCanonical`, `InvalidHash`, `BadMaker`) —
and then `check_order` with the maker asset, the taker asset, the resolved
receiver (empty means the maker), the order amounts and the predicate
expiry. `order_for(burner, token, salt, sell_amount)` returns the
publishable pair for a live lot: the whole available amount by default,
priced at the current block, linear getters, no fee, any taker, expiring
with the lot; an order placed through the KyberSwap API instead carries the
fields its `sign-message` endpoint returns (salt, `feeConfig`, the `and`
predicate), and the signature is built the same way from them.
`describe(order)` unpacks the resolved receiver, the fee recipient, percent
and side, and the expiry. Like the 1inch router, the protocol swallows a
failed contract signature check into its own `'LOP: bad signature'`, so a
publisher classifies an order by `eth_call` against the burner's
`isValidSignature`; and it checks the contract signature on the first fill
only, so a partially filled order keeps filling at its signed price until
its predicate expires, and an emergency disable is complete only with
`sync_executor_approvals(protocol, tokens)`.

### Velora Delta rail: `VeloraDeltaAdapter`

Velora (ex-ParaSwap) Delta is a second adapter, built the same way.
`adapters/velora/VeloraDeltaAdapter.vy` is stateless and immutable, pinned
to the chain's Delta contract (the Portikus V2 adapter,
`0x0000000000bbf5c5fd284e657f01bd000933c96d` on every supported chain; the
EIP-712 domain `Portikus` / `2.0.0` / chain / Delta is rebuilt at deploy and
cross-checked against the contract's `DOMAIN_SEPARATOR`). Its registry entry
is `adapter = VeloraDeltaAdapter, executor = Delta`, because the settlement
modules run inside the Delta contract by delegatecall and pull the sold
tokens with a plain `transferFrom` from the owner (an order with an empty
`permit`); the Delta contract is what `sync_executor_approvals(delta,
tokens)` approves. A registered Delta agent settles with `swapSettle`
(fill-or-kill under the `Order` primary type) or `swapSettleFillable`
(pro-rata slices under `FillableOrder`), passing `OrderWithSig.signature =
VeloraDeltaAdapter ++ abi.encode(Order)`; `SignatureLib.verify` forwards
those bytes verbatim to the burner's `isValidSignature` with the digest of
the entry point's primary type (contract owners are ERC-1271 signers there;
the on-chain pre-signature mapping is only an alternative and needs no
burner state). The burner strips the adapter prefix and the adapter
recomputes the digest from the bare order (`adapters/velora/delta.vy` holds
the order type, both primary type hashes, the domain and the fee packing),
accepting whichever of the two digests matches since the economics are the
same, checks the shape — canonical encoding, the burner as owner, a sell
order, an empty permit (a permit routes the pull through Permit2, which the
burner never approves, or skips it), no bridge (the proceeds must reach the
beneficiary on this chain), `expectedAmount` at or above `destAmount` — and
delegates the economics to `check_order` (dest token is the target,
beneficiary is the FeeCollector, `srcAmount` within the snapshot, the net
floor at or above the live quote, `deadline` no later than the lot end).
The net floor is what the beneficiary actually receives after
`FeeManagerLib.processSellFees`: the protocol's cut and a surplus-taking
partner's cut come only out of the surplus above `expectedAmount` (hence
the shape rule), while a partner fee in bps comes off the top and is netted
out of `destAmount` before pricing, so an order that carries the API's
partner fee must sign a correspondingly higher `destAmount`. Shape
failures revert with `delta.OrderNotValid(reason)` (`NonCanonical`,
`InvalidHash`, `BadOwner`, `BadOrderKind`, `BadPermit`, `BadBridge`,
`BadExpectedAmount`); the auction's typed `check_order` revert bubbles
unchanged, through the real Delta contract included, and a disabled adapter
surfaces as the settlement's own `InvalidSignature`.

`VeloraDeltaAdapter.order_for(burner, token, nonce, sell_amount)` returns
the publishable pair for a live lot: the whole available amount by default,
`destAmount = expectedAmount =` the quote at the current block, no partner,
no permit, no metadata, no bridge, valid until the lot end; either entry
point may fill it. Two Delta facts shape publishing. Nonces are unordered
(a bitmap per owner, burnt once the order is filled in full), so every
re-post as the curve decays needs a fresh nonce; `isNonceUsed(owner,
nonce)` reads the bitmap. And only agents registered in the Portikus V2
registry (`getAgents()`) can call the settlement: orders reach them through
Velora's public Delta API (`POST /v2/delta/orders`, no key required, an
order built by `/v2/delta/orders/build` for `owner = burner`), whose
acceptance of an ERC-1271 owner signature of this shape is not documented
and has not been exercised here, or by handing orders to an agent
directly.

Emergency procedure is the CoW one with `executor = Delta`:
`registry.disable_adapter(VeloraDeltaAdapter)` batched with
`sync_executor_approvals(delta, tokens)`.

### OIF escrow rail (LI.FI Intents): `OifEscrowAdapter`

The Open Intents Framework escrow input settler, as deployed by LI.FI for
LI.FI Intents (`InputSettlerEscrowLIFI`
`0x00fC00edbe7C003b006f870068c548940000223e` with `OutputSettlerSimple`
`0x75220B7600c300005038432a0000f308e0000068` on Ethereum; the reference
`InputSettlerEscrow` `0x1CC9260E285C2C8AC8D2E7102F3978056Ec1d0a8` has the
same signing scheme), is a third adapter, built the same way.
`adapters/oif/OifEscrowAdapter.vy` is stateless and immutable, pinned to
the escrow, the same-chain output settler and Permit2 (whose EIP-712 domain
separator is read at deploy); its registry entry is `adapter =
OifEscrowAdapter, executor = Permit2`, because the escrow opens a sponsored
intent through `Permit2.permitWitnessTransferFrom`: Permit2 is what pulls
the sold tokens, so Permit2 is what `sync_executor_approvals(permit2,
tokens)` approves (shared with every other Permit2-based adapter through
the executor reference count). A solver calls
`escrow.openForAndFinalise(order, sponsor = burner, signature = 0x00 ++
OifEscrowAdapter ++ abi.encode(StandardOrder), destination, call)` (or
anyone calls `openFor` with the same bytes); the escrow strips its
signature-type byte and Permit2 forwards the rest verbatim to the burner's
`isValidSignature` with the `PermitBatchWitnessTransferFrom` digest (permit
= the input token and amount, `order.nonce`, deadline = `fillDeadline`;
spender = the escrow; witness = the `Permit2Witness` hash of user, expires,
input oracle and outputs). The burner strips the adapter prefix and the
adapter recomputes that digest from the bare order (`adapters/oif/oif.vy`
holds the types, the OIF and Permit2 type hashes and the output settler's
context shapes), checks the shape — canonical encoding, the burner as
user, this chain as origin, exactly one input and one output, clean
identifiers, the pinned output settler as the order's input oracle and as
the output's oracle and settler on this chain, no callback, a plain
limit-order context (none or limit; an exclusive one would let the opener
lock every other solver out until a start time it picks), `expires` equal
to `fillDeadline` (the escrow refunds an opened but unfilled intent only
after `expires`, so a later `expires` would let any opener park the lot in
the escrow past the lot end), and `fillDeadline` at most 15 minutes past
the open (`MAX_FILL_WINDOW`: anyone may `openFor` a burner intent, which
moves the lot into the escrow before anything is paid, so this bounds how
long an unpaid open can keep the lot from every other rail) — and
delegates the economics to `check_order` (output token is the target,
recipient is the FeeCollector, input within the snapshot, output amount at
or above the live quote, `fillDeadline` no later than the lot end). The
oracle pin is what makes the floor real: the escrow releases the
input only against the input oracle's attestation, and the pinned output
settler attests only its own fills, which pay `output.amount` to the
recipient. Shape failures revert with `oif.OrderNotValid(reason)`
(`NonCanonical`, `InvalidHash`, `BadUser`, `BadChain`, `BadInputs`,
`BadOutputs`, `BadInputOracle`, `BadOutputChain`, `BadOutputSettler`,
`BadIdentifier`, `BadCallback`, `BadContext`, `BadExpires`,
`BadFillDeadline`); the auction's typed
`check_order` revert bubbles unchanged, through the real escrow and Permit2
included, and a disabled adapter surfaces as Permit2's
`InvalidContractSignature`.

`OifEscrowAdapter.order_for(burner, token, nonce, sell_amount,
fill_deadline)` returns the publishable pair for a live lot: a limit intent
for the whole available amount by default, priced at the current block,
open to every solver, `expires = fillDeadline` (15 minutes from now by
default, never past the lot end).
Three escrow facts shape publishing. Intents are fill-or-kill and
nonce-burning (Permit2 unordered nonces, burnt on open), so every re-post
as the curve decays needs a fresh nonce. An intent is settled either
atomically (the solver fills the output and attests on the output settler,
then `openForAndFinalise` pulls the input and hands it to the solver in the
same block) or in two steps (`openFor` moves the input into the escrow
first; the solver then fills, attests and `finalise`s): in the second shape
the input sits in the escrow until the fill or, after `expires`, a
permissionless `refund` back to the burner, so a publisher opening intents
itself keeps `fill_deadline` short. And distribution: LI.FI's order server
(`order.li.fi`, no API key) quotes same-chain intents and fills the ones
opened on-chain, but as of this writing accepts only user-opened orders
(`oif-user-open-v0`), not sponsored escrow orders, so a sponsored intent
reaches a solver directly or through a keeper that opens it on-chain for
the server's solver to fill. A governance fee the escrow owner may set is
taken from the input on release and is the solver's cost, never the
FeeCollector's.

Emergency procedure is the CoW one with `executor = Permit2`:
`registry.disable_adapter(OifEscrowAdapter)` batched with
`sync_executor_approvals(permit2, tokens)` (Permit2 stays approved while
another Permit2-based adapter is active). An intent already opened keeps
its input in the escrow until it is filled or refunded.

### 0x rail: `ZeroExSettlerAdapter`

0x Settler v2's gasless flows are a further adapter, built the same way.
`adapters/zerox/ZeroExSettlerAdapter.vy` is stateless and immutable, pinned
to one Settler contract and its flavor — `SettlerMetaTxn` (the Gasless API
rail, permissionless relay) or `SettlerIntent` (the solver rail) — and to
Permit2 (its EIP-712 domain separator is read at deploy). The live
implementations are the 0x deployer registry's `ownerOf(3)` and `ownerOf(4)`
(`0x00000000000004533Fe15556B1E086BB1A72cEae`, the same address on every
chain), previous implementations keep settling, and a flavor mismatch fails
every digest, so each Settler to be reachable is one adapter deployment. The
adapter also pins that registry and follows it, as 0x's integration guidance
asks (never hardcode a Settler; check `ownerOf(feature)` / `prev(feature)`
before signing): 0x retires a Settler only there, by deploying a newer one
or by removing a faulty one (its pause), while the retired bytecode keeps
executing. At deploy the registry must know the Settler under the flavor's
feature (`deployInfo`, 3 = `SettlerMetaTxn`, 4 = `SettlerIntent`;
`BadFlavor` otherwise) and list it as the feature's current or previous
instance (`SettlerRetired` otherwise), and every validation repeats the
latter (`OrderNotValid(SettlerRetired)`; one registry staticcall while the
Settler is current, two once it is the previous one), so the adapter stops
signing by itself once 0x has removed its Settler or deployed two newer
ones, and a new adapter deployment follows the registry. A registry revert
(`ownerOf` once a feature has no instance left) bubbles and fails closed
too. Its registry entry is `adapter = ZeroExSettlerAdapter, executor =
Permit2`: the Settler's first action (`METATXN_TRANSFER_FROM` or another `METATXN_*_VIP`)
pulls the sold token through `Permit2.permitWitnessTransferFrom`, so Permit2
is what `sync_executor_approvals(permit2, tokens)` approves. A relayer (for
`SettlerIntent`, an allow-listed solver) calls
`settler.executeMetaTxn(slippage, actions, zid, msgSender = burner, sig =
ZeroExSettlerAdapter ++ abi.encode(order))`; the Settler hands `sig` to
Permit2 with the digest over the permit (token, amount, nonce, deadline;
spender = the Settler) and the flavor's witness —
`SlippageAndActions(address recipient,address buyToken,uint256
minAmountOut,bytes[] actions)` for `SettlerMetaTxn`, `Slippage(address
recipient,address buyToken,uint256 minAmountOut)` for `SettlerIntent` — and
Permit2 forwards it verbatim to the burner's `isValidSignature`. The burner
strips the adapter prefix, and the adapter recomputes that digest from the
bare order (`adapters/zerox/settler.vy` holds the order type, the Settler
and Permit2 type hashes and the Settler's action-list hash), requires the
permitted amount to be available right now (the fill is atomic and pulls
exactly it), and delegates the economics to `check_order` (buy token is the
target, recipient is the FeeCollector, amount within the snapshot,
`minAmountOut` at or above the live quote, `deadline` no later than the lot
end). Shape failures revert with `settler.OrderNotValid(reason)`
(`NonCanonical`, `InvalidHash`, `BadActionsHash`, `SettlerRetired`,
`ExceedsAvailable`); the
auction's typed `check_order` revert bubbles unchanged, through the real
Settler and Permit2 included, and a disabled adapter's `0xffffffff` surfaces
as Permit2's `InvalidContractSignature`.

The order carries the permit, the slippage and, for `SettlerMetaTxn`, only
the Settler's hash of the signed action list, not the actions: the digest
binds them, and nothing in them can lower the floor. After the action list
has run, `SettlerBase._checkSlippageAndTransfer` requires at least
`minAmountOut` of `buyToken` on the Settler and pays its whole balance to
`recipient`; no action reads or rewrites the slippage (`BASIC`, `UNISWAPV3`,
`POSITIVE_SLIPPAGE` and the rest only move the Settler's own balances), so
the signed actions (authored by the Gasless API or by the publisher) and, on
the intent rail, the solver's unsigned ones decide only the margin above the
floor: the API's fee actions (`BASIC` ppm transfers of the sell token) come
out of the permitted amount, never out of `minAmountOut`. The pull, the swap
and the floor check run in one `executeMetaTxn` and nothing is escrowed, so
unlike an OIF open there is no unpaid state to bound and no fill window: the
permit deadline (at most the lot end) is the only time limit. A native buy
token (the Settler's ETH sentinel) is never the target and fails
`check_order`.

`ZeroExSettlerAdapter.order_for(burner, token, nonce, sell_amount, deadline,
actions_hash)` returns the publishable pair for a live lot, priced at the
current block, for the whole available amount and until the lot end by
default; `hash_actions(actions)` is the Settler's hash of an action list and
`describe(payload)` returns the decoded order with the digest the adapter
would accept it for, to compare with the EIP-712 hash a quote asks the
burner to sign. Three 0x facts shape publishing. Orders are fill-or-kill:
Permit2 burns the nonce on settlement, every re-post needs a nonce no
earlier fill has used (the publisher's choice; the adapter neither derives
nor checks it) and an order for the full amount dies by itself after any
other fill (`ExceedsAvailable`). Relaying a `SettlerMetaTxn` order is
permissionless (`metaTx` only forbids the signer relaying its own order); a
`SettlerIntent` order can only be submitted by one of the Settler's
`getSolvers()` (about 60 on mainnet, managed by 0x). And routes come from
off-chain: the Gasless API (`/gasless/quote`, API key required; it returns
the action list, the slippage and the Permit2 EIP-712 payload) relays through
`SettlerMetaTxn` after `/gasless/submit`, which accepts `signatureType: 5`
raw signatures for contract wallets, but whether it accepts a plain ERC-1271
contract as `taker`, and the long adapter signature as that raw signature, is
unverified (no key); a publisher may instead author its own action list (the
fork test sells USDC for crvUSD through a Curve pool with
`METATXN_TRANSFER_FROM` + `BASIC`) and relay it, or hand a `SettlerIntent`
order to a solver.

Emergency procedure is the CoW one with `executor = Permit2`:
`registry.disable_adapter(ZeroExSettlerAdapter)` batched with
`sync_executor_approvals(permit2, tokens)`. Permit2 is shared by every
Permit2-based adapter (UniswapX, OIF, 0x): the executor reference count keeps
the allowance while any of them is active and clears it only when the last
one is disabled, so disabling the 0x adapter alone stops its routing at once
but leaves the burner's allowance to Permit2 in place for the others. 0x's
own pause (removing the Settler from its registry) needs no action on our
side: the adapter rejects every order for a Settler the registry no longer
lists.

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
permissionless and returns held target tokens to the FeeCollector (see
"Receiver on the auction": `FeeCollector.forward()` calls it too).

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
