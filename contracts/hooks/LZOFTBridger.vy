# pragma version 0.4.3
"""
@title LZOFTBridger
@license MIT
@author Curve Finance
@notice LayerZero V2 OFT bridge wrapper
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
    def quoteSend(_sendParam: SendParam, _payInLzToken: bool) -> MessagingFee: view
    def send(_sendParam: SendParam, _fee: MessagingFee, _refundAddress: address): payable


OFT_CONTRACT: public(immutable(OFT))
DST_EID: public(immutable(uint32))
TOKEN: public(immutable(address))

QUOTE_AMOUNT: immutable(uint256)
DECIMAL_CONVERSION_RATE: immutable(uint256)
APPROVAL_REQUIRED: immutable(bool)


@deploy
def __init__(_oft: OFT, _dst_eid: uint32, _quote_amount: uint256):
    assert _oft.address != empty(address), "Bad OFT"
    OFT_CONTRACT = _oft
    DST_EID = _dst_eid

    token: address = staticcall _oft.token()
    assert token != empty(address), "Bad token"
    TOKEN = token

    assert _quote_amount > 0, "Bad quote amount"
    QUOTE_AMOUNT = _quote_amount

    decimal_conversion_rate: uint256 = staticcall _oft.decimalConversionRate()
    assert decimal_conversion_rate > 0, "Bad rate"
    DECIMAL_CONVERSION_RATE = decimal_conversion_rate
    APPROVAL_REQUIRED = staticcall _oft.approvalRequired()


@internal
@view
def _remove_dust(_amount: uint256) -> uint256:
    return _amount // DECIMAL_CONVERSION_RATE * DECIMAL_CONVERSION_RATE


@payable
@external
def bridge(_token: address, _to: address, _amount: uint256, _min_amount: uint256=0) -> uint256:
    """
    @notice Bridge a LayerZero V2 OFT or OFTAdapter token
    @param _token OFT underlying token address
    @param _to The receiver on the destination chain
    @param _amount The amount of `_token` to bridge
    @param _min_amount Minimum amount when to bridge
    @return Bridged amount
    """
    assert _token == TOKEN, "Unsupported token"

    token: ERC20 = ERC20(_token)
    amount: uint256 = _amount

    if amount == max_value(uint256):
        amount = staticcall token.balanceOf(msg.sender)

    amount = self._remove_dust(amount)
    if amount < _min_amount:
        return 0

    send_param: SendParam = SendParam(
        dstEid=DST_EID,
        to=convert(_to, bytes32),
        amountLD=amount,
        minAmountLD=_min_amount,
        extraOptions=b"",
        composeMsg=b"",
        oftCmd=b"",
    )
    fee: MessagingFee = staticcall OFT_CONTRACT.quoteSend(send_param, False)
    assert msg.value >= fee.nativeFee, "Bad msg.value"

    assert extcall token.transferFrom(msg.sender, self, amount, default_return_value=True)

    if APPROVAL_REQUIRED and staticcall token.allowance(self, OFT_CONTRACT.address) < amount:
        assert extcall token.approve(OFT_CONTRACT.address, max_value(uint256), default_return_value=True)

    extcall OFT_CONTRACT.send(
        send_param,
        MessagingFee(nativeFee=msg.value, lzTokenFee=0),
        msg.sender,
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

    send_param: SendParam = SendParam(
        dstEid=DST_EID,
        to=empty(bytes32),
        amountLD=amount,
        minAmountLD=amount,
        extraOptions=b"",
        composeMsg=b"",
        oftCmd=b"",
    )
    fee: MessagingFee = staticcall OFT_CONTRACT.quoteSend(send_param, False)
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
