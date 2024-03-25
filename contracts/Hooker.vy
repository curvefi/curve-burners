# @version 0.3.10
"""
@title Callbacker
@notice Everything about callbacks
"""

from vyper.interfaces import ERC20


interface FeeCollector:
    def owner() -> address: view
    def emergency_owner() -> address: view


struct Callback:
    to: address
    data: Bytes[4000]


# Hooks can be:
# - mandatory / optional with additional fee
# - [bridge / transfer to FeeDistributor] / static [] / extra parameters from caller
struct Hook:
    to: address
    calldata: Bytes[4000]


struct HookInput:
    data: Bytes[1024]


ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
SUPPORTED_INTERFACES: constant(bytes4[2]) = [
    # ERC165: method_id("supportsInterface(bytes4)") == 0x01ffc9a7
    0x01ffc9a7,
    # Hooker:
    #   method_id("callback((address,bytes))") == 0x1a82b228
    #   method_id("forward()") == 0xd264e05e
    0xc8e65276,
]

MAX_LEN: constant(uint256) = 64
MAX_HOOK_LEN: constant(uint256) = 32
fee_collector: public(immutable(FeeCollector))

MAX_HOOKS: constant(uint256) = 16
forward_hooks: public(DynArray[Hook, MAX_HOOKS])


@external
def __init__(_fee_collector: FeeCollector):
    fee_collector = _fee_collector


@external
@payable
def callback(_callback: Callback):
    """
    @dev For safety of FeeCollector, no coins at stake
    """
    raw_call(_callback.to, _callback.data, value=msg.value)


@external
def forward(_hook_inputs: DynArray[HookInput, MAX_HOOK_LEN]):
    # TODO
    pass


@external
def set_forward_hooks(_new_hooks: DynArray[Hook, MAX_HOOKS]):
    assert msg.sender == fee_collector.owner()
    self.forward_hooks = _new_hooks


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
