"""Market pricing: the sources, their priority chain, and the config wiring."""

import pytest

from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.chain.contracts import LotView
from keepers.auction_keeper.config import CowSettings, PricingSettings
from keepers.auction_keeper.pricing import (
    CoinGeckoSource,
    CowQuoteSource,
    CurvePricesSource,
    KyberQuoteSource,
    LlamaSource,
    MarketPricer,
    OneInchQuoteSource,
    VeloraQuoteSource,
    build_sources,
)
from keepers.auction_keeper.rails.common import ApiError

AUCTION = "0x" + "aa" * 20
RECEIVER = "0x" + "bb" * 20


def make_lot(token, want, available, needed=1):
    return LotView(
        auction=AUCTION,
        token=token,
        want=want,
        receiver=RECEIVER,
        timestamp=1000,
        start=900,
        end=2000,
        initial_amount=available,
        available=available,
        amount_needed=needed,
    )


@pytest.fixture
def client(keeper_account):
    return BoaClient(sender=keeper_account)


def test_curve_source_converts_usd_prices_through_decimals(
    client, http, sell_token, erc20_deployer
):
    usdc = erc20_deployer.deploy("USD Coin", "USDC", 6)
    http.on(
        "GET", f"/usd_price/polygon/{sell_token.address.lower()}", {"data": {"usd_price": 0.25}}
    )
    http.on("GET", f"/usd_price/polygon/{usdc.address.lower()}", {"data": {"usd_price": 1.0}})
    source = CurvePricesSource("polygon", client, http=http)
    assert source.quote(make_lot(sell_token.address, usdc.address, 250 * 10**18)) == 62_500_000
    # Decimals are read once per token.
    source.quote(make_lot(sell_token.address, usdc.address, 100 * 10**18))
    assert len(http.requests("GET", "/usd_price/")) == 4


def test_curve_source_without_a_price_answers_none(client, http, sell_token, want):
    def missing(params, json):
        raise ApiError(404, {"detail": "Token data not found"})

    http.on("GET", f"/usd_price/polygon/{sell_token.address.lower()}", missing)
    http.on("GET", f"/usd_price/polygon/{want.address.lower()}", {"data": {"usd_price": 1.0}})
    source = CurvePricesSource("polygon", client, http=http)
    assert source.quote(make_lot(sell_token.address, want.address, 10**18)) is None

    http.handlers.clear()
    http.on("GET", f"/usd_price/polygon/{sell_token.address.lower()}", {"data": {"usd_price": 0}})
    http.on("GET", f"/usd_price/polygon/{want.address.lower()}", {"data": {"usd_price": 1.0}})
    assert source.quote(make_lot(sell_token.address, want.address, 10**18)) is None

    def broken(params, json):
        raise ApiError(503, "down")

    http.handlers.clear()
    http.on("GET", "/usd_price/", broken)
    with pytest.raises(ApiError):
        source.quote(make_lot(sell_token.address, want.address, 10**18))


def test_cow_source_quotes_a_sell_from_the_auction(http, sell_token, want):
    def quote(params, json):
        assert json["from"] == AUCTION and json["receiver"] == RECEIVER
        assert json["kind"] == "sell" and json["sellAmountBeforeFee"] == str(10**18)
        assert json["signingScheme"] == "eip1271"
        return {"quote": {"buyAmount": "123456"}}

    http.on("POST", "/api/v1/quote", quote)
    source = CowQuoteSource("https://orderbook/", http=http)
    assert source.quote(make_lot(sell_token.address, want.address, 10**18)) == 123456
    assert http.calls[0]["url"] == "https://orderbook/api/v1/quote"

    def no_liquidity(params, json):
        raise ApiError(400, {"errorType": "NoLiquidity"})

    http.handlers.clear()
    http.on("POST", "/api/v1/quote", no_liquidity)
    assert source.quote(make_lot(sell_token.address, want.address, 10**18)) is None


