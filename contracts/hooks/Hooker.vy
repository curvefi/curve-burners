# pragma version 0.3.10
"""
@title Hooker
@license MIT
@author Curve Finance
@notice Support hooks
"""

from vyper.interfaces import ERC20


interface FeeCollector:
    def target() -> ERC20: view
    def owner() -> address: view
    def emergency_owner() -> address: view
    def epoch_time_frame(_epoch: Epoch, _ts: uint256=block.timestamp) -> (uint256, uint256): view


enum Epoch:
    SLEEP  # 1
    COLLECT  # 2
    EXCHANGE  # 4
    FORWARD  # 8


event DutyAct:
    pass

event Act:
    receiver: indexed(address)
    compensation: uint256

event HookShot:
    hook_id: indexed(uint8)
    compensation: uint256


struct CompensationCooldown:
    duty_counter: uint64  # last compensation epoch
    used: uint64
    limit: uint64  # Maximum number of compensations between duty acts (week)

struct CompensationStrategy:
    amount: uint256  # In case of Dutch auction max amount
    cooldown: CompensationCooldown
    start: uint256
    end: uint256
    dutch: bool


struct Hook:
    to: address
    foreplay: Bytes[1024]  # including method_id
    compensation_strategy: CompensationStrategy
    duty: bool  # Hooks mandatory to act after fee_collector transfer


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
    #   method_id("duty_act((uint8,uint256,bytes)[],address)") == 0x8c88eb86
    #   method_id("buffer_amount()") == 0x69e15fcb
    0xe569b44d,
]

START_TIME: constant(uint256) = 1600300800  # ts of distribution start
WEEK: constant(uint256) = 7 * 24 * 3600
MAX_LEN: constant(uint256) = 64
MAX_HOOKS_LEN: constant(uint256) = 32
fee_collector: public(immutable(FeeCollector))

hooks: public(DynArray[Hook, MAX_HOOKS_LEN])
duties_checklist: uint256  # mask of hooks with `duty` flag
buffer_amount: public(uint256)

duty_counter: public(uint64)


@external
def __init__(_fee_collector: FeeCollector,
             _initial_oth: DynArray[Hook, MAX_HOOKS_LEN], _initial_oth_inputs: DynArray[HookInput, MAX_HOOKS_LEN],
             _initial_hooks: DynArray[Hook, MAX_HOOKS_LEN]):
    """
    @notice Contract constructor
    @param _fee_collector Hooker is _hooked_ to fee_collector contract with no update possibility
    @param _initial_oth One time hooks at initialization
    @param _initial_oth_inputs One time hooks input at initialization
    @param _initial_hooks Hooks to set at initialization
    """
    fee_collector = _fee_collector

    self._one_time_hooks(_initial_oth, _initial_oth_inputs)
    self._set_hooks(_initial_hooks)


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
def _compensate(hook: Hook, ts: uint256=block.timestamp, _num: uint64=1) -> uint256:
    """
    @notice Calculate compensation of calling hook at timestamp
    @dev Does not update compensation strategy to keep view mutability
    @param hook Hook to act
    @param ts Timestamp to calculate at (current by default)
    @param _num Number of executions, needed for view function to track (used/limit)
    @return Amount to compensate according to strategy
    """
    strategy: CompensationStrategy = hook.compensation_strategy
    # duty hook or not compensating yet or
    if strategy.amount == 0 or self.duty_counter < strategy.cooldown.duty_counter or\
        strategy.cooldown.used + _num > strategy.cooldown.limit:  # limit on number of compensations
        return 0

    ts = (ts - START_TIME) % WEEK
    if ts < strategy.start:
        ts += WEEK
    end: uint256 = strategy.end
    if end <= strategy.start:
        end += WEEK
    if end <= ts:  # out of bound
        return 0

    compensation: uint256 = strategy.amount
    if strategy.dutch:
        compensation = strategy.amount * (ts - strategy.start) / (end - strategy.start)
    return compensation


@view
@external
def calc_compensation(_hook_inputs: DynArray[HookInput, MAX_HOOKS_LEN],
                      _duty: bool=False, _ts: uint256=block.timestamp) -> uint256:
    """
    @notice Calculate compensation for acting hooks. Checks input according to execution rules.
        Older timestamps might work incorrectly.
    @param _hook_inputs HookInput of hooks to act, only ids are used
    @param _duty Bool whether act is through fee_collector (False by default).
        If True, assuming calling from fee_collector if possible
    @param _ts Timestamp at which to calculate compensations (current by default)
    @return Amount of target coin to receive as compensation
    """
    current_duty_counter: uint64 = self.duty_counter
    if _duty:
        hook_mask: uint256 = 0
        for solicitation in _hook_inputs:
            hook_mask |= 1 << solicitation.hook_id
        duties_checklist: uint256 = self.duties_checklist
        assert hook_mask & duties_checklist == duties_checklist, "Not all duties"

        time_frame: (uint256, uint256) = fee_collector.epoch_time_frame(Epoch.FORWARD, _ts)
        if time_frame[0] <= _ts and _ts < time_frame[1]:
            current_duty_counter = convert((_ts - START_TIME) / WEEK, uint64)

    compensation: uint256 = 0
    prev_idx: uint8 = 0
    num: uint64 = 0
    for solicitation in _hook_inputs:
        if prev_idx > solicitation.hook_id:
            raise "Hooks not sorted"
        else:
            num = num + 1 if prev_idx == solicitation.hook_id else 1

        hook: Hook = self.hooks[solicitation.hook_id]
        if hook.compensation_strategy.cooldown.duty_counter < current_duty_counter:
            hook.compensation_strategy.cooldown.used = 0
        compensation += self._compensate(hook, _ts, num)
        prev_idx = solicitation.hook_id

    return compensation


