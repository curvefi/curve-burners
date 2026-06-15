# @version 0.3.10
"""
@title ProxyFeeCollectorBurner
@license MIT
@notice Sidechain proxy burner that rate-limits ERC20 transfers into FeeCollector
"""


interface ERC20:
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable
    def balanceOf(_owner: address) -> uint256: view


event SetAdmin:
    admin: address

event SetLimit:
    coin: indexed(address)
    limit: uint256

proxy: public(immutable(address))
fee_collector: public(immutable(address))

admin: public(address)
limits: public(HashMap[address, uint256])


@external
def __init__(_proxy: address, _fee_collector: address, _admin: address):
    """
    @notice Contract constructor
    @param _proxy Sidechain StableSwap proxy that will call this burner
    @param _fee_collector FeeCollector that will receive fees from proxy
    @param _admin Account allowed to manage per-coin limits
    """
    assert _proxy != empty(address), "Bad proxy"
    assert _fee_collector != empty(address), "Bad FeeCollector"
    assert _admin != empty(address), "Bad admin"

    proxy = _proxy
    fee_collector = _fee_collector
    self.admin = _admin

    log SetAdmin(_admin)


@external
def set_admin(_admin: address):
    """
    @notice Set admin account
    @param _admin New admin account
    """
    assert msg.sender == self.admin, "Only admin"
    assert _admin != empty(address), "Bad admin"

    self.admin = _admin
    log SetAdmin(_admin)


@external
def set_limit(_coin: address, _limit: uint256):
    """
    @notice Set absolute remaining transfer limit for coin
    @param _coin Coin address
    @param _limit Remaining transfer limit
    """
    assert msg.sender == self.admin, "Only admin"

    self.limits[_coin] = _limit
    log SetLimit(_coin, _limit)


@internal
def _burn_balance(_coin: address, _source: address):
    limit: uint256 = self.limits[_coin]
    amount: uint256 = min(ERC20(_coin).balanceOf(_source), limit)
    if amount > 0:
        if _source == self:
            assert ERC20(_coin).transfer(fee_collector, amount, default_return_value=True)
        else:
            assert ERC20(_coin).transferFrom(_source, fee_collector, amount, default_return_value=True)

    self.limits[_coin] = limit - amount


@external
def burn(_coin: address) -> bool:
    """
    @notice Transfer up to coin limit from proxy to FeeCollector
    @dev Intended to be called by PoolProxy.burn/burn_many.
    @param _coin Coin address
    @return True for legacy burner compatibility
    """
    assert msg.sender == proxy, "Only proxy"

    self._burn_balance(_coin, proxy)
    return True


@external
def burn_self(_coin: address) -> bool:
    """
    @notice Transfer up to coin limit from this burner to FeeCollector
    @param _coin Coin address
    @return True for legacy burner compatibility
    """
    self._burn_balance(_coin, self)
    return True
