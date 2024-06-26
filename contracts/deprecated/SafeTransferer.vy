# pragma version 0.3.10

# FeeCollector does not do safe transfer on burn what happens to be an issue for some coins(USDT)
# A patch to transfer coins from Proxy to FeeCollector

interface ERC20:
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable
    def balanceOf(_owner: address) -> uint256: view


fee_collector: public(immutable(address))


@external
def __init__(_fee_collector: address):
    fee_collector = _fee_collector


@external
@payable
def burn(_coin: ERC20) -> bool:
    """
    @notice Transfer coin from contract with approval
    @dev Needed for back compatability
    @param _coin Coin to transfer
    @return True if did not fail, back compatability
    """
    amount: uint256 = _coin.balanceOf(msg.sender)
    assert _coin.transferFrom(msg.sender, fee_collector, amount, default_return_value=True)
    return True
