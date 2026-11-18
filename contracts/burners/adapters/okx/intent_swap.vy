# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title OKX Intent Swap order library
@author Curve Finance
@license MIT
@notice Stateless OKX Intent Swap order type, its EIP-712 digest and domain,
        the order uid, the commission rules, the trade flag layout, and the
        OrderNotValid revert shared by OKX-facing adapters.
@dev Pure parametric helpers only: no storage, no external calls, no abstract
     hooks. Hashing must stay byte-identical to OrderLib.hash and
     Signing.domainSeparator, the uid to OrderLib.packOrderUidParams, the
     commission rules to FeeLib.validateAndComputeFromFees and the flag
     layout to TradeLib.extractFlags (OKX Intent Swap Settlement, a GPv2
     fork; verified source at the production settlements on Ethereum).
"""


# Protocol-level order rejection (selector OrderNotValid(string), the same
# shape the CoW-, 1inch- and Bebop-facing adapters use) so every adapter
# reports shape and constant failures the same way.
error OrderNotValid:
    reason: String[32]


# OrderLib.CommissionInfo: a fee the order signs away. feePercent is in 1e9
# precision; flag packs the direction (bit 255 from-token, bit 254
# to-token), the fund flow (bit 253 ToB: the settlement pays it instead of
# the owner) and off-chain labels in lower bits.
struct CommissionInfo:
    feePercent: uint256
    referrerWalletAddress: address
    flag: uint256


# OrderLib.Data, the signed order: the eleven EIP-712 fields in type order
# (the quote API's `signData.message`, field for field).
struct Order:
    fromTokenAddress: address
    toTokenAddress: address
    owner: address
    receiver: address
    fromTokenAmount: uint256
    toTokenAmount: uint256
    validTo: uint32
    appData: bytes32
    swapMode: bytes32
    partiallyFillable: bool
    commissionInfos: DynArray[CommissionInfo, MAX_COMMISSIONS]


# FeeLib.MAX_COMMISSION_ENTRIES: the settlement rejects more, so a payload
# with more entries fails ABI decoding before any check runs.
MAX_COMMISSIONS: constant(uint256) = 3
# abi_encode(Order) at the bound: the tuple offset word, eleven head words,
# the array length word and MAX_COMMISSIONS three-word entries.
ENCODED_ORDER_BOUND: constant(uint256) = 32 + 11 * 32 + 32 + MAX_COMMISSIONS * 3 * 32
# abi_encode(Order) without commissions: the shape order_for builds.
ENCODED_PLAIN_ORDER_LEN: constant(uint256) = 32 + 11 * 32 + 32

# OrderLib.TYPE_HASH.
ORDER_TYPE_HASH: constant(bytes32) = keccak256(
    "Order(address fromTokenAddress,address toTokenAddress,address owner,address receiver,"
    "uint256 fromTokenAmount,uint256 toTokenAmount,uint32 validTo,bytes32 appData,"
    "bytes32 swapMode,bool partiallyFillable,CommissionInfo[] commissionInfos)"
    "CommissionInfo(uint256 feePercent,address referrerWalletAddress,uint256 flag)"
)  # 0xc70e289c8e7a87f3aeb5bfa5b860828276fa66d7f73bc34926b2e0383bfab574
# OrderLib.COMMISSION_INFO_TYPE_HASH.
COMMISSION_INFO_TYPE_HASH: constant(bytes32) = keccak256(
    "CommissionInfo(uint256 feePercent,address referrerWalletAddress,uint256 flag)"
)  # 0x383c49be8b10b90a55f1289bd1243f7250baff231facaad7e486d2391903bf5a
# Signing: the EIP-712 domain with fields name, version, chainId and
# verifyingContract, name "OKX Intent Swap", version "v1.0.0".
EIP712_DOMAIN_TYPE_HASH: constant(bytes32) = keccak256(
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
DOMAIN_NAME: constant(String[15]) = "OKX Intent Swap"
DOMAIN_VERSION: constant(String[6]) = "v1.0.0"

# OrderLib.SWAPMODE_EXACTIN: the only swap mode the settlement fills
# (TradeLib.extractFlags rejects the buy-order bit).
EXACT_IN: constant(bytes32) = keccak256("exactIn")
# TransferLib.BUY_ETH_ADDRESS: the native coin placeholder, allowed as a
# buy token only.
BUY_ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
# OrderLib.RECEIVER_SAME_AS_OWNER: an empty receiver means the owner.
RECEIVER_SAME_AS_OWNER: constant(address) = empty(address)
# OrderLib.UID_LENGTH: digest ++ owner ++ validTo.
UID_LENGTH: constant(uint256) = 56

# FeeLib: fee precision, the total commission cap (3%) and the flag bits.
COMMISSION_DENOM: constant(uint256) = 10**9
COMMISSION_MAX_TOTAL_PERCENT: constant(uint256) = 30_000_000
FROM_TOKEN_COMMISSION: constant(uint256) = 1 << 255
TO_TOKEN_COMMISSION: constant(uint256) = 1 << 254
IS_TO_B: constant(uint256) = 1 << 253

# TradeLib.extractFlags: bit 0 is the swap mode (must be 0, exactIn), bit 1
# the partial-fill bit (must be 0), bits 5-6 the signing scheme (0 EIP-712,
# 1 eth_sign, 2 EIP-1271, 3 pre-sign). A contract owner's trade carries
# EIP1271_TRADE_FLAGS.
SIGNING_SCHEME_OFFSET: constant(uint256) = 5
EIP1271_SCHEME: constant(uint256) = 2
EIP1271_TRADE_FLAGS: constant(uint256) = EIP1271_SCHEME << SIGNING_SCHEME_OFFSET


# Hashing


@internal
@pure
def _domain_separator(_chain_id: uint256, _verifying_contract: address) -> bytes32:
    """
    @notice Signing._computeDomainSeparator for the fixed name and version.
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
def _commission_hash(_info: CommissionInfo) -> bytes32:
    """
    @notice OrderLib.commissionInfoHash: the hashStruct of one entry.
    """
    # All fields are static types, so abi_encode(struct) is exactly the
    # word-per-field layout hashStruct expects.
    return keccak256(abi_encode(COMMISSION_INFO_TYPE_HASH, _info))


