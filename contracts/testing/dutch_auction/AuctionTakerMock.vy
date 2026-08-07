# pragma version 0.4.3
"""
@title Dutch auction callback taker test double
@author Curve Finance
@license MIT
@notice Exercises Yearn-compatible callback payment and reentrancy behavior.
@custom:kill Test-only contract; no production kill path is required.
@custom:security This mock deliberately permits arbitrary test configuration.
"""


interface ERC20:
    def approve(spender: address, amount: uint256) -> bool: nonpayable
    def transfer(receiver: address, amount: uint256) -> bool: nonpayable


interface DutchAuctionBurner:
    def take(
        from_token: address,
        max_amount: uint256,
        taker_receiver: address,
        data: Bytes[8192],
    ) -> uint256: nonpayable

    def take_with_limits(
        from_token: address,
        max_amount: uint256,
        min_amount: uint256,
        max_payment: uint256,
        receiver: address,
        expected_week: uint256,
        deadline: uint256,
        data: Bytes[8192],
    ) -> (uint256, uint256): nonpayable


# Callback payment modes.
PAY_NOTHING: constant(uint256) = 0
PAY_COLLECTOR: constant(uint256) = 1
PAY_BURNER: constant(uint256) = 2
PAY_SPLIT: constant(uint256) = 3
PAY_PULL: constant(uint256) = 4

MAX_APPROVAL: constant(uint256) = max_value(uint256)

burner: public(immutable(DutchAuctionBurner))
fee_collector: public(immutable(address))
target: public(immutable(ERC20))

payment_mode: public(uint256)
reenter: public(bool)
callback_count: public(uint256)
callback_from: public(address)
callback_sender: public(address)
callback_amount_taken: public(uint256)
callback_amount_needed: public(uint256)
callback_data: public(Bytes[8192])


@deploy
def __init__(_burner: DutchAuctionBurner, _fee_collector: address, _target: ERC20):
    burner = _burner
    fee_collector = _fee_collector
    target = _target


@external
def configure(_payment_mode: uint256, _reenter: bool):
    assert _payment_mode <= PAY_PULL, "Bad payment mode"
    self.payment_mode = _payment_mode
    self.reenter = _reenter


@external
def execute_take(
    from_token: address,
    max_amount: uint256,
    receiver: address,
    data: Bytes[8192],
) -> uint256:
    if self.payment_mode == PAY_PULL:
        assert extcall target.approve(burner.address, MAX_APPROVAL)
    return extcall burner.take(from_token, max_amount, receiver, data)


@external
def execute_take_with_limits(
    from_token: address,
    max_amount: uint256,
    min_amount: uint256,
    max_payment: uint256,
    receiver: address,
    expected_week: uint256,
    deadline: uint256,
    data: Bytes[8192],
) -> (uint256, uint256):
    if self.payment_mode == PAY_PULL:
        assert extcall target.approve(burner.address, MAX_APPROVAL)
    return extcall burner.take_with_limits(
        from_token,
        max_amount,
        min_amount,
        max_payment,
        receiver,
        expected_week,
        deadline,
        data,
    )


@external
def auctionTakeCallback(
    from_token: address,
    sender: address,
    amount_taken: uint256,
    amount_needed: uint256,
    data: Bytes[8192],
):
    assert msg.sender == burner.address, "Only burner"

    self.callback_count += 1
    self.callback_from = from_token
    self.callback_sender = sender
    self.callback_amount_taken = amount_taken
    self.callback_amount_needed = amount_needed
    self.callback_data = data

    if self.reenter:
        extcall burner.take(from_token, amount_taken, self, b"")

    if self.payment_mode == PAY_COLLECTOR:
        assert extcall target.transfer(fee_collector, amount_needed)
    elif self.payment_mode == PAY_BURNER:
        assert extcall target.transfer(burner.address, amount_needed)
    elif self.payment_mode == PAY_SPLIT:
        paid_to_collector: uint256 = amount_needed // 2
        assert extcall target.transfer(fee_collector, paid_to_collector)
        assert extcall target.transfer(burner.address, amount_needed - paid_to_collector)
