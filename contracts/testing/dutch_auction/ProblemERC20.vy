# pragma version 0.4.3
"""
@title Configurable problem ERC-20 test double
@author Curve Finance
@license MIT
@notice Models false returns, approval resets, transfer fees, blacklists, rebases, and hooks.
@custom:kill Test-only contract; no production kill path is required.
@custom:security This mock deliberately exposes arbitrary balance and behavior controls.
"""


event Transfer:
    sender: indexed(address)
    receiver: indexed(address)
    amount: uint256

event Approval:
    owner: indexed(address)
    spender: indexed(address)
    amount: uint256


BPS: constant(uint256) = 10_000

name: public(String[64])
symbol: public(String[32])
decimals: public(uint256)
total_supply: public(uint256)

balances: HashMap[address, uint256]
allowances: HashMap[address, HashMap[address, uint256]]
blacklisted: public(HashMap[address, bool])

returns_false: public(bool)
fails_nonzero_approval: public(bool)
requires_approval_reset: public(bool)
revert_balance_of: public(bool)
fee_bps: public(uint256)

hook_target: public(address)
hook_sender: public(address)
hook_data: public(Bytes[4096])
hook_reverts_on_failure: public(bool)


@deploy
def __init__(_name: String[64], _symbol: String[32], _decimals: uint256):
    self.name = _name
    self.symbol = _symbol
    self.decimals = _decimals


@external
@view
def totalSupply() -> uint256:
    return self.total_supply


@external
@view
def balanceOf(owner: address) -> uint256:
    assert not self.revert_balance_of, "balanceOf reverted"
    return self.balances[owner]


@external
@view
def allowance(owner: address, spender: address) -> uint256:
    return self.allowances[owner][spender]


@internal
def _run_hook():
    if self.hook_target != empty(address) and msg.sender == self.hook_sender:
        success: bool = raw_call(
            self.hook_target,
            self.hook_data,
            max_outsize=0,
            revert_on_failure=False,
        )
        assert success or not self.hook_reverts_on_failure, "Hook failed"


@internal
def _transfer(sender: address, receiver: address, amount: uint256):
    assert not self.blacklisted[sender] and not self.blacklisted[receiver], "Blacklisted"
    self.balances[sender] -= amount

    fee: uint256 = amount * self.fee_bps // BPS
    received: uint256 = amount - fee
    self.balances[receiver] += received
    self.total_supply -= fee

    log Transfer(sender=sender, receiver=receiver, amount=received)
    if fee != 0:
        log Transfer(sender=sender, receiver=empty(address), amount=fee)

    self._run_hook()


@external
def transfer(receiver: address, amount: uint256) -> bool:
    if self.returns_false:
        return False
    self._transfer(msg.sender, receiver, amount)
    return True


@external
def transferFrom(sender: address, receiver: address, amount: uint256) -> bool:
    if self.returns_false:
        return False
    self.allowances[sender][msg.sender] -= amount
    self._transfer(sender, receiver, amount)
    return True


@external
def approve(spender: address, amount: uint256) -> bool:
    if self.returns_false:
        return False
    if self.fails_nonzero_approval and amount != 0:
        return False
    if self.requires_approval_reset:
        assert amount == 0 or self.allowances[msg.sender][spender] == 0, "Reset approval first"
    self.allowances[msg.sender][spender] = amount
    log Approval(owner=msg.sender, spender=spender, amount=amount)
    return True


@external
def mint(receiver: address, amount: uint256):
    self.total_supply += amount
    self.balances[receiver] += amount
    log Transfer(sender=empty(address), receiver=receiver, amount=amount)


@external
def _mint_for_testing(receiver: address, amount: uint256):
    self.total_supply += amount
    self.balances[receiver] += amount
    log Transfer(sender=empty(address), receiver=receiver, amount=amount)


@external
def set_balance(owner: address, amount: uint256):
    previous: uint256 = self.balances[owner]
    self.balances[owner] = amount
    if amount >= previous:
        self.total_supply += amount - previous
    else:
        self.total_supply -= previous - amount


@external
def set_allowance_for_testing(owner: address, spender: address, amount: uint256):
    self.allowances[owner][spender] = amount


@external
def set_returns_false(enabled: bool):
    self.returns_false = enabled


@external
def set_fails_nonzero_approval(enabled: bool):
    self.fails_nonzero_approval = enabled


@external
def set_requires_approval_reset(enabled: bool):
    self.requires_approval_reset = enabled


@external
def set_revert_balance_of(enabled: bool):
    self.revert_balance_of = enabled


@external
def set_fee_bps(_fee_bps: uint256):
    assert _fee_bps <= BPS, "Bad fee"
    self.fee_bps = _fee_bps


@external
def set_blacklisted(account: address, enabled: bool):
    self.blacklisted[account] = enabled


@external
def configure_hook(
    target: address,
    sender: address,
    data: Bytes[4096],
    revert_on_failure: bool,
):
    self.hook_target = target
    self.hook_sender = sender
    self.hook_data = data
    self.hook_reverts_on_failure = revert_on_failure