class Source:
    def __init__(self, name, answer):
        self.name = name
        self.answer = answer
        self.calls = 0

    def quote(self, lot):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_pricer_priority_fallback_and_cache(sell_token, want):
    clock = [100.0]
    first, second = Source("first", None), Source("second", 42)
    pricer = MarketPricer([first, second], clock=lambda: clock[0])
    lot = make_lot(sell_token.address, want.address, 10**18)

    quote = pricer.refresh(lot)
    assert quote is not None and (quote.want_amount, quote.source) == (42, "second")
    assert (first.calls, second.calls) == (1, 1)

    # Kept until asked for again; another amount of the token is the same
    # estimate pro rata, with nobody asked.
    clock[0] += 61
    assert pricer.cached(lot.auction, lot.token, lot.available) is quote
    assert pricer.age(lot.auction, lot.token) == 61
    double = pricer.cached(lot.auction, lot.token, 2 * 10**18)
    assert (double.want_amount, double.amount) == (84, 2 * 10**18)
    assert (first.calls, second.calls) == (1, 1)

    # Both answer: both are levels, the highest is the estimate; a failing source is skipped.
    first.answer = 7
    assert pricer.refresh(lot).levels == (("second", 42), ("first", 7))
    first.answer = RuntimeError("feed down")
    assert pricer.refresh(lot).want_amount == 42

    # No answer anywhere: no market (and nothing stale left in the cache).
    second.answer = None
    assert pricer.refresh(lot) is None
    assert pricer.cached(lot.auction, lot.token, lot.available) is None


def test_llama_source_reads_prices_and_skips_low_confidence(client, http, sell_token, want):
    sell, buy = sell_token.address.lower(), want.address.lower()

    def prices(params, json):
        return {
            "coins": {
                f"polygon:{sell}": {"price": 0.25, "confidence": 0.99},
                f"polygon:{buy}": {"price": 1.0, "confidence": 0.99},
            }
        }

    http.on("GET", f"/prices/current/polygon:{sell},polygon:{buy}", prices)
    source = LlamaSource("polygon", client, http=http)
    assert source.quote(make_lot(sell_token.address, want.address, 4 * 10**18)) == 10**18

    http.handlers.clear()
    http.on(
        "GET",
        "/prices/current/",
        {"coins": {f"polygon:{sell}": {"price": 0.25, "confidence": 0.3}}},
    )
    assert source.quote(make_lot(sell_token.address, want.address, 10**18)) is None


def test_coingecko_source_sends_the_key_for_its_api(client, http, sell_token, want):
    sell, buy = sell_token.address.lower(), want.address.lower()
    http.on("GET", "/simple/token_price/polygon-pos", {sell: {"usd": 2.0}, buy: {"usd": 0.5}})
    source = CoinGeckoSource("polygon-pos", client, http=http, api_key="demo-key")
    assert source.quote(make_lot(sell_token.address, want.address, 10**18)) == 4 * 10**18
    call = http.calls[-1]
    assert call["params"] == {"contract_addresses": f"{sell},{buy}", "vs_currencies": "usd"}
    assert call["headers"] == {"x-cg-demo-api-key": "demo-key"}

    pro = CoinGeckoSource(
        "polygon-pos",
        client,
        http=http,
        api_key="pro-key",
        base_url="https://pro-api.coingecko.com/api/v3",
    )
    http.handlers.clear()
    http.on("GET", "pro-api.coingecko.com", {})
    assert pro.quote(make_lot(sell_token.address, want.address, 10**18)) is None
    assert http.calls[-1]["headers"] == {"x-cg-pro-api-key": "pro-key"}


def test_kyber_source_reads_the_route_summary(http, sell_token, want):
    def routes(params, json):
        assert params == {
            "tokenIn": sell_token.address,
            "tokenOut": want.address,
            "amountIn": str(10**18),
        }
        return {"code": 0, "data": {"routeSummary": {"amountOut": "987"}}}

    http.on("GET", "/polygon/api/v1/routes", routes)
    source = KyberQuoteSource("polygon", http=http, client_id="curve")
    assert source.quote(make_lot(sell_token.address, want.address, 10**18)) == 987
    assert http.calls[-1]["headers"] == {"x-client-id": "curve"}

    http.handlers.clear()
    http.on("GET", "/polygon/api/v1/routes", {"code": 4221, "message": "no route"})
    assert source.quote(make_lot(sell_token.address, want.address, 10**18)) is None


