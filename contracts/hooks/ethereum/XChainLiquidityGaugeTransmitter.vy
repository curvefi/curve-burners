# @version 0.4.0
"""
@title XChainLiquidityGaugeTransmitter
@license MIT
@author Curve Finance
@notice Helper contract for transmitting new emissions to L2s. Non-ETH gas tokens are not supported yet.
@custom:version 0.0.1
"""



from ethereum.ercs import IERC20

interface Bridger:
    def cost() -> uint256: view

interface RootGauge:
    def transmit_emissions(): nonpayable
    def total_emissions() -> uint256: view
    def last_period() -> uint256: view
    def bridger() -> Bridger: view
    def inflation_params() -> InflationParams: view

interface GaugeController:
    def checkpoint_gauge(addr: address): nonpayable
    def n_gauge_types() -> int128: view
    def gauge_types(_addr: address) -> int128: view
    def points_weight(gauge_addr: address, time: uint256) -> Point: view  # gauge_addr -> time -> Point
#    def changes_weight: HashMap[address, HashMap[uint256, uint256]]  # gauge_addr -> time -> slope
    def time_weight(gauge_addr: address) -> uint256: view  # gauge_addr -> last scheduled time (next week)
    def points_sum(type_id: int128, time: uint256) -> Point: view  # type_id -> time -> Point
#    def changes_sum: HashMap[int128, HashMap[uint256, uint256]]  # type_id -> time -> slope
    def time_sum(type_id: uint256) -> uint256: view  # type_id -> last scheduled time (next week)
    def points_total(time: uint256) -> uint256: view  # time -> total weight
    def time_total() -> uint256: view  # last scheduled time
    def points_type_weight(type_id: int128, time: uint256) -> uint256: view  # type_id -> time -> type weight
    def time_type_weight(type_id: uint256) -> uint256: view  # type_id -> last scheduled time (next week)

interface Minter:
    def minted(_user: address, _gauge: RootGauge) -> uint256: view


# Gauge controller replication
struct Point:
    bias: uint256
    slope: uint256

# RootGauge replication
struct InflationParams:
    rate: uint256
    finish_time: uint256

# Gas for bridgers
struct GasTopUp:
    amount: uint256
    token: IERC20  # ETH_ADDRESS for raw ETH
    receiver: address


CRV: public(constant(IERC20)) = IERC20(0xD533a949740bb3306d119CC777fa900bA034cd52)
ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
MAX_LEN: constant(uint256) = 64

MINTER: public(constant(Minter)) = Minter(0xd061D61a4d941c39E5453435B6345Dc261C2fcE0)

# Gauge related
GAUGE_CONTROLLER: public(constant(GaugeController)) = GaugeController(0x2F50D538606Fa9EDD2B11E2446BEb18C9D5846bB)
WEEK: constant(uint256) = 604800
YEAR: constant(uint256) = 86400 * 365
RATE_DENOMINATOR: constant(uint256) = 10 ** 18
RATE_REDUCTION_COEFFICIENT: constant(uint256) = 1189207115002721024  # 2 ** (1/4) * 1e18
RATE_REDUCTION_TIME: constant(uint256) = YEAR


@internal
def _transmit(_gauge: RootGauge) -> uint256:
    transmitted: uint256 = staticcall _gauge.total_emissions() - staticcall CRV.balanceOf(_gauge.address)

    extcall _gauge.transmit_emissions()

    return staticcall _gauge.total_emissions() - transmitted - staticcall CRV.balanceOf(_gauge.address)


