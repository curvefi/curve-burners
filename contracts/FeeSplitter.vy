# pragma version 0.3.10

from vyper.interfaces import ERC20

event SetOwner:
    owner: indexed(address)

interface Controller:
    def collect_fees() -> uint256: nonpayable


ONE: constant(uint256) = 10**18

controllers: DynArray[address, 20]
distribution_weight: uint256
distribution_receiver: public(address)
incentives_receiver: public(address)
owner: public(address)
crvusd: immutable(address)

@external
def __init__(_crvusd: address, distribution_weight: uint256, distribution_receiver: address, incentives_receiver: address, _owner: address):
    """
    @notice Initialize the contract with initial values
    @dev Sets up the contract with the crvUSD token address, distribution weight, receivers, and owner
    @param _crvusd The address of the crvUSD token contract
    @param distribution_weight The initial weight for distribution (scaled by 1e18)
    @param distribution_receiver The address to receive the distribution amount
    @param incentives_receiver The address to receive the incentives amount
    @param _owner The address of the contract owner
    """
    crvusd = _crvusd
    self.distribution_weight = distribution_weight
    self.incentives_receiver = incentives_receiver
    self.distribution_receiver = distribution_receiver
    self.owner = _owner

@nonreentrant("lock")
@external
def claim_controller_fees():
    """
    @notice Claim fees from all controllers and distribute them
    @dev Collects fees from all controllers, then splits and transfers the balance
         according to the distribution weight
    """
    for controller in self.controllers:
        Controller(controller).collect_fees()

    balance: uint256 = ERC20(crvusd).balanceOf(self)

    distribution_amount = balance * self.distribution_weight / ONE
    incentives_amount = balance - distribution_amount

    ERC20(crvusd).transfer(self.distribution_receiver, distribution_amount)
    ERC20(crvusd).transfer(self.incentives_receiver, incentives_amount)

@external
def set_weights(distribution_weight: uint256):
    """
    @notice Set the distribution weight
    @dev Can only be called by the contract owner
    @param distribution_weight The new distribution weight (scaled by 1e18)
    """
    assert msg.sender == self.owner, "Only owner"
    if distribution_weight > ONE:
        raise "Weight bigger than 100%"
    self.distribution_weight = distribution_weight


@external
def set_controllers(controllers: DynArray[address, 20]):
    """
    @notice Set the list of controller addresses
    @param controllers The new list of controller addresses
    """
    assert msg.sender == self.owner, "Only owner"
    self.controllers = controllers

@external
def set_distribution_receiver(_distribution_receiver: address):
    """
    """
    assert msg.sender == self.owner, "Only owner"
    self.distribution_receiver = _distribution_receiver

@external
def set_incentives_receiver(_incentives_receiver: address):
    """
    """
    assert msg.sender == self.owner, "Only owner"
    self.incentives_receiver = _incentives_receiver

@external
def set_owner(_new_owner: address):
    """
    @notice Set owner of the contract
    @dev Callable only by current owner
    @param _new_owner Address of the new owner
    """
    assert msg.sender == self.owner, "Only owner"
    assert _new_owner != empty(address)
    self.owner = _new_owner
    log SetOwner(_new_owner)

@internal
def _incentives_weight() -> uint256:
    return ONE - self.distribution_weight

@external
def incentives_weight() -> uint256:
    """
    """
    return self._incentives_weight()