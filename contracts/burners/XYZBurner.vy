# @version 0.3.10
"""
@title XYZBurner
@notice Template of a Burner
"""


interface ERC20:
    def approve(_to: address, _value: uint256): nonpayable
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable
    def balanceOf(_owner: address) -> uint256: view


interface FeeCollector:
    def target() -> ERC20: view
    def owner() -> address: view
    def emergency_owner() -> address: view

ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
MAX_LEN: constant(uint256) = 64
SUPPORTED_INTERFACES: constant(bytes4[2]) = [
    # ERC165: method_id("supportsInterface(bytes4)") == 0x01ffc9a7
    0x01ffc9a7,
    # Burner:
    #   method_id("burn(address[],address)") == 0x72a436a8
    #   method_id("push_target()") == 0x2eb078cd
    #   method_id("VERSION()") == 0xffa1ad74
    0xa3b5e311,
]
VERSION: public(constant(String[20])) = "XYZ"

fee_collector: public(immutable(FeeCollector))


@external
def __init__(_fee_collector: FeeCollector):
    """
    @notice Contract constructor
    @param _fee_collector FeeCollector contract it is used with
    """
    fee_collector = _fee_collector


@external
def burn(_coins: DynArray[ERC20, MAX_LEN], _receiver: address):
    """
    @notice Post hook after collect to register coins for burn
    @param _coins Which coins to burn
    @param _receiver Receiver of profit. Might be needed for multiple transactions actions
    """
    pass



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
    """
    return _interface_id in SUPPORTED_INTERFACES


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
            coin.transfer(fee_collector.address, coin.balanceOf(self))  # do not need safe transfer
