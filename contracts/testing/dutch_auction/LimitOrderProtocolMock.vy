# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title LimitOrderProtocolMock
@author Curve Finance
@license MIT
@notice Minimal 1inch LOP v4 stand-in for OneInchAdapter deployments in
        tests: the ERC-5267 domain description and hashOrder over a
        configurable domain. Settlement itself is exercised against the real
        protocol in the fork test.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

from contracts.burners.adapters.oneinch import oneinch

MAX_EXTENSIONS: constant(uint256) = 8

fields: immutable(bytes1)
name: immutable(String[oneinch.MAX_DOMAIN_NAME])
version: immutable(String[oneinch.MAX_DOMAIN_VERSION])
# When set, hashOrder hashes under this separator instead of the one the
# domain description implies, so the adapter's deploy-time cross-check
# can be made to fail.
domain_override: immutable(bytes32)


@deploy
def __init__(
    _name: String[oneinch.MAX_DOMAIN_NAME],
    _version: String[oneinch.MAX_DOMAIN_VERSION],
    _fields: bytes1,
    _domain_override: bytes32,
):
    self.name = _name
    self.version = _version
    self.fields = _fields
    self.domain_override = _domain_override


@external
@view
def eip712Domain() -> (
    bytes1,
    String[oneinch.MAX_DOMAIN_NAME],
    String[oneinch.MAX_DOMAIN_VERSION],
    uint256,
    address,
    bytes32,
    DynArray[uint256, MAX_EXTENSIONS],
):
    return self.fields, self.name, self.version, chain.id, self, empty(bytes32), []


@external
@view
def DOMAIN_SEPARATOR() -> bytes32:
    if self.domain_override != empty(bytes32):
        return self.domain_override
    return oneinch._domain_separator(self.name, self.version, chain.id, self)


@external
@view
def hashOrder(_order: oneinch.OrderWords) -> bytes32:
    # The protocol's ABI: address fields are uint256 words, hashed raw.
    domain: bytes32 = self.domain_override
    if domain == empty(bytes32):
        domain = oneinch._domain_separator(self.name, self.version, chain.id, self)
    return oneinch._words_digest(_order, domain)