def test_velora_source_quotes_a_sell_with_decimals(client, http, sell_token, want):
    def prices(params, json):
        assert params["network"] == 137 and params["side"] == "SELL"
        assert (params["srcDecimals"], params["destDecimals"]) == (18, 18)
        assert params["partner"] == "curve"
        return {"priceRoute": {"destAmount": "555"}}

    http.on("GET", "/prices", prices)
    source = VeloraQuoteSource(137, client, http=http, partner="curve")
    assert source.quote(make_lot(sell_token.address, want.address, 10**18)) == 555

    def invalid(params, json):
        raise ApiError(400, {"error": "Invalid network"})

    http.handlers.clear()
    http.on("GET", "/prices", invalid)
    assert source.quote(make_lot(sell_token.address, want.address, 10**18)) is None


def test_oneinch_source_sends_the_bearer_key(http, sell_token, want):
    http.on("GET", "/swap/v6.1/1/quote", {"dstAmount": "321"})
    source = OneInchQuoteSource(1, "secret", http=http)
    assert source.quote(make_lot(sell_token.address, want.address, 10**18)) == 321
    call = http.calls[-1]
    assert call["headers"] == {"Authorization": "Bearer secret"}
    assert call["params"] == {"src": sell_token.address, "dst": want.address, "amount": str(10**18)}


def test_build_sources_follows_the_configured_priority(client, network_config, monkeypatch):
    monkeypatch.delenv("ONEINCH_API_KEY", raising=False)
    config = network_config(
        pricing=PricingSettings(
            sources=("cow", "kyber", "velora", "llama", "coingecko", "curve", "oneinch")
        ),
        cow=CowSettings(orderbook_url="https://orderbook"),
    )
    sources = build_sources(config, client)
    # 1inch without its key is skipped, the rest keep their order.
    assert [type(s) for s in sources] == [
        CowQuoteSource,
        KyberQuoteSource,
        VeloraQuoteSource,
        LlamaSource,
        CoinGeckoSource,
        CurvePricesSource,
    ]
    assert sources[0].orderbook_url == "https://orderbook"
    assert sources[1].chain == "ethereum" and sources[2].chain_id == 1  # boa's chain id 1
    assert (sources[3].chain, sources[4].chain, sources[5].chain) == (
        "ethereum",
        "ethereum",
        "ethereum",
    )

    monkeypatch.setenv("ONEINCH_API_KEY", "k")
    keyed = build_sources(network_config(pricing=PricingSettings(sources=("oneinch",))), client)
    assert [type(s) for s in keyed] == [OneInchQuoteSource] and keyed[0].api_key == "k"
    monkeypatch.setenv("OTHER_1INCH_KEY", "k2")
    keyed = build_sources(
        network_config(
            pricing=PricingSettings(sources=("oneinch",), oneinch_api_key_env="OTHER_1INCH_KEY")
        ),
        client,
    )
    assert keyed[0].api_key == "k2"

    overridden = build_sources(
        network_config(
            pricing=PricingSettings(sources=("curve", "llama"), chains={"curve": "xdai"})
        ),
        client,
    )
    assert [(s.name, s.chain) for s in overridden] == [("curve", "xdai"), ("llama", "ethereum")]
    assert build_sources(network_config(), client) == []


def test_pricer_asks_every_source_and_keeps_every_answer(sell_token, want):
    clock = [100.0]
    curve, cow, third = Source("curve", 100), Source("cow", 130), Source("third", None)
    pricer = MarketPricer([curve, cow, third], clock=lambda: clock[0])
    lot = make_lot(sell_token.address, want.address, 10**18)

    # The source with no price was asked and adds no level.
    quote = pricer.refresh(lot)
    assert (quote.want_amount, quote.source) == (130, "cow")
    assert quote.levels == (("cow", 130), ("curve", 100))
    assert (curve.calls, cow.calls, third.calls) == (1, 1, 1)

    # Far apart: still every answer, none replaces another.
    curve.answer, third.answer = 5, 1
    quote = pricer.refresh(lot)
    assert quote.levels == (("cow", 130), ("curve", 5), ("third", 1))
    assert pricer.cached(lot.auction, lot.token, lot.available) is quote  # kept like any estimate

    # Only one source has a price: nothing contradicts it.
    cow.answer = None
    third.answer = None
    curve.answer = 120
    quote = pricer.refresh(lot)
    assert quote.levels == (("curve", 120),)
