"""HashflowAdapter: the standalone Hashflow RFQ-m adapter behind the prefix
route.

Validation runs end-to-end through the auction's signature router: a relayer
submits the quote with takerSignature `adapter ++ abi_encode(quote)`, the
harness strips the prefix and forwards the bare quote, the adapter proves the
router's quote digest and the quote shape, and every economic decision comes
from the auction's shared check_order view with the auction itself as the
receiver (the protocol credits the trader). Shape failures revert with the
adapter's OrderNotValid(reason); the auction's typed check_order revert
bubbles unchanged for economic ones. The router and pool mocks then fill
quotes the way HashflowRouter and HashflowPool do: the txid burnt, the
contract signature checked on every fill (a failure is the router's own
"Invalid signer."), the base token pulled from the trader by the router, the
pool paying the trader after recovering the maker's signature.
"""

from typing import Any

import boa
import pytest
from boa.util.abi import Address
from eth_abi import encode
from eth_keys import keys
from eth_utils import keccak

from tests.burners.adapters.hashflow.test_module import (
    DOMAIN_NAME,
    DOMAIN_VERSION,
    QUOTE_ABI,
    domain_separator_reference,
    quote_digest_reference,
    sign_maker,
    to_calldata,
)
from tests.burners.conftest import custom_err

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WAD = 10**18
MAX_UINT256 = 2**256 - 1

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
AUCTION_LENGTH = 24 * 60 * 60
LOT_AMOUNT = 250 * WAD
TXID = keccak(b"txid")

FOREIGN_DOMAIN = keccak(b"FOREIGN_DOMAIN")
MAKER_KEY = keys.PrivateKey(keccak(b"market maker"))
OTHER_KEY = keys.PrivateKey(keccak(b"not the market maker"))

ERC1271_MAGIC_VALUE = bytes.fromhex("1626ba7e")
ERC1271_INVALID = bytes.fromhex("ffffffff")
INVALID_SIGNER = "HashflowRouter::_validateRFQMSignature Invalid signer."

# Quote tuple fields.
QUOTE_TXID = 0
QUOTE_TRADER = 1
QUOTE_POOL = 2
QUOTE_EXTERNAL_ACCOUNT = 3
QUOTE_BASE_TOKEN = 4
QUOTE_QUOTE_TOKEN = 5
QUOTE_BASE_AMOUNT = 6
QUOTE_QUOTE_AMOUNT = 7
QUOTE_EXPIRY = 8


@pytest.fixture(autouse=True)
def anchor():
    with boa.env.anchor():
        yield


@pytest.fixture(scope="module")
def owner():
    return boa.env.generate_address("owner")


@pytest.fixture(scope="module")
def emergency_owner():
    return boa.env.generate_address("emergency_owner")


@pytest.fixture(scope="module")
def proceeds_receiver():
    return boa.env.generate_address("fee_collector")


@pytest.fixture(scope="module")
def pool_operations():
    return boa.env.generate_address("pool_operations")


@pytest.fixture(scope="module")
def relayer():
    return boa.env.generate_address("relayer")


@pytest.fixture(scope="module")
def maker():
    """The market maker's signer: the address the pool recovers quotes to."""
    return MAKER_KEY.public_key.to_checksum_address()


@pytest.fixture(scope="module")
def erc20_deployer():
    return boa.load_partial("contracts/testing/ERC20Mock.vy")


@pytest.fixture(scope="module")
def want(erc20_deployer):
    return erc20_deployer.deploy("Curve Stablecoin", "crvUSD", 18)


@pytest.fixture(scope="module")
def sell_token(erc20_deployer):
    return erc20_deployer.deploy("Curve DAO", "CRV", 18)


@pytest.fixture(scope="module")
def role_source(owner, emergency_owner):
    return boa.load("contracts/testing/dutch_auction/RoleSourceMock.vy", owner, emergency_owner)


@pytest.fixture(scope="module")
def router_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/HashflowRouterMock.vy")


