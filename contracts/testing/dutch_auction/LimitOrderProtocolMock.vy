# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title LimitOrderProtocolMock
@author Curve Finance
@license MIT
@notice Minimal 1inch LOP v4 stand-in for the 1inch adapter deployments and
        settlement tests: the ERC-5267 domain description and hashOrder over
        a configurable domain, and a fillContractOrderArgs that follows
        OrderMixin._fill for what the adapters rely on — the contract
        signature checked on the first fill only, the extension bound by
        flag and salt, amounts through the extension's getters (linear
        without them), the transfers, and the post-interaction call. Not
        modelled: taker thresholds, permits, pre-interactions, WETH paths,
        taker interactions. Settlement against the real protocol is the fork
        tests' job.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

from ethereum.ercs import IERC20

from contracts.burners.adapters.oneinch import oneinch
from contracts.utils import constants as c


interface AmountGetter:
    def getMakingAmount(
        _order: oneinch.OrderWords,
        _extension: Bytes[oneinch.MAX_EXTENSION],
        _order_hash: bytes32,
        _taker: address,
        _taking_amount: uint256,
        _remaining_making_amount: uint256,
        _extra_data: Bytes[oneinch.MAX_EXTENSION],
    ) -> uint256: view
    def getTakingAmount(
        _order: oneinch.OrderWords,
        _extension: Bytes[oneinch.MAX_EXTENSION],
        _order_hash: bytes32,
        _taker: address,
        _making_amount: uint256,
        _remaining_making_amount: uint256,
        _extra_data: Bytes[oneinch.MAX_EXTENSION],
    ) -> uint256: view


interface PostInteraction:
    def postInteraction(
        _order: oneinch.OrderWords,
        _extension: Bytes[oneinch.MAX_EXTENSION],
        _order_hash: bytes32,
        _taker: address,
        _making_amount: uint256,
        _taking_amount: uint256,
        _remaining_making_amount: uint256,
        _extra_data: Bytes[oneinch.MAX_EXTENSION],
    ): nonpayable


error BadSignature:
    pass


error InvalidatedOrder:
    pass


error InvalidExtension:
    pass


MAX_EXTENSIONS: constant(uint256) = 8
SIGNATURE_BOUND: constant(uint256) = 20 + oneinch.ENCODED_ORDER_LEN + 2 * 32 + oneinch.MAX_EXTENSION
# TakerTraitsLib: the fill amount is a making amount.
TAKER_MAKING_AMOUNT_FLAG: constant(uint256) = 1 << 255

# The domain description, kept as bytes: boa cannot render a revert trace
# through a mock whose frames hold string variables (reused memory fails
# UTF-8 decoding), and fills revert on purpose in the adapter tests.
fields: immutable(bytes1)
name: immutable(Bytes[oneinch.MAX_DOMAIN_NAME])
version: immutable(Bytes[oneinch.MAX_DOMAIN_VERSION])
# The separator hashOrder uses: derived from the domain description, or an
# override that makes the adapter's deploy-time cross-check fail.
domain: immutable(bytes32)

# RemainingInvalidatorLib: 0 for an untouched order, else remaining + 1.
remaining_raw: HashMap[bytes32, uint256]


@deploy
def __init__(
    _name: Bytes[oneinch.MAX_DOMAIN_NAME],
    _version: Bytes[oneinch.MAX_DOMAIN_VERSION],
    _fields: bytes1,
    _domain_override: bytes32,
):
    self.name = _name
    self.version = _version
    self.fields = _fields
    separator: bytes32 = _domain_override
    if separator == empty(bytes32):
        # OpenZeppelin EIP712._buildDomainSeparator over the raw bytes.
        separator = keccak256(
            abi_encode(
                oneinch.EIP712_DOMAIN_TYPE_HASH,
                keccak256(_name),
                keccak256(_version),
                chain.id,
                self,
            )
        )
    self.domain = separator


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
    return (
        self.fields,
        convert(self.name, String[oneinch.MAX_DOMAIN_NAME]),
        convert(self.version, String[oneinch.MAX_DOMAIN_VERSION]),
        chain.id,
        self,
        empty(bytes32),
        [],
    )


@external
@view
def DOMAIN_SEPARATOR() -> bytes32:
    return self.domain


@external
@view
def hashOrder(_order: oneinch.OrderWords) -> bytes32:
    # The protocol's ABI: address fields are uint256 words, hashed raw.
    return oneinch._words_digest(_order, self.domain)


@external
@view
def remainingInvalidatorForOrder(_maker: address, _order_hash: bytes32) -> uint256:
    return self.remaining_raw[_order_hash] - 1  # dev: untouched order


