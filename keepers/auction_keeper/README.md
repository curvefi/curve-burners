# auction_keeper

Off-chain backend for the Dutch auction burner: it watches every configured
auction, finds live lots, and publishes signed orders to the auction's
settlement adapters (CoW today). The same package drives custom executions:
import it, describe the take, and it waits for the trigger and sends.

The design follows what CoW's watch-tower did for the old `CowSwapBurner`
(poll, build, post, dedupe), without ComposableCoW: orders are built from the
auction's live quote and signed through the burner's ERC-1271 router.

## Install

```bash
pip install -r requirements.in      # web3, PyYAML on top of the test stack
```

## Layout

| Package | What it holds |
|---|---|
| `keepers/auction_keeper/config/` | The YAML config: `core.py` (`NetworkConfig`, `KeeperConfig`, loading, `${ENV}` expansion), `pricing.py` (`pricing` / `ttl` sections), `native.py` (`native`), `rails.py` (`cow` and the per-rail sections: `RailSettings`, `parse_rail`), `common.py` (`ConfigError`, primitive validators). `from keepers.auction_keeper.config import ...` exports the dataclasses and `load_config`. |
| `keepers/auction_keeper/chain/` | The node: `client.py` (`ChainClient` protocol, `Web3Client`), `abi.py` (selectors, call encoding, revert decoding), `contracts.py` (typed readers of the auction, registry, adapters, `AuctionTaker`, ERC-20), `events.py` (the `LotStaged` scan), `boa_client.py` (the Titanoboa client the tests and fork rehearsals use). |
| `keepers/auction_keeper/core/` | The publishing loop: `keeper.py` (`NetworkKeeper`, `run_networks`), `state.py` (the state file), `schedule.py` (market zones and order validity). |
| `keepers/auction_keeper/pricing/` | `MarketPricer`, `build_sources` and `sources/` (`curve`, `llama`, `coingecko`, `cow`, `kyber`, `velora`, `oneinch`, `router`). |
| `keepers/auction_keeper/routing/` | `curve_router.py`: the Curve pool catalogue, the route search and the router quote/exchange calls. |
| `keepers/auction_keeper/rails/` | The settlement rails: the `AdapterPublisher` base and registry (`__init__.py`), `common.py` (`JsonClient`, bearer headers, salts), `cow.py`, `native.py` (the keeper's own take through the `AuctionTaker`); every other rail's publisher lands here as `<rail>.py`. |
| `keepers/auction_keeper/execute.py` | Custom executions (`Executor`, `TakePlan`, `connect`); one module, imported by scripts and agents. |
| `configs/` | `config.example.yaml` and the production configs (`config.polygon.yaml`, `config.fraxtal.yaml`, `config.polygon-test.yaml`). |

`tests/keeper/` mirrors it (`chain/`, `config/`, `core/`, `pricing/`, `routing/`, `rails/`), with the shared fixtures in `tests/keeper/conftest.py`.

## Configure

Copy [`config.example.yaml`](config.example.yaml). Per network only `rpc`
and `auctions` are required; `${NAME}` placeholders read the environment
(`~/.env` is loaded first).

| Setting | Default | Meaning |
|---|---|---|
| `poll_interval` | 60 | Seconds between node polls (global or per network). |
| `order_ttl` | 300 | Seconds a published order stays valid when the schedule has no market for the lot, or the quote is already below it. Reposted at the live quote when it expires, never past the lot window. |
| `pricing.sources` | `[curve, llama, cow]` | Market estimate sources in priority order; the first with a price wins, `[]` disables pricing (uniform TTL). USD feeds: `curve` (prices.curve.finance), `llama` (coins.llama.fi), `coingecko`. Swap quotes for the exact lot: `cow` (the orderbook `/quote`), `kyber` (KyberSwap routes), `velora` (ParaSwap `/prices`), `oneinch` (swap quote with `pricing.oneinch_api_key_env`, skipped without the key; UNVERIFIED). |
| `pricing.cache` | 60 | Seconds a lot's market estimate is reused. |
| `pricing.cross_check` | 0.25 | The winning source must agree with the next source that has a price within this fraction; otherwise the lot has no market this round (uniform TTL) and both prices are logged. `0` disables. |
| `pricing.chains` | by chain id | Chain slug per feed (`{curve: polygon, llama: polygon, coingecko: polygon-pos, kyber: polygon}`); the defaults in `pricing.CHAINS` cover Ethereum, Polygon, Arbitrum, Base, Optimism, Gnosis, Avalanche, BSC, Fantom and Fraxtal where the feed does (Kyber and Velora do not serve Fraxtal or Gnosis). |
| `pricing.coingecko_api_key_env`, `pricing.coingecko_url` | `COINGECKO_API_KEY`, `https://api.coingecko.com/api/v3` | CoinGecko demo key and API base; a pro key goes with `https://pro-api.coingecko.com/api/v3`. |
| `pricing.oneinch_api_key_env` | `ONEINCH_API_KEY` | Developer Portal key for the `oneinch` quote source. |
| `ttl.near_above`, `ttl.near_below` | 0.2, 0.1 | The band around the market: a quote at most 20% above or 10% below it is "near". |
| `ttl.min` | 60 | Order validity inside the band (the CoW orderbook minimum; one or two curve steps). |
| `ttl.max` | 7200 | Longest validity far above the market, where one order waits for the curve to reach the band. |
| `native.enabled` | `false` | The native rail: take lots through the `AuctionTaker` with a Curve router swap, alongside the adapters (see below). Needs `auction_taker`; sends in `keeper` mode, simulates and reports in `offline` mode. |
| `native.margin` | 0.005 | Take when the auction's quote for the chunk is at least this far under the Curve route quote (the keeper's profit). |
| `native.max_impact` | 0.01 | Chunk the take so the route's price impact stays under this fraction; the rest of the lot is quoted again next poll. |
| `native.poll` | 30 | Seconds between polls while a lot's quote is within `ttl.near_above` of its route quote. |
| `native.router`, `native.chain`, `native.api_url` | by chain id | Curve router NG address, `api.curve.finance` chain slug and pool-list base URL (`curve_router.ROUTERS` / `API_CHAINS` cover Ethereum, Optimism, Gnosis, Polygon, Fantom, Fraxtal, Base, Arbitrum, Avalanche). |
| `native.min_pool_usd`, `native.max_hops` | 1000, 2 | Pools below this TVL are not routed through; hops per route (plain `exchange` only). |
| `native.routes` | `{}` | Explicit routes by lot token, `{token: {path: [token, pool, token, ...], params: [[i, j, swap_type, pool_type, n_coins], ...]}}`, used instead of the search (metapool underlying swaps, zaps). |
| `native.profit_receiver` | the sender | Where the take's profit and any unspent lot go. |
| `max_open_orders` | 8 | Open orders per adapter at once; lots farthest from the market wait for a slot. The CoW orderbook rejects an owner's eleventh open outside-market order (`TooManyLimitOrders`). |
| `tokens.whitelist` | `[]` | Non-empty: sell only these. |
| `tokens.blacklist` | `[]` | Never sell these (wins over the whitelist). |
| `start_block` | none | Where the `LotStaged` scan starts (burner deployment block). Without it only lots staged after start-up and the whitelist are known. |
| `mode` | `offline` | `offline`: reads the chain and posts orders, never sends a transaction (no key is loaded). `keeper`: also sends the on-chain housekeeping (the executor allowance sync, the Hashflow `tradeRFQM` relay and `push_target()`); needs `keeper_key_env`. |
| `keeper_key_env` | none | Env var with the private key used in `keeper` mode and by custom executions. |
| `cow.orderbook_url` | by chain id | mainnet, gnosis, arbitrum, base, polygon, avalanche, bnb, sepolia have defaults. |
| `cow.app_data_document` | none | Full appData JSON whose keccak is `CowAdapter.app_data()`; registered with the orderbook once. |
| `auction_taker` | none | `AuctionTaker` address for route executions. |
| `state_path` | `auction_keeper_state.json` | Published orders and scan cursor. Losing it costs at most one extra post per lot. |
| `okx.api_key_env`, `okx.secret_key_env`, `okx.passphrase_env` | `OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE` | Onchain OS project credentials; every request (the quote included) is HMAC-signed with all three. |
| `okx.enabled`, `okx.post` | `true`, `true` | Serve `OKXIntentAdapter`; `post: false` logs standing asks only. |
| `okx.api_url`, `okx.chain_index` | `https://web3.okx.com`, by chain id | API base and `chainIndex` (Ethereum, BSC, Arbitrum, Base, X Layer by default). |
| `okx.slippage_percent`, `okx.expiration` | `"0.1"`, 180 | Quote parameters: slippage (the signed `toTokenAmount` is the quote after it) and order validity in seconds (max 300). |
| `okx.owner_prefix` | `true` | UNVERIFIED: whether the posted eip1271 signature carries the owner prefix (see below). |
| `kyber.enabled`, `kyber.post` | `true`, `true` | Serve `KyberDsloAdapter`; `post: false` logs standing asks only. |
| `kyber.api_url`, `kyber.client_id` | `https://limit-order.kyberswap.com`, none | Maker API base (no key) and the optional `X-Client-Id` (fee recipient selection; also sent with the `kyber` price source). |
| `velora.enabled`, `velora.post` | `true`, `true` | Serve `VeloraDeltaAdapter`; `post: false` logs standing asks only. |
| `velora.api_url`, `velora.partner` | `https://api.paraswap.io`, none | Delta API base (no key) and the partner string sent with the price and the order (also with the `velora` price source). |
| `velora.partner_address`, `velora.partner_fee_bps` | none, 0 | A partner fee the order signs away (`partnerAndFee`); the signed `destAmount` is grossed up so the netted floor still covers the curve. |
| `velora.partially_fillable` | `false` | Sign the `FillableOrder` primary type (pro-rata fills) instead of the fill-or-kill `Order`. |
| `velora.order_type` | `LIMIT` | ASSUMED: the API's order `type` for a standing ask at the curve floor (the API default is `MARKET`). |
| `zerox.api_key_env` | `ZEROX_API_KEY` | 0x Gasless API key (`0x-api-key` header); without it MetaTxn orders go the self-relay way. |
| `zerox.enabled`, `zerox.post` | `true`, `true` | Serve `ZeroExSettlerAdapter`; `post: false` skips the Gasless API. |
| `zerox.api_url`, `zerox.chain_id` | `https://api.0x.org`, connected chain | API base and the `chainId` sent with the quote and the submit. |
| `zerox.flavor` | none | Override of the adapter's pinned flavor (`metatxn` or `intent`); the default reads `intent()`. |
| `zerox.slippage_ppm` | none | `slippagePpm` for the quote (the signed `minAmountOut` is the quote after it); none leaves the API default. |
| `zerox.relay` | `log` | MetaTxn orders without the API: `none` (skip), `log` (print the `executeMetaTxn` calldata), `self-actions` (keeper mode: simulate and send it from the keeper key). |
| `zerox.routes` | `[]` | `{token, pool, i, j}` entries: the Curve pool the self-authored action list sells `token` (coin `i`) for the payment token (coin `j`) on. |
| `hashflow.api_key_env` | `HASHFLOW_API_KEY` | The credential Hashflow issues; every taker call sends it as `Authorization`. |
| `hashflow.enabled`, `hashflow.post` | `true`, `true` | Serve `HashflowAdapter`; `post: false` (or no credential) logs the quote-request template only, there are no standing asks. |
| `hashflow.api_url`, `hashflow.source` | `https://api.hashflow.com`, `api` | API base and the RFQ `source` identity (`api` for an API wallet trader, else the aggregator name Hashflow registered). |
| `hashflow.chunk` | none | Base-token amount requested per quote (an RFQ-m fill is exact); default: everything available. |
| `oneinch.api_key_env` | `ONEINCH_API_KEY` | Developer Portal key for the orderbook and Fusion endpoints. |
| `oneinch.enabled` | `true` | Serve the three 1inch adapters. |
| `oneinch.post` | `true` | Plain `OneInchAdapter` orders only: `false` logs and records them for direct fills instead of POSTing (the public orderbook rejects plain orders). |
| `oneinch.orderbook_url`, `oneinch.fusion_url` | `https://api.1inch.dev/orderbook/v4.0`, `https://api.1inch.dev/fusion` | Base URLs; `/{chainId}/...` is appended. |
| `oneinch.fusion_auction_duration` | 180 | Fusion resolver auction length (s), clipped to `order_ttl`. |
| `oneinch.fusion_start_premium_bps` | 100 | Fusion auction start amount above the curve quote (the end amount). |
| `uniswapx.enabled` | `true` | Serve `UniswapXAdapter`. |
| `uniswapx.post` | `false` | Also attempt the order-service POST and record its rejection (see below). |
| `uniswapx.order_service_url` | `https://api.uniswap.org/v2/order` | Where the attempt goes. |
| `bebop.api_key_env` | `BEBOP_API_KEY` | Optional bearer key for the JAM API. |
| `bebop.enabled`, `bebop.post` | `true`, `true` | Serve `BebopJamAdapter`; `post: false` publishes standing asks only. |
| `bebop.api_url`, `bebop.chain` | `https://api.bebop.xyz`, by chain id | API base and chain slug (ethereum, polygon, arbitrum, base, bsc, optimism, hyperevm, avalanche; set `chain` for others, e.g. robinhood). |

