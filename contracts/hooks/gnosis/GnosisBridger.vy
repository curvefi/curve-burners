# @version 0.3.10
"""
@title GnosisBridger
@license MIT
@author Curve Finance
@notice Curve Gnosis (prev Xdai) Omni Bridge Wrapper
"""
from vyper.interfaces import ERC20

interface BridgedERC20:
    def allowance(_from: address, _to: address) -> uint256: view
    def balanceOf(_of: address) -> uint256: view
    def approve(_to: address, _amount: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _amount: uint256) -> bool: nonpayable
    def bridgeContract() -> address: view

interface Bridge:
    def relayTokens(_token: BridgedERC20, _receiver: address, _value: uint256): nonpayable


@external
def bridge(_token: BridgedERC20, _to: address, _amount: uint256, _min_amount: uint256=0) -> uint256:
    """
    @notice Bridge an asset using the Omni Bridge
    @param _token The ERC20 asset to bridge
    @param _to The receiver on Ethereum
    @param _amount The amount of `_token` to bridge
    @param _min_amount Minimum amount when to bridge
    @return Bridged amount
    """
    amount: uint256 = _amount
    if amount == max_value(uint256):
        amount = _token.balanceOf(msg.sender)
    if amount < _min_amount:
        return 0
    assert _token.transferFrom(msg.sender, self, amount, default_return_value=True)

    bridge: address = _token.bridgeContract()
    if _token.allowance(self, bridge) < amount:
        assert _token.approve(bridge, max_value(uint256), default_return_value=True)

    amount = _token.balanceOf(self)
    Bridge(bridge).relayTokens(_token, _to, amount)
    return amount


@pure
@external
def cost() -> uint256:
    """
    @notice Cost in ETH to bridge
    @return Amount of ETH to inlcude
    """
    return 0


@pure
@external
def check(_account: address) -> bool:
    """
    @notice Check if `_account` may bridge via `transmit_emissions`
    @param _account The account to check
    @return True if `_account` may bridge
    """
    return True