@internal
@view
def _getter_data(
    _extension: Bytes[oneinch.MAX_EXTENSION], _field: uint256
) -> (address, Bytes[oneinch.MAX_EXTENSION]):
    data: Bytes[oneinch.MAX_EXTENSION] = oneinch._field(
        _extension, oneinch._extension_offsets(_extension), _field
    )
    if len(data) == 0:
        return empty(address), b""
    getter: address = convert(slice(data, 0, 20), address)
    if len(data) == 20:
        return getter, b""
    return getter, slice(data, 20, len(data) - 20)


@external
def fillContractOrderArgs(
    _order: oneinch.OrderWords,
    _signature: Bytes[SIGNATURE_BOUND],
    _amount: uint256,
    _taker_traits: uint256,
    _extension: Bytes[oneinch.MAX_EXTENSION],
) -> (uint256, uint256, bytes32):
    """
    @notice OrderMixin._fillContractOrder/_fill for the modelled paths; the
            extension is passed directly instead of packed into args.
    """
    order_hash: bytes32 = oneinch._words_digest(_order, self.domain)
    maker: address = convert(_order.maker, address)
    raw: uint256 = self.remaining_raw[order_hash]
    remaining: uint256 = _order.makingAmount
    if raw != 0:
        remaining = raw - 1
    assert remaining > 0, InvalidatedOrder()
    if remaining == _order.makingAmount:
        # OrderLib.isValidExtension, then the contract signature: both on
        # the first fill only.
        if _order.makerTraits & oneinch.HAS_EXTENSION_FLAG != 0:
            assert len(_extension) > 0 and (
                convert(keccak256(_extension), uint256) & oneinch.EXTENSION_HASH_MASK
                == _order.salt & oneinch.EXTENSION_HASH_MASK
            ), InvalidExtension()
        else:
            assert len(_extension) == 0, InvalidExtension()
        success: bool = False
        response: Bytes[32] = b""
        success, response = raw_call(
            maker,
            abi_encode(
                order_hash, _signature, method_id=method_id("isValidSignature(bytes32,bytes)")
            ),
            max_outsize=32,
            is_static_call=True,
            revert_on_failure=False,
        )
        assert (
            success
            and len(response) == 32
            and (convert(slice(response, 0, 4), bytes4) == c.ERC1271_MAGIC_VALUE)
        ), BadSignature()

    getter: address = empty(address)
    data: Bytes[oneinch.MAX_EXTENSION] = b""
    making_amount: uint256 = 0
    taking_amount: uint256 = 0
    if _taker_traits & TAKER_MAKING_AMOUNT_FLAG != 0:
        making_amount = min(_amount, remaining)
        getter, data = self._getter_data(_extension, oneinch.TAKING_AMOUNT_DATA)
        if getter == empty(address):
            taking_amount = (
                _order.takingAmount * making_amount + _order.makingAmount - 1
            ) // _order.makingAmount
        else:
            taking_amount = staticcall AmountGetter(getter).getTakingAmount(
                _order, _extension, order_hash, msg.sender, making_amount, remaining, data
            )
    else:
        taking_amount = _amount
        getter, data = self._getter_data(_extension, oneinch.MAKING_AMOUNT_DATA)
        if getter == empty(address):
            making_amount = _order.makingAmount * taking_amount // _order.takingAmount
        else:
            making_amount = staticcall AmountGetter(getter).getMakingAmount(
                _order, _extension, order_hash, msg.sender, taking_amount, remaining, data
            )
        if making_amount > remaining:
            making_amount = remaining
            getter, data = self._getter_data(_extension, oneinch.TAKING_AMOUNT_DATA)
            if getter == empty(address):
                taking_amount = (
                    _order.takingAmount * making_amount + _order.makingAmount - 1
                ) // _order.makingAmount
            else:
                taking_amount = staticcall AmountGetter(getter).getTakingAmount(
                    _order, _extension, order_hash, msg.sender, making_amount, remaining, data
                )
    self.remaining_raw[order_hash] = remaining - making_amount + 1

    assert extcall IERC20(convert(_order.makerAsset, address)).transferFrom(
        maker, msg.sender, making_amount, default_return_value=True
    )
    receiver: address = convert(_order.receiver, address)
    if receiver == empty(address):
        receiver = maker
    assert extcall IERC20(convert(_order.takerAsset, address)).transferFrom(
        msg.sender, receiver, taking_amount, default_return_value=True
    )

    if _order.makerTraits & oneinch.POST_INTERACTION_CALL_FLAG != 0:
        listener: address = maker
        listener, data = self._getter_data(_extension, oneinch.POST_INTERACTION_DATA)
        if listener == empty(address):
            listener = maker
        extcall PostInteraction(listener).postInteraction(
            _order,
            _extension,
            order_hash,
            msg.sender,
            making_amount,
            taking_amount,
            remaining,
            data,
        )
    return making_amount, taking_amount, order_hash