Adapters are never configured: each cycle the keeper reads the auction's
`AdapterRegistry`, takes the active adapters, and matches their
`ADAPTER_VERSION()` to a publisher (`CowAdapter` → `CowPublisher`,
`OKXIntentAdapter` → `OKXIntentPublisher`,
`KyberDsloAdapter` → `KyberDsloPublisher`,
`VeloraDeltaAdapter` → `VeloraDeltaPublisher`,
`ZeroExSettlerAdapter` → `ZeroExSettlerPublisher`,
`HashflowAdapter` → `HashflowPublisher`,
`OneInchAdapter` / `OneInchFeeTakerAdapter` / `OneInchFusionAdapter` → the
three 1inch publishers,
`UniswapXAdapter` → `UniswapXPublisher`,
`BebopJamAdapter` → `BebopJamPublisher`). A rail section with `enabled:
false` skips its adapters. Adding a rail is one class in
`keepers/auction_keeper/rails/`.

## Run

```bash
python -m keepers.auction_keeper run --config config.yaml              # all networks, forever
python -m keepers.auction_keeper run --config config.yaml --network gnosis --once --dry-run
python -m keepers.auction_keeper status --config config.yaml           # lots and recorded orders
```

Each cycle, per auction:

1. candidate tokens = whitelist ∪ tokens with a `LotStaged` event since the last scan, minus the blacklist;
2. snapshot each lot (`available`, `getAmountNeeded`, `window`); inactive lots are skipped;
3. estimate each live lot's market (`pricing.sources`, first answer wins) and plan its order validity (`keepers/auction_keeper/core/schedule.py`):
   * far above the market (quote > market × (1 + `ttl.near_above`)): one order valid until the curve is predicted to enter the band, found by bisecting the auction's own `getAmountNeeded(token, amount, ts)` projection, capped at `ttl.max`;
   * inside the band: `ttl.min`, replaced the moment it expires;
   * below the market but unfilled, or no market known: `order_ttl`;