@pytest.fixture(scope="module")
def pool_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/HashflowPoolMock.vy")


def _forget_stale_storage_trace(contract: Any) -> None:
    """boa artifact: anchor() rolls the chain back but not boa's global sstore
    trace, so a contract deployed at an address another suite's contract used
    earlier inherits foreign slots in boa's storage dump, which then fails
    while rendering a revert of this contract. Drop them right after deploy."""
    boa.env.sstore_trace.pop(Address(contract.address), None)


@pytest.fixture(scope="module")
def router(router_deployer):
    router = router_deployer.deploy(False)
    _forget_stale_storage_trace(router)
    return router


@pytest.fixture(scope="module")
def pool(pool_deployer, router, maker, pool_operations):
    with boa.env.prank(pool_operations):
        pool = pool_deployer.deploy(maker, router.address)
    _forget_stale_storage_trace(pool)
    router.updatePoolAuthorization(pool.address, True)
    return pool


@pytest.fixture(scope="module")
def domain_separator(router):
    return domain_separator_reference(boa.env.evm.patch.chain_id, router.address)


@pytest.fixture(scope="module")
def adapter_deployer():
    return boa.load_partial("contracts/burners/adapters/hashflow/HashflowAdapter.vy")


@pytest.fixture
def registry(role_source):
    return boa.load("contracts/burners/adapters/AdapterRegistry.vy", role_source.address)


@pytest.fixture
def adapter(adapter_deployer, router):
    return adapter_deployer.deploy(router)


@pytest.fixture
def harness(role_source, want, proceeds_receiver, registry, adapter, owner, router):
    harness = boa.load(
        "contracts/testing/dutch_auction/CoreHarness.vy",
        want.address,
        proceeds_receiver,
        registry.address,
        role_source.address,
        START_TOTAL,
        FLOOR_TOTAL,
        STEP_DURATION,
        AUCTION_LENGTH,
    )
    with boa.env.prank(owner):
        # The router is the executor: a fill pulls the base token through it.
        registry.set_adapter(adapter, router.address)
        registry.activate_adapter(adapter)
    # The core fences lots staged in the block that set its economics (the
    # deployment block included): stage from the next second on.
    boa.env.time_travel(seconds=1)
    return harness


@pytest.fixture
def lot(harness, sell_token):
    sell_token._mint_for_testing(harness.address, LOT_AMOUNT)
    harness.stage(sell_token.address)
    return harness.lots(sell_token.address)


def _now() -> int:
    return boa.env.evm.vm.state.timestamp


@pytest.fixture
def make_quote(harness, sell_token, want, pool, lot):
    """Build a signed Quote tuple the way a market maker's RFQ-m quote for the
    auction would read.

    Overrides address the nine fields (txid, trader, pool, external_account,
    base_token, quote_token, base_amount, quote_amount, expiry).
    """

    def _make(**overrides) -> tuple:
        base_amount = overrides.pop("base_amount", LOT_AMOUNT)
        quote_amount = overrides.pop("quote_amount", None)
        if quote_amount is None:
            quote_amount = harness.getAmountNeeded(sell_token.address, min(base_amount, LOT_AMOUNT))
        fields = {
            "txid": TXID,
            "trader": harness.address,
            "pool": pool.address,
            "external_account": ZERO_ADDRESS,
            "base_token": sell_token.address,
            "quote_token": want.address,
            "base_amount": base_amount,
            "quote_amount": quote_amount,
            "expiry": harness.frame_end(),
        }
        unknown = set(overrides) - set(fields)
        assert not unknown, unknown
        fields.update(overrides)
        return tuple(fields.values())

    return _make


def _prefix(adapter: Any) -> bytes:
    return bytes.fromhex(str(adapter.address)[2:])


def _bare(quote: tuple) -> bytes:
    return encode([QUOTE_ABI], [quote])


def _signature(adapter: Any, quote: tuple) -> bytes:
    """What a relayer submits as takerSignature: the router forwards it verbatim."""
    return _prefix(adapter) + _bare(quote)