@internal
def _act(_hook_inputs: DynArray[HookInput, MAX_HOOKS_LEN], _receiver: address) -> uint256:
    current_duty_counter: uint64 = self.duty_counter

    compensation: uint256 = 0
    prev_idx: uint8 = 0
    for solicitation in _hook_inputs:
        hook: Hook = self.hooks[solicitation.hook_id]
        self._shot(hook, solicitation)

        if hook.compensation_strategy.cooldown.duty_counter < current_duty_counter:
            hook.compensation_strategy.cooldown.used = 0
            hook.compensation_strategy.cooldown.duty_counter = current_duty_counter
        hook_compensation: uint256 = self._compensate(hook)

        if hook_compensation > 0:
            compensation += hook_compensation
            hook.compensation_strategy.cooldown.used += 1
            self.hooks[solicitation.hook_id].compensation_strategy.cooldown = hook.compensation_strategy.cooldown

        if prev_idx > solicitation.hook_id:
            raise "Hooks not sorted"
        prev_idx = solicitation.hook_id
        log HookShot(prev_idx, hook_compensation)

    log Act(_receiver, compensation)

    # happy ending
    if compensation > 0:
        coin: ERC20 = fee_collector.target()
        coin.transferFrom(fee_collector.address, _receiver, compensation)
    return compensation


@external
@payable
def duty_act(_hook_inputs: DynArray[HookInput, MAX_HOOKS_LEN], _receiver: address=msg.sender) -> uint256:
    """
    @notice Entry point to run hooks for FeeCollector
    @param _hook_inputs Inputs assembled by keepers
    @param _receiver Receiver of compensation (sender by default)
    @return Compensation received
    """
    if msg.sender == fee_collector.address:
        self.duty_counter = convert((block.timestamp - START_TIME) / WEEK, uint64)  # assuming time frames are divided weekly

    hook_mask: uint256 = 0
    for solicitation in _hook_inputs:
        hook_mask |= 1 << solicitation.hook_id
    duties_checklist: uint256 = self.duties_checklist
    assert hook_mask & duties_checklist == duties_checklist, "Not all duties"

    log DutyAct()

    return self._act(_hook_inputs, _receiver)


@external
@payable
def act(_hook_inputs: DynArray[HookInput, MAX_HOOKS_LEN], _receiver: address=msg.sender) -> uint256:
    """
    @notice Entry point to run hooks and receive compensation
    @param _hook_inputs Inputs assembled by keepers
    @param _receiver Receiver of compensation (sender by default)
    @return Compensation received
    """
    return self._act(_hook_inputs, _receiver)


@internal
def _one_time_hooks(hooks: DynArray[Hook, MAX_HOOKS_LEN], inputs: DynArray[HookInput, MAX_HOOKS_LEN]):
    for i in range(len(hooks), bound=MAX_HOOKS_LEN):
        self._shot(hooks[i], inputs[i])


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

    self._one_time_hooks(_hooks, _inputs)


@internal
def _set_hooks(new_hooks: DynArray[Hook, MAX_HOOKS_LEN]):
    self.hooks = new_hooks

    buffer_amount: uint256 = 0
    mask: uint256 = 0
    for i in range(len(new_hooks), bound=MAX_HOOKS_LEN):
        assert new_hooks[i].compensation_strategy.start < WEEK
        assert new_hooks[i].compensation_strategy.end < WEEK

        buffer_amount += new_hooks[i].compensation_strategy.amount *\
                            convert(new_hooks[i].compensation_strategy.cooldown.limit, uint256)
        if new_hooks[i].duty:
            mask |= 1 << i
    self.buffer_amount = buffer_amount
    self.duties_checklist = mask


@external
def set_hooks(_new_hooks: DynArray[Hook, MAX_HOOKS_LEN]):
    """
    @notice Set new hooks
    @dev Callable only by owner
    @param _new_hooks New list of hooks
    """
    assert msg.sender == fee_collector.owner(), "Only owner"

    self._set_hooks(_new_hooks)


@pure
@external
def supportsInterface(_interface_id: bytes4) -> bool:
    """
    @dev Interface identification is specified in ERC-165.
    @param _interface_id Id of the interface
    @return True if contract supports given interface
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
            coin.transfer(fee_collector.address, coin.balanceOf(self), default_return_value=True)  # do not need safe transfer
