# pragma version 0.4.3
"""
@title FeeCollector frame test double
@author Curve Finance
@license MIT
@notice Supplies a configurable EXCHANGE duration for constructor-boundary tests.
@custom:kill Test-only contract; no production kill path is required.
@custom:security This mock implements only constructor-time FeeCollector views.
"""


target: public(immutable(address))
exchange_duration: public(immutable(uint256))


@deploy
def __init__(_target: address, _exchange_duration: uint256):
    target = _target
    exchange_duration = _exchange_duration


@external
@view
def epoch_time_frame(_epoch: uint256, _timestamp: uint256 = block.timestamp) -> (uint256, uint256):
    return _timestamp, _timestamp + exchange_duration