def _as_tuple(quote: Any) -> tuple:
    """A Quote as the adapter returns it, txid as bytes."""
    quote = tuple(quote)
    return (bytes(quote[0]), *quote[1:])


@pytest.fixture
def validate(harness, adapter, domain_separator):
    """Route a quote through the harness: digest under the router's domain,
    signature `adapter ++ abi_encode(quote)` by default."""

    def _validate(quote: tuple, signature: bytes | None = None, domain: bytes = domain_separator):
        signature = _signature(adapter, quote) if signature is None else signature
        return harness.isValidSignature(quote_digest_reference(quote, domain), signature)

    return _validate


# Deployment pinning


def test_deploy_pins_router_and_domain(adapter, router, domain_separator):
    assert adapter.router() == router.address
    assert bytes(adapter.domain_separator()) == domain_separator
    assert adapter.ADAPTER_VERSION() == "HashflowAdapter"


def test_deploy_rejects_zero_router(adapter_deployer):
    with boa.reverts(custom_err("BadRouter()")):
        adapter_deployer.deploy(ZERO_ADDRESS)


def test_deploy_rejects_a_router_with_another_domain(router_deployer, adapter_deployer):
    # Another name and version (a fork, a future protocol version): the
    # reported domain is not the one the adapter signs under.
    lying = router_deployer.deploy(True)
    assert lying.eip712Domain()[1:3] != (DOMAIN_NAME, DOMAIN_VERSION)
    with boa.reverts(custom_err("BadDomain()")):
        adapter_deployer.deploy(lying)


# Routing: the Hashflow rail is a regular registry adapter behind the prefix


def test_prefixed_bare_quote_is_valid_and_unprefixed_is_not(harness, adapter, make_quote, validate):
    quote = make_quote()
    assert validate(quote) == ERC1271_MAGIC_VALUE
    assert validate(quote, _bare(quote)) == ERC1271_INVALID


def test_disabled_adapter_stops_routing(
    harness, adapter, make_quote, registry, emergency_owner, validate
):
    quote = make_quote()
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter)
    assert validate(quote) == ERC1271_INVALID


# Transport encoding


def test_payload_must_be_exactly_one_quote(harness, adapter, make_quote, validate):
    quote = make_quote()
    # Every field is static, so one length is the one encoding: truncated or
    # empty payloads are NonCanonical before any decoding; a longer one
    # fails ABI decoding before any check runs.
    for payload in (_bare(quote)[:-1], b"", _bare(quote)[:32]):
        with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
            validate(quote, _prefix(adapter) + payload)
    with boa.reverts():
        validate(quote, _signature(adapter, quote) + b"\x00")


# Digest and shape


def test_hash_mismatch_invalid(harness, adapter, make_quote):
    quote = make_quote()
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        harness.isValidSignature(keccak(b"other"), _signature(adapter, quote))


def test_foreign_domain_separator_invalid(harness, adapter, make_quote, validate):
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        validate(make_quote(), domain=FOREIGN_DOMAIN)


def test_foreign_trader_invalid(harness, adapter, make_quote, validate, proceeds_receiver):
    # The trader is the debited and the credited account: only the auction
    # itself; naming the FeeCollector would make it the one debited.
    for trader in (boa.env.generate_address("other_trader"), proceeds_receiver, ZERO_ADDRESS):
        with boa.reverts(custom_err("OrderNotValid(string)", "BadTrader")):
            validate(make_quote(trader=trader))


def test_maker_fields_pass_through(harness, adapter, make_quote, validate):
    # pool, externalAccount and txid only decide which market-maker account
    # pays and the replay key; the auction prices the amounts alone.
    assert validate(make_quote(pool=boa.env.generate_address("pool"))) == ERC1271_MAGIC_VALUE
    assert validate(make_quote(external_account=boa.env.generate_address("mm"))) == (
        ERC1271_MAGIC_VALUE
    )
    assert validate(make_quote(txid=bytes(32))) == ERC1271_MAGIC_VALUE


