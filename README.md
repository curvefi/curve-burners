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
This may be done using different burners like [`CowSwapBurner`](contracts/burners/CowSwapBurner.vy)
which delegates price discovery and settlement to CowSwap auction.

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
pytest test
```

