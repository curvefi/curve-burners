# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title ZeroExRegistryMock
@author Curve Finance
@license MIT
@notice Minimal 0x Settler deployer/registry stand-in for
        ZeroExSettlerAdapter deployments in tests: per feature the list of
        its deployments still listed, newest last, so ownerOf is the newest
        and prev the one before it, and per instance its deployInfo. Anyone
        moves it through add (a newer deployment), remove (0x's pause of
        one instance, dropped from its feature's list) and remove_all. The
        views revert the way the real registry does (ERC721NonexistentToken,
        NoInstance, ERC721InvalidOwner) so fail-closed paths can be
        exercised.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""


error ERC721NonexistentToken:
    token_id: uint256


error ERC721InvalidOwner:
    owner: address


error NoInstance:
    pass


MOCK_VERSION: public(constant(String[18])) = "ZeroExRegistryMock"
MAX_INSTANCES: constant(uint256) = 16

listed: public(HashMap[uint256, DynArray[address, MAX_INSTANCES]])
feature_of: HashMap[address, uint128]
nonce_of: HashMap[address, uint32]
last_nonce: HashMap[uint256, uint32]


@external
def add(_feature: uint256, _instance: address):
    """
    @notice Register a newer deployment of a feature: it becomes the
            current instance, the current one the previous.
    """
    self.last_nonce[_feature] += 1
    self.feature_of[_instance] = convert(_feature, uint128)
    self.nonce_of[_instance] = self.last_nonce[_feature]
    self.listed[_feature].append(_instance)


@external
def remove(_instance: address):
    """
    @notice 0x's pause of one instance: it stops being listed; its
            deployInfo stays.
    """
    feature: uint256 = convert(self.feature_of[_instance], uint256)
    kept: DynArray[address, MAX_INSTANCES] = []
    for instance: address in self.listed[feature]:
        if instance != _instance:
            kept.append(instance)
    self.listed[feature] = kept


@external
def remove_all(_feature: uint256):
    self.listed[_feature] = []


@external
@view
def ownerOf(tokenId: uint256) -> address:
    count: uint256 = len(self.listed[tokenId])
    assert count > 0, ERC721NonexistentToken(token_id=tokenId)
    return self.listed[tokenId][count - 1]


@external
@view
def prev(feature: uint128) -> address:
    token_id: uint256 = convert(feature, uint256)
    count: uint256 = len(self.listed[token_id])
    assert count > 0, ERC721NonexistentToken(token_id=token_id)
    assert count > 1, NoInstance()
    return self.listed[token_id][count - 2]


@external
@view
def deployInfo(instance: address) -> (uint128, uint32):
    feature: uint128 = self.feature_of[instance]
    assert feature != 0, ERC721InvalidOwner(owner=instance)
    return feature, self.nonce_of[instance]
