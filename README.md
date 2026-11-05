# Curve DAO fees burning process
DAO fees are accumulated as coins in pools, controllers and other entities.
Every week comes a process of burning these coins scattered around different networks and contracts into crvUSD,
that is distributed to veCRV holders.

## Architecture
Contracts are designed so earned fees can be moved to [`FeeCollector`](contracts/FeeCollector.vy)
via `withdraw_admin_fees()` or similar calls.
This is grouped into phase `Collect`.
Keepers get % from each earned coin according to Dutch auction.

Next comes `Exchange` phase when all collected coins are converted into crvUSD.
This may be done using [`DutchAuctionBurner`](contracts/burners/DutchAuctionBurner.vy),
which supports native Dutch-auction settlement and external settlement adapters.

Final phase is `Forward` which is applied to resulting crvUSD.
Mainly it bridges to Ethereum or FeeDistributor,
but also handles hooks for xDAO using [`Hooker`](contracts/hooks/Hooker.vy) paying some fee to keeper.

## Tests
Install:
```bash
python3 -m virtualenv venv/ && source venv/bin/activate
pip install -r requirements.in
```

Run:
```bash
pytest tests
```

Code style (the `lint` workflow runs the same checks; `pre-commit install` runs them on commit):
```bash
pip install ruff==0.16.9 mamushi==0.1.2
ruff check tests scripts && ruff format --check tests scripts
# every Vyper source except the contracts pinned to 0.3.x/0.4.x (deployed as-is)
mamushi --check --line-length 100 $(grep -rL -E '^#\s*(pragma version|@version)\s*[=^]*0\.[34]\.' contracts --include='*.vy' --include='*.vyi')
```

Gas report (median execution gas per external call of the Dutch auction contracts; the
`gas` workflow posts the head-vs-base comparison on every pull request):
```bash
python scripts/gas_report.py --output gas-report.json   # measure (defaults to the Dutch auction tests)
python scripts/compare_gas_report.py --head gas-report.json --base other.json --output gas-report.md
```

## Keeper backend
[`auction_keeper`](keepers/auction_keeper/README.md) publishes the Dutch auction lots to the
settlement adapters (CoW orderbook today) and runs custom takes:
```bash
python -m keepers.auction_keeper run --config config.yaml
```

## Monitoring
[`index.html`](index.html) is a single static page (no build step) that shows the
Dutch auction per network and week: the week as one cell per UTC day labelled
by the FeeCollector epoch, the lots and fills read from the burner's
`LotStaged` / `Taken` logs through `eth_getLogs` (CoW fills merged in, one
column per adapter) with the auction's CoW orders in the same block (explorer
link, collapsible filled and open orders, a sync-approvals button), the
auction economics (max fee per epoch) and the contract addresses split into
settings and collection, each with an explorer link and a copy widget. Serve
the repo root and open it, or open the file directly in a browser:
```bash
python3 -m http.server 8000   # then http://localhost:8000/index.html
```
The week is shown Mon → Sun by default or Thu → Wed (the FeeCollector epoch)
with a local/UTC time toggle; the RPC provider in the header (public, dRPC,
Ankr, Alchemy with their keys, or a custom URL and logs span) is kept in the
browser's localStorage, the collapsed panels (contracts and configuration,
tokens on the FeeCollector not staged this week) load when opened, and
`#network/YYYY-MM-DD` in the URL deep-links to a network and week.

## CowSwap (legacy CowSwapBurner)
Orders for the deprecated `CowSwapBurner` were posted by running
[WatchTower](https://github.com/cowprotocol/watch-tower); the setup is kept in
[keepers/fee_keeper/WatchTower.md](keepers/fee_keeper/WatchTower.md).
