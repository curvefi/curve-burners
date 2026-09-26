# pragma version 0.5.0a4
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
# Compiler: vyper@03e096e74b53993e652ed83dddecbee6f889fcc5
"""
@title JobBoard payments module
@author Curve Finance
@license MIT
@notice Rewards are priced in `target` and paid out in exactly ONE token per
        call: target by default (pulled from FeeCollector's weekly buffer
        allowance), or an owner-whitelisted token from this contract's pool
        at a fixed conversion rate. Quotes are honest: capped by the budget
        actually available, so simulation matches execution.
"""


interface ERC20:
    def transfer(_receiver: address, _amount: uint256) -> bool: nonpayable
    def transferFrom(_owner: address, _receiver: address, _amount: uint256) -> bool: nonpayable
    def balanceOf(_owner: address) -> uint256: view
    def allowance(_owner: address, _spender: address) -> uint256: view


event SetRate:
    token: indexed(address)
    rate: uint256


ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
ONE: constant(uint256) = 10 ** 18

# Target base units per 1e18 base units of token; owner-set, conservative,
# no oracles. 0 = token not allowed.
# NOTE: decimals are baked into the rate. Examples for target = crvUSD (18):
#   ETH  @ $3000: rate = 3000 * 10**18
#   USDC @ $1 (6 decimals): rate = 10**18 * 10**(18-6) * 1 = 10**30
rate: public(HashMap[address, uint256])


@deploy
def __init__():
    pass


@internal
def _set_rate(_token: address, _rate: uint256):
    self.rate[_token] = _rate
    log SetRate(token=_token, rate=_rate)


@view
@internal
def _resolve(_payout_token: address, _target: ERC20) -> address:
    if _payout_token == empty(address):
        return _target.address
    return _payout_token


@view
@internal
def _available(_token: address, _target: ERC20, _fee_collector: address) -> uint256:
    """
    @notice Budget available right now for a payout token
    """
    if _token == _target.address:
        return min(
            staticcall _target.allowance(_fee_collector, self),
            staticcall _target.balanceOf(_fee_collector),
        )
    if _token == ETH_ADDRESS:
        return self.balance
    return staticcall ERC20(_token).balanceOf(self)


@view
@internal
def _quote(_reward: uint256, _payout_token: address, _target: ERC20, _fee_collector: address) -> uint256:
    """
    @param _reward Accrued reward in target terms
    @return Actually payable amount in the chosen payout token
    """
    if _reward == 0:
        return 0

    token: address = self._resolve(_payout_token, _target)
    amount: uint256 = _reward
    if token != _target.address:
        r: uint256 = self.rate[token]
        if r == 0:  # token not allowed
            return 0
        amount = _reward * ONE // r
    return min(amount, self._available(token, _target, _fee_collector))


@internal
def _payout(_receiver: address, _reward: uint256, _payout_token: address,
            _target: ERC20, _fee_collector: address) -> uint256:
    """
    @notice Pay out accrued reward in a single token
    @return Amount paid in the payout token
    """
    amount: uint256 = self._quote(_reward, _payout_token, _target, _fee_collector)
    if amount == 0:
        return 0

    token: address = self._resolve(_payout_token, _target)
    if token == _target.address:
        assert extcall _target.transferFrom(_fee_collector, _receiver, amount, default_return_value=True)
    elif token == ETH_ADDRESS:
        raw_call(_receiver, b"", value=amount)
    else:
        assert extcall ERC20(token).transfer(_receiver, amount, default_return_value=True)
    return amount
