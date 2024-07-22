# pragma version 0.3.10

"""
@title FeeSplitter
@notice A contract that collects fees from multiple crvUSD controllers
in a single transaction and distributes them according to some weights.
@license Copyright (c) Curve.Fi, 2020-2024 - all rights reserved
@author curve.fi
@custom:security security@curve.fi
"""

from vyper.interfaces import ERC20

event SetWeights:
    distribution_weight: uint256

event SetCollector:
    distribution_receiver: address

event SetIncentivesManager:
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

controllers: public(DynArray[Controller, MAX_CONTROLLERS])
allowed_controllers: public(HashMap[Controller, bool])
collector_weight: public(uint256)
collector: public(address)
incentives_manager: public(address)
owner: public(address)

factory: immutable(ControllerFactory)
crvusd: immutable(ERC20)

@external
def __init__(_crvusd: address, _factory: address, collector_weight: uint256, collector: address, incentives_manager: address, owner: address):
    """
    @notice Contract constructor
    @param _crvusd The address of the crvUSD token contract
    @param collector_weight The initial weight for veCRV distribution (scaled by 1e18)
    @param collector The address to receive the amount for veCRV holders
    @param incentives_manager The address to receive the incentives amount
    @param owner The address of the contract owner
    """
    assert _crvusd != empty(address), "zeroaddr: crvusd"
    assert _factory != empty(address), "zeroaddr: factory"

    assert collector != empty(address), "zeroaddr: collector"
    assert incentives_manager != empty(address), "zeroaddr: incentives_manager"
    assert owner != empty(address), "zeroaddr: owner"

    assert collector_weight <= MAX_BPS, "weights: collector_weight > MAX_BPS"

    # setting immutables
    crvusd = ERC20(_crvusd)
    factory = ControllerFactory(_factory)

    # setting storage vars
    self.collector_weight = collector_weight
    self.incentives_manager = incentives_manager
    self.collector = collector
    self.owner = owner

@external
def update_controllers():
    """
    @notice Update the list of controllers so that it corresponds to the
        list of controllers in the factory
    """
    old_len: uint256 = len(self.controllers)
    new_len: uint256 = factory.n_collaterals()
    for i in range(new_len - old_len, bound=MAX_CONTROLLERS):
        i_shifted: uint256 = i + old_len
        c: Controller = Controller(factory.controllers(i_shifted))
        self.allowed_controllers[c] = True
        self.controllers.append(c)

@nonreentrant("lock")
@external
def claim_controller_fees(controllers: DynArray[Controller, MAX_CONTROLLERS]=empty(DynArray[Controller, MAX_CONTROLLERS])) -> (uint256, uint256):
    """
    @notice Claim fees from all controllers and distribute them
    @param controllers The list of controllers to claim fees from (default: all)
    @dev Splits and transfers the balance according to the distribution weights
    """
    if len(controllers) == 0:
        for c in self.controllers:
            c.collect_fees()
    else:
        for c in controllers:
            if not self.allowed_controllers[c]:
                raise "controller: not in factory"
            c.collect_fees()

    balance: uint256 = crvusd.balanceOf(self)

    collector_amount: uint256 = balance * self.collector_weight / MAX_BPS
    incentives_amount: uint256 = balance - collector_amount

    crvusd.transfer(self.collector, collector_amount)
    crvusd.transfer(self.incentives_manager, incentives_amount)

    return collector_amount, incentives_amount

@external
def set_weights(collector_weight: uint256):
    """
    @notice Set the collector weight (and implicitly the incentives weight)
    @dev Up to 100% (MAX_BPS)
    @param collector_weight The new collector weight
    """
    assert msg.sender == self.owner, "auth: only owner"
    assert collector_weight <= MAX_BPS, "weights: collector weight > MAX_BPS"

    self.collector_weight = collector_weight

    log SetWeights(collector_weight)


@external
def set_collector(collector: address):
    """
    @notice Set the address that will receive crvUSD for distribution
        to veCRV holders.
    @param collector_receiver The address that will receive crvUSD
    """
    assert msg.sender == self.owner, "auth: only owner"
    assert collector != empty(address), "zeroaddr: collector"

    self.collector = collector

    log SetCollector(collector)

@external
def set_incentives_manager(incentives_manager: address):
    """
    @notice Set the address that will receive crvUSD that
        will be used for incentives
    @param incentives_manager The address that will receive
        crvUSD to be used for incentives
    """
    assert msg.sender == self.owner, "auth: only owner"
    assert incentives_manager != empty(address), "zeroaddr: incentives_manager"

    self.incentives_manager = incentives_manager

    log SetIncentivesManager(incentives_manager)

@external
def set_owner(new_owner: address):
    """
    @notice Set owner of the contract
    @param new_owner Address of the new owner
    """
    assert msg.sender == self.owner, "auth: only owner"
    assert new_owner != empty(address), "zeroaddr: new_owner"

    self.owner = new_owner

    log SetOwner(new_owner)

@view
@external
def incentives_weight() -> uint256:
    """
    @notice Getter to compute the weight for incentives
    @return The weight for voting incentives
    """
    return MAX_BPS - self.collector_weight