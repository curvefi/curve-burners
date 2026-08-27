# pragma version 0.5.0a4
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title SettlementMock
@author Curve Finance
@license MIT
@notice Minimal GPv2Settlement stand-in: fixed EIP-712 domain separator and
        vault relayer for CowAdapter deployment in tests.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

domain_separator: immutable(bytes32)
vault_relayer: immutable(address)


@deploy
def __init__(_domain_separator: bytes32, _vault_relayer: address):
    self.domain_separator = _domain_separator
    self.vault_relayer = _vault_relayer


@external
@view
def domainSeparator() -> bytes32:
    return self.domain_separator


@external
@view
def vaultRelayer() -> address:
    return self.vault_relayer