4. for every active adapter with a publisher, (re)publish, lots nearest the market first, when there is no recorded order, it has expired, or it was built for a previous lot window, keeping the adapter's open orders within `max_open_orders`;
5. with `native.enabled`, try to take every live lot itself (next section).

Between cycles the loop sleeps until the earliest recorded expiry (at most
`poll_interval`, or `native.poll` while a lot is near its route quote), so
an expiring order is replaced without a gap and a lot never has two open
orders. `status` prints each live lot's plan (zone, market, validity,
predicted band entry) and, with the native rail, its route quote and chunk.

### The native rail (Curve)

Where no settlement adapter is active, or alongside them, the keeper fills
lots itself through the `AuctionTaker` periphery (`keepers/auction_keeper/rails/native.py`):
the taker receives the lot, swaps it to want on Curve through the router
NG, pays the auction's quote from the proceeds and sends the rest to the
keeper. It needs only gas and works on any chain with Curve pools.

Routes come from the `api.curve.finance` pool list (`keepers/auction_keeper/routing/curve_router.py`):
plain `exchange` hops, up to `native.max_hops`, through pools above
`native.min_pool_usd`, ranked by the router's own `get_dy` so the quote the
keeper acts on is what the swap pays. The take is chunked: the largest
amount whose price impact on the route stays under `native.max_impact` is
taken, the rest stays in the lot and is quoted again next poll, so a thin
pool is hit in pieces that arbitrage refills in between. A chunk is taken
when the auction's quote for it is `native.margin` under the route quote,
after a simulation; a reverted simulation is reported and retried.

The same router quote is available to the schedule as the `router` price
source (`pricing.sources`), which needs no feed and no key.

The CoW publisher builds the GPv2 order (`sellAmount` = available,
`buyAmount` = live quote, `validTo` = the scheduled validity, never past
the window end), checks it
through the auction's `isValidSignature` (typed reverts such as
`BadBuyAmount()` or `OrderNotValid('BadAppData')` are logged), checks the
vault relayer's allowance, and posts with `signingScheme: eip1271`, `from` =
auction, signature `cow_adapter ++ abi_encode(order)`. The buy token is
whatever the auction's `want()` is (the FeeCollector target: crvUSD on most
chains, but any token works).

