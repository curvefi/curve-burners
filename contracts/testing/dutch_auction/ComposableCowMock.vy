# pragma version 0.4.3
"""
@title ComposableCoW test double
@author Curve Finance
@license MIT
@notice Records conditional-order registrations and exposes configurable ERC-1271 behavior.
@custom:kill Test-only contract; no production kill path is required.
@custom:security This mock deliberately trusts callers and must never be deployed for production use.
"""


struct ConditionalOrderParams:
    handler: address
    salt: bytes32
    staticData: Bytes[52]


struct GPv2OrderData:
    sellToken: address
    buyToken: address
    receiver: address
    sellAmount: uint256
    buyAmount: uint256
    validTo: uint32
    appData: bytes32
    feeAmount: uint256
    kind: bytes32
    partiallyFillable: bool
    sellTokenBalance: bytes32
    buyTokenBalance: bytes32


struct PayloadStruct:
    proof: DynArray[bytes32, 32]
    params: ConditionalOrderParams
    offchainInput: Bytes[1]


event ConditionalOrderCreated:
    owner: indexed(address)
    handler: indexed(address)
    salt: bytes32
    static_data: Bytes[52]
    dispatch: bool


DOMAIN_SEPARATOR: constant(bytes32) = 0x8f05589c4b810bc2f706854508d66d447cd971f8354a4bb0b3471ceb0a466bc7
ERC1271_MAGIC_VALUE: constant(bytes4) = 0x1626ba7e
VERIFY_SELECTOR: constant(Bytes[4]) = method_id(
    "verify(address,address,bytes32,bytes32,bytes32,bytes,bytes,(address,address,address,uint256,uint256,uint32,bytes32,uint256,bytes32,bool,bytes32,bytes32))"
)

create_count: public(uint256)
last_owner: public(address)
last_handler: public(address)
last_salt: public(bytes32)
last_static_data: public(Bytes[52])
last_dispatch: public(bool)
signature_result: public(bytes4)


@deploy
def __init__():
    self.signature_result = ERC1271_MAGIC_VALUE


@external
def create(params: ConditionalOrderParams, dispatch: bool):
    self.create_count += 1
    self.last_owner = msg.sender
    self.last_handler = params.handler
    self.last_salt = params.salt
    self.last_static_data = params.staticData
    self.last_dispatch = dispatch

    log ConditionalOrderCreated(
        owner=msg.sender,
        handler=params.handler,
        salt=params.salt,
        static_data=params.staticData,
        dispatch=dispatch,
    )


@external
@view
def domainSeparator() -> bytes32:
    return DOMAIN_SEPARATOR


@external
@view
def isValidSafeSignature(
    safe: address,
    sender: address,
    _hash: bytes32,
    _domain_separator: bytes32,
    type_hash: bytes32,
    encode_data: Bytes[480],
    payload: Bytes[2048],
) -> bytes4:
    if self.signature_result != ERC1271_MAGIC_VALUE:
        return self.signature_result

    order: GPv2OrderData = abi_decode(encode_data, GPv2OrderData)
    decoded_payload: PayloadStruct = abi_decode(payload, PayloadStruct)
    call_data: Bytes[1024] = abi_encode(
        safe,
        sender,
        _hash,
        _domain_separator,
        empty(bytes32),
        decoded_payload.params.staticData,
        decoded_payload.offchainInput,
        order,
        method_id=VERIFY_SELECTOR,
    )
    success: bool = raw_call(
        decoded_payload.params.handler,
        call_data,
        max_outsize=0,
        revert_on_failure=False,
        is_static_call=True,
    )
    assert success, "Handler rejected order"
    return self.signature_result


@external
def set_signature_result(result: bytes4):
    self.signature_result = result
