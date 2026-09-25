# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title AdapterMock
@author Curve Finance
@license MIT
@notice Configurable adapter test double for the ERC-1271 signature router:
        answers with a settable magic value, can revert on demand, and can
        attempt a state write to prove the router's staticcall neutralizes it.
@dev isValidSignature is deliberately nonpayable (and the contract skips the
     nonreentrancy pragma): called through the router's staticcall it behaves
     like a view unless write_state_on_validate is set, in which case the
     attempted SSTORE makes the staticcall revert.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately unsafe and caller-trusting by design.
"""

MAX_SIGNATURE_LEN: constant(uint256) = 4096

ERC1271_MAGIC_VALUE: constant(bytes4) = 0x1626ba7e


interface DutchAuction:
    def check_order(
        _sell_token: address,
        _buy_token: address,
        _receiver: address,
        _sell_amount: uint256,
        _min_buy_amount: uint256,
        _valid_to: uint256,
    ) -> bool: view


response: public(bytes4)
should_revert: public(bool)
write_state_on_validate: public(bool)
write_count: public(uint256)


@deploy
def __init__():
    self.response = ERC1271_MAGIC_VALUE


@external
def set_response(_response: bytes4):
    self.response = _response


@external
def set_revert(_should_revert: bool):
    self.should_revert = _should_revert


@external
def set_write_state(_write: bool):
    self.write_state_on_validate = _write


@external
def isValidSignature(_hash: bytes32, _signature: Bytes[MAX_SIGNATURE_LEN]) -> bytes4:
    assert not self.should_revert, "Adapter revert"
    if self.write_state_on_validate:
        self.write_count += 1
    return self.response


@external
@view
def check_order_via_auction(
    _auction: address,
    _sell_token: address,
    _buy_token: address,
    _receiver: address,
    _sell_amount: uint256,
    _min_buy_amount: uint256,
    _valid_to: uint256,
) -> bool:
    """@notice Exercise the auction's shared economic check like an adapter would."""
    return staticcall DutchAuction(_auction).check_order(
        _sell_token, _buy_token, _receiver, _sell_amount, _min_buy_amount, _valid_to
    )
