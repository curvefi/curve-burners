# @version 0.3.10
"""
@title DutchAuctionBurner
@license MIT
@author Curve Finance
@notice Exchange tokens using Dutch auction
"""


interface ERC20:
    def approve(_to: address, _value: uint256) -> bool: nonpayable
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable
    def balanceOf(_owner: address) -> uint256: view


interface FeeCollector:
    def target() -> ERC20: view
    def owner() -> address: view
    def emergency_owner() -> address: view
    def epoch(ts: uint256=block.timestamp) -> Epoch: view
    def epoch_time_frame(_epoch: Epoch, _ts: uint256=block.timestamp) -> (uint256, uint256): view
    def fee(_epoch: Epoch=empty(Epoch), _ts: uint256=block.timestamp) -> uint256: view
    def can_exchange(_coins: DynArray[ERC20, MAX_LEN]) -> bool: view
    def transfer(_transfers: DynArray[Transfer, MAX_LEN]): nonpayable


interface Multicall:
    def aggregate3Value(calls: DynArray[Call3Value, MAX_CALL_LEN]) -> DynArray[MulticallResult, MAX_CALL_LEN]: payable


event Exchanged:
    coin: indexed(ERC20)
    keeper: indexed(address)
    exchange_amount: uint256
    target_amount: uint256


enum Epoch:
    SLEEP  # 1
    COLLECT  # 2
    EXCHANGE  # 4
    FORWARD  # 8


struct Transfer:
    coin: ERC20
    to: address
    amount: uint256  # 2^256-1 for the whole balance


struct Call3Value:
    target: address
    allow_failure: bool
    value: uint256
    call_data: Bytes[8192]

struct MulticallResult:
    success: bool
    return_data: Bytes[1024]


struct WeightedPrice:
    exchange_amount: uint256
    target_amount: uint256

struct PriceRecord:
    prev: WeightedPrice
    cur: WeightedPrice
    cur_week: uint256

struct PriceRecordInput:
    coin: ERC20
    record: PriceRecord


ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
ONE: constant(uint256) = 10 ** 18  # Precision
MAX_LEN: constant(uint256) = 64
MAX_CALL_LEN: constant(uint256) = 64
SUPPORTED_INTERFACES: constant(bytes4[2]) = [
    # ERC165: method_id("supportsInterface(bytes4)") == 0x01ffc9a7
    0x01ffc9a7,
    # Burner:
    #   method_id("burn(address[],address)") == 0x72a436a8
    #   method_id("push_target()") == 0x2eb078cd
    #   method_id("VERSION()") == 0xffa1ad74
    0xa3b5e311,
]
VERSION: public(constant(String[20])) = "DutchAuction"
balances: HashMap[ERC20, uint256]

WEEK: constant(uint256) = 7 * 24 * 3600

fee_collector: public(immutable(FeeCollector))
multicall: public(immutable(Multicall))

target_threshold: public(uint256)  # min amount to exchange
max_price_amplifier: public(uint256)

records: public(HashMap[ERC20, PriceRecord])
records_smoothing: public(uint256)

base: public(uint256)
ln_base: public(uint256)


@external
def __init__(
    _fee_collector: FeeCollector,
    _target_threshold: uint256, _max_price_amplifier: uint256,
    _initial_records: DynArray[PriceRecordInput, MAX_LEN], _records_smoothing: uint256):
    """
    @notice Contract constructor
    @param _fee_collector FeeCollector contract it is used with
    @param _target_threshold Minimum amount of target to receive, with base=10**18
    @param _max_price_amplifier Spread of maximum and minimum prices, without base=10**18
    @param _initial_records Set price records for coin cushions waiting
    @param _records_smoothing Coefficient to reduce previous price impact, with base=10**18
    """
    fee_collector = _fee_collector
    multicall = Multicall(0xcA11bde05977b3631167028862bE2a173976CA11)  # https://github.com/mds1/multicall/ v3

    self.target_threshold = _target_threshold
    assert _max_price_amplifier <= ONE, "max_price_amplifier has no 10^18 base"
    self.max_price_amplifier = _max_price_amplifier

    self._set_records(_initial_records)
    assert _records_smoothing <= 10 ** 18, "Bad smoothing value"
    self.records_smoothing = _records_smoothing

    self.base = 2718281828459045235  # Euler's number
    self.ln_base = ONE


