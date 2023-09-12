## About

- SwapBurner for all `exchange`s. (1)
- DepositBurner for `add_liquidity`.
Does not work for first versions of crypto pools since there is no `.lp_price()` and StableSwap metapools without price oracle.
Omitted as most probably won't be needed. (1)
- LPBurner for `remove_liquidity_one_coin`.
- Wrapped Burners (wrapped ETH, aave tokens, etc.).

(1) Raw ETH(and other native coins) can not be taken from `burn_amount`, so in case of slippage it would be stuck in proxy.
Looks fine since this implementation is deprecated and will use only wrapped in the future.
Raw to wrapped can be converted via WrappedBurner.

## Thoughts about future
- Move priorities into separate contract with update from swap, etc.
- Move metadata about implementations into separate contract. Set up for old pools and automatically fetch for new without need of handling.
- Reset all burners and go through old in case of couches.
