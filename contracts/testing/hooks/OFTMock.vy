# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title OFTMock
@author Curve Finance
@license MIT
@notice LayerZero V2 OFT/OFTAdapter stand-in with the endpoint's fee
        behaviour folded in: a fixed native quote per send, the exact-value
        check of OFTCore, and the endpoint's refund of the excess to
        `_refundAddress` that reverts when the receiver rejects ETH.
@dev Settable peers, enforced options, fee quote, and an optional OFT fee
     (bps) taken from the delivered amount to exercise `minAmountLD`.
     Records the last SendParam for assertions.
@custom:kill Testing-only contract, never deployed to production.
@custom:security Deliberately trusting test double.
"""

from ethereum.ercs import IERC20


struct SendParam:
    dstEid: uint32
    to: bytes32
    amountLD: uint256
    minAmountLD: uint256
    extraOptions: Bytes[1024]
    composeMsg: Bytes[1024]
    oftCmd: Bytes[1024]


struct MessagingFee:
    nativeFee: uint256
    lzTokenFee: uint256


# Pinned at deployment: the bridged token and whether sends pull it
# (OFTAdapter) or burn it from the sender's balance (plain OFT, here also a
# pull for simplicity).
token: public(immutable(address))
approvalRequired: public(immutable(bool))
decimalConversionRate: public(immutable(uint256))

native_fee: public(uint256)
fee_bps: public(uint256)
peers: public(HashMap[uint32, bytes32])
enforced: HashMap[uint32, HashMap[uint16, Bytes[256]]]

last_send: public(SendParam)
last_fee: public(MessagingFee)
last_refund: public(address)
sends: public(uint256)


@deploy
def __init__(_token: address, _approval_required: bool, _rate: uint256, _native_fee: uint256):
    self.token = _token
    self.approvalRequired = _approval_required
    self.decimalConversionRate = _rate
    self.native_fee = _native_fee


# Configuration


@external
def set_peer(_eid: uint32, _peer: bytes32):
    self.peers[_eid] = _peer


@external
def set_enforced_options(_eid: uint32, _msg_type: uint16, _options: Bytes[256]):
    self.enforced[_eid][_msg_type] = _options


@external
def set_native_fee(_fee: uint256):
    self.native_fee = _fee


@external
def set_fee_bps(_bps: uint256):
    self.fee_bps = _bps


@external
@view
def enforcedOptions(_eid: uint32, _msg_type: uint16) -> Bytes[256]:
    return self.enforced[_eid][_msg_type]


# OFT surface


@external
@view
def quoteSend(_send_param: SendParam, _pay_in_lz_token: bool) -> MessagingFee:
    assert self.peers[_send_param.dstEid] != empty(bytes32), "NoPeer"
    return MessagingFee(nativeFee=self.native_fee, lzTokenFee=0)


@external
@payable
def send(_send_param: SendParam, _fee: MessagingFee, _refund_address: address):
    assert self.peers[_send_param.dstEid] != empty(bytes32), "NoPeer"
    # OFTCore._payNative: the supplied value must equal the declared fee.
    assert msg.value == _fee.nativeFee, "NotEnoughNative"
    received: uint256 = _send_param.amountLD - _send_param.amountLD * self.fee_bps // 10_000
    assert received >= _send_param.minAmountLD, "SlippageExceeded"
    assert extcall IERC20(self.token).transferFrom(
        msg.sender, self, _send_param.amountLD, default_return_value=True
    )

    # EndpointV2._payNative: refund the excess, reverting when it cannot land.
    required: uint256 = self.native_fee
    assert msg.value >= required, "InsufficientFee"
    if msg.value > required:
        raw_call(_refund_address, b"", value=msg.value - required)

    self.last_send = _send_param
    self.last_fee = _fee
    self.last_refund = _refund_address
    self.sends += 1