@external
def burn(_coins: DynArray[ERC20, MAX_LEN], _receiver: address, _just_revise: bool=False):
    """
    @notice Post hook after collect to register coins for burn
    @dev Pays out fee and saves coins on fee_collector.
    @param _coins Which coins to burn
    @param _receiver Receiver of profit
    @param _just_revise Revise balances of coins without paying out
    """
    if not _just_revise:
        assert msg.sender == fee_collector.address, "Only FeeCollector"

        fee: uint256 = fee_collector.fee(Epoch.COLLECT)
        fee_payouts: DynArray[Transfer, MAX_LEN] = []
        for coin in _coins:
            amount: uint256 = (coin.balanceOf(fee_collector.address) - self.balances[coin]) * fee / ONE
            fee_payouts.append(Transfer({coin: coin, to: _receiver, amount: amount}))
        fee_collector.transfer(fee_payouts)
    else:
        assert fee_collector.epoch() != Epoch.EXCHANGE,  "Can't update at Exchange"

    for coin in _coins:
        self.balances[coin] = coin.balanceOf(fee_collector.address)


# https://github.com/pcaversaccio/snekmate/blob/3dff18ae4bbc4b0a98a57cfbce4994c7739a991f/src/snekmate/utils/Math.vy#L420C1-L485C57
@internal
@pure
def _wad_exp(x: int256) -> int256:
    """
    @dev Calculates the natural exponential function of a signed integer with
         a precision of 1e18.
    @notice Note that this function consumes about 810 gas units. The implementation
            is inspired by Remco Bloemen's implementation under the MIT license here:
            https://xn--2-umb.com/22/exp-ln.
    @param x The 32-byte variable.
    @return int256 The 32-byte calculation result.
    """
    value: int256 = x

    # If the result is `< 1`, we return zero. This happens when we have the following:
    # "x <= (log(1e-18) * 1e18) ~ -4.15e19".
    if (x <= -41_446_531_673_892_822_313):
        return empty(int256)

    # When the result is "> (2 ** 255 - 1) / 1e18" we cannot represent it as a signed integer.
    # This happens when "x >= floor(log((2 ** 255 - 1) / 1e18) * 1e18) ~ 135".
    assert x < 135_305_999_368_893_231_589, "Math: wad_exp overflow"

    # `x` is now in the range "(-42, 136) * 1e18". Convert to "(-42, 136) * 2 ** 96" for higher
    # intermediate precision and a binary base. This base conversion is a multiplication with
    # "1e18 / 2 ** 96 = 5 ** 18 / 2 ** 78".
    value = unsafe_div(x << 78, 5 ** 18)

    # Reduce the range of `x` to "(-½ ln 2, ½ ln 2) * 2 ** 96" by factoring out powers of two
    # so that "exp(x) = exp(x') * 2 ** k", where `k` is a signer integer. Solving this gives
    # "k = round(x / log(2))" and "x' = x - k * log(2)". Thus, `k` is in the range "[-61, 195]".
    k: int256 = unsafe_add(unsafe_div(value << 96, 54_916_777_467_707_473_351_141_471_128), 2 ** 95) >> 96
    value = unsafe_sub(value, unsafe_mul(k, 54_916_777_467_707_473_351_141_471_128))

    # Evaluate using a "(6, 7)"-term rational approximation. Since `p` is monic,
    # we will multiply by a scaling factor later.
    y: int256 = unsafe_add(unsafe_mul(unsafe_add(value, 1_346_386_616_545_796_478_920_950_773_328), value) >> 96, 57_155_421_227_552_351_082_224_309_758_442)
    p: int256 = unsafe_add(unsafe_mul(unsafe_add(unsafe_mul(unsafe_sub(unsafe_add(y, value), 94_201_549_194_550_492_254_356_042_504_812), y) >> 96,\
                           28_719_021_644_029_726_153_956_944_680_412_240), value), 4_385_272_521_454_847_904_659_076_985_693_276 << 96)

    # We leave `p` in the "2 ** 192" base so that we do not have to scale it up
    # again for the division.
    q: int256 = unsafe_add(unsafe_mul(unsafe_sub(value, 2_855_989_394_907_223_263_936_484_059_900), value) >> 96, 50_020_603_652_535_783_019_961_831_881_945)
    q = unsafe_sub(unsafe_mul(q, value) >> 96, 533_845_033_583_426_703_283_633_433_725_380)
    q = unsafe_add(unsafe_mul(q, value) >> 96, 3_604_857_256_930_695_427_073_651_918_091_429)
    q = unsafe_sub(unsafe_mul(q, value) >> 96, 14_423_608_567_350_463_180_887_372_962_807_573)
    q = unsafe_add(unsafe_mul(q, value) >> 96, 26_449_188_498_355_588_339_934_803_723_976_023)

    # The polynomial `q` has no zeros in the range because all its roots are complex.
    # No scaling is required, as `p` is already "2 ** 96" too large. Also,
    # `r` is in the range "(0.09, 0.25) * 2**96" after the division.
    r: int256 = unsafe_div(p, q)

    # To finalise the calculation, we have to multiply `r` by:
    #   - the scale factor "s = ~6.031367120",
    #   - the factor "2 ** k" from the range reduction, and
    #   - the factor "1e18 / 2 ** 96" for the base conversion.
    # We do this all at once, with an intermediate result in "2**213" base,
    # so that the final right shift always gives a positive value.

    # Note that to circumvent Vyper's safecast feature for the potentially
    # negative parameter value `r`, we first convert `r` to `bytes32` and
    # subsequently to `uint256`. Remember that the EVM default behaviour is
    # to use two's complement representation to handle signed integers.
    return convert(unsafe_mul(convert(convert(r, bytes32), uint256), 3_822_833_074_963_236_453_042_738_258_902_158_003_155_416_615_667) >>\
           convert(unsafe_sub(195, k), uint256), int256)