A missing allowance is the one on-chain step: in `keeper` mode the loop
sends the permissionless `sync_executor_approvals` itself; in `offline` mode
it posts anyway and logs the call, which anyone (another keeper, a
multisig, the `Executor`) can send before a fill.

Every other publisher follows the same shape — build from the live lot quote
(through the adapter's `order_for`, so the adapter's typed shape errors
surface at build time), validate through `isValidSignature` (economic
errors such as `BadBuyAmount()` are logged), check the executor allowance,
post or log, record one state entry per (adapter, token) with the lot
window so the order is reposted before it expires and never past the lot
end. Every (re)post uses a fresh salt or nonce. The shared steps live in
`AdapterPublisher` (`valid_to`, `validate_signature`, `ensure_allowance`);
the rail's feed is reached through `adapters/common.py`'s `JsonClient`.

### OKX rail (`okx`)

Executor: the OKX DEX `TokenApprove` (`adapter.token_approve()`). Feed
mode (`post: true` with all three credentials and a `chainIndex`): every
poll, `GET {api_url}/api/v6/dex/aggregator/quote?chainIndex&mode=intent&swapMode=exactIn&fromTokenAddress=<lot>&toTokenAddress=<want>&amount=<available>&userWalletAddress=<auction>&swapReceiverAddress=<FeeCollector>&signingScheme=eip1271&slippagePercent&expiration`
(parameters and the `{code, data[{quoteId, signData{domain, message,
primaryType, types}, toTokenAmount, ...}], msg}` answer from the Onchain OS
"Get Intent Quotes" page, `web3.okx.com/onchainos/dev-docs/trade/dex-intent-get-quote`).
The order is `signData.message` as the API dictates it — `appData`,
`commissionInfos`, `validTo` and the signed `toTokenAmount` (the client
cannot set amounts: the create-order body must match the message, and its
`toTokenAmount` is the quoted amount with slippage applied, per the
"Create Intent Order" page); the publisher checks the domain
(`verifyingContract` = the adapter's settlement, `chainId`), owner,
receiver, pair, `exactIn` without partial fills, the sell amount within the
available amount, `toTokenAmount` at or above the curve quote for it (a
quote below the curve is skipped: `check_order` would reject it) and
`validTo` inside the lot window; signs `adapter ++ abi_encode(message)`,
validates it on-chain, then `POST
{api_url}/api/v6/dex/aggregator/intent/create-order` with `{chainIndex,
fromTokenAddress, toTokenAddress, fromTokenAmount, toTokenAmount,
userWalletAddress, swapReceiverAddress, validTo, quoteId, appData,
signingScheme: "eip1271", signature, commissionInfos}` (field list from
`.../trade/dex-intent-create-order`; the answer's `data[0].orderUid` becomes
the record id, else the on-chain uid `digest ++ owner ++ validTo`). Every
request carries `OK-ACCESS-KEY`, `OK-ACCESS-SIGN =
base64(HMAC-SHA256(timestamp + METHOD + requestPath + body, secret))`,
`OK-ACCESS-TIMESTAMP` (ISO 8601 UTC with milliseconds) and
`OK-ACCESS-PASSPHRASE` (`.../home/api-access-and-usage`); the page's JS
example appends the query string to the GET request path, which is what
is signed here (assumed, the prose example has no query). API-level errors
arrive as HTTP 200 with a non-zero `code` and are logged.

**UNVERIFIED (`okx.owner_prefix`)**: on-chain the settlement reads the
owner from the first 20 bytes of an eip1271 signature and forwards the
rest to the owner's `isValidSignature`, and the OKX intent solver SDK
(`okxlabs/Web3-DEX-evm-intent-sdk`, `packages/solver`) copies the
orderbook's `signature` into the settle calldata verbatim — so the bytes
the orderbook stores must carry the owner prefix. Whether the submitter
sends `burner ++ adapter ++ abi_encode(order)` (`owner_prefix: true`, the
default) or only `adapter ++ abi_encode(order)` with the orderbook adding
the owner (`false`) is not documented (the docs only say an eip1271
signature is "built per the OKX Intent SDK rules", and the SDK has no
signing code); one keyed probe settles it. Also unverified: whether the
orderbook `eth_call`s the owner's ERC-1271 before relaying (the quickstart
says verification is an on-chain read, so presumably yes). No OKX
credentials were available here, so nothing was exercised live; the test
fixtures copy the docs' example shapes.

Standing-ask mode (`post: false`, credentials or `chainIndex` missing):
the `order_for` order (no commissions, random `appData` as salt, `validTo`
shortened to the scheduled validity) is validated and logged. Only
allowlisted OKX solvers can settle, so this is a log, not a permissionless

### Kyber rail (`kyber`)

