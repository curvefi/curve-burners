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

event SetControllers:
    controllers: DynArray[address, 20]

event SetOwner:
    owner: address

interface Controller:
    def collect_fees() -> uint256: nonpayable


MAX_BPS: constant(uint256) = 10_000

controllers: DynArray[address, 20]
distribution_weight: uint256
distribution_receiver: public(address)
incentives_receiver: public(address)
owner: public(address)
crvusd: immutable(address)

@external
def __init__(_crvusd: address, distribution_weight: uint256, distribution_receiver: address, incentives_receiver: address, owner: address):
    """
    @notice Contract constructor
    @param _crvusd The address of the crvUSD token contract
    @param distribution_weight The initial weight for distribution (scaled by 1e18)
    @param distribution_receiver The address to receive the distribution amount
    @param incentives_receiver The address to receive the incentives amount
    @param owner The address of the contract owner
    """
    crvusd = _crvusd
    self.distribution_weight = distribution_weight
    self.incentives_receiver = incentives_receiver
    self.distribution_receiver = distribution_receiver
    self.owner = owner

@nonreentrant("lock")
@external
def claim_controller_fees():
    """
    @notice Claim fees from all controllers and distribute them
    @dev Splits and transfers the balance according to the distribution weight
    """
    for controller in self.controllers:
        Controller(controller).collect_fees()

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

    log SetWeights(distribution_weight)


@external
def set_controllers(controllers: DynArray[address, 20]):
    """
    @notice Set the list of controller addresses
    @param controllers The new list of controller addresses
    """
    assert msg.sender == self.owner, "Only owner"
    self.controllers = controllers

    log SetControllers(controllers)

@external
def set_distribution_receiver(distribution_receiver: address):
    """
    @notice Set the address that will receive crvUSD for distribution
        to veCRV holders.
    @param distribution_receiver The address that will receive
        crvUSD for distribution to veCRV holders
    """
    assert msg.sender == self.owner, "Only owner"
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