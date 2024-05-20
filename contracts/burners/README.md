Burners maintain exchanging coins into _target_ and needed actions at the time of collect (like registering a new coin).  
Note: latest StableSwap and CryptoSwap implementations send fees automatically


## XYZBurner
Basically a template Burner, that allows to collect coins with associated payout.


## CowSwapBurner
Using `ComposableCow` to post orders into CowSwap.
Coins are priced via CowSwap solvers' internal auction.


## DutchAuctionBurner
Sell coins using custom DutchAuction.
Using these formulas to price coins:

$$ price = low + max\\_price\\_amplifier \cdot low \cdot \frac{base ^ {time} - 1}{base - 1} $$  

$$ low = \frac{record_c(t) \cdot smoothing + target\\_threshold}{record_c(c) \cdot smoothing + balance_c} $$

### target_threshold
In limit, auction will be triggered when the price crosses $$ amount \cdot coin\\_price - tx\\_cost $$.
In times of $$ amount \approx \frac{tx\\_cost}{coin\\_price} $$ the whole profit will be reduced.
To prevent this, `target_threshold` is introduced describing minimum exchange amount available.
We have no information about arbitrary coin price, though we can estimate `tx_cost` for different chains.
This parameter also makes possible to anchor to some minimum price and support any prices of coins.

### low
`low` price is chosen, so $$ balance_c \cdot low \ge target\\_threshold $$.
It accounts previous exchange values, so $$ weighted \: price \ge low \ge \frac{target\\_threshold}{balance_c} $$,
where weight is calculated from previous exchanges and time applying `records_smoothing`.  
Note: exchange leads to ascent of `low` hence ascent of the current price, so it kinda fluctuates around the actual price for some time.  

### Parameters
| parameter           | description                                        | reference formula                                          | reference value                                            |
|---------------------|----------------------------------------------------|------------------------------------------------------------|------------------------------------------------------------|
| target_threshold    | Minimum amount to exchange                         | $$ \frac{tx\\_cost}{acceptable \: keeper \: fee} $$        | $$ \frac{0.1 \: USD}{1\%} = 0.1 \cdot 100 = 10 \: crvUSD$$ |
| max_price_amplifier | Prices range                                       | $$ \frac{max \: possible \: amount}{target\\_threshold} $$ | $$ \frac{100\,000}{10} = 10\,000 $$                        |
| base                | Constant for exponential price movement            | TBD depending on block time                                | $$ 100 $$                                                  |
| records_smoothing   | Previous exchanges contribution into current price | TBD basically not dependent on anything                    | $$ \frac{1}{2} $$                                          |

