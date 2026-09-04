# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title RoleSourceMock
@author Curve Finance
@license MIT
@notice Settable owner/emergency_owner pair standing in for the FeeCollector
        as an AdapterRegistry role source.
@custom:kill Testing-only contract, never deployed to production.
"""


owner: public(address)
emergency_owner: public(address)


@deploy
def __init__(_owner: address, _emergency_owner: address):
    self.owner = _owner
    self.emergency_owner = _emergency_owner


@external
def set_owner(_owner: address):
    self.owner = _owner


@external
def set_emergency_owner(_emergency_owner: address):
    self.emergency_owner = _emergency_owner