# Economics: the whole quote priced by check_order with the auction as receiver


def test_auction_economics_revert_bubbles_through_adapter(
    harness, adapter, make_quote, sell_token, validate
):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    with boa.reverts(custom_err("BadBuyAmount()")):
        validate(make_quote(quote_amount=quote - 1))
    assert validate(make_quote(quote_amount=quote + 1)) == ERC1271_MAGIC_VALUE


def test_quote_expiry_is_the_validity_bound(harness, adapter, make_quote, validate):
    with boa.reverts(custom_err("BadValidTo()")):
        validate(make_quote(expiry=harness.frame_end() + 1))
    with boa.reverts(custom_err("BadValidTo()")):
        validate(make_quote(expiry=0))
    with boa.reverts(custom_err("BadValidTo()")):
        validate(make_quote(expiry=_now() - 1))
    assert validate(make_quote(expiry=_now())) == ERC1271_MAGIC_VALUE


def test_wrong_tokens_invalid(harness, adapter, make_quote, sell_token, validate):
    with boa.reverts(custom_err("BadBuyToken()")):
        validate(make_quote(quote_token=sell_token.address))
    # The native coin (address zero on Hashflow) is neither the payment
    # token nor a lot; the router rejects it as baseToken anyway.
    with boa.reverts(custom_err("BadBuyToken()")):
        validate(make_quote(quote_token=ZERO_ADDRESS))
    with boa.reverts(custom_err("LotInactive()")):
        validate(make_quote(base_token=ZERO_ADDRESS))


def test_sell_amount_is_bounded_by_the_snapshot(
    harness, adapter, make_quote, sell_token, want, validate
):
    # A native take shrinks the remainder; a quote over the whole snapshot
    # still validates (the router's transferFrom is what fails then), one
    # above the snapshot never does.
    quote = make_quote()
    taker = boa.env.generate_address("taker")
    taken = LOT_AMOUNT // 4
    want._mint_for_testing(taker, harness.getAmountNeeded(sell_token.address, taken))
    with boa.env.prank(taker):
        want.approve(harness.address, MAX_UINT256)
        harness.take(sell_token.address, taken)
    assert harness.available(sell_token.address) == LOT_AMOUNT - taken
    assert validate(quote) == ERC1271_MAGIC_VALUE
    with boa.reverts(custom_err("BadSellAmount()")):
        validate(make_quote(base_amount=LOT_AMOUNT + 1, quote_amount=quote[QUOTE_QUOTE_AMOUNT]))


# Publisher helpers: the quote-request template, the signature, the digest


def test_order_for_builds_the_trader_side_of_a_quote_request(
    harness, adapter, sell_token, want, lot, validate
):
    template = _as_tuple(adapter.order_for(harness.address, sell_token.address))
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    assert template == (
        bytes(32),
        harness.address,
        ZERO_ADDRESS,
        ZERO_ADDRESS,
        sell_token.address,
        want.address,
        LOT_AMOUNT,
        quote,
        harness.frame_end(),
    )
    # A partial amount is priced proportionally.
    partial = _as_tuple(adapter.order_for(harness.address, sell_token.address, LOT_AMOUNT // 4))
    assert partial[QUOTE_BASE_AMOUNT] == LOT_AMOUNT // 4
    assert partial[QUOTE_QUOTE_AMOUNT] == harness.getAmountNeeded(
        sell_token.address, LOT_AMOUNT // 4
    )
    # The market maker fills its fields and may shorten the expiry; the
    # completed quote validates as published.
    completed = (
        TXID,
        *template[1:2],
        boa.env.generate_address("pool"),
        *template[3:8],
        _now() + 60,
    )
    assert validate(completed) == ERC1271_MAGIC_VALUE
    assert bytes(adapter.signature_for(completed)) == _signature(adapter, completed)


def test_order_for_rejects_empty_lots(harness, adapter, sell_token, erc20_deployer):
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, sell_token.address)
    fresh = erc20_deployer.deploy("Fresh", "FRESH", 18)
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, fresh.address)


