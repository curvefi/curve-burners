# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title HashflowPoolMock
@author Curve Finance
@license MIT
@notice Minimal HashflowPool stand-in for the RFQ-m adapter settlement
        tests: the router-only tradeRFQM recovering the market maker's
        EIP-191 signature (HashflowPool._hashQuoteRFQM) and paying
        quoteTokenAmount to the trader from the pool's own balance or from
        its external account, the signer update and the killswitch. Not
        modelled: RFQ-t, cross-chain trades, the native coin, liquidity
        management. Settlement against the real protocol is the fork test's
        job.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

from ethereum.ercs import IERC20

from contracts.burners.adapters.hashflow import hashflow


event Trade:
    trader: address
    effective_trader: address
    txid: bytes32
    base_token: address
    quote_token: address
    base_token_amount: uint256
    quote_token_amount: uint256


event UpdateSigner:
    signer: address
    previous_signer: address


router: public(immutable(address))
operations: public(immutable(address))
signer: public(address)
enabled: public(bool)


@deploy
def __init__(_signer: address, _router: address):
    self.router = _router
    self.operations = msg.sender
    self.signer = _signer
    self.enabled = True
    log UpdateSigner(signer=_signer, previous_signer=empty(address))


@internal
@view
def _recover(_digest: bytes32, _signature: Bytes[hashflow.MAKER_SIGNATURE_LEN]) -> address:
    # OpenZeppelin ECDSA.recover: a 65-byte (r, s, v) signature only.
    assert len(_signature) == hashflow.MAKER_SIGNATURE_LEN, "ECDSA: invalid signature length"
    r: uint256 = convert(extract32(_signature, 0), uint256)
    s: uint256 = convert(extract32(_signature, 32), uint256)
    v: uint256 = convert(slice(_signature, 64, 1), uint256)
    return ecrecover(_digest, v, r, s)


@external
def tradeRFQM(_quote: hashflow.RFQMQuote):
    """
    @notice HashflowPool.tradeRFQM: the maker's signature over the pool's
            EIP-191 digest, then quoteTokenAmount to the trader from the
            pool or from the external account.
    """
    assert msg.sender == self.router, "HashflowPool::authorizedRouter Sender must be Router."
    assert self.enabled, "HashflowPool::tradeRFQM Disabled."
    digest: bytes32 = hashflow._maker_digest(hashflow._signed(_quote), chain.id)
    assert self._recover(digest, _quote.makerSignature) == self.signer, (
        "HashflowPool::tradeRFQM Invalid signer."
    )
    log Trade(
        trader=_quote.trader,
        effective_trader=_quote.trader,
        txid=_quote.txid,
        base_token=_quote.baseToken,
        quote_token=_quote.quoteToken,
        base_token_amount=_quote.baseTokenAmount,
        quote_token_amount=_quote.quoteTokenAmount,
    )
    if _quote.externalAccount == empty(address):
        assert extcall IERC20(_quote.quoteToken).transfer(
            _quote.trader, _quote.quoteTokenAmount, default_return_value=True
        )
    else:
        assert extcall IERC20(_quote.quoteToken).transferFrom(
            _quote.externalAccount,
            _quote.trader,
            _quote.quoteTokenAmount,
            default_return_value=True,
        )


@external
def updateSigner(_signer: address):
    assert (
        msg.sender == self.operations
    ), "HashflowPool:authorizedOperations Sender must be operator."
    log UpdateSigner(signer=_signer, previous_signer=self.signer)
    self.signer = _signer


@external
def killswitchOperations(_enabled: bool):
    assert (
        msg.sender == self.operations
    ), "HashflowPool:authorizedOperations Sender must be operator."
    self.enabled = _enabled