@internal
@view
def _low(current_amount: uint256, target_amount: uint256, price_record: PriceRecord) -> uint256:
    """
    @notice Get lowest price in auction
    """
    t: uint256 = target_amount + price_record.prev.target_amount + price_record.cur.target_amount
    a: uint256 = current_amount + price_record.prev.exchange_amount + price_record.cur.exchange_amount
    return t * ONE / a


@internal
@view
def _get_price_record(coin: ERC20, week: uint256, smoothing: uint256) -> PriceRecord:
    """
    @notice Get price record applying new week
    """
    price_record: PriceRecord = self.records[coin]
    if price_record.cur_week < week:
        if week - price_record.cur_week > 4:
            price_record.prev = price_record.cur
        else:
            price_record.prev.exchange_amount += price_record.cur.exchange_amount
            price_record.prev.target_amount += price_record.cur.target_amount
            for i in range(week - price_record.cur_week, bound=4):
                price_record.prev.exchange_amount = price_record.prev.exchange_amount * smoothing / ONE
                price_record.prev.target_amount = price_record.prev.target_amount * smoothing / ONE

        price_record.cur = empty(WeightedPrice)
        price_record.cur_week = week

    return price_record


@internal
@view
def _price(low_price: uint256, time_amplifier: uint256) -> uint256:
    # low + high * log_scale(time)
    # high = max_price_amplifier * low
    return low_price + self.max_price_amplifier * low_price * time_amplifier / ONE


@internal
@view
def _get_week_from_ts(ts: uint256) -> uint256:
    """
    @notice Week number needed for records
    """
    start: uint256 = 0
    end: uint256 = 0
    start, end = fee_collector.epoch_time_frame(Epoch.EXCHANGE, ts)
    return ts / WEEK


@internal
@view
def _time_amplifier(ts: uint256) -> uint256:
    start: uint256 = 0
    end: uint256 = 0
    start, end = fee_collector.epoch_time_frame(Epoch.EXCHANGE, ts)
    assert start <= ts and ts < end, "Bad time"

    # log_scale(time) = (base ** time - 1) / (base - 1)
    # base ** time = e ** (time * ln(base))
    # time = remaining / whole period
    return (convert(self._wad_exp(convert((end - ts) * self.ln_base / (end - start), int256)), uint256) - ONE) * ONE / (self.base - ONE)


@external
@view
def price(_coin: ERC20, _ts: uint256=block.timestamp) -> uint256:
    """
    @notice Get price of `_coin` at `_ts`
    @param _coin Coin to get price of
    @param _ts Timestamp at which to count price
    @return Price of coin, with base=10**18
    """
    return self._price(
            self._low(
                _coin.balanceOf(fee_collector.address),
                self.target_threshold,
                self._get_price_record(_coin, self._get_week_from_ts(_ts), self.records_smoothing),
            ),
            self._time_amplifier(_ts),
    )