@external
@payable
def transmit(
    _min_amount: uint256,
    _gauges: DynArray[RootGauge, MAX_LEN],
    _gas_top_ups: DynArray[GasTopUp, MAX_LEN]=[],
    _eth_refund: address=msg.sender,
) -> uint256:
    """
    @notice Transmit emissions for xchain gauges
    @param _min_amount Minimum amount
    @param _gauges Gauges to transmit emissions for
    @param _gas_top_ups Gas amount to send
    @param _eth_refund Receiver of excess ETH (msg.sender by default)
    @return Number of gauges surpassed `_min_amount` emissions
    """
    self._top_up(_gas_top_ups if len(_gas_top_ups) > 0 else self._get_gas_top_ups(_gauges), _eth_refund)
    surpassed: uint256 = 0
    for gauge: RootGauge in _gauges:
        if self._transmit(gauge) >= _min_amount:
            surpassed += 1
    return surpassed


@payable
@internal
def _top_up(top_ups: DynArray[GasTopUp, MAX_LEN], eth_refund: address):
    eth_sent: uint256 = 0
    for top_up: GasTopUp in top_ups:
        if top_up.amount != 0:
            if top_up.token.address == ETH_ADDRESS:
                send(top_up.receiver, top_up.amount)
                eth_sent += top_up.amount
            else:
                # transfer coins beforehand
                extcall top_up.token.transfer(top_up.receiver, top_up.amount)
    if eth_refund != empty(address):
        send(eth_refund, msg.value - eth_sent)


@external
@payable
def top_up_gas(_top_ups: DynArray[GasTopUp, MAX_LEN], _eth_refund: address=msg.sender):
    """
    @notice Top up contracts for transmitting emissions
    @param _top_ups Gas amount to send
    @param _eth_refund Receiver of excess ETH (msg.sender by default)
    """
    self._top_up(_top_ups, _eth_refund)


@view
@internal
def _get_gas_top_ups(gauges: DynArray[RootGauge, MAX_LEN]) -> DynArray[GasTopUp, MAX_LEN]:
    top_ups: DynArray[GasTopUp, MAX_LEN] = []
    for gauge: RootGauge in gauges:
        bridger: Bridger = staticcall gauge.bridger()
        if bridger == empty(Bridger):
            top_ups.append(empty(GasTopUp))
        else:
            bal: uint256 = gauge.address.balance
            cost: uint256 = staticcall bridger.cost()
            print("Balance ", bal)
            print("Cost ", cost)
            top_ups.append(
                GasTopUp(
                    amount = max(staticcall bridger.cost(), bal) - bal,
                    token = IERC20(ETH_ADDRESS),
                    receiver = gauge.address,
                )
            )
    return top_ups


@view
@external
def get_gas_top_ups(_gauges: DynArray[RootGauge, MAX_LEN]) -> DynArray[GasTopUp, MAX_LEN]:
    """
    @notice Get amounts of gas for bridging. Non ETH gas not supported.
    @param _gauges Gauges intended for transmission
    """
    return self._get_gas_top_ups(_gauges)


### GAUGE_CONTROLLER replication
### Can not follow fully bc of private variables,
### should work in most cases


@view
@internal
def _get_weight(gauge: RootGauge, time: uint256) -> uint256:
    t: uint256 = min(staticcall GAUGE_CONTROLLER.time_weight(gauge.address), time)
    if t > 0:
        pt: Point = staticcall GAUGE_CONTROLLER.points_weight(gauge.address, t)
        for i: uint256 in range(500):
            if t >= time:
                break
            t += WEEK
            d_bias: uint256 = pt.slope * WEEK
            if pt.bias > d_bias:
                pt.bias -= d_bias
                # d_slope: uint256 = staticcall GAUGE_CONTROLLER.changes_weight(gauge_addr, t)
                # pt.slope -= d_slope
            else:
                pt.bias = 0
                pt.slope = 0
        return pt.bias
    else:
        return 0


@view
@internal
def _get_sum(gauge_type: int128, time: uint256) -> uint256:
    t: uint256 = min(staticcall GAUGE_CONTROLLER.time_sum(convert(gauge_type, uint256)), time)
    if t > 0:
        pt: Point = staticcall GAUGE_CONTROLLER.points_sum(gauge_type, t)
        for i: uint256 in range(500):
            if t >= time:
                break
            t += WEEK
            d_bias: uint256 = pt.slope * WEEK
            if pt.bias > d_bias:
                pt.bias -= d_bias
                # d_slope: uint256 = staticcall GAUGE_CONTROLLER.changes_sum(gauge_type, t)
                # pt.slope -= d_slope
            else:
                pt.bias = 0
                pt.slope = 0
        return pt.bias
    else:
        return 0