@internal
@pure
def _commissions_hash(_infos: DynArray[CommissionInfo, MAX_COMMISSIONS]) -> bytes32:
    """
    @notice OrderLib.commissionInfosHash: keccak256 of the concatenated
            entry hashes, the EIP-712 array encoding.
    """
    if len(_infos) == 0:
        return keccak256(b"")
    hashes: DynArray[bytes32, MAX_COMMISSIONS] = []
    for info: CommissionInfo in _infos:
        hashes.append(self._commission_hash(info))
    # abi_encode of one dynamic argument is the tuple encoding: an offset
    # word and a length word ahead of the elements.
    encoded: Bytes[64 + MAX_COMMISSIONS * 32] = abi_encode(hashes)
    return keccak256(slice(encoded, 64, len(hashes) * 32))


@internal
@pure
def _struct_hash(_order: Order) -> bytes32:
    """
    @notice The EIP-712 hashStruct of an order, as OrderLib.hash computes it
            before mixing in the domain.
    """
    return keccak256(
        abi_encode(
            ORDER_TYPE_HASH,
            _order.fromTokenAddress,
            _order.toTokenAddress,
            _order.owner,
            _order.receiver,
            _order.fromTokenAmount,
            _order.toTokenAmount,
            _order.validTo,
            _order.appData,
            _order.swapMode,
            _order.partiallyFillable,
            self._commissions_hash(_order.commissionInfos),
        )
    )


@internal
@pure
def _order_digest(_order: Order, _domain_separator: bytes32) -> bytes32:
    """
    @notice OrderLib.hash: the digest the settlement asks the owner to sign
            and passes to a contract owner's isValidSignature.
    """
    return keccak256(concat(b"\x19\x01", _domain_separator, self._struct_hash(_order)))


@internal
@pure
def _order_uid(_digest: bytes32, _owner: address, _valid_to: uint32) -> Bytes[UID_LENGTH]:
    """
    @notice OrderLib.packOrderUidParams: the key of the settlement's
            filledAmount and invalidateOrder.
    """
    return concat(_digest, convert(_owner, bytes20), convert(_valid_to, bytes4))


# Order fields


@internal
@pure
def _receiver(_order: Order) -> address:
    """
    @notice OrderLib.actualReceiver: an empty receiver means the owner.
    """
    if _order.receiver == RECEIVER_SAME_AS_OWNER:
        return _order.owner
    return _order.receiver


@internal
@pure
def _check_commissions(_infos: DynArray[CommissionInfo, MAX_COMMISSIONS]) -> uint256:
    """
    @notice FeeLib.validateAndComputeFromFees, the commission part: every
            entry names exactly one direction, a paid entry has a recipient,
            the total stays within the cap, paid entries share one direction
            and one fund flow. Reverts with the protocol's reasons.
    @return The total commission rate (1e9 precision).
    """
    total: uint256 = 0
    seen_from: bool = False
    seen_to: bool = False
    seen_to_b: bool = False
    seen_to_c: bool = False
    for info: CommissionInfo in _infos:
        is_from: bool = info.flag & FROM_TOKEN_COMMISSION != 0
        is_to: bool = info.flag & TO_TOKEN_COMMISSION != 0
        assert is_from != is_to, OrderNotValid(reason="BadCommissionDirection")
        # The per-entry bound keeps the sum from overflowing with a typed
        # reason; the protocol checks the sum alone.
        assert info.feePercent <= COMMISSION_MAX_TOTAL_PERCENT, (
            OrderNotValid(reason="CommissionTooHigh")
        )
        total += info.feePercent
        if info.feePercent > 0:
            assert info.referrerWalletAddress != empty(address), (
                OrderNotValid(reason="ZeroFeeRecipient")
            )
            seen_from = seen_from or is_from
            seen_to = seen_to or is_to
            if info.flag & IS_TO_B != 0:
                seen_to_b = True
            else:
                seen_to_c = True
    assert total <= COMMISSION_MAX_TOTAL_PERCENT, OrderNotValid(reason="CommissionTooHigh")
    assert not (seen_from and seen_to), OrderNotValid(reason="MixedCommissionDirections")
    assert not (seen_to_b and seen_to_c), OrderNotValid(reason="MixedCommissionFlows")
    return total