@external
@payable
def exchange(_transfers: DynArray[Transfer, MAX_LEN], _calls: DynArray[Call3Value, MAX_CALL_LEN]) ->\
    (uint256, DynArray[MulticallResult, MAX_CALL_LEN]):
    """
    @notice Exchange coins according to internal Dutch Auction
    @dev Coins are transferred first so they can be used for flashswap
    @param _transfers Transfers to make from FeeCollector for buying out from auction
    @param _calls Multicall data to initiate any callbacks
    @return (total amount of sold target, results of _calls)
    """
    coins: DynArray[ERC20, MAX_LEN] = []
    for transfer in _transfers:
        coins.append(transfer.coin)
    assert fee_collector.can_exchange(coins)

    fee_collector.transfer(_transfers)

    target_threshold: uint256 = self.target_threshold
    week: uint256 = self._get_week_from_ts(block.timestamp)
    time_amplifier: uint256 = self._time_amplifier(block.timestamp)
    records_smoothing: uint256 = self.records_smoothing

    target_total: uint256 = 0
    for transfer in _transfers:
        new_balance: uint256 = self.balances[transfer.coin]
        price_record: PriceRecord = self._get_price_record(transfer.coin, week, records_smoothing)
        # fee-on-transfer coins will have a small impact
        target_amount: uint256 = self._price(
            self._low(new_balance + transfer.amount, target_threshold, price_record),
            time_amplifier,
        ) * transfer.amount / ONE

        assert target_amount >= target_threshold,  "Target threshold"
        target_total += target_amount
        price_record.cur.exchange_amount += transfer.amount
        price_record.cur.target_amount += target_amount
        self.records[transfer.coin] = price_record

        self.balances[transfer.coin] = new_balance - transfer.amount
        log Exchanged(transfer.coin, msg.sender, transfer.amount, target_amount)

    results: DynArray[MulticallResult, MAX_CALL_LEN] = multicall.aggregate3Value(_calls, value=msg.value)

    target: ERC20 = fee_collector.target()
    target_balance: uint256 = target.balanceOf(self)
    if target_balance >= target_total:  # without approvals
        target.transfer(fee_collector.address, target_balance)
    else:
        target.transferFrom(msg.sender, fee_collector.address, target_total)

    return target_total, results


@external
def push_target() -> uint256:
    """
    @notice In case target coin is left in contract can be pushed to forward
    @return Amount of coin pushed further
    """
    target: ERC20 = fee_collector.target()
    amount: uint256 = target.balanceOf(self)
    if amount > 0:
        target.transfer(fee_collector.address, amount)
    return amount


@pure
@external
def supportsInterface(_interface_id: bytes4) -> bool:
    """
    @dev Interface identification is specified in ERC-165.
    @param _interface_id Id of the interface
    @return True if contract supports given interface
    """
    return _interface_id in SUPPORTED_INTERFACES


@internal
def _set_records(_records: DynArray[PriceRecordInput, MAX_LEN]):
    for input in _records:
        self.records[input.coin] = input.record


@external
def set_records(_records: DynArray[PriceRecordInput, MAX_LEN]):
    """
    @notice Set price records. Might be needed in anomaly coins feed.
    @dev Callable only by owner and emergency owner
    @param _records Records to set prices for
    """
    assert msg.sender in [fee_collector.owner(), fee_collector.emergency_owner()], "Only owner"

    self._set_records(_records)


@external
def set_records_smoothing(_records_smoothing: uint256):
    """
    @dev Callable only by owner
    @param _records_smoothing Coefficient to reduce previous price impact, with base=10**18
    """
    assert msg.sender == fee_collector.owner(), "Only owner"
    assert _records_smoothing <= 10 ** 18, "Bad smoothing value"

    self.records_smoothing = _records_smoothing


@external
def set_price_parameters(_target_threshold: uint256, _max_price_amplifier: uint256):
    """
    @dev Callable only by owner
    @param _target_threshold Minimum amount of target to receive, with base=10**18
    @param _max_price_amplifier Spread of maximum and minimum prices, without base=10**18
    """
    assert msg.sender == fee_collector.owner(), "Only owner"
    assert _max_price_amplifier <= ONE, "max_price_amplifier has no 10^18 base"

    self.target_threshold = _target_threshold
    self.max_price_amplifier = _max_price_amplifier


@external
def set_time_amplifier_base(_base: uint256, _ln_base: uint256):
    """
    @dev Callable only by owner
    @param _base Base to count time amplifier for, >1, with base=10 ** 18
    @param _ln_base Approximate value of ln(base), with base=10 ** 18
    """
    assert msg.sender == fee_collector.owner(), "Only owner"
    assert _base > ONE, "Bad base value"

    exp_ln_base: uint256 = convert(self._wad_exp(convert(_ln_base, int256)), uint256)
    assert exp_ln_base >= _base * 99 / 100 and exp_ln_base <= _base * 101 / 100, "Bad base value"

    self.base = _base
    self.ln_base = _ln_base


@external
def recover(_coins: DynArray[ERC20, MAX_LEN]):
    """
    @notice Recover ERC20 tokens or Ether from this contract
    @dev Callable only by owner and emergency owner
    @param _coins Token addresses
    """
    assert msg.sender in [fee_collector.owner(), fee_collector.emergency_owner()], "Only owner"

    for coin in _coins:
        if coin.address == ETH_ADDRESS:
            raw_call(fee_collector.address, b"", value=self.balance)
        else:
            coin.transfer(fee_collector.address, coin.balanceOf(self), default_return_value=True)  # do not need safe transfer
