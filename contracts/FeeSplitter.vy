# pragma version 0.3.10

"""
@title FeeSplitter
@notice A contract that collects fees from multiple controllers in a single
transaction and distributes them according to some weights.
@license Copyright (c) Curve.Fi, 2020-2024 - all rights reserved
@author curve.fi
@custom:security security@curve.fi
"""

from vyper.interfaces import ERC20

event SetWeights:
    distribution_weight: uint256

event SetDistributionReceiver:
    distribution_receiver: address

event SetIncentivesReceiver:
    incentives_receiver: address


event SetOwner:
    owner: address

interface Controller:
    def collect_fees() -> uint256: nonpayable

interface ControllerFactory:
    def controllers(index: uint256) -> address: nonpayable
    def n_collaterals() -> uint256: nonpayable

version: public(constant(String[8])) = "0.1.0"
# maximum number of claims in a single transaction
MAX_CONTROLLERS: constant(uint256) = 100
# maximum basis points (100%)
MAX_BPS: constant(uint256) = 10_000

controllers: DynArray[address, MAX_CONTROLLERS]
distribution_weight: uint256
distribution_receiver: public(address)
incentives_receiver: public(address)
owner: public(address)

factory: immutable(address)
crvusd: immutable(address)

@external
def __init__(_crvusd: address, _factory: address, distribution_weight: uint256, distribution_receiver: address, incentives_receiver: address, owner: address, factory: address):
    """
    @notice Contract constructor
    @param _crvusd The address of the crvUSD token contract
    @param distribution_weight The initial weight for distribution (scaled by 1e18)
    @param distribution_receiver The address to receive the distribution amount
    @param incentives_receiver The address to receive the incentives amount
    @param owner The address of the contract owner
    """
    assert _crvusd != empty(address), "zeroaddr: crvusd"
    assert _factory != empty(address), "zeroaddr: factory"

    assert distribution_receiver != empty(address), "zeroaddr: distribution_receiver"
    assert incentives_receiver != empty(address), "zeroaddr: incentives_receiver"
    assert owner != empty(address), "zeroaddr: owner"

    assert distribution_weight <= MAX_BPS, "wrongarg: distribution_weight > MAX_BPS"

    # setting immutables
    crvusd = _crvusd
    factory = _factory

    # setting storage vars
    self.distribution_weight = distribution_weight
    self.incentives_receiver = incentives_receiver
    self.distribution_receiver = distribution_receiver
    self.owner = owner

@external
def update_controllers():
    factory: ControllerFactory = ControllerFactory(self.factory)
    old_len: uint256 = len(self.controllers)
    new_len: uint256 = factory.n_collaterals()
    for i in range(old_len, new_len, bound=MAX_CONTROLLERS):
        self.controllers.append(factory.controllers(i))

@nonreentrant("lock")
@external
def claim_controller_fees(controllers: DynArray[address, MAX_CONTROLLERS]=empty(DynArray[address, MAX_CONTROLLERS])):
    """
    @notice Claim fees from all controllers and distribute them
    @dev Splits and transfers the balance according to the distribution weight
    """
    if len(controllers) == 0:
        for c in self.controllers:
            Controller(c).collect_fees()
    else:
        for c in controllers:
            if c not in self.controllers:
                raise "Controller not found"
            Controller(c).collect_fees()

    balance: uint256 = ERC20(crvusd).balanceOf(self)

    distribution_amount: uint256 = balance * self.distribution_weight / MAX_BPS
    incentives_amount: uint256 = balance - distribution_amount

    ERC20(crvusd).transfer(self.distribution_receiver, distribution_amount)
    ERC20(crvusd).transfer(self.incentives_receiver, incentives_amount)

@external
def set_weights(distribution_weight: uint256):
    """
    @notice Set the distribution weight
    @dev Up to 100% (MAX_BPS)
    @param distribution_weight The new distribution weight
    """
    assert msg.sender == self.owner, "Only owner"
    if distribution_weight > MAX_BPS:
        raise "Weight bigger than 100%"

    self.distribution_weight = distribution_weight

    log SetWeights(distribution_weight, self.incentives_weight())


@external
def set_distribution_receiver(distribution_receiver: address):
    """
    @notice Set the address that will receive crvUSD for distribution
        to veCRV holders.
    @param distribution_receiver The address that will receive
        crvUSD for distribution to veCRV holders
    """
    assert msg.sender == self.owner, "Only owner"
    assert distribution_receiver != empty(address)

    self.distribution_receiver = distribution_receiver

    log SetDistributionReceiver(distribution_receiver)

@external
def set_incentives_receiver(incentives_receiver: address):
    """
    @notice Set the address that will receive crvUSD that
        will be used for incentives
    @param incentives_receiver The address that will receive
        crvUSD to be used for incentives
    """
    assert msg.sender == self.owner, "Only owner"
    assert incentives_receiver != empty(address)

    self.incentives_receiver = incentives_receiver

    log SetIncentivesReceiver(incentives_receiver)

@external
def set_owner(new_owner: address):
    """
    @notice Set owner of the contract
    @dev Callable only by current owner
    @param new_owner Address of the new owner
    """
    assert msg.sender == self.owner, "Only owner"
    assert new_owner != empty(address)

    self.owner = new_owner

    log SetOwner(new_owner)

@external
def incentives_weight() -> uint256:
    """
    @notice Getter to compute the weight for incentives
    @return The weight for incentives
    """
    return MAX_BPS - self.distribution_weight