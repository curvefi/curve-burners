# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title SettlerRelayMock
@author Curve Finance
@license MIT
@notice Executing 0x SettlerMetaTxn stand-in that plays Permit2 too, for the
        keeper's self-relay tests: executeMetaTxn rebuilds the MetaTxn
        Permit2 witness digest from the permit of the first action, the
        slippage and the action-list hash, asks the taker's isValidSignature
        for it, pulls the permitted amount from the taker (which approved
        this contract as its executor), burns the nonce and pays the
        recipient its whole buy-token balance, at least minAmountOut (the
        test funds it). The actions are hashed, never run: swapping through
        the real Settler and Permit2 is the fork test's job.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

from ethereum.ercs import IERC20

from contracts.burners.adapters.zerox import settler as zerox


interface ERC1271:
    def isValidSignature(_hash: bytes32, _signature: Bytes[1024]) -> bytes4: view


ERC1271_MAGIC_VALUE: constant(bytes4) = 0x1626BA7E
# ISettlerActions.METATXN_TRANSFER_FROM(address,((address,uint256),uint256,uint256))
METATXN_TRANSFER_FROM: constant(bytes4) = 0x0DFEB419
# The action's arguments: the recipient word and the four permit words.
TRANSFER_FROM_ARGS_LEN: constant(uint256) = 5 * 32

DOMAIN_SEPARATOR: public(immutable(bytes32))
# keccak256(abi_encode(taker, nonce)) of every nonce a settlement burnt.
used_nonces: public(HashMap[bytes32, bool])
relays: public(uint256)


@deploy
def __init__(_domain_separator: bytes32):
    self.DOMAIN_SEPARATOR = _domain_separator


@external
def executeMetaTxn(
    _slippage: zerox.Slippage,
    _actions: DynArray[Bytes[zerox.MAX_ACTION_LEN], zerox.MAX_ACTIONS],
    _zid: bytes32,
    _msg_sender: address,
    _sig: Bytes[1024],
) -> bool:
    """
    @notice Settle a MetaTxn order the way the real Settler + Permit2 pair
            does, minus the swap: the taker's signature check, the pull, the
            nonce burn and the slippage payout.
    """
    assert msg.sender != _msg_sender, "signer relays itself"
    assert len(_actions) > 0, "no actions"
    first: Bytes[zerox.MAX_ACTION_LEN] = _actions[0]
    assert convert(slice(first, 0, 4), bytes4) == METATXN_TRANSFER_FROM, "bad first action"
    recipient: address = empty(address)
    permit: zerox.Permit = empty(zerox.Permit)
    recipient, permit = abi_decode(slice(first, 4, TRANSFER_FROM_ARGS_LEN), (address, zerox.Permit))
    assert recipient == self, "bad recipient"

    order: zerox.Order = zerox.Order(
        permit=permit, slippage=_slippage, actions_hash=zerox._hash_actions(_actions)
    )
    digest: bytes32 = zerox._permit_digest(order, self, self.DOMAIN_SEPARATOR, False)
    assert (
        staticcall ERC1271(_msg_sender).isValidSignature(digest, _sig) == ERC1271_MAGIC_VALUE
    ), "invalid signature"
    assert block.timestamp <= permit.deadline, "permit expired"
    nonce_key: bytes32 = keccak256(abi_encode(_msg_sender, permit.nonce))
    assert not self.used_nonces[nonce_key], "nonce used"
    self.used_nonces[nonce_key] = True

    assert extcall IERC20(permit.token).transferFrom(
        _msg_sender, self, permit.amount, default_return_value=True
    )
    buy_token: IERC20 = IERC20(_slippage.buyToken)
    payout: uint256 = staticcall buy_token.balanceOf(self)
    assert payout >= _slippage.minAmountOut, "slippage"
    assert extcall buy_token.transfer(_slippage.recipient, payout, default_return_value=True)
    self.relays += 1
    return True