def test_describe_decodes_the_payload_and_its_digest(adapter, make_quote, domain_separator):
    quote = make_quote()
    decoded, digest = adapter.describe(_bare(quote))
    assert _as_tuple(decoded) == quote
    assert bytes(digest) == quote_digest_reference(quote, domain_separator)
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        adapter.describe(_bare(quote)[:-1])


# Settlement through the mocks: the txid burnt, the signature checked on
# every fill, the base token pulled by the router, the pool paying the trader.


@pytest.fixture
def approved(harness, router, sell_token, lot):
    harness.sync_executor_approvals(router.address, [sell_token.address])
    assert sell_token.allowance(harness.address, router.address) == MAX_UINT256


@pytest.fixture
def pool_liquidity(want, pool):
    want._mint_for_testing(pool.address, 1_000_000 * WAD)


def _chain_id() -> int:
    return boa.env.evm.patch.chain_id


def _calldata(adapter: Any, quote: tuple, maker_key: keys.PrivateKey = MAKER_KEY) -> tuple:
    return to_calldata(quote, _signature(adapter, quote), sign_maker(maker_key, quote, _chain_id()))


def test_relayer_fills_a_quote_the_pool_pays_the_auction(
    harness, adapter, router, pool, sell_token, want, relayer, approved, pool_liquidity
):
    half = LOT_AMOUNT // 2
    template = _as_tuple(adapter.order_for(harness.address, sell_token.address, half))
    quote = (TXID, *template[1:2], pool.address, *template[3:8], _now() + 60)
    price = quote[QUOTE_QUOTE_AMOUNT]
    with boa.env.prank(relayer):
        router.tradeRFQM(_calldata(adapter, quote))
    # The pool holds the base token; the proceeds landed on the auction
    # itself (the trader), not on the proceeds receiver.
    assert sell_token.balanceOf(pool.address) == half
    assert harness.available(sell_token.address) == half
    assert want.balanceOf(harness.address) == price
    assert want.balanceOf(harness.receiver()) == 0
    assert router.usedTxids(TXID)
    # Exact fill: the txid is burnt, so the same quote (still acceptable to
    # the auction) never settles twice; the same ask under a fresh txid does.
    with (
        boa.env.prank(relayer),
        boa.reverts("HashflowRouter::_validateRFQMQuote txid has already been used."),
    ):
        router.tradeRFQM(_calldata(adapter, quote))
    reposted = (keccak(b"txid 2"), *quote[1:])
    with boa.env.prank(relayer):
        router.tradeRFQM(_calldata(adapter, reposted))
    assert sell_token.balanceOf(pool.address) == LOT_AMOUNT
    assert harness.available(sell_token.address) == 0
    assert want.balanceOf(harness.address) == 2 * price


def test_external_account_pays_the_auction(
    harness, adapter, router, pool, sell_token, want, relayer, approved, make_quote
):
    # A pool whose funds sit on an external account: the router sends the
    # base token there and the pool pulls the payment from it.
    external_account = boa.env.generate_address("market_maker_account")
    want._mint_for_testing(external_account, 1_000_000 * WAD)
    with boa.env.prank(external_account):
        want.approve(pool.address, MAX_UINT256)
    quote = make_quote(external_account=external_account)
    with boa.env.prank(relayer):
        router.tradeRFQM(_calldata(adapter, quote))
    assert sell_token.balanceOf(external_account) == LOT_AMOUNT
    assert want.balanceOf(harness.address) == quote[QUOTE_QUOTE_AMOUNT]


