## About

- SwapBurner for all `exchange`s.
- DepositBurner for `add_liquidity`, does not work for first versions of crypto pools since there is no `.lp_price()`. Omitted as most probably won't be needed.
- LPBurner for `remove_liquidity_one_coin`.
- Wrapped Burners (wrapped ETH, aave tokens, etc.).

## Thoughts about future
- Move priorities into separate contract with update from swap, etc.
- Move metadata about implementations into separate contract. Set up for old pools and automatically fetch for new without need of handling.
- Reset all burners and go through old in case of couches.
