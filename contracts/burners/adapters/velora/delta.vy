# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title Velora Delta order library
@author Curve Finance
@license MIT
@notice Stateless Velora (ex-ParaSwap) Delta order type, its two EIP-712
        primary types (fill-or-kill Order and partially fillable
        FillableOrder), the Portikus domain, the partnerAndFee packing and
        the fee netting the settlement applies, plus the OrderNotValid
        revert shared by Delta-facing adapters.
@dev Pure parametric helpers only: no storage, no external calls, no abstract
     hooks. Hashing must stay byte-identical to OrderHashLib.hash and
     FillableOrderHashLib.hash of the Portikus V2 settlement modules
     (verified source of the Delta contract
     0x0000000000bbf5c5fd284e657f01bd000933c96d, modules
     SwapSettlementModule and FillableSwapSettlementModule v2.3.0), the fee
     arithmetic to FeeManagerLib.processSellFees.
"""


# Protocol-level order rejection (selector OrderNotValid(string), the same
# shape the CoW-, UniswapX- and JAM-facing adapters use) so every adapter
# reports shape and constant failures the same way.
error OrderNotValid:
    reason: String[32]


# Delta Bridge: the cross-chain leg of an order; every field zero for a
# same-chain order (BridgeLib.isBridge is false for destinationChainId 0).
struct Bridge:
    protocolSelector: bytes4
    destinationChainId: uint256
    outputToken: address
    scalingFactor: int8
    protocolData: Bytes[MAX_PROTOCOL_DATA]


# Delta Order (types/Order.sol): the fourteen signed fields in type order.
# The same struct is hashed under either primary type: Order for the
# fill-or-kill swapSettle path, FillableOrder for swapSettleFillable (the
# API's partiallyFillable flag picks the type when the order is built).
struct Order:
    owner: address
    beneficiary: address
    srcToken: address
    destToken: address
    srcAmount: uint256
    destAmount: uint256
    expectedAmount: uint256
    deadline: uint256
    kind: uint8
    nonce: uint256
    partnerAndFee: uint256
    permit: Bytes[MAX_PERMIT]
    metadata: Bytes[MAX_METADATA]
    bridge: Bridge


# Decoding bounds of the dynamic fields. The protocol's permit payloads are
# at most 256 bytes (ERC20UtilsLib.permit: 0, 1, 96, 192, 224 or 256);
# metadata and protocolData are opaque bytes the settlement only hashes.
# Longer fields fail ABI decoding before any check runs.
MAX_PERMIT: constant(uint256) = 256
MAX_METADATA: constant(uint256) = 256
MAX_PROTOCOL_DATA: constant(uint256) = 256
# abi_encode(Order) at the bounds: the tuple offset word, fourteen head
# words, the permit and metadata bodies (length word plus padded bytes) and
# the bridge body (five head words plus the protocolData body).
ENCODED_ORDER_BOUND: constant(uint256) = (
    32 + 14 * 32 + (32 + MAX_PERMIT) + (32 + MAX_METADATA) + (5 * 32 + 32 + MAX_PROTOCOL_DATA)
)

# OrderKind.SELL: the only kind swapSettle and swapSettleFillable accept.
KIND_SELL: constant(uint8) = 0

# EIP-712 type hashes (OrderHashLib, FillableOrderHashLib). The Bridge
# sub-type string follows the primary type as EIP-712 requires; the hashes
# are those spelled out in the verified sources.
ORDER_TYPE_HASH: constant(bytes32) = keccak256(
    "Order(address owner,address beneficiary,address srcToken,address destToken,"
    "uint256 srcAmount,uint256 destAmount,uint256 expectedAmount,uint256 deadline,"
    "uint8 kind,uint256 nonce,uint256 partnerAndFee,bytes permit,bytes metadata,Bridge bridge)"
    "Bridge(bytes4 protocolSelector,uint256 destinationChainId,address outputToken,"
    "int8 scalingFactor,bytes protocolData)"
)  # 0xc75d848e51cd0f81113e24c5a62c9b8566b0ff0d476245a7882709315eefbbf7
FILLABLE_ORDER_TYPE_HASH: constant(bytes32) = keccak256(
    "FillableOrder(address owner,address beneficiary,address srcToken,address destToken,"
    "uint256 srcAmount,uint256 destAmount,uint256 expectedAmount,uint256 deadline,"
    "uint8 kind,uint256 nonce,uint256 partnerAndFee,bytes permit,bytes metadata,Bridge bridge)"
    "Bridge(bytes4 protocolSelector,uint256 destinationChainId,address outputToken,"
    "int8 scalingFactor,bytes protocolData)"
)  # 0xe9c0719dc3d5cb1a78318dfcf50c0b5d86013bc715805be49f4154bb98b994c8
BRIDGE_TYPE_HASH: constant(bytes32) = keccak256(
    "Bridge(bytes4 protocolSelector,uint256 destinationChainId,address outputToken,"
    "int8 scalingFactor,bytes protocolData)"
)  # 0x8d8bcbf96f8246b1dcca3f4e1750aecf6cd6971d370b908f10af1d459a2df6eb
# EIP712 (modules/util/EIP712.sol): name "Portikus", version "2.0.0", this
# chain, and the Delta contract (the adapter the modules run in) as the
# verifying contract.
EIP712_DOMAIN_TYPE_HASH: constant(bytes32) = keccak256(
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
DOMAIN_NAME: constant(String[8]) = "Portikus"
DOMAIN_VERSION: constant(String[5]) = "2.0.0"

# FeeManagerLib: partnerAndFee packs [partner address (160 bits from bit 96)
# | capSurplus (bit 9) | partnerTakesSurplus (bit 8) | partner fee in bps
# (8 bits, clamped to 2%)]. The partner fee is taken off the amount in hand
# whenever a partner is set; the protocol's share and a surplus-taking
# partner's share come only out of the surplus above expectedAmount.
FEE_BPS_MASK: constant(uint256) = 2**8 - 1
PARTNER_TAKES_SURPLUS_MASK: constant(uint256) = 2**8
CAP_SURPLUS_MASK: constant(uint256) = 2**9
PARTNER_OFFSET: constant(uint256) = 96
MAX_FEE_BPS: constant(uint256) = 200
HUNDRED_PERCENT: constant(uint256) = 10000


# Hashing


@internal
@pure
def _domain_separator(_chain_id: uint256, _verifying_contract: address) -> bytes32:
    """
    @notice EIP712._domainSeparatorV4 for the fixed name and version.
    """
    return keccak256(
        abi_encode(
            EIP712_DOMAIN_TYPE_HASH,
            keccak256(DOMAIN_NAME),
            keccak256(DOMAIN_VERSION),
            _chain_id,
            _verifying_contract,
        )
    )


@internal
@pure
def _bridge_hash(_bridge: Bridge) -> bytes32:
    # OrderHashLib: the bytes4 selector left-aligned in its word, the int8
    # sign-extended, the dynamic bytes hashed.
    return keccak256(
        abi_encode(
            BRIDGE_TYPE_HASH,
            _bridge.protocolSelector,
            _bridge.destinationChainId,
            _bridge.outputToken,
            _bridge.scalingFactor,
            keccak256(_bridge.protocolData),
        )
    )


@internal
@pure
def _struct_hash(_order: Order, _type_hash: bytes32) -> bytes32:
    """
    @notice OrderHashLib.hash / FillableOrderHashLib.hash: the same field
            layout under the primary type hash given.
    """
    return keccak256(
        abi_encode(
            _type_hash,
            _order.owner,
            _order.beneficiary,
            _order.srcToken,
            _order.destToken,
            _order.srcAmount,
            _order.destAmount,
            _order.expectedAmount,
            _order.deadline,
            _order.kind,
            _order.nonce,
            _order.partnerAndFee,
            keccak256(_order.permit),
            keccak256(_order.metadata),
            self._bridge_hash(_order.bridge),
        )
    )


@internal
@pure
def _order_digest(_order: Order, _domain_separator: bytes32, _fillable: bool) -> bytes32:
    """
    @notice The digest a settlement module asks the owner to sign
            (_hashTypedDataV4 over the order's struct hash).
    @param _order The order being settled.
    @param _domain_separator The Delta contract's EIP-712 domain separator.
    @param _fillable True for the FillableOrder primary type
           (swapSettleFillable), false for Order (swapSettle).
    """
    type_hash: bytes32 = ORDER_TYPE_HASH
    if _fillable:
        type_hash = FILLABLE_ORDER_TYPE_HASH
    return keccak256(concat(b"\x19\x01", _domain_separator, self._struct_hash(_order, type_hash)))


# Fees


@internal
@pure
def _parse_partner_and_fee(_partner_and_fee: uint256) -> (address, uint256, bool, bool):
    """
    @notice FeeManagerLib.parsePartnerAndFee.
    @return Partner address, partner fee in bps (clamped to MAX_FEE_BPS),
            partnerTakesSurplus, capSurplus.
    """
    fee_bps: uint256 = min(_partner_and_fee & FEE_BPS_MASK, MAX_FEE_BPS)
    return (
        convert(convert(_partner_and_fee >> PARTNER_OFFSET, uint160), address),
        fee_bps,
        _partner_and_fee & PARTNER_TAKES_SURPLUS_MASK != 0,
        _partner_and_fee & CAP_SURPLUS_MASK != 0,
    )


@internal
@pure
def _net_floor(_order: Order) -> uint256:
    """
    @notice The least the beneficiary receives from a fill of the whole
            order, after the settlement's fees.
    @dev FeeManagerLib.processSellFees with received >= destAmount and
         expectedAmount >= destAmount (a shape requirement of the
         adapters): the protocol's cut is at most half of the surplus above
         expectedAmount, so what remains is at least expectedAmount and
         hence destAmount; a surplus-taking partner takes the rest of that
         surplus, leaving exactly expectedAmount. A partner fee in bps is
         then taken off the amount in hand (`amount - amount * bps //
         10000`, nondecreasing in the amount), so the floor is that
         function of destAmount. A partial fill scales srcAmount,
         destAmount and expectedAmount by the same percentage before the
         same arithmetic, so the floor scales pro rata up to rounding.
    """
    partner: address = empty(address)
    fee_bps: uint256 = 0
    takes_surplus: bool = False
    cap_surplus: bool = False
    partner, fee_bps, takes_surplus, cap_surplus = self._parse_partner_and_fee(_order.partnerAndFee)
    if partner == empty(address):
        return _order.destAmount
    return _order.destAmount - _order.destAmount * fee_bps // HUNDRED_PERCENT
