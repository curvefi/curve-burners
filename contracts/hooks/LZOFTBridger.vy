# pragma version 0.4.3
"""
@title LZOFTBridger
@license MIT
@author Curve Finance
@notice LayerZero V2 OFT bridge wrapper
@dev Stateless: pulls the token from the caller, sends it through the OFT to
     DST_EID, and lets the endpoint refund the unused native fee to `_refund`.
     The caller (Hooker) usually cannot receive ETH, so keepers pass their own
     refund address as the last `bridge` argument; a below-threshold call
     returns the whole msg.value to it as well.
@custom:kill Nothing to pause: holds no funds between calls and has no owner.
             Retire it by removing the hook from the Hooker.
@custom:security The OFT owner controls fees, peers, and enforced options:
                 `minAmountLD` is pinned to the sent amount so a fee-charging
                 upgrade reverts the bridge instead of shaving the transfer.
"""


interface ERC20:
    def allowance(_owner: address, _spender: address) -> uint256: view
    def balanceOf(_owner: address) -> uint256: view
    def approve(_spender: address, _amount: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _amount: uint256) -> bool: nonpayable


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


interface OFT:
    def token() -> address: view
    def decimalConversionRate() -> uint256: view
    def approvalRequired() -> bool: view
    def peers(_eid: uint32) -> bytes32: view
    def quoteSend(_sendParam: SendParam, _payInLzToken: bool) -> MessagingFee: view
    def send(_sendParam: SendParam, _fee: MessagingFee, _refundAddress: address): payable


# OAppOptionsType3 message type of a plain OFT send.
SEND_MSG_TYPE: constant(uint16) = 1
MAX_OPTIONS: constant(uint256) = 256

OFT_CONTRACT: public(immutable(OFT))
DST_EID: public(immutable(uint32))
TOKEN: public(immutable(address))
# Executor options appended to every send; empty relies on the OFT's enforced
# options for DST_EID (checked at deployment).
EXTRA_OPTIONS: public(immutable(Bytes[MAX_OPTIONS]))

QUOTE_AMOUNT: immutable(uint256)
DECIMAL_CONVERSION_RATE: immutable(uint256)
APPROVAL_REQUIRED: immutable(bool)


@deploy
def __init__(_oft: OFT, _dst_eid: uint32, _quote_amount: uint256, _extra_options: Bytes[MAX_OPTIONS]):
    """
    @param _oft OFT or OFTAdapter of the bridged token on this chain
    @param _dst_eid LayerZero endpoint id of the destination chain; must be a peer of `_oft`
    @param _quote_amount Amount `cost()` quotes by default
    @param _extra_options Executor options for every send (type-3 options bytes, b"" for none);
           empty requires the OFT to have enforced options for `_dst_eid`
    """
    assert _oft.address != empty(address), "Bad OFT"
    OFT_CONTRACT = _oft
    DST_EID = _dst_eid
    assert staticcall _oft.peers(_dst_eid) != empty(bytes32), "No peer"

    token: address = staticcall _oft.token()
    assert token != empty(address), "Bad token"
    TOKEN = token

    assert _quote_amount > 0, "Bad quote amount"
    QUOTE_AMOUNT = _quote_amount

    decimal_conversion_rate: uint256 = staticcall _oft.decimalConversionRate()
    assert decimal_conversion_rate > 0, "Bad rate"
    DECIMAL_CONVERSION_RATE = decimal_conversion_rate
    APPROVAL_REQUIRED = staticcall _oft.approvalRequired()

    EXTRA_OPTIONS = _extra_options
    if len(_extra_options) == 0:
        # Without own options the executor gas comes from the OFT's enforced
        # options; an OFT without them would deliver an unexecutable message.
        success: bool = False
        response: Bytes[MAX_OPTIONS + 64] = b""
        success, response = raw_call(
            _oft.address,
            abi_encode(_dst_eid, SEND_MSG_TYPE, method_id=method_id("enforcedOptions(uint32,uint16)")),
            max_outsize=MAX_OPTIONS + 64,
            is_static_call=True,
            revert_on_failure=False,
        )
        assert success, "No enforcedOptions"
        assert len(abi_decode(response, Bytes[MAX_OPTIONS])) > 0, "No options"


@internal
@view
def _remove_dust(_amount: uint256) -> uint256:
    return _amount // DECIMAL_CONVERSION_RATE * DECIMAL_CONVERSION_RATE


@internal
@view
def _send_param(_to: bytes32, _amount: uint256) -> SendParam:
    # The whole dust-free amount must arrive: no OFT fee is tolerated.
    return SendParam(
        dstEid=DST_EID,
        to=_to,
        amountLD=_amount,
        minAmountLD=_amount,
        extraOptions=EXTRA_OPTIONS,
        composeMsg=b"",
        oftCmd=b"",
    )


@payable
@external
def bridge(
    _token: address,
    _to: address,
    _amount: uint256,
    _min_amount: uint256 = 0,
    _refund: address = msg.sender,
) -> uint256:
    """
    @notice Bridge a LayerZero V2 OFT or OFTAdapter token
    @param _token OFT underlying token address
    @param _to The receiver on the destination chain
    @param _amount The amount of `_token` to bridge
    @param _min_amount Minimum amount when to bridge
    @param _refund Receiver of the unused native fee (the whole msg.value when
           nothing is bridged); must accept ETH
    @return Bridged amount
    """
    assert _token == TOKEN, "Unsupported token"
    assert _refund != empty(address), "Bad refund"

    token: ERC20 = ERC20(_token)
    amount: uint256 = _amount

    if amount == max_value(uint256):
        amount = staticcall token.balanceOf(msg.sender)

    amount = self._remove_dust(amount)
    if amount == 0 or amount < _min_amount:
        if msg.value > 0:
            raw_call(_refund, b"", value=msg.value)
        return 0

    send_param: SendParam = self._send_param(convert(_to, bytes32), amount)
    fee: MessagingFee = staticcall OFT_CONTRACT.quoteSend(send_param, False)
    assert msg.value >= fee.nativeFee, "Bad msg.value"

    assert extcall token.transferFrom(msg.sender, self, amount, default_return_value=True)

    if APPROVAL_REQUIRED and staticcall token.allowance(self, OFT_CONTRACT.address) < amount:
        assert extcall token.approve(OFT_CONTRACT.address, max_value(uint256), default_return_value=True)

    # The endpoint charges the live fee and refunds the rest to `_refund`.
    extcall OFT_CONTRACT.send(
        send_param,
        MessagingFee(nativeFee=msg.value, lzTokenFee=0),
        _refund,
        value=msg.value,
    )
    return amount


@internal
@view
def _quote(amount: uint256) -> uint256:
    if amount == 0:
        amount = QUOTE_AMOUNT

    amount = self._remove_dust(amount)
    assert amount > 0, "Dust amount"

    fee: MessagingFee = staticcall OFT_CONTRACT.quoteSend(self._send_param(empty(bytes32), amount), False)
    return fee.nativeFee


@view
@external
def cost(_amount: uint256 = 0) -> uint256:
    """
    @notice Cost in ETH to bridge
    @param _amount Amount to quote. Uses deployment quote amount when 0
    @return Amount of ETH to include
    """
    return self._quote(_amount)


@pure
@external
def check(_account: address) -> bool:
    """
    @notice Check if `_account` may bridge via `transmit_emissions`
    @param _account The account to check
    @return True if `_account` may bridge
    """
    return True