@view
@internal
def _get_type_weight(gauge_type: int128, time: uint256) -> uint256:
    t: uint256 = min(staticcall GAUGE_CONTROLLER.time_type_weight(convert(gauge_type, uint256)), time)
    if t > 0:
        return staticcall GAUGE_CONTROLLER.points_type_weight(gauge_type, t)
    else:
        return 0


@view
@internal
def _get_total(gauge: RootGauge, time: uint256) -> uint256:
    t: uint256 = min(staticcall GAUGE_CONTROLLER.time_total(), time)
    _n_gauge_types: int128 = staticcall GAUGE_CONTROLLER.n_gauge_types()
    if t >= time + WEEK:
        return staticcall GAUGE_CONTROLLER.points_total(time)

    pt: uint256 = 0
    for gauge_type: int128 in range(100):
        type_sum: uint256 = self._get_sum(gauge_type, time)
        type_weight: uint256 = self._get_type_weight(gauge_type, time)
        pt += type_sum * type_weight
    return pt



@view
@internal
def _gauge_relative_weight(gauge: RootGauge, time: uint256) -> uint256:
    t: uint256 = time // WEEK * WEEK
    _total_weight: uint256 = self._get_total(gauge, t)

    if _total_weight > 0:
        gauge_type: int128 = staticcall GAUGE_CONTROLLER.gauge_types(gauge.address)
        _type_weight: uint256 = self._get_type_weight(gauge_type, t)
        _gauge_weight: uint256 = self._get_weight(gauge, t)
        return 10 ** 18 * _type_weight * _gauge_weight // _total_weight

    else:
        return 0


@view
@internal
def _to_mint(gauge: RootGauge, ts: uint256) -> uint256:
    last_period: uint256 = staticcall gauge.last_period()
    current_period: uint256 = ts // WEEK

    params: InflationParams = staticcall gauge.inflation_params()
    emissions: uint256 = staticcall gauge.total_emissions()

    if last_period < current_period:
        for i: uint256 in range(last_period, current_period, bound=256):
            period_time: uint256 = i * WEEK
            weight: uint256 = self._gauge_relative_weight(gauge, period_time)

            if period_time <= params.finish_time and params.finish_time < period_time + WEEK:
                emissions += weight * params.rate * (params.finish_time - period_time) // 10 ** 18
                params.rate = params.rate * RATE_DENOMINATOR // RATE_REDUCTION_COEFFICIENT
                emissions += weight * params.rate * (period_time + WEEK - params.finish_time) // 10 ** 18
                params.finish_time += RATE_REDUCTION_TIME
            else:
                emissions += weight * params.rate * WEEK // 10 ** 18

    return emissions - staticcall MINTER.minted(gauge.address, gauge)


@view
@external
def calculate_emissions(
    _gauges: DynArray[RootGauge, MAX_LEN], _ts: uint256 = block.timestamp,
) -> DynArray[uint256, MAX_LEN]:
    """
    @notice Calculate amounts of CRV being transmitted at `_ts`.
        Gas-guzzling function, considered for off-chain use.
        Also not precise, better to simulate txs beforehand.
    @dev Replicated logic from GaugeController, but not precise because some variables are private.
    @param _gauges List of gauge addresses
    @param _ts Timestamp at which to calculate
    @return Amounts of CRV to be transmitted at `_ts`
    """
    emissions: DynArray[uint256, MAX_LEN] = []
    for gauge: RootGauge in _gauges:
        emissions.append(staticcall CRV.balanceOf(gauge.address) + self._to_mint(gauge, _ts))
    return emissions
