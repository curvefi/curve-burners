# @version 0.3.10
"""
@title Hooker
@notice Support hooks
"""

from vyper.interfaces import ERC20


interface FeeCollector:
    def target() -> ERC20: view
    def owner() -> address: view
    def emergency_owner() -> address: view


struct CompensationStrategy:
    amount: uint256  # In case of Dutch auction max amount
    last_payout_ts: uint256
    start: uint256
    end: uint256
    dutch: bool


struct Hook:
    to: address
    foreplay: Bytes[8192]  # including method_id
    compensation_strategy: CompensationStrategy
    mandatory: bool  # Hooks mandatory to act after fee_collector transfer


# Property: no future changes in FeeCollector
struct HookInput:
    hook_id: uint8
    value: uint256
    data: Bytes[8192]


ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
SUPPORTED_INTERFACES: constant(bytes4[2]) = [
    # ERC165: method_id("supportsInterface(bytes4)") == 0x01ffc9a7
    0x01ffc9a7,
    # Hooker:
    #   method_id("act((uint8,uint256,bytes)[],address,bool)") == 0xd0ba45fe
    #   method_id("buffer_amount()") == 0x69e15fcb
    0xb95b1a35,
]

START_TIME: constant(uint256) = 1600300800  # ts of distribution start
WEEK: constant(uint256) = 7 * 24 * 3600
MAX_LEN: constant(uint256) = 64
MAX_HOOKS_LEN: constant(uint256) = 32
fee_collector: public(immutable(FeeCollector))

hooks: public(DynArray[Hook, MAX_HOOKS_LEN])
mandatory_hook_mask: uint256
buffer_amount: public(uint256)


@external
def __init__(_fee_collector: FeeCollector):
    """
    @notice Contract constructor
    @param _fee_collector Hooker is _hooked_ to fee_collector contract with no update possibility
    """
    fee_collector = _fee_collector


@internal
def _shot(hook: Hook, hook_input: HookInput):
    """
    @notice Hook run implementation
    """
    raw_call(
        hook.to,
        concat(hook.foreplay, hook_input.data),
        value=hook_input.value,
    )


@internal
@view
def _compensate(hook: Hook, ts: uint256=block.timestamp) -> uint256:
    """
    @notice Calculate compensation of calling hook at timestamp
    @dev Does not update last_payout_ts parameter to keep view mutability
    @param hook Hook to act
    @param ts Timestamp to calculate at (current by default)
    @return Amount to compensate according to strategy
    """
    strategy: CompensationStrategy = hook.compensation_strategy
    if strategy.amount == 0 or ts < strategy.last_payout_ts:  # mandatory hooks or not compensating yet
        return 0

    since_last_payout: uint256 = ts - strategy.last_payout_ts
    ts = (ts - START_TIME) % WEEK
    if ts < strategy.start:
        ts += WEEK
    end: uint256 = strategy.end
    if end <= strategy.start:
        end += WEEK
    if end <= ts or since_last_payout <= ts - strategy.start:  # out of bound or already compensated
        return 0

    compensation: uint256 = strategy.amount
    if strategy.dutch:
        compensation = strategy.amount * (ts - strategy.start) / (end - strategy.start)
    return compensation


@view
@external
def calc_compensation(_hook_inputs: DynArray[HookInput, MAX_HOOKS_LEN],
                      _mandatory: bool=False, _ts: uint256=block.timestamp) -> uint256:
    """
    @notice Calculate compensation for acting hooks. Checks input
    @param _hook_inputs HookInput of hooks to act, only ids are used
    @param _mandatory Bool whether act is through fee_collector (False by default)
    @param _ts Timestamp at which to calculate compensations (current by default)
    @return Amount of target coin to receive as compensation
    """
    hook_mask: uint256 = 0
    compensation: uint256 = 0
    prev_idx: uint8 = 0
    for solicitation in _hook_inputs:
        hook: Hook = self.hooks[solicitation.hook_id]
        compensation += self._compensate(hook, _ts)

        hook_mask |= 1 << solicitation.hook_id
        if prev_idx > solicitation.hook_id:
            raise "Hooks not sorted"
        prev_idx = solicitation.hook_id

    if _mandatory:
        mandatory_hook_mask: uint256 = self.mandatory_hook_mask
        assert hook_mask & mandatory_hook_mask == mandatory_hook_mask, "Not all mandatory hooks"

    return compensation


@external
@payable
def act(_hook_inputs: DynArray[HookInput, MAX_HOOKS_LEN],
        _receiver: address=msg.sender, _mandatory: bool=False) -> uint256:
    """
    @notice Entry point to run hooks and receive compensation
    @param _hook_inputs Inputs assembled by keepers
    @param _receiver Receiver of compensation (sender by default)
    @param _mandatory Check mandatory hooks to trigger from fee_collector (False by default)
    @return Compensation received
    """
    hook_mask: uint256 = 0
    compensation: uint256 = 0
    prev_idx: uint8 = 0
    for solicitation in _hook_inputs:
        hook: Hook = self.hooks[solicitation.hook_id]
        self._shot(hook, solicitation)
        compensation += self._compensate(hook)
        self.hooks[solicitation.hook_id].compensation_strategy.last_payout_ts = block.timestamp

        hook_mask |= 1 << solicitation.hook_id
        if prev_idx > solicitation.hook_id:
            raise "Hooks not sorted"
        prev_idx = solicitation.hook_id

    if _mandatory:
        mandatory_hook_mask: uint256 = self.mandatory_hook_mask
        assert hook_mask & mandatory_hook_mask == mandatory_hook_mask, "Not all mandatory hooks"

    # happy ending
    if msg.sender != fee_collector.address and compensation > 0:
        coin: ERC20 = fee_collector.target()
        coin.transferFrom(fee_collector.address, _receiver, compensation)
    return compensation


@external
@payable
def one_time_hooks(_hooks: DynArray[Hook, MAX_HOOKS_LEN], _inputs: DynArray[HookInput, MAX_HOOKS_LEN]):
    """
    @notice Coin approvals, any settings that need to be executed once
    @dev Callable only by owner
    @param _hooks Hook input
    @param _inputs May be used to include native coin
    """
    assert msg.sender == fee_collector.owner(), "Only owner"

    for i in range(len(_hooks), bound=MAX_HOOKS_LEN):
        self._shot(_hooks[i], _inputs[i])


@external
def set_hooks(_new_hooks: DynArray[Hook, MAX_HOOKS_LEN]):
    """
    @notice Set new hooks
    @dev Callable only by owner
    """
    assert msg.sender == fee_collector.owner(), "Only owner"

    self.hooks = _new_hooks

    buffer_amount: uint256 = 0
    mask: uint256 = 0
    for i in range(len(_new_hooks), bound=MAX_HOOKS_LEN):
        assert _new_hooks[i].compensation_strategy.start < WEEK
        assert _new_hooks[i].compensation_strategy.end < WEEK

        buffer_amount += _new_hooks[i].compensation_strategy.amount
        if _new_hooks[i].mandatory:
            mask ^= 1 << i
    self.buffer_amount = buffer_amount
    self.mandatory_hook_mask = mask


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
