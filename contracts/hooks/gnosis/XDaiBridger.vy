# pragma version 0.4.3
"""
@title XDaiBridger
@license MIT
@author Curve Finance
@notice Bridges native xDAI from Gnosis to Ethereum
"""


interface WrappedXDAI:
    def balanceOf(_owner: address) -> uint256: view
    def transferFrom(_from: address, _to: address, _amount: uint256) -> bool: nonpayable
    def withdraw(_amount: uint256): nonpayable


interface XDaiBridge:
    def relayTokens(_receiver: address): payable


ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE

WXDAI: public(immutable(WrappedXDAI))
XDAI_BRIDGE: public(immutable(XDaiBridge))


@deploy
def __init__(_wxdai: WrappedXDAI, _xdai_bridge: XDaiBridge):
    assert _wxdai.address != empty(address), "Bad wxDAI"
    assert _xdai_bridge.address != empty(address), "Bad bridge"

    WXDAI = _wxdai
    XDAI_BRIDGE = _xdai_bridge


@payable
@external
def __default__():
    assert msg.sender == WXDAI.address, "Use .bridge()"


@internal
def _bridge_xdai(_to: address, _amount: uint256) -> uint256:
    extcall XDAI_BRIDGE.relayTokens(_to, value=_amount)
    return _amount


@payable
@external
def bridge(_token: address, _to: address, _amount: uint256, _min_amount: uint256=0) -> uint256:
    """
    @notice Bridge xDAI to Ethereum either from wxDAI or from native coin supplied in msg.value
    @param _token wxDAI address or ETH_ADDRESS for native xDAI
    @param _to Receiver on Ethereum
    @param _amount Amount to bridge. Use max_value(uint256) to use all available amount
    @param _min_amount Minimum amount when to bridge
    @return Bridged amount
    """
    amount: uint256 = _amount

    if _token == ETH_ADDRESS:
        if amount == max_value(uint256):
            amount = msg.value
        else:
            assert msg.value == amount, "Bad msg.value"
        assert amount >= _min_amount, "Insufficient amount"
        return self._bridge_xdai(_to, amount)

    assert msg.value == 0, "Non-zero value"
    assert _token == WXDAI.address, "Unsupported token"

    if amount == max_value(uint256):
        amount = staticcall WXDAI.balanceOf(msg.sender)
    assert amount >= _min_amount, "Insufficient amount"

    assert extcall WXDAI.transferFrom(msg.sender, self, amount, default_return_value=True)
    extcall WXDAI.withdraw(amount)
    return self._bridge_xdai(_to, amount)


@pure
@external
def cost() -> uint256:
    """
    @notice Cost in ETH to bridge
    @return Amount of ETH to include
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
