# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title Hashflow RFQ-m quote library
@author Curve Finance
@license MIT
@notice Stateless Hashflow RFQ-m quote types, the trader's EIP-712 digest
        and domain, the market maker's EIP-191 digest, the protocol's
        calldata shape, and the OrderNotValid revert shared by
        Hashflow-facing adapters.
@dev Pure parametric helpers only: no storage, no external calls, no abstract
     hooks. Hashing must stay byte-identical to HashflowRouter._hashQuoteRFQM
     (the trader side, verified by the router through OpenZeppelin's
     SignatureChecker) and HashflowPool._hashQuoteRFQM (the maker side,
     recovered by the pool), hashflownetwork/x-protocol as verified on
     Ethereum.
"""


# Protocol-level order rejection (selector OrderNotValid(string), the same
# shape the CoW- and Bebop-facing adapters use) so every adapter reports
# shape and constant failures the same way.
error OrderNotValid:
    reason: String[32]


# The trader-signed RFQ-m quote: the nine EIP-712 fields in type order (the
# router's QUOTE_TYPEHASH). The protocol has no receiver field: the pool
# credits quoteTokenAmount to `trader`, the account the router debits
# baseTokenAmount from.
struct Quote:
    txid: bytes32
    trader: address
    pool: address
    externalAccount: address
    baseToken: address
    quoteToken: address
    baseTokenAmount: uint256
    quoteTokenAmount: uint256
    quoteExpiry: uint256


# The same quote as the protocol ABI spells it (IQuote.RFQMQuote): the fields
# in calldata order plus both signatures. Calls into the router
# (tradeRFQM) and the pool take this shape.
struct RFQMQuote:
    pool: address
    externalAccount: address
    trader: address
    baseToken: address
    quoteToken: address
    baseTokenAmount: uint256
    quoteTokenAmount: uint256
    quoteExpiry: uint256
    txid: bytes32
    takerSignature: Bytes[MAX_TAKER_SIGNATURE_LEN]
    makerSignature: Bytes[MAKER_SIGNATURE_LEN]


# Every Quote field is a static ABI type, so the struct encodes to 9 words.
ENCODED_QUOTE_LEN: constant(uint256) = 9 * 32
# A contract trader's signature as the auction's router expects it: the
# 20-byte adapter prefix and the encoded quote.
MAX_TAKER_SIGNATURE_LEN: constant(uint256) = 20 + ENCODED_QUOTE_LEN
# The market maker's signature: a 65-byte ECDSA signature (r, s, v); the pool
# recovers it with OpenZeppelin ECDSA, which takes no other length.
MAKER_SIGNATURE_LEN: constant(uint256) = 65

# HashflowRouter.QUOTE_TYPEHASH.
QUOTE_TYPE_HASH: constant(bytes32) = keccak256(
    "Quote(bytes32 txid,address trader,address pool,address externalAccount,"
    "address baseToken,address quoteToken,uint256 baseTokenAmount,"
    "uint256 quoteTokenAmount,uint256 quoteExpiry)"
)
# The router's OpenZeppelin EIP712 domain: fields name, version, chainId and
# verifyingContract (eip712Domain().fields == 0x0f), name
# "Hashflow - Router", version "1.0".
EIP712_DOMAIN_TYPE_HASH: constant(bytes32) = keccak256(
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
DOMAIN_NAME: constant(String[17]) = "Hashflow - Router"
DOMAIN_VERSION: constant(String[3]) = "1.0"
DOMAIN_FIELDS: constant(bytes1) = 0x0f
# The EIP-191 prefix of the maker digest ("\x19Ethereum Signed Message:\n32").
EIP191_PREFIX: constant(Bytes[28]) = b"\x19Ethereum Signed Message:\n32"


# Hashing


@internal
@pure
def _domain_separator(_chain_id: uint256, _router: address) -> bytes32:
    """
    @notice The router's EIP712._domainSeparatorV4 for the fixed name and
            version.
    """
    return keccak256(
        abi_encode(
            EIP712_DOMAIN_TYPE_HASH,
            keccak256(DOMAIN_NAME),
            keccak256(DOMAIN_VERSION),
            _chain_id,
            _router,
        )
    )


@internal
@pure
def _struct_hash(_quote: Quote) -> bytes32:
    """
    @notice The EIP-712 hashStruct of the quote: all fields are static types,
            so abi_encode(struct) is exactly the word-per-field layout.
    """
    return keccak256(abi_encode(QUOTE_TYPE_HASH, _quote))


@internal
@pure
def _quote_digest(_quote: Quote, _domain_separator: bytes32) -> bytes32:
    """
    @notice The digest HashflowRouter._validateRFQMSignature passes to the
            trader's isValidSignature: "\x19\x01" ++ domain ++ hashStruct.
    """
    return keccak256(concat(b"\x19\x01", _domain_separator, self._struct_hash(_quote)))


@internal
@pure
def _maker_digest(_quote: Quote, _chain_id: uint256) -> bytes32:
    """
    @notice HashflowPool._hashQuoteRFQM: the EIP-191 digest the pool recovers
            the market maker's signer from, over the packed quote fields in
            the pool's order and the chain id.
    """
    packed: bytes32 = keccak256(
        concat(
            convert(_quote.pool, bytes20),
            convert(_quote.externalAccount, bytes20),
            convert(_quote.trader, bytes20),
            convert(_quote.baseToken, bytes20),
            convert(_quote.quoteToken, bytes20),
            convert(_quote.baseTokenAmount, bytes32),
            convert(_quote.quoteTokenAmount, bytes32),
            convert(_quote.quoteExpiry, bytes32),
            _quote.txid,
            convert(_chain_id, bytes32),
        )
    )
    return keccak256(concat(EIP191_PREFIX, packed))


# Calldata shape


@internal
@pure
def _signed(_quote: RFQMQuote) -> Quote:
    """
    @notice The trader-signed fields of a protocol-shaped quote.
    """
    return Quote(
        txid=_quote.txid,
        trader=_quote.trader,
        pool=_quote.pool,
        externalAccount=_quote.externalAccount,
        baseToken=_quote.baseToken,
        quoteToken=_quote.quoteToken,
        baseTokenAmount=_quote.baseTokenAmount,
        quoteTokenAmount=_quote.quoteTokenAmount,
        quoteExpiry=_quote.quoteExpiry,
    )


@internal
@pure
def _to_calldata(
    _quote: Quote,
    _taker_signature: Bytes[MAX_TAKER_SIGNATURE_LEN],
    _maker_signature: Bytes[MAKER_SIGNATURE_LEN],
) -> RFQMQuote:
    """
    @notice The quote in the protocol's ABI shape, as tradeRFQM takes it.
    """
    return RFQMQuote(
        pool=_quote.pool,
        externalAccount=_quote.externalAccount,
        trader=_quote.trader,
        baseToken=_quote.baseToken,
        quoteToken=_quote.quoteToken,
        baseTokenAmount=_quote.baseTokenAmount,
        quoteTokenAmount=_quote.quoteTokenAmount,
        quoteExpiry=_quote.quoteExpiry,
        txid=_quote.txid,
        takerSignature=_taker_signature,
        makerSignature=_maker_signature,
    )
