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
| `keepers/auction_keeper/config/` | The YAML config: `core.py` (`NetworkConfig`, `KeeperConfig`, loading, `${ENV}` expansion), `pricing.py` (`pricing` / `ttl` sections), `loop.py` (`focus` / `lookahead` / `fees`), `native.py` (`native`), `rails.py` (`cow` and the per-rail sections: `RailSettings`, `parse_rail`), `common.py` (`ConfigError`, primitive validators). `from keepers.auction_keeper.config import ...` exports the dataclasses and `load_config`. |
| `keepers/auction_keeper/chain/` | The node: `client.py` (`ChainClient` protocol, `Web3Client`: next-block calls, private send endpoints), `abi.py` (selectors, call encoding, revert decoding), `contracts.py` (typed readers of the auction, registry, adapters, `AuctionTaker`, ERC-20), `events.py` (the auction's logs: `LotStaged`, `Taken`, settings changes), `boa_client.py` (the Titanoboa client the tests and fork rehearsals use). |
| `keepers/auction_keeper/core/` | The loop: `keeper.py` (`NetworkKeeper`, `run_networks`), `model.py` (the auction simulated locally: the curve and every lot's ask, integer for integer), `book.py` (one auction's lot records, fed by logs), `focus.py` (the ranking pass and the selection of the lots to work on), `schedule.py` (market zones and order validity), `fees.py` (the tip of a take), `state.py` (the state file). |
| `keepers/auction_keeper/pricing/` | `MarketPricer` (every source asked, the highest answer believed, estimates kept for a long time), `build_sources` and `sources/` (`curve`, `llama`, `coingecko`, `cow`, `kyber`, `velora`, `oneinch`, `router`). |
| `keepers/auction_keeper/routing/` | `curve_router.py`: the Curve pool catalogue, the route search and the router quote/exchange calls. |
| `keepers/auction_keeper/rails/` | The settlement rails: the `AdapterPublisher` base and registry (`__init__.py`), `common.py` (`JsonClient`, bearer headers, salts), `cow.py`, `native.py` (the keeper's own take through the `AuctionTaker`); every other rail's publisher lands here as `<rail>.py`. |
| `keepers/auction_keeper/execute.py` | Custom executions (`Executor`, `TakePlan`, `connect`); one module, imported by scripts and agents. |
| `configs/` | `config.example.yaml` and the production configs (`config.polygon.yaml`, `config.fraxtal.yaml`, `config.polygon-test.yaml`). |

`tests/keeper/` mirrors it (`chain/`, `config/`, `core/`, `pricing/`, `routing/`, `rails/`), with the shared fixtures in `tests/keeper/conftest.py`; `tests/keeper/pure/` holds the tests that need neither Titanoboa nor a node.

## Configure

Copy [`config.example.yaml`](config.example.yaml). Per network only `rpc`
and `auctions` are required; `${NAME}` placeholders read the environment
(`~/.env` is loaded first).

| Setting | Default | Meaning |
|---|---|---|
| `poll_interval` | 60 | Longest wait between cycles (global or per network). The loop wakes earlier for an expiring order and for the next price step of a lot it is working on. A cycle over hundreds of lots is two requests plus the work on the lots in focus, so a short interval is cheap. |
| `send_rpc` | none | Endpoints transactions are submitted through instead of `rpc`: a URL or a list of them (`${ENV}` expanded like `rpc`). The same signed transaction is posted to all of them at once, each with a 5 s timeout; one acceptance is enough, a refusal or a timeout of the others is logged, and only when none accepts does the send fail. Reads, the nonce, the gas estimate and the receipt stay on `rpc`. Meant for private, MEV-protected relays and builders, so a take is not broadcast to the public mempool; on Ethereum for example `[https://rpc.flashbots.net/fast, https://rpc.mevblocker.io/fullprivacy, https://rpc.titanbuilder.xyz, https://rpc.beaverbuild.org]`. MEV Blocker's default endpoint shares transactions with searchers for backrunning; `/fullprivacy` does not. See "Sending privately" below. |
| `receipt_timeout` | 300 | Seconds to wait for a transaction's receipt before it is reported as not included (the allowance syncs; takes use `native.receipt_timeout`). |
| `order_ttl` | 300 | Seconds a published order stays valid when the schedule has no market for the lot, or the quote is already below it. Reposted at the live quote when it expires, never past the lot window. |
| `pricing.sources` | `[curve, llama, cow]` | Market estimate sources; every one is asked and each answer is a price level the lot is worked around (see "Disagreeing price sources"), `[]` disables pricing (uniform TTL, no ranking). USD feeds: `curve` (prices.curve.finance), `llama` (coins.llama.fi), `coingecko`. Swap quotes for the exact lot: `cow` (the orderbook `/quote`), `kyber` (KyberSwap routes), `velora` (ParaSwap `/prices`), `oneinch` (swap quote with `pricing.oneinch_api_key_env`, skipped without the key; UNVERIFIED). |
| `pricing.cache` | 60 | Seconds a market estimate is reused for a lot near its market (within reach of `focus.threshold`). |
| `pricing.cache_far` | 3600 | Seconds an estimate is reused for a lot far above its market; never longer than half the time the curve needs to bring the lot near. Estimates are kept per token, scaled to what is left of the lot, and saved in the state file, so a restart asks nobody. A token no source prices is asked about again after this long. |
| `pricing.budget` | 5 | Seconds a cycle may spend asking the sources for estimates that are due (the lots near their market first, then the ones never asked about, then the far ones). The first pass over a freshly staged week is spread over several cycles; lots staged for a window that has not opened are priced ahead of it. `0`: never ask. |
| `pricing.workers` | 4 | Lots asked about at once (threads). |
| `pricing.chains` | by chain id | Chain slug per feed (`{curve: polygon, llama: polygon, coingecko: polygon-pos, kyber: polygon}`); the defaults in `pricing.CHAINS` cover Ethereum, Polygon, Arbitrum, Base, Optimism, Gnosis, Avalanche, BSC, Fantom and Fraxtal where the feed does (Kyber and Velora do not serve Fraxtal or Gnosis). |
| `pricing.coingecko_api_key_env`, `pricing.coingecko_url` | `COINGECKO_API_KEY`, `https://api.coingecko.com/api/v3` | CoinGecko demo key and API base; a pro key goes with `https://pro-api.coingecko.com/api/v3`. |
| `pricing.oneinch_api_key_env` | `ONEINCH_API_KEY` | Developer Portal key for the `oneinch` quote source. |
| `ttl.near_above`, `ttl.near_below` | 0.05, 0.03 | The band around each source's price: a quote at most 5% above or 3% below one of them is "near". The curve falls about a third of a percent per step, so a band is some 25 steps (a quarter of an hour) of dense orders per price. Prices closer to each other than a band share one dense zone. |
| `ttl.min` | 60 | Order validity inside the band (the CoW orderbook minimum; one or two curve steps). |
| `ttl.every_step` | `false` | Inside the band, replace an order as soon as the curve has stepped below its limit instead of when it expires. The replaced order stays open until its own expiry, so a lot holds two slots of `max_open_orders` for up to `ttl.min`. |
| `ttl.max` | 7200 | Longest validity far above the market, where one order waits for the curve to reach the band. |
| `focus.threshold` | 0.25 | Work on every lot whose ask is at most this far above its highest market estimate (0.25: within 25%), however many they are. Only these lots cost requests (see "Each cycle"). |
| `focus.nearest` | 5 | Plus this many of the lots nearest to their market, whatever the distance (so the lots next in line already have an order and a route quote). |
| `focus.blind` | 2 | Plus this many lots no source has a price for, in rotation: each gets an order at `order_ttl` and, with the native rail, a route quote, which then serves as its estimate. |
| `focus.max_lots` | 40 | At most this many lots a cycle, nearest first. |
| `focus.confirm` | 60 | Seconds the on-chain `available` read of a worked lot is reused. Takes are also seen in the logs every cycle; this read catches what no log says. |
| `focus.audit` | 2 | Lots read back from the chain per cycle (three calls each), in rotation over all of them: a kill switch, a lot staged before the log scan, tokens moved without a take. `0` turns it off. |
| `lookahead.blocks` | 1 | Price lots for the block this many blocks after the latest one: the block a take sent now can land in. The timestamp is the latest block's plus `blocks` x the chain's block time (12 s on Ethereum, 1 s on Arbitrum, ...: `config.loop.BLOCK_TIMES`). `0`: price the latest block. |
| `lookahead.seconds` | unset | The same in seconds, for a chain without a known block time or with irregular blocks; wins over `blocks`. |
| `lookahead.orders` | `false` | Also give adapter orders the next block's price. Off by default: the order is checked against the auction (and by the orderbook) on the chain as it is, where a price one step ahead is `BadBuyAmount`. Turn it on only for an orderbook known to validate signatures on the pending block. |
| `fees.priority_floor`, `fees.priority_cap` | 0, 5 | Gwei per gas a take tips at least and at most. The floor is paid whatever the take earns. |
| `fees.profit_share` | 0.5 | Share of a take's profit (route quote minus payment) that may go to gas. The base fee comes out of it first, the rest is the tip (see "The tip of a take"). |
| `fees.native_price` | from the price sources | Want tokens per native token, to convert the profit into a gas price. Unset, the price sources are asked about the wrapped native token; with no answer the tip is the node's suggestion within the floor and the cap. |
| `native.enabled` | `false` | The native rail: take lots through the `AuctionTaker` with a router swap, alongside the adapters (see below). Needs `auction_taker`; sends in `keeper` mode, simulates and reports in `offline` mode. |
| `native.backends` | `[curve]` | The routers of the rail, a list out of `curve` (router NG over routes from the pool catalogue) and `electric` (the electric router, see below). With several, every lot is quoted on all of them at once and swapped through the one that pays more; a router that is down or cannot route the lot is left out of that quote. |
| `native.workers` | 4 | Lots quoted at once (threads). The takes themselves are sent one by one. |
| `native.margin` | 0.005 | Take when the auction's quote for the chunk is at least this far under the Curve route quote (the keeper's profit). |
| `native.max_impact` | 0.01 | Chunk the take so the route's price impact stays under this fraction; the rest of the lot is quoted again next poll. |
| `native.requote` | 300 | Seconds between route quotes of a lot whose ask is still above the band (`ttl.near_above`) over its last route quote. Inside the band the lot is quoted every cycle. The ask itself needs no quote: it comes from the local model. |
| `native.receipt_timeout` | 60 | Seconds to wait for a take's receipt. A private relay never includes a take that would revert (the lot went to someone else), so the wait ends here with "not included" and the loop goes on. |
| `native.min_profit` | 0 | Want tokens a take must earn after its gas. The gas is the take's own estimate priced at the base fee plus the tip it would pay, converted at the native token's price (`fees.native_price`, else the price sources). A take that earns less waits for the curve; the same floor goes into the transaction, so a route that pays less on chain than it quoted reverts. See "Gas in the profit". |
| `native.packed_route`, `native.max_calls` | `true`, 256 | How the deployed `AuctionTaker` takes its route. As built from this repository: packed bytes of any length (`target ++ 2-byte length ++ calldata` per step), 256 steps at most, and a route costs only what its own bytes cost. The deployments made before 2026-11-25 take a bounded array of four calls: `packed_route: false` and `max_calls: 4` for those. The gas refill is left out when it does not fit. |
| `native.gas_refill` | `true` | Every take buys its own gas back: its gas cost in want is swapped to the native token and paid to a sending account in the same transaction, so the accounts' gas money stays level and the profit arrives in want, net of gas. The take never depends on it: where the refill has no route, no room in the route, would fail or would cost the take its profit, the take goes without. See "Gas in the profit". |
| `native.refill_below`, `native.refill_amount` | 0, 0 | A top-up on top of that: a sending account holding less than `refill_below` native tokens gets `refill_amount` native tokens' worth of a take's profit (half of the profit at most) the same way. Both or neither; `0`: off. |
| `native.router`, `native.chain`, `native.api_url` | by chain id | Curve router NG address, `api.curve.finance` chain slug and pool-list base URL (`curve_router.ROUTERS` / `API_CHAINS` cover Ethereum, Optimism, Gnosis, Polygon, Fantom, Fraxtal, Base, Arbitrum, Avalanche). |
| `native.min_pool_usd`, `native.max_hops` | 1000, 5 | Pools below this TVL are not routed through; hops per route (the router takes five at most). |
| `native.routes` | `{}` | Explicit routes by lot token, `{token: {path: [token, pool, token, ...], params: [[i, j, swap_type, pool_type, n_coins], ...], pools: [pool or zero per hop]}}`, used instead of the search (`pools` only for zap hops, swap type 3). |
| `native.profit_receiver` | the sender | Where the take's profit and any unspent lot go. |
| `native.electric.command` | `[erouter]` | The electric router's command line (a path, or the words of a command). |
| `native.electric.chain`, `native.electric.rpc_url` | by chain id, the tool's own | `erouter` chain name (`electric_router.CHAINS`) and the RPC endpoint it reads the chain through. |
| `native.electric.router` | unset | Refuse a call whose target is not this `ElectricRouter` address. Unset, the address in the tool's report is used. |
| `native.electric.slippage_bp` | unset | Whole-route tolerance in bp, divided between the legs by the tool; unset, each leg gets the tool's per-pool rule. |
| `native.electric.max_legs`, `native.electric.min_tvl` | 16, the tool's 10000 | Legs per route (the `AuctionTaker` takes 1888 bytes of calldata per step) and the pool TVL floor in USD. |
| `native.electric.timeout`, `native.electric.cache` | 180, 30 | Seconds one `erouter` run may take, and how long its answer for an amount is reused. |
| `max_open_orders` | 8 | Open orders per adapter at once; lots farthest from the market wait for a slot. The CoW orderbook rejects an owner's eleventh open outside-market order (`TooManyLimitOrders`). An order cannot be withdrawn (the owner is a contract), so the slots are kept free in other ways: see "Orders that do not get stuck". |
| `limits.http` | 60 | Requests per minute to one HTTP host (an orderbook, a price feed, an aggregator), a quarter of it at once; `0`: no limit. Global: the hosts limit and ban by IP. A request over the budget is not sent and not queued, see "Request budgets". |
| `limits.hosts` | `{api.cow.fi: 20}` | The budget of single hosts, on top of the built-in ones. |
| `limits.cooldown` | 600 | Seconds a host is left alone after it answered 403 or 429; twice as long each time in a row, at most eight times. |
| `limits.rpc`, `rpc_rate` | 0 | Requests per second to a network's node (global default and per network); they wait their turn, none is dropped. `0`: no limit. They are counted either way and reported in the cycle's summary line. |
| `log.file` | none | Log file (also `run --log-file`); without it the log goes to standard error. The file is rotated at `log.max_mb` (20) megabytes and `log.backups` (3) old files are kept, so it never takes more than 80 MB. |
| `log.repeat` | 300 | Seconds the same line (same logger, level and text) is held back after it was written; when it is written again it says how many were held back. `0`: every line. |
| `dust` | 0.01 | Lots the market values under this many want tokens are left alone: no orders, no takes, and no pricing again until the lot changes. A lot with no market estimate is never dust. `0` turns it off. |
| `tokens.whitelist` | `[]` | Non-empty: sell only these. |
| `tokens.blacklist` | `[]` | Never sell these (wins over the whitelist). |
| `start_block` | none | Where the first scan of the auction's logs starts (burner deployment block). Without it only lots staged after start-up and the whitelist are known. |
| `approvals` | `startup` | Executor allowances (the auction approving an adapter's executor, e.g. the CoW vault relayer, for a lot's token; the grant is permissionless and infinite, so a token needs it once). `startup`: with the first cycle the allowance of every live or upcoming lot is read once and the missing ones are synced with `sync_executor_approvals`, 64 tokens per transaction; a token is never looked at again by the process, a lot staged later joins the next batch, and a token whose `approve` reverts is singled out by halving the batch and gets no orders. Sends only in `mode: keeper`; offline, the missing tokens are listed once. `lazy`: read before each order and synced token by token. `off`: never read, never sent. |
| `mode` | `offline` | `offline`: reads the chain and posts orders, never sends a transaction (no key is loaded). `keeper`: also sends the on-chain housekeeping, today the executor allowance sync; needs `keeper_key_env`. |
| `keeper_key_env` | none | Env var with the private key used in `keeper` mode and by custom executions, or a list of them: several sending accounts, see "Several sending accounts". The first is the main account (the sender of simulations, the default profit receiver). |
| `cow.orderbook_url` | by chain id | mainnet, gnosis, arbitrum, base, polygon, avalanche, bnb, sepolia have defaults. |
| `cow.app_data_document` | none | Full appData JSON whose keccak is `CowAdapter.app_data()`; registered with the orderbook once. |
| `auction_taker` | none | `AuctionTaker` address for route executions. |
| `state_path` | `auction_keeper_state.json` | Published orders, the lot records with the log cursor, and the price estimates. Losing it costs at most one extra post per lot, one log scan from `start_block` and one round of price lookups. |

Adapters are never configured: each cycle the keeper reads the auction's
`AdapterRegistry`, takes the active adapters, and matches their
`ADAPTER_VERSION()` to a publisher (`CowAdapter` → `CowPublisher`). A rail
section with `enabled: false` skips its adapters. Adding a rail is one class
in `keepers/auction_keeper/rails/`.

## Run

```bash
python -m keepers.auction_keeper run --config config.yaml              # all networks, forever
python -m keepers.auction_keeper run --config config.yaml --network gnosis --once --dry-run
python -m keepers.auction_keeper status --config config.yaml           # lots and recorded orders
python -m keepers.auction_keeper run --config config.yaml --log-file keeper.log   # a log with a size limit
```

A cycle in which nothing happened (orders kept, quotes waiting) writes no
line; one summary line every ten minutes shows the loop is alive. A summary
line ends with the requests made since the previous one: `requests: rpc 14,
api.cow.fi 3 (+2 held back)`.

Each cycle, per auction:

1. read the latest block and the auction's logs since the last cycle (two requests) into the local book: `LotStaged` opens a lot record, `Taken` sets what is left of it, a settings change re-reads the curve;
2. rank every live lot with no request at all: its ask at the next block's timestamp, computed by the local model, against its highest remembered market estimate. Sold lots, lots outside their window and dust are not ranked, so they cost nothing for the rest of the window;
3. spend up to `pricing.budget` seconds refreshing the estimates that are due;
4. select the lots to work on (`focus`): everything within `focus.threshold` of its market, the `focus.nearest` nearest of the rest, `focus.blind` unpriced ones;
5. per selected lot: one `available` read (reused for `focus.confirm` seconds), then its order validity (`keepers/auction_keeper/core/schedule.py`):
   * inside the band of any source's price (from `ttl.near_above` over it to `ttl.near_below` under it): `ttl.min`, replaced the moment it expires (or at every curve step with `ttl.every_step`);
   * above a band not reached yet (over the highest price, or in a gap between two prices): one order valid until the curve enters the next band down, which the local model answers, capped at `ttl.max`;
   * under every band but unfilled, or no market known: `order_ttl`;
6. for every active adapter with a publisher (the registry is read every five minutes), (re)publish, lots nearest the market first, when there is no recorded order, it has expired, or it was built for a previous lot window, keeping the adapter's open orders within `max_open_orders`;
7. with `native.enabled`, quote the selected lots that are due for a route quote and take the ones whose next-block ask is under it (next section).

Between cycles the loop sleeps until the earliest recorded expiry or until
the latest block is the one whose successor is in the next price step of a
lot in focus, at most `poll_interval`. `status` prints every live lot in
rank order with its distance to the market, its plan and, for the lots in
focus with the native rail, the route quote; it uses the book and the
estimates the running keeper saved.

### The local model

The curve of the auction is public arithmetic over four numbers
(`start_total`, `floor_total`, `step_duration`, `auction_length`), and a
lot is its `LotStaged` log plus what the `Taken` logs left of it. So the
keeper computes `getAmountNeeded` itself (`core/model.py`): the same
integer operations as `dutch_auction_math.vy` and snekmate's `wad_ln` /
`wad_exp`, rounding included. On 2026-11-24 it matched the Ethereum burner
`0xc014…AbD4` to the wei on 26 lots x 60 quotes (whole lot, a partial
amount and `price`, at 20 timestamps across the window each).

What the model cannot know is what no log says: a kill switch or a target
migration, a lot staged before `start_block`, tokens moved out without a
take. Hence the `available` read of every lot in focus, the rotating
`focus.audit`, and the on-chain simulation of every take. A whitelisted
token no log was seen for is read from the chain once (three calls) and
audited like the rest.

### Pricing the next block

A take sent now executes in the next block at the earliest, and the curve
steps down on a fixed grid (36 s on Ethereum). A keeper that prices lots at
the latest block therefore sees a new step one block late, exactly when it
matters. With `lookahead` the ask is computed for `latest timestamp +
block time`: on the last block before a step boundary that is already the
new, lower price. The take is simulated, and its gas estimated, with that
block timestamp too: `eth_call` with a block override (`{"time": ...}`) on
the latest state; an endpoint that refuses the override is asked for its
`pending` block instead, and one that refuses that as well gets the plain
call (the take then waits for the next block, as before). Inside a step
the next block costs the same, so the lookahead changes nothing there.

A slot can be missed, so the next block may come later than assumed. That
only makes the real price lower or equal: the take pays the auction what
the contract asks in the block it lands in, never more than was simulated.

### Disagreeing price sources

Sources often disagree, and the usual reason is not a wrong price but a
missing route: an LP token or a wrapper that one source can unwrap and the
others cannot, so they answer far too low or not at all. Throwing the
differing source away, or letting the others outvote it, would make the
keeper sleep through the real market. The rule (`MarketPricer`):

* every source is asked; one with no price adds nothing;
* every answer is kept as a price level of its own. No source outvotes or
  replaces another;
* around each level, from `ttl.near_above` over it down to `ttl.near_below`
  under it, the lot is worked densely (`ttl.min` orders, or one per curve
  step with `ttl.every_step`);
* above the highest band and in the gaps between bands the orders are
  sparse: one order valid until the curve enters the next band down, at
  most `ttl.max`.

With `sources: [curve, cow, coingecko]` answering 1000, 400 and 380 the lot
gets dense orders from 1200 to 900, one long order across the gap, and
dense orders again from 480 to 342. Answers within the band width of each
other give one continuous dense zone.

The belief is not bounded, the cost is: a source that is absurdly wrong
costs dense orders for the time the curve needs to cross its band (it
falls by `near_above` + `near_below`), they count against `max_open_orders`
like any other, and nothing is ever sold on an estimate: an order's limit
is the auction's own quote and a take needs a route that pays. A route
quote of the native rail joins the answers as one more level (`route`).

### The tip of a take

Other takers compete for the same lot in the same block, and the builder
orders them by tip. The node's suggested tip is what an idle transaction
pays (0.00002 gwei on Ethereum at the time of writing) and wins nothing.
The tip of a take is therefore sized by the take (`core/fees.py`):

```
budget = fees.profit_share × (route quote − payment), converted to the native token
tip    = (budget − gas × base fee) / gas, not below zero
tip    = max(fees.priority_floor, min(fees.priority_cap, tip))
```

A wider margin buys a higher tip, and without a floor the keeper never
spends more on gas than `profit_share` of what the take earns. What this
cannot do is win small lots at thin margins: a $300 lot taken 0.1% under
its route earns $0.30, while the base fee alone on a 600k-gas take is
around $0.36 at 0.2 gwei and ETH at $3000, so the default tips zero, and
the 0.84 gwei that won such a lot on 2026-11-24 costs about $1.50 more.
Outbidding that is a loss per take; `fees.priority_floor` buys it if the
owner decides the lot is worth it. The allowance syncs pay the node's
suggested fee.

### Request budgets

What a cycle asks for is bounded by construction, and each bound has one
owner:

* the node: two requests per cycle (the latest block, the auction's logs)
  plus the work on the lots in focus, `focus.max_lots` at most; `rpc_rate`
  spaces them out for a node that answers 429;
* the price sources: `pricing.budget` seconds per cycle, and a source that
  answered about a lot less than `pricing.cache` seconds ago is not asked
  about it again in the same pass;
* every HTTP host: `limits.http` requests a minute (`limits.hosts` for
  single ones). A request over the budget raises `Throttled` instead of
  waiting, and the caller goes on: a price source that is out of budget
  keeps the level it gave before and is the only one asked on a later
  cycle; an order that could not be posted is posted a cycle later. The
  quotes of the `cow` price source leave half of the orderbook's budget to
  the orders;
* a host that answers 403 or 429 gets no requests for `limits.cooldown`
  seconds. This is what a burst of quotes and posts cost on 2026-11-24: the
  CoW orderbook's CDN refused the IP for more than half an hour.

### Orders that do not get stuck

An order of the auction cannot be cancelled off-chain (its owner is a
contract), so an order that is posted stands until its `validTo`. The
keeper keeps that from blocking anything:

* an order whose lot is sold, over or staged again cannot fill (the
  auction's router rejects it), so its record is dropped at once and does
  not count against `max_open_orders`;
* the long orders of lots above a band (`far`, up to `ttl.max`) take half of
  the slots at most; the other half is always there for a lot that reaches
  a price;
* a lot has one order per adapter; it is replaced when it expires (or at
  the next curve step with `ttl.every_step`, the two then overlapping for
  `ttl.min` at most);
* a post the backend refused is tried again after `ttl.min` seconds, then
  twice that, up to ten minutes, and written to the log once per try; the
  orderbook's `DuplicatedOrder` means the order is there and is recorded as
  posted.

### Several sending accounts

A take is one transaction that waits for its receipt, and an account can
have one such transaction in flight: a second one would need the next nonce
and hang behind the first if a relay never includes it. With one account
the takes of a cycle therefore go one after another, a block each on
Ethereum, while the curve of the other lots keeps falling.

```yaml
keeper_key_env: [KEEPER_KEY, KEEPER_KEY_2, KEEPER_KEY_3]
native:
  profit_receiver: "0x..."   # one address for the profit of every account
```

Each key is a lane with its own nonce count. `send` takes the account that
has waited longest and holds it until the receipt, so the accounts rotate
and as many takes as there are accounts are in flight at once; a
transaction that is never included holds up its own lane only. The
`AuctionTaker` is permissionless and pays the profit to the receiver named
in the call, so the accounts need nothing but gas money (their balances are
logged at start, an empty one with a warning). Without
`native.profit_receiver` the profit goes to the first account. Two or three
accounts cover a busy window on Ethereum; on a chain with sub-second blocks
one is enough. Run one keeper process per set of keys: two processes
sharing a key take each other's nonces.

### Gas in the profit

A take 0.5% under its route quote is not a profitable take when the chunk is
small: on 2026-11-24 takes that earned ten cents paid a dollar and a half of
gas on Ethereum. So before a take is sent:

1. its gas is estimated (the estimate is also the simulation) and priced at
   the latest base fee plus the tip the take would pay (`fees`);
2. that cost is converted to want at the native token's price and compared
   with what the take earns, `route_dy - payment`: under the cost plus
   `native.min_profit` the take waits, and since the curve keeps falling the
   same lot pays for its gas a few steps later, unless someone takes it
   first, who then pays that gas;
3. the cost plus `native.min_profit` is the transaction's `_min_profit`: a
   route that pays less on chain than it quoted reverts (a private relay
   then does not include it) instead of selling at a loss.

A take that earns less than half of what the previous take's gas cost is
turned down without a request for two minutes. With no price for the native
token the gas cannot be counted and the take goes as before, with a warning:
set `fees.native_price` where no source prices the wrapped native token. A
`fees.priority_floor` is part of the cost, so a high floor makes small takes
wait longer.

The gas is bought back in the same transaction (`native.gas_refill`, on by
default). After the lot's own swap the route gets two more steps: approve
the router for the take's gas cost in want and swap it to the native token,
which the router pays straight to a sending account. The take is estimated
with those steps in it, so the amount covers the refill's own gas as well.
What reaches `native.profit_receiver` is then the profit net of gas, in
want, and the accounts' native balances stay where they were, give or take
the swap's slippage (2% at most) and the difference between the base fee
the cost was counted with and the one of the block the take lands in. With
several accounts the gas goes to the poorest one, so the balances even out
whoever sends; `native.refill_below` adds a top-up for an account that has
run low.

Taking the lot comes first. The refill is left out, and the take goes as a
plain one with its gas as the on-chain floor, when the router has no route
from want to the native token, when the take has no room (the
`AuctionTaker` carries `native.max_calls` route steps and the lot's own swap
uses two),
when the take would revert with the refill and not without, or when the
refill's own gas would turn a take that pays into one that does not.

### Sending privately

With `send_rpc` the signed transaction goes only to the listed endpoints.
Two consequences are handled in `Web3Client`:

* A private relay does not include a transaction that would revert, and
  says nothing. The receipt is polled on `rpc` for `native.receipt_timeout`
  seconds (takes) or `receipt_timeout` (everything else), then the send is
  reported as not included and the loop goes on. The wait also ends as soon
  as the account's mined nonce has passed the transaction's.
* The public node never sees a private transaction in its pending pool, so
  the nonce is the node's pending count or one past the last nonce this
  process saw mined, whichever is higher. A transaction that was not
  included leaves its nonce to the next one, which then replaces it should
  the relay still hold it.

### The native rail (Curve)

Where no settlement adapter is active, or alongside them, the keeper fills
lots itself through the `AuctionTaker` periphery (`keepers/auction_keeper/rails/native.py`):
the taker receives the lot, swaps it to want on Curve through the router
NG, pays the auction's quote from the proceeds and sends the rest to the
keeper. It needs only gas and works on any chain with Curve pools.

Routes are found in `keepers/auction_keeper/routing/curve_router.py` from
Curve's own data: `api.curve.finance` pools and lending vaults,
`prices.curve.finance` crvUSD markets, and the chain for what no API says.
Every operation of the router is a hop kind: `exchange`,
`exchange_underlying` (metapools, lending pools), zaps, `add_liquidity` and
`remove_liquidity_one_coin` (so LP token lots unwrap and a path can cross a
base pool), LLAMMAs, the wrapped native token and the Ethereum wrappers,
ERC4626 vaults. Paths run up to `native.max_hops` through pools above
`native.min_pool_usd` and are ranked by the router's own `get_dy`, so the
quote the keeper acts on is what the swap pays; a hop the deployed router
does not take reverts there and its route is dropped. The best route of a
lot is kept for two minutes, so a retry costs one quote, not a search.

The take is chunked: the largest amount whose price impact on the route
stays under `native.max_impact` is taken, so a thin pool is hit in pieces.
A chunk is taken when the auction's quote for it is `native.margin` under
the route quote, after a simulation; a reverted simulation is reported and
retried. After a take the next chunk of the lot is tried at once (up to 16
in a row) rather than next cycle, since the curve keeps falling in between.
A lot within `ttl.near_above` of its route quote is quoted and tried every
cycle; one further away keeps its quote for `native.requote` seconds, while
its ask is followed by the local model for free.

The same router quote is available to the schedule as the `router` price
source (`pricing.sources`), which needs no feed and no key.

### The native rail (electric router)

With `electric` in `native.backends` the swap can go through
[electric-router](https://github.com/michwill/electric-router)
(`keepers/auction_keeper/routing/electric_router.py`). It solves the whole pool graph as a
resistor network, so one lot can be split across pools and pass through LP
tokens, vaults and wrappers, and it settles through its `ElectricRouter`
contract with a minimum rate on every leg. The rest of the rail is the same:
chunk by `native.max_impact`, take at `native.margin` under the route quote,
simulate first.

Route finding is off-chain, so the keeper runs the tool's command line once
per quote and executes the call from its JSON report as it is:

```
erouter route --chain fraxtal --from TOKEN --to WANT --amount-wei N \
    --calldata needed --receiver AUCTION_TAKER --max-legs 16 --json OUT.json
```

Install it in its own environment and point `native.electric.command` at it:

```
python -m venv ~/electric-router-venv
~/electric-router-venv/bin/pip install git+https://github.com/michwill/electric-router
```

It stays a separate process because it is AGPL-licensed, pins its own
dependencies and changes fast; nothing of it is imported or vendored here.
The call is opaque bytes to the keeper, so a leg kind added to the tool
needs no change on this side. What the keeper checks in a report: the
amount, pair and receiver are the ones asked for, the output was verified
on-chain, the calldata fits the `AuctionTaker`'s 1888-byte step, and, when
`native.electric.router` is set, the target address. Beyond that the take
protects itself: it reverts unless the swap leaves the auction's payment in
want on the taker.

How it differs in operation from the Curve backend:

- A quote is one `erouter` run: about 20 seconds cold on Fraxtal, against
  one `eth_call` for router NG. Answers are reused for `native.electric.cache`
  seconds, and the chunk search scales the amount by the price impact the
  tool reports (at most three more runs) where the Curve backend bisects.
- A route is priced for one amount at one block. Its per-leg minimum rates
  make a take that lands after the pools moved revert; the simulation
  catches that and the lot is quoted again next poll.
- The tool exits with code 2 when nothing routes (reported as `unroutable`);
  any other failure of the tool is reported as `failed` and retried.

### Several routers, many lots

`native.backends: [curve, electric]` asks both routers for every lot, at the
same time, and takes through the better quote. The routers are independent:
if one fails (the tool missing or timing out, a catalogue that does not
load) or cannot route the lot, the quote is the other's, and if the winner
then cannot chunk the lot the next best answer is used. Only a lot no
router can quote is `unroutable` or `failed`, and that never stops the
other lots. A router is one entry of `BACKENDS` in `rails/native.py` (a
module with `best_route`, `chunk` and `swap_calls`) plus its name in
`config/native.py`.

Quotes are the slow part (a handful of `eth_call`s for router NG, seconds
of solving for the electric router), so only the lots in focus that are due
for one are quoted, `native.workers` at a time in threads. The
takes are then simulated and sent one by one from the keeper's account. A
quote that aged while others were taken is harmless: the chunk is priced
again by the model for the block after the latest one at that moment, the
auction's price only falls, and the take is simulated before it is sent.

The `electric` price source (`pricing.sources`) is the same quote for the
whole lot; with the native rail on the same network it shares the cache.

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

`tests/keeper/pure/` needs neither Titanoboa nor a node: the local model
against quotes read from the live burner, the ranking and selection, the
pricing rule, the next-block pricing, the fee arithmetic, the client's
private sends, and whole cycles against an in-memory chain that counts the
requests. It runs in any environment with `web3` and `pytest`, also where
Titanoboa does not import:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/keeper/pure --confcutdir=tests/keeper/pure -q
```

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