def test_router_reports_what_the_auction_rejects_as_an_invalid_signer(
    harness,
    adapter,
    router,
    sell_token,
    relayer,
    approved,
    pool_liquidity,
    make_quote,
    registry,
    emergency_owner,
    validate,
):
    # The router's SignatureChecker swallows the auction's typed revert: an
    # underpriced quote is "Invalid signer." on-chain, while an eth_call of
    # the auction's isValidSignature carries the reason.
    price = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    cheaper = make_quote(quote_amount=price - 1)
    with boa.reverts(custom_err("BadBuyAmount()")):
        validate(cheaper)
    with boa.env.prank(relayer), boa.reverts(INVALID_SIGNER):
        router.tradeRFQM(_calldata(adapter, cheaper))
    # A disabled adapter answers 0xffffffff through the auction's router:
    # the same router error, and nothing moved.
    quote = make_quote()
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter)
    with boa.env.prank(relayer), boa.reverts(INVALID_SIGNER):
        router.tradeRFQM(_calldata(adapter, quote))
    assert sell_token.balanceOf(harness.address) == LOT_AMOUNT
    # A txid of a failed fill is not burnt: the whole transaction reverted.
    assert not router.usedTxids(TXID)


def test_fills_without_the_adapter_signature_are_refused(
    harness, adapter, router, sell_token, relayer, approved, pool_liquidity, make_quote
):
    # Bytes shorter than a prefix or an unknown prefix select no adapter.
    cheaper = make_quote(quote_amount=1)
    maker_signature = sign_maker(MAKER_KEY, cheaper, _chain_id())
    for taker_signature in (b"", _bare(cheaper), b"\x11" * 20 + _bare(cheaper)):
        with boa.env.prank(relayer), boa.reverts(INVALID_SIGNER):
            router.tradeRFQM(to_calldata(cheaper, taker_signature, maker_signature))
    assert sell_token.balanceOf(harness.address) == LOT_AMOUNT


def test_protocol_liveness_checks_are_the_routers_and_the_pools(
    harness,
    adapter,
    router,
    pool,
    sell_token,
    relayer,
    pool_operations,
    approved,
    pool_liquidity,
    make_quote,
):
    # Expiry: the router's check is inclusive like the auction's.
    expiring = make_quote(expiry=_now())
    boa.env.time_travel(seconds=1)
    with (
        boa.env.prank(relayer),
        boa.reverts("HashflowRouter::_validateRFQMQuote Quote has expired."),
    ):
        router.tradeRFQM(_calldata(adapter, expiring))
    # Only factory-created pools are authorized.
    foreign_pool = make_quote(pool=boa.env.generate_address("foreign_pool"))
    with (
        boa.env.prank(relayer),
        boa.reverts("HashflowRouter::_validateRFQMQuote Pool not authorized."),
    ):
        router.tradeRFQM(_calldata(adapter, foreign_pool))
    # The pool recovers the maker's signature to its configured signer.
    quote = make_quote()
    with boa.env.prank(relayer), boa.reverts("HashflowPool::tradeRFQM Invalid signer."):
        router.tradeRFQM(_calldata(adapter, quote, OTHER_KEY))
    with boa.env.prank(pool_operations):
        pool.killswitchOperations(False)
    with boa.env.prank(relayer), boa.reverts("HashflowPool::tradeRFQM Disabled."):
        router.tradeRFQM(_calldata(adapter, quote))
    with boa.env.prank(pool_operations):
        pool.killswitchOperations(True)
        pool.updateSigner(OTHER_KEY.public_key.to_checksum_address())
    with boa.env.prank(relayer):
        router.tradeRFQM(_calldata(adapter, quote, OTHER_KEY))
    assert sell_token.balanceOf(pool.address) == LOT_AMOUNT


def test_fill_needs_the_router_allowance(
    harness, adapter, router, sell_token, relayer, lot, pool_liquidity, make_quote
):
    # Staging grants nothing: without sync_executor_approvals(router, ...)
    # the router's transferFrom fails after the signature check.
    quote = make_quote()
    with boa.env.prank(relayer), boa.reverts():
        router.tradeRFQM(_calldata(adapter, quote))
    harness.sync_executor_approvals(router.address, [sell_token.address])
    with boa.env.prank(relayer):
        router.tradeRFQM(_calldata(adapter, quote))
    assert harness.available(sell_token.address) == 0
