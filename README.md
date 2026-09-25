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

## CowSwap
In order to swap accumulated coins into crvUSD, one should post orders to CowSwap backend.
This can be done by running [WatchTower](https://github.com/cowprotocol/watch-tower).
Instruction for setup can be found [here](fee_keeper/WatchTower.md).
