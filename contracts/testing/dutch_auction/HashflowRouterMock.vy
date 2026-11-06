# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title HashflowRouterMock
@author Curve Finance
@license MIT
@notice Minimal HashflowRouter stand-in for the RFQ-m adapter deployment
        and settlement tests: eip712Domain over a configurable name and
        version, the pool authorization map, and tradeRFQM following
        HashflowRouter._validateRFQMQuote, _validateRFQMSignature and
        _executeRFQMTrade — expiry, pool authorization, no native base
        token, the txid burnt, the trader's contract signature checked the
        way OpenZeppelin's SignatureChecker does (a staticcall whose
        revert or wrong answer is the router's own "Invalid signer."), the
        base token pulled from the trader with a plain transferFrom, then
        the pool paying. Not modelled: RFQ-t, permits, limit orders,
        cross-chain trades, EOA traders. Settlement against the real
        protocol is the fork test's job.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

from ethereum.ercs import IERC20

from contracts.burners.adapters.hashflow import hashflow
from contracts.utils import constants as c


interface Pool:
    def tradeRFQM(_quote: hashflow.RFQMQuote): nonpayable


# Another name and version: a router whose reported domain is not the one
# the adapter signs under (a fork, a future protocol version).
FOREIGN_NAME: constant(String[64]) = "Hashflow - Router (foreign)"
FOREIGN_VERSION: constant(String[64]) = "2.0"

# The domain the router reports and signs under (storage rather than
# immutables: the constructor picks it by branch).
name: String[64]
version: String[64]
domain: immutable(bytes32)

authorizedPools: public(HashMap[address, bool])
usedTxids: public(HashMap[bytes32, bool])


# No string parameters or locals here: boa attributes the runtime's string
# reverts to this frame and cannot decode string locals from it.
@deploy
def __init__(_foreign_domain: bool):
    if _foreign_domain:
        self.name = FOREIGN_NAME
        self.version = FOREIGN_VERSION
    else:
        self.name = hashflow.DOMAIN_NAME
        self.version = hashflow.DOMAIN_VERSION
    self.domain = keccak256(
        abi_encode(
            hashflow.EIP712_DOMAIN_TYPE_HASH,
            keccak256(self.name),
            keccak256(self.version),
            chain.id,
            self,
        )
    )


@external
@view
def eip712Domain() -> (
    bytes1, String[64], String[64], uint256, address, bytes32, DynArray[uint256, 8]
):
    return hashflow.DOMAIN_FIELDS, self.name, self.version, chain.id, self, empty(bytes32), []


@external
def updatePoolAuthorization(_pool: address, _authorized: bool):
    # The real router takes this from the factory; the mock is open.
    self.authorizedPools[_pool] = _authorized


@external
def tradeRFQM(_quote: hashflow.RFQMQuote):
    """
    @notice HashflowRouter.tradeRFQM for a contract trader.
    """
    assert _quote.quoteExpiry >= block.timestamp, (
        "HashflowRouter::_validateRFQMQuote Quote has expired."
    )
    assert self.authorizedPools[_quote.pool], (
        "HashflowRouter::_validateRFQMQuote Pool not authorized."
    )
    assert _quote.baseToken != empty(address), (
        "HashflowRouter::_validateRFQMQuote RFQ-M does not support native tokens."
    )
    assert not self.usedTxids[_quote.txid], (
        "HashflowRouter::_validateRFQMQuote txid has already been used."
    )
    self.usedTxids[_quote.txid] = True

    # SignatureChecker.isValidSignatureNow for a contract signer: the
    # ECDSA attempt fails on the signature length, then a staticcall of
    # isValidSignature whose revert, short answer or wrong answer all read
    # as an invalid signature.
    digest: bytes32 = hashflow._quote_digest(hashflow._signed(_quote), self.domain)
    success: bool = False
    response: Bytes[32] = b""
    success, response = raw_call(
        _quote.trader,
        abi_encode(
            digest, _quote.takerSignature, method_id=method_id("isValidSignature(bytes32,bytes)")
        ),
        max_outsize=32,
        is_static_call=True,
        revert_on_failure=False,
    )
    assert (
        success
        and len(response) == 32
        and convert(slice(response, 0, 4), bytes4) == c.ERC1271_MAGIC_VALUE
    ), "HashflowRouter::_validateRFQMSignature Invalid signer."

    recipient: address = _quote.pool
    if _quote.externalAccount != empty(address):
        recipient = _quote.externalAccount
    assert extcall IERC20(_quote.baseToken).transferFrom(
        _quote.trader, recipient, _quote.baseTokenAmount, default_return_value=True
    )
    extcall Pool(_quote.pool).tradeRFQM(_quote)
