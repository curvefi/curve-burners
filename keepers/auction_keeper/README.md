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
| `pricing.cross_check` | 0.25 | The winning source must agree with the next source that has a price within this fraction; otherwise every source is asked and believed: the market is the span of their answers, so orders turn dense (`ttl.min`) from `ttl.near_above` over the highest down to `ttl.near_below` under the lowest, and the prices are logged. `0` disables. |
| `pricing.chains` | by chain id | Chain slug per feed (`{curve: polygon, llama: polygon, coingecko: polygon-pos, kyber: polygon}`); the defaults in `pricing.CHAINS` cover Ethereum, Polygon, Arbitrum, Base, Optimism, Gnosis, Avalanche, BSC, Fantom and Fraxtal where the feed does (Kyber and Velora do not serve Fraxtal or Gnosis). |
| `pricing.coingecko_api_key_env`, `pricing.coingecko_url` | `COINGECKO_API_KEY`, `https://api.coingecko.com/api/v3` | CoinGecko demo key and API base; a pro key goes with `https://pro-api.coingecko.com/api/v3`. |
| `pricing.oneinch_api_key_env` | `ONEINCH_API_KEY` | Developer Portal key for the `oneinch` quote source. |
| `ttl.near_above`, `ttl.near_below` | 0.2, 0.1 | The band around the market: a quote at most 20% above or 10% below it is "near". |
| `ttl.min` | 60 | Order validity inside the band (the CoW orderbook minimum; one or two curve steps). |
| `ttl.max` | 7200 | Longest validity far above the market, where one order waits for the curve to reach the band. |
| `native.enabled` | `false` | The native rail: take lots through the `AuctionTaker` with a router swap, alongside the adapters (see below). Needs `auction_taker`; sends in `keeper` mode, simulates and reports in `offline` mode. |
| `native.backends` | `[curve]` | The routers of the rail, a list out of `curve` (router NG over routes from the pool catalogue) and `electric` (the electric router, see below). With several, every lot is quoted on all of them at once and swapped through the one that pays more; a router that is down or cannot route the lot is left out of that quote. |
| `native.workers` | 4 | Lots quoted at once (threads). The takes themselves are sent one by one. |
| `native.margin` | 0.005 | Take when the auction's quote for the chunk is at least this far under the Curve route quote (the keeper's profit). |
| `native.max_impact` | 0.01 | Chunk the take so the route's price impact stays under this fraction; the rest of the lot is quoted again next poll. |
| `native.poll` | 30 | Seconds between tries of a lot whose quote is within `ttl.near_above` of its route quote. These lots are on a fast lane of their own, tried between the steps of the full cycle and while it waits, so the full cycle's length does not delay them. |
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
| `max_open_orders` | 8 | Open orders per adapter at once; lots farthest from the market wait for a slot. The CoW orderbook rejects an owner's eleventh open outside-market order (`TooManyLimitOrders`). |
| `tokens.whitelist` | `[]` | Non-empty: sell only these. |
| `tokens.blacklist` | `[]` | Never sell these (wins over the whitelist). |
| `start_block` | none | Where the `LotStaged` scan starts (burner deployment block). Without it only lots staged after start-up and the whitelist are known. |
| `mode` | `offline` | `offline`: reads the chain and posts orders, never sends a transaction (no key is loaded). `keeper`: also sends the on-chain housekeeping, today the executor allowance sync; needs `keeper_key_env`. |
| `keeper_key_env` | none | Env var with the private key used in `keeper` mode and by custom executions. |
| `cow.orderbook_url` | by chain id | mainnet, gnosis, arbitrum, base, polygon, avalanche, bnb, sepolia have defaults. |
| `cow.app_data_document` | none | Full appData JSON whose keccak is `CowAdapter.app_data()`; registered with the orderbook once. |
| `auction_taker` | none | `AuctionTaker` address for route executions. |
| `state_path` | `auction_keeper_state.json` | Published orders and scan cursor. Losing it costs at most one extra post per lot. |

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
A lot within `ttl.near_above` of its route quote is tried every
`native.poll` seconds whatever the full cycle is busy with.

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
of solving for the electric router), so the lots of a cycle, and the lots
on the fast lane, are quoted `native.workers` at a time in threads. The
takes are then simulated and sent one by one from the keeper's account. A
quote that aged while others were taken is harmless: the auction's price
only falls, and the take is simulated before it is sent.

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
