I started implementing this architecture along with fee collecting contracts to support the system.
I found no good Python templates for keepers and accepted it as a challenge.
Main reasoning was `py-curve` had problems with execution when brownie became unmaintained and as other also unstable,
the system should be modularized to easily switch between tools.
There are adapters for tools and other modules(FeeSources, Calculators, etc.).
Everything is cached as possible for fast fixes.

I'll pivot to one-filers with Web3Py :( / :)

# About
Keeper bot to support FeeCollector architecture.
Main purpose is to show example of all features along with having open-source working version for easy start on other L2s.

# Run
Fill [config](fee_keeper.yaml) and run
```bash
python3 fee_keeper/main.py
```




# Draft notes
source -> {coin: amount}

## Modules
Tx constructor (through proxy or direct)  
Calculator: simple, knapsack, dynamic programming  
Where to get price from? Raw, Curve API, coingecko  
How to send txs? raw, boa, ape, brownie  
Source of new pools: on-chain, curve API  

## Pipeline
Fetch amount(chain, pools, coins) ->  
Calculate(gas from tx constructor?, rates) ->  

SourceFetcher@pipeline  
    FeeSource@data  
Calculator@pipeline  
    PriceSource@pipeline  
    FeeApplier@pipeline  
TxSender@pipeline  

## Cache
```yaml
Chain:
    FeeSource:
        type: ""
        coins: [addresses]

Chain:
    FeeSourceType:
        FetchFrom: index
```

## Parameters
Whitelist and blacklist of coins
Whitelist and blacklist of pools
