# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title DutchAuctionResolver
@author Curve Finance
@license MIT
@notice ERC-7683-inspired intent resolver for the native Dutch auction take()
        settlement: turns a signed-nothing DutchAuctionIntent payload into an
        executable call step with live amounts read through eth_call.
@dev ERC-7683 is a Draft, so this contract defines its own versioned structs
     instead of the standard's. Resolution is advisory: the auction's take()
     independently re-enforces availability, pricing, and atomic payment, so a
     stale or manipulated resolution can never extract more than the auction
     curve allows. All rejection paths revert with deterministic custom errors
     so off-chain feeds can classify invalid intents from a plain eth_call.
@custom:kill Stateless view contract: no owner, no storage, no funds, no
     approvals. There is nothing to kill or migrate; retire it by no longer
     advertising its address in intent feeds and metadata.
@custom:security Holds no custody and receives no allowances, and cannot
     weaken on-chain checks: take() enforces everything at execution time.
     Solvers must treat output as a same-block quote; the want payment is
     monotone non-increasing within a lot, so it stays a safe payment cap for
     later fills of the same lot.
"""

from contracts.interfaces import IDutchAuction


error WrongChain:
    pass


error BadAuction:
    pass


error IntentExpired:
    pass


error WrongLot:
    pass


error NothingAvailable:
    pass


# The intent payload: the abi-encoding of this struct is what feeds publish.
# The field set is this contract's own (ERC-7683 is a Draft; see @dev above).
struct DutchAuctionIntent:
    chain_id: uint256
    auction: address
    sell_token: address
    # Window start of the lot the intent was built for: pins the intent to
    # one lot instance so a restaged lot's fresh curve never resolves.
    lot_start: uint64
    max_sell_amount: uint256
    deadline: uint48


# One asset movement of the resolved fill.
struct TokenAmount:
    token: address
    amount: uint256
    recipient: address


# One on-chain call the solver should perform to settle the intent.
struct CallStep:
    target: address
    value: uint256
    call_data: Bytes[TAKE_CALLDATA_BOUND]


# Versioned resolution result (RESOLVER_VERSION is echoed in resolver_version).
#
# - sell_payout: sell token paid out by the auction; recipient is
#   empty(address) because the taker receiver is solver-chosen at fill time.
# - want_payment: want owed for the fill; take() pulls the full amount from
#   the caller's want allowance. recipient is informational: the auction's
#   receiver, where the auction forwards the payment.
# - quoted_at/valid_from/fill_deadline: amounts are exact at quoted_at; the
#   fill window is [valid_from, fill_deadline] inclusive.
# - call_step: minimal take() calldata template. The taker receiver argument
#   is left as the zero address so the unmodified template cannot execute
#   (ZeroReceiver); the solver must write its receiver address,
#   right-aligned, into the 32-byte word at taker_receiver_offset. Solvers
#   that want callback data must build their own take() calldata instead.
struct ResolvedOrder:
    resolver_version: uint256
    chain_id: uint256
    auction: address
    quoted_at: uint256
    valid_from: uint256
    fill_deadline: uint256
    sell_payout: TokenAmount
    want_payment: TokenAmount
    call_step: CallStep
    taker_receiver_offset: uint256


RESOLVER_VERSION: public(constant(uint256)) = 1

# abi-encoded DutchAuctionIntent: six static head words.
INTENT_PAYLOAD_LEN: public(constant(uint256)) = 6 * 32
# take(address,uint256,address,bytes) with empty callback data: 4-byte
# selector, four head words, and the zero length word of the bytes tail
# occupy 164 bytes; the bound adds one padded word for the Bytes[1] buffer.
TAKE_CALLDATA_BOUND: public(constant(uint256)) = 4 + 5 * 32 + 32
# Calldata offset of the takerReceiver head word: selector + two words.
TAKER_RECEIVER_OFFSET: public(constant(uint256)) = 4 + 2 * 32


@external
@view
def resolve(_payload: Bytes[INTENT_PAYLOAD_LEN]) -> ResolvedOrder:
    """
    @notice Resolve a native auction intent payload against live auction state.
    @dev Reverts (deterministically, for eth_call classification) on a foreign
         chain, an expired deadline, a lot other than the intent's, and an
         inactive or empty lot; a malformed payload fails abi decoding. The returned amounts are
         quoted at block.timestamp: the want payment only decreases later
         within the same lot, while the sell payout can shrink if other fills
         land first, so solvers should re-resolve close to execution.
         Named token assumption: amounts assume vanilla ERC-20 transfers;
         fee-on-transfer or rebasing sell tokens may deliver less than
         sell_payout.amount, while the full want payment stays owed. take()
         independently re-checks that the lot is live at execution.
    @param _payload abi-encoded DutchAuctionIntent.
    @return The versioned resolved order (see the struct documentation).
    """
    intent: DutchAuctionIntent = abi_decode(_payload, DutchAuctionIntent)
    assert intent.chain_id == chain.id, WrongChain()
    assert intent.auction != empty(address), BadAuction()
    assert convert(intent.deadline, uint256) >= block.timestamp, IntentExpired()

    auction: IDutchAuction = IDutchAuction(intent.auction)
    # available() is 0 for unstaged, fenced, out-of-window, killed, and
    # drained lots alike; take() would reject all of them the same way.
    available_amount: uint256 = staticcall auction.available(intent.sell_token)
    sell_amount: uint256 = min(intent.max_sell_amount, available_amount)
    assert sell_amount > 0, NothingAvailable()
    # A live lot: now make sure it is the one the intent was built for and
    # not a restage with a fresh curve.
    lot_start: uint256 = 0
    lot_end: uint256 = 0
    lot_start, lot_end = staticcall auction.window(intent.sell_token)
    assert convert(intent.lot_start, uint256) == lot_start, WrongLot()

    payment: uint256 = staticcall auction.getAmountNeeded(intent.sell_token, sell_amount)
    want: address = (staticcall auction.want()).address
    receiver: address = staticcall auction.receiver()

    empty_callback: Bytes[1] = b""
    call_data: Bytes[TAKE_CALLDATA_BOUND] = abi_encode(
        intent.sell_token,
        sell_amount,
        empty(address),
        empty_callback,
        method_id=method_id("take(address,uint256,address,bytes)"),
    )

    return ResolvedOrder(
        resolver_version=RESOLVER_VERSION,
        chain_id=chain.id,
        auction=intent.auction,
        quoted_at=block.timestamp,
        valid_from=lot_start,
        # The lot window is exclusive of its end; the intent deadline is inclusive.
        fill_deadline=min(convert(intent.deadline, uint256), lot_end - 1),
        sell_payout=TokenAmount(
            token=intent.sell_token, amount=sell_amount, recipient=empty(address)
        ),
        want_payment=TokenAmount(token=want, amount=payment, recipient=receiver),
        call_step=CallStep(target=intent.auction, value=0, call_data=call_data),
        taker_receiver_offset=TAKER_RECEIVER_OFFSET,
    )