Executor: the DSLO protocol itself (`adapter.protocol()`; it does the
`transferFrom`). Feed mode (`post: true`): the maker API's two calls, no
key (pages: docs.kyberswap.com → Developer guide → Limit Order API →
"Maker API" reference and the "Place a Limit Order" how-to). `POST
{api_url}/write/api/v1/orders/sign-message` with `{chainId, makerAsset,
takerAsset, maker: <auction>, receiver: <FeeCollector>, makingAmount:
<available>, takingAmount: <curve quote>, expiredAt: the scheduled
validity, never past the window end}` returns `data{domain, types,
primaryType, message}`, the message carrying the API's `salt`, `feeConfig`
(the server-computed fee, selected by the optional `X-Client-Id` header),
`allowedSender`, the linear getters and the `and([nonceEquals(maker, 0),
timestampBelow(expiredAt)])` predicate; the publisher checks
`domain.verifyingContract` (the adapter's protocol) and `chainId`, maker,
receiver, pair and amounts against the lot, asks the adapter's `describe`
for the predicate's last fillable second (`expiredAt - 1`,
`timestampBelow` is strict; never past the requested bound, which never
passes the lot end), signs `adapter ++ abi_encode(message)`, validates it
on-chain, then `POST {api_url}/write/api/v1/orders` with the sign-message
body plus `salt` and `signature` (the reference says `data.id` comes back,
the how-to says `orderId`; both are logged). The reference states that
ERC-1271 and ERC-6492 smart-wallet signatures are accepted, so no `maker`
prefix is added; this was verified from the docs only, no order was
submitted here. Fills carry KyberSwap's per-fill operator co-signature
(not the maker's concern) and the maker signature is checked on the first
fill only, so disabling the adapter stops nothing already validated: run
the allowance sync. The record is keyed by the order digest; a repost at a
lower quote is a new order (the API issues a fresh salt).

Standing-ask mode (`post: false`): the `order_for` order (no fee, any
taker) with a bare `timestampBelow(valid_to + 1)` predicate, validated and
logged as `fillOrderTo(order, orderSignature)`; every fill still needs the
operator co-signature, so this is a log rather than a permissionless
fallback.

### Velora rail (`velora`)

Executor: the Delta contract itself (`adapter.settlement()`; it does the
`transferFrom` with an empty permit). Feed mode (`post: true`): `GET
{api_url}/v2/delta/prices?srcToken&destToken&amount=<available>&srcDecimals&destDecimals&chainId&side=SELL&userAddress=<auction>&beneficiary=<FeeCollector>[&partner]`
(parameters and the `DeltaPrice` answer — `route.{origin,destination}.
output.amount`, `partner`, `spender` — from the Velora SDK's
`getDeltaPrice`, `src/methods/delta/getDeltaPrice.ts` and `types.ts`; the
docs page "Delta swap" at docs.velora.xyz/docs/integrate/api/delta-swap
documents the same flow through `GET /v2/quote?mode=DELTA`). The order is
built here, not by the API: the docs' `POST /v2/delta/orders/build` wants
the quote's `route` object verbatim (undocumented contents), so the
publisher takes `VeloraDeltaAdapter.order_for` (owner = the auction,
beneficiary = the FeeCollector, the whole available amount, the curve quote,
a random nonce, no permit/metadata/bridge) and sets `deadline` = the
scheduled validity (never past the window end), `partnerAndFee` from
`velora.partner_address` / `partner_fee_bps` with `destAmount` grossed up
so the fee-netted floor the adapter checks still covers the curve, and
`expectedAmount` = max(quoted output, `destAmount`) (surplus above it is
shared). A quote below the curve does not block the post: the floor is
ours to set, the order simply stands until the curve meets the market. The
pair `adapter ++ abi_encode(order)` is validated on-chain under the `Order`
or, with `partially_fillable`, the `FillableOrder` digest, then `POST
{api_url}/v2/delta/orders` sends `{chainId, order{owner, beneficiary,
srcToken, destToken, srcAmount, destAmount, expectedAmount, deadline, kind:
0, nonce, partnerAndFee, permit, metadata, bridge}, signature,
partiallyFillable, type, partner?}` — the SDK's `DeltaOrderToPost`
(`postDeltaOrder.ts`, `helpers/types.ts`: `DeltaAuctionOrder`); the answer
is the stored `DeltaAuction` (`id`, `status`, `orderHash`, ...). The record
is keyed by the order digest.

Unverified: the docs describe the `signature` field as a 64-byte ERC-2098
compact signature and the SDK comment says "EOA signatures must be
submitted in ERC-2098 compact representation"; whether the API relays a
long ERC-1271 signature from a contract owner is not documented and was
not exercised (no order was submitted). `type: LIMIT` for a standing ask
at the curve floor is an assumption (`velora.order_type`; the API default
is `MARKET`). Only Velora's registered agents can settle.

Standing-ask mode (`post: false`): the same order without the price call
(`expectedAmount` = `destAmount`), validated and logged as
`swapSettle(order, signature)`; a log, not a permissionless fallback.

### 0x rail (`zerox`)

Executor: Permit2 (`adapter.permit2()`; the Settler's first action pulls the
lot through `permitWitnessTransferFrom`). One adapter deployment pins one
Settler and its flavor, read from `intent()`: a `SettlerMetaTxn` adapter is
served through the Gasless API or a self-authored relay, a `SettlerIntent`
adapter publishes standing asks for 0x's solvers. The signed object is
always the Permit2 witness digest of `Order{permit(token, amount, nonce,
deadline), slippage(recipient, buyToken, minAmountOut), actions_hash}`
(signature `adapter ++ abi_encode(order)`, 276 bytes; `actions_hash` is the
Settler's hash of the action list for a MetaTxn and zero for an Intent),
the permit nonce is random per publish (Permit2 burns it on settlement) and
the record is keyed by that digest. The adapter also pins the 0x
deployer/registry and signs only while its Settler is the feature's
current or previous instance there; the publisher asks the registry
(`ownerOf` / `prev`) before every quote and, on a `SettlerRetired` answer
from either the registry or the auction router, logs once and stops
publishing for that adapter for good (0x retired the Settler: a new
adapter deployment follows the registry's current one).

**Gasless API** (`post: true`, a key in `api_key_env`, MetaTxn flavor):
every poll, `GET {api_url}/gasless/quote?chainId&sellToken=<lot>&buyToken=
<want>&sellAmount=<available>&taker=<auction>&recipient=<FeeCollector>
[&slippagePpm]` with the `0x-api-key` and `0x-version: v2` headers
(parameters, headers and the answer — `trade{type:
"settler_metatransaction", hash, eip712{types, domain, primaryType,
message}}`, `minBuyAmount`, `liquidityAvailable`, `zid` — from the v2
reference, docs.0x.org/api-reference/evm-ap-is/gasless/getquote, and the
sample response at docs.0x.org/evm/gasless-api/introduction). The
`message` is the Permit2 `PermitWitnessTransferFrom` the taker must sign:
`permitted{token, amount}`, `spender` (the Settler), `nonce`, `deadline`,
`slippageAndActions{recipient, buyToken, minAmountOut, actions}`, amounts
and the nonce as decimal strings. The publisher checks `primaryType`, the
domain (`verifyingContract` = the adapter's Permit2, `chainId`), `spender`
= the pinned Settler, the token pair, `recipient` = the FeeCollector, the
amount within the available amount, `minAmountOut` at or above the curve
quote for it (a quote below the curve is skipped: it cannot be signed,
`check_order` would reject it — `slippage_ppm` moves `minAmountOut` towards
`buyAmount`), the deadline inside the lot window and
`METATXN_TRANSFER_FROM` as the first action; rebuilds the order with the
Settler hash of the quote's actions, requires the quote's `trade.hash` to
equal the adapter's digest, validates `adapter ++ abi_encode(order)` on-chain,
then `POST {api_url}/gasless/submit` with `{chainId, trade{type, eip712
(verbatim), signature{signatureType: 5, signatureBytes}}}` and no `approval`
(the burner's allowance to Permit2 is a plain approval). The body, the
`signatureType` enum (2 EIP712 / 3 EthSign with `v, r, s`; 5 Raw with
`signatureBytes` only, "for smart contract wallets (e.g., ERC-1271)") and
the answer `{tradeHash, type, zid}` are from
docs.0x.org/api-reference/evm-ap-is/gasless/submit; `tradeHash` becomes the
record id. Unverified (no key was available, 401 on a keyless probe):
whether the backend accepts a plain ERC-1271 contract as `taker` (the quote
endpoint lists a `SMART_WALLET_NOT_SUPPORTED` error) and relays the long
raw signature verbatim to Permit2 (`INVALID_SIGNATURE` / `INVALID_SIGNER`
on submit would say no). API errors are HTTP 4xx `{name, message, data}`
and are logged; a quote that cannot be signed falls through to the
self-relay path below unless `relay: none`.

**Self-relay** (MetaTxn flavor, `post: false`, no key, or a quote that
could not be signed): the publisher authors the action list of the fork
test — `METATXN_TRANSFER_FROM(settler, permit)` then `BASIC(token, 1e6 ppm,
pool, 68, exchange(i, j, 0, 0))` through the Curve pool configured for the
token in `zerox.routes` (`min_dy` 0: the Settler's slippage check is the
floor, and it pays the recipient its whole buy-token balance, so the pool's
surplus goes to the FeeCollector) — computes `actions_hash`, builds the
order through `order_for(auction, token, nonce, 0, <the scheduled validity>,
actions_hash)`, validates it, checks the Permit2 allowance and
then, per `zerox.relay`: `none` publishes nothing, `log` logs
`executeMetaTxn(slippage, actions, zid = 0, msgSender = auction, sig)` with
its calldata for anyone but the auction to send, `self-actions` in keeper
mode simulates that call from the keeper account (the signer may not relay
its own order; the keeper EOA is fine) and sends it, a revert being logged
(the auction's typed reason bubbles through the Settler and Permit2); in
offline mode it logs like `log`. A token without a route is skipped.

**Intent** (`intent()` true): the Gasless API serves
`settler_metatransaction` trades only, so the `order_for` order (no action
hash, deadline at the scheduled validity) is validated and logged for 0x's
allow-listed solvers (`getSolvers()`), who route and call `executeMetaTxn`
themselves; a log, not a permissionless fallback.

### Hashflow rail (`hashflow`)

Executor: the `HashflowRouter` (`adapter.router()`; it pulls the base token
from the trader with a plain `transferFrom`). The rail is keeper-driven
only: an RFQ-m quote lives seconds to minutes, every fill is one exact
`txid`, nothing settles without a market maker's signature, and the pool
pays `quote.trader` — the burner — so the proceeds land on the burner and
the keeper calls the permissionless `push_target()` to move them to the
FeeCollector (`FeeCollector.forward()` calls it too at the FORWARD step).

Feed mode (`post: true` with a credential): every poll, `POST
{api_url}/taker/v3/rfq` with the header `Authorization: <credential>` and
the body `{source, baseChain: {chainType: "evm", chainId}, quoteChain:
{same}, rfqs: [{baseToken: <lot>, quoteToken: <want>, baseTokenAmount:
<available or chunk>, trader: <auction>, rfqType: 1}]}`. The URL, header,
body and the answer `{status, error?, rfqId, internalRfqIds?, quotes:
[{quoteData: {baseChain, quoteChain, baseToken, baseTokenAmount,
quoteToken, quoteTokenAmount, trader, effectiveTrader, txid, pool,
dstPool, quoteExpiry, nonce, externalAccount, dstExternalAccount},
signature, xChainFeeEstimate, targetContract, calldata, value,
hftTradingRewards}]}` are from the taker page "Getting Started - API v3"
(`docs.hashflow.com/hashflow/taker/getting-started-api-v3`), which
documents RFQ-t only (execution through `tradeRFQT` by the trader). The
`rfqType: 1` request field is ASSUMED: RFQ-m appears in the public material
only as `QuoteData.rfqType: 0 | 1` of `@hashflow/taker-js` and as
`"rfqType": 1` in an error example of the market-maker page
(`.../market-making/getting-started-api-v3`), neither of which says how a
taker asks for it; the maker page also fixes the RFQ-t maker hash (it
includes `effectiveTrader` and `nonce`), which differs from the pool's
RFQ-m hash, so an RFQ-t quote answered to an RFQ-m request fails the
signer check below instead of being relayed. The trader side of the request
comes from `HashflowAdapter.order_for(auction, token, amount)` (typed
reverts surface at build time; its `quoteTokenAmount` is the curve floor).

The quote is checked before anything is signed: this chain, `rfqType` 1
when present, the auction as `trader`, the lot pair, a pool, the base
amount exactly as requested and within the available amount,
`quoteTokenAmount` at or above the curve quote for it (below: skipped, the
auction's `check_order` would reject it), `quoteExpiry` inside the lot
window, and the maker's 65-byte signature recovering, under the pool's
EIP-191 digest (`HashflowPool._hashQuoteRFQM`), to the signer the pool's
`signerConfiguration()` reports, with the pool enabled. Then the trader
signature `adapter ++ abi_encode(Quote)` (the nine EIP-712 fields) is
validated through the auction's `isValidSignature` on the router's digest
(an `eth_call`: the router itself reports every signature failure as its
own `Invalid signer.`, hiding the typed reason), and the router's allowance
is checked (synced in keeper mode).

Submission: there is no public endpoint to hand a maker-signed RFQ-m quote
back to Hashflow (the taker docs name only `tradeRFQT`), while
`HashflowRouter.tradeRFQM` takes the quote with both signatures from any
sender (`IHashflowRouter`: "the 3rd party can be the Market Maker itself").
So in keeper mode the keeper relays `tradeRFQM(RFQMQuote{pool,
externalAccount, trader, baseToken, quoteToken, baseTokenAmount,
quoteTokenAmount, quoteExpiry, txid, takerSignature, makerSignature})` from
its own EOA and calls `push_target()` on the burner right after (a revert
of the relay is logged, nothing is recorded, the `txid` stays unburnt); in
offline mode the calldata is logged for any relayer and a `want` balance on
the burner is reported. Target already on the burner is pushed at the start
of every poll in keeper mode. The record is keyed by `txid` and expires
with the quote, so the next poll quotes again while the lot is live. API
failures arrive as `status: "fail"` with an `error` string (or HTTP 401/403
without a valid credential) and are logged.

No credential was available here, so nothing was exercised against the
live API; the test fixtures copy the docs' shapes and settle through the
router and pool mocks of the adapter tests.

### 1inch rail (`oneinch`)

Executor: the Limit Order Protocol v4 host (AggregationRouterV6), whose
allowance is checked/synced. Signature: `adapter ++ abi_encode(order,
extension)`. The order's expiration is shortened to the scheduled validity.

* **`OneInchAdapter` (plain orders)**: `order_for` output, no extension.
  The public orderbook only relays orders with the standard FeeTaker
  extension, so with `oneinch.post: false` the publisher logs
  `fillContractOrder(order, signature)` and records the order hash for
  direct on-chain fills (anyone may fill). `post: true` POSTs the SDK body
  (`{orderHash, signature, data{...order, extension: "0x"}}`) to
  `oneinch.orderbook_url/{chainId}` — for a private feed; the public one is
  expected to reject it.
* **`OneInchFeeTakerAdapter` (orderbook)**: `GET
  {orderbook_url}/{chainId}/fee-info?makerAsset&takerAsset&makerAmount&takerAmount`
  (bearer key), the answer's `extensionAddress` must be the adapter's
  FeeTaker, and its fees, whitelist (promotees), fee receivers and the
  auction's receiver are encoded into the FeeTaker extension exactly as
  `@1inch/limit-order-sdk`'s `Sdk.createOrder` does (byte-identical to the
  SDK vector in `tests/burners/test_feetaker_module.py`); the `source`
  track code lands in salt bits [224, 256) like the SDK's `setSource`.
  `order_for(auction, token, salt, 0, extension)` builds the order,
  `describe(extension)` is logged, and `POST {orderbook_url}/{chainId}`
  sends `{orderHash, signature, data{salt, maker, receiver, makerAsset,
  takerAsset, makingAmount, takingAmount, makerTraits, extension}}` (the
  SDK's `submitOrder` body; the SDK master points at `orderbook/v4.1`, the
  default here is `v4.0` — override `orderbook_url` if the portal moves).
* **`OneInchFusionAdapter` (Fusion)**: `POST
  {fusion_url}/quoter/v2.0/{chainId}/quote/receive?fromTokenAddress&toTokenAddress&amount&walletAddress=<auction>&surplus=true`
  with body `{customPreset: {auctionDuration, auctionStartAmount,
  auctionEndAmount}}` where the end amount is the curve quote now and the
  start amount is `fusion_start_premium_bps` above it (the
  `QuoterApi.getQuoteWithCustomPreset` request). The answer's
  `settlementAddress` must be the adapter's settlement; `presets.custom`,
  `whitelist`, `fee`, `surplusFee`, `marketAmount`, the integrator fields
  and `source` are encoded into the Fusion extension the way
  `quote.createFusionOrder({receiver: FeeCollector, preset: "custom"})`
  does (auction from `now + startAuctionIn`, resolvers whitelisted from now,
  an exclusive resolver first, surplus block = market amount and fee when
  the market beats the quote; byte-identical to the SDK vector in
  `tests/burners/test_fusion_module.py`). The order expires 12 s after the
  auction (the SDK's delay), the auction length is clipped so that fits
  the scheduled validity and the lot window. Submit: `POST
  {fusion_url}/relayer/v2.0/{chainId}/order/submit` with `{order (8
  fields), signature, quoteId, extension}` (`RelayerApi.submit`).

Request and body shapes were taken from the SDK sources
(`limit-order-sdk/src/api/api.ts`, `fusion-sdk/src/api/{quoter,relayer}`)
and the extension encoders from the SDK vectors already in the contract
tests; no Developer Portal key was available here, so neither endpoint was
exercised live, and whether the backends accept a non-65-byte ERC-1271
signature from a contract maker remains the open question noted in
`contracts/burners/README.md`.

### UniswapX rail (`uniswapx`)

Executor: Permit2. `UniswapXAdapter.order_for` builds a fixed-price
fill-or-kill `ExclusiveDutchOrder` for the whole available amount with a
fresh Permit2 nonce; the deadline is shortened to the scheduled validity;
the pair `(abi_encode(order), adapter ++ abi_encode(order))` is validated
against the Permit2 witness digest and logged as
`reactor.execute(SignedOrder(order, sig))` for fillers, and recorded by
order hash. **Known limitation**: the public Uniswap order service accepts
only 65-byte ECDSA signatures and only orders it quoted itself, so the
ERC-1271 signature cannot be posted to the public feed; the publisher is
"build + validate + log". With `uniswapx.post: true` it also POSTs
`{encodedOrder, signature, chainId}` (the `uniswapx-service` body) to
`uniswapx.order_service_url` and records the backend's answer or rejection
under `details.orderService` without failing the publish.

### Bebop rail (`bebop`)

Executor: the JAM balance manager. Feed mode (`post: true`): every poll,
`GET {api_url}/jam/{chain}/v2/quote?sell_tokens=<lot>&buy_tokens=<want>&sell_amounts=<available>&taker_address=<auction>&receiver_address=<FeeCollector>&approval_type=Standard&gasless=true`
(bearer key when configured; a quote for a contract taker worked without
one on 2026-11-02). The answer's `toSign` is checked against the lot —
taker, receiver, the token pair, the sell amount within the available
amount, `buyAmounts[0]` at or above the curve quote for that amount (a JAM
quote below the curve is skipped: it cannot be signed, `check_order` would
reject it), no hooks, expiry inside the lot window — signed as `adapter ++
abi_encode(toSign)`, validated on-chain, and `POST
{api_url}/jam/{chain}/v2/order` sends `{quote_id, signature, sign_scheme:
"EIP1271"}`. The winning solver settles right away; the record expires with
the quote (about a minute), so the next poll quotes again while the lot is
live. API-level errors arrive as HTTP 200 `{"error": {errorCode,
message}}` and are logged. Note that `toSign.buyAmounts[0]` is the quote's
`minimumAmount` (the API's default slippage applied), which is what the
settlement enforces. The order endpoint's body was verified structurally
in an earlier probe (`sign_scheme` must be one of `EIP712`, `EIP1271`,
`ETHSIGN`); its success response shape is not documented and is recorded
as returned. Standing-ask mode (`post: false`, or no chain slug): the
permissionless `order_for` order (any executor, no fees), expiry shortened
to the scheduled validity, validated and logged as `settle(order, sig)`.

Run it as a long-lived service (systemd, docker); it is stateless beyond the
state file and safe to restart.

## Custom executions

```python
from auction_keeper import TakePlan, approve_call, connect, curve_router_exchange, raw_call

ex = connect("config.yaml", "ethereum")  # key from keeper_key_env, taker from auction_taker
lot = ex.lot(AUCTION, CRV)  # available, amount_needed, window, timestamp
print(lot)

# 1) Route through the AuctionTaker: the lot is swapped inside the take
#    callback, the quote is paid from the proceeds, the rest is profit.
route = [
    approve_call(CRV, CURVE_ROUTER, lot.available),
    curve_router_exchange(
        CURVE_ROUTER,
        [CRV, POOL, lot.want],
        [[1, 0, 1, 1, 2]],
        lot.available,
        min_out,
        receiver=ex.taker.address,
    ),
    # or raw_call(aggregator_tx["to"], aggregator_tx["data"]) from an aggregator API
]
ex.take_with_route(
    TakePlan(AUCTION, CRV, route=route, min_profit=0),
    max_payment=min_out,  # wait until the quote is at most this
    not_before=lot.start + 2 * 3600,  # and not before this timestamp
    poll=15,
)

# 2) Pay from your own want balance (lot.want) with take_with_limits (payment cap + deadline).
ex.take_direct(AUCTION, CRV, max_payment=10_000 * 10**18, max_amount=lot.available)
```

Both calls simulate first (`CallReverted` carries the decoded reason) and
return the receipt; `dry_run=True` stops after the simulation. `wait_for`
is available on its own for scheduling without executing. A `ChainClient`
other than web3 (tests use Titanoboa) can be passed straight to `Executor`.

## Tests

```bash
python -m pytest tests/keeper -q
```

The tests run the package unchanged against the real contracts in Titanoboa
through a `BoaClient`; only the orderbook HTTP calls are faked.

## Test in prod (Polygon, CoW)

`scripts/polygon_test_in_prod.py` deploys the real burner stack against
`contracts/testing/dutch_auction/FeeCollectorMock.vy`, a FeeCollector stand-in
whose calendar (3-hour periods: SLEEP 15 min, COLLECT 30 min, EXCHANGE 2 h,
FORWARD 15 min), target (frxUSD), and keeper fee are set in the script. The
EOA in `POLYGON_TEST_KEEPER_KEY` owns everything and gets every balance back.

```bash
python scripts/polygon_test_in_prod.py rehearse   # whole flow on a fork, nothing sent
python scripts/polygon_test_in_prod.py deploy     # live; writes deployments.polygon-test.json
                                                  # and keepers/auction_keeper/configs/config.polygon-test.yaml
# send the sell tokens (CRV) to the FeeCollectorMock, then inside COLLECT:
python scripts/polygon_test_in_prod.py collect    # stages the lot, syncs the relayer allowance
python -m keepers.auction_keeper run --config keepers/auction_keeper/configs/config.polygon-test.yaml
python scripts/polygon_test_in_prod.py status     # epoch, lot, quote, balances
python scripts/polygon_test_in_prod.py recover    # everything back to the EOA
```
