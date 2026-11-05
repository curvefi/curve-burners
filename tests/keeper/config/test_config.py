import pytest

from keepers.auction_keeper.config import (
    DEFAULT_ORDER_TTL,
    DEFAULT_POLL_INTERVAL,
    ConfigError,
    TokenFilter,
    load_config,
    parse_config,
)

AUCTION = "0x" + "aa" * 20
TOKEN_A = "0x" + "01" * 20
TOKEN_B = "0x" + "02" * 20


def test_minimal_config_gets_defaults():
    config = parse_config({"networks": {"gnosis": {"rpc": "https://rpc", "auctions": [AUCTION]}}})
    network = config.network("gnosis")
    assert network.auctions == ("0x" + "Aa" * 20,) or network.auctions[0].lower() == AUCTION
    assert network.poll_interval == DEFAULT_POLL_INTERVAL
    assert network.order_ttl == DEFAULT_ORDER_TTL
    assert network.tokens.allows(TOKEN_A)
    assert network.cow.orderbook_url is None
    assert network.private_key is None


def test_env_expansion_and_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_RPC_KEY", "secret")
    monkeypatch.setenv("TEST_KEEPER_KEY", "0x" + "11" * 32)
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
poll_interval: 10
order_ttl: 120
state_path: {tmp_path / "state.json"}
networks:
  ethereum:
    rpc: https://rpc/${{TEST_RPC_KEY}}
    auctions: ["{AUCTION}"]
    start_block: 123
    poll_interval: 5
    keeper_key_env: TEST_KEEPER_KEY
    tokens:
      whitelist: ["{TOKEN_A}"]
      blacklist: ["{TOKEN_B}"]
    cow:
      orderbook_url: https://orderbook
"""
    )
    config = load_config(path, dotenv=False)
    network = config.network("ethereum")
    assert network.rpc == "https://rpc/secret"
    assert network.start_block == 123
    assert network.poll_interval == 5
    assert network.order_ttl == 120
    assert network.private_key == "0x" + "11" * 32
    assert network.tokens.allows(TOKEN_A) and not network.tokens.allows(TOKEN_B)
    assert not network.tokens.allows("0x" + "03" * 20)
    assert network.cow.orderbook_url == "https://orderbook"
    assert config.state_path == tmp_path / "state.json"


def test_missing_env_variable_is_an_error(monkeypatch):
    monkeypatch.delenv("DEFINITELY_UNSET_KEY", raising=False)
    with pytest.raises(ConfigError, match="DEFINITELY_UNSET_KEY"):
        parse_config(
            {"networks": {"x": {"rpc": "https://${DEFINITELY_UNSET_KEY}", "auctions": [AUCTION]}}}
        )


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"networks": {}},
        {"networks": {"x": {"auctions": [AUCTION]}}},
        {"networks": {"x": {"rpc": "r", "auctions": []}}},
        {"networks": {"x": {"rpc": "r", "auctions": ["nope"]}}},
        {"networks": {"x": {"rpc": "r", "auctions": [AUCTION], "poll_interval": 0}}},
        {"networks": {"x": {"rpc": "r", "auctions": [AUCTION], "mode": "online"}}},
        # keeper mode without a key to send with
        {"networks": {"x": {"rpc": "r", "auctions": [AUCTION], "mode": "keeper"}}},
    ],
)
def test_invalid_configs_rejected(raw):
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_mode_defaults_to_offline_and_inherits():
    config = parse_config(
        {
            "mode": "keeper",
            "keeper_key_env": "KEY",
            "networks": {
                "a": {"rpc": "r", "auctions": [AUCTION]},
                "b": {"rpc": "r", "auctions": [AUCTION], "mode": "offline"},
            },
        }
    )
    assert config.network("a").onchain and not config.network("b").onchain
    plain = parse_config({"networks": {"a": {"rpc": "r", "auctions": [AUCTION]}}})
    assert plain.network("a").mode == "offline" and not plain.network("a").onchain


def test_token_filter_blacklist_beats_whitelist():
    both = TokenFilter(whitelist=frozenset([TOKEN_A]), blacklist=frozenset([TOKEN_A]))
    assert not both.allows(TOKEN_A)
    assert not both.allows(TOKEN_A.lower()) and not both.allows(TOKEN_B)


def test_rail_section_mechanism(monkeypatch):
    from keepers.auction_keeper.config.rails import RailSettings
    from keepers.auction_keeper.config.rails import parse_rail as _rail

    assert _rail({}, "x", "rail", RailSettings) == RailSettings()
    assert _rail({"rail": None}, "x", "rail", RailSettings) == RailSettings()
    settings = _rail(
        {"rail": {"api_key_env": "MY_KEY", "enabled": False, "post": False}},
        "x",
        "rail",
        RailSettings,
    )
    assert settings == RailSettings(api_key_env="MY_KEY", enabled=False, post=False)
    assert settings.api_key is None
    monkeypatch.setenv("MY_KEY", "k1")
    assert settings.api_key == "k1"
    for section in (
        {"rail": {"nope": 1}},
        {"rail": {"post": "yes"}},
        {"rail": {"enabled": 1}},
        {"rail": {"api_key_env": 7}},
        {"rail": "polygon"},
    ):
        with pytest.raises(ConfigError):
            _rail(section, "x", "rail", RailSettings)


def test_pricing_ttl_and_budget_default_and_override(monkeypatch):
    from keepers.auction_keeper.config import PricingSettings, TtlSettings

    minimal = parse_config({"networks": {"x": {"rpc": "r", "auctions": [AUCTION]}}}).network("x")
    assert minimal.pricing == PricingSettings() and minimal.pricing.sources == (
        "curve",
        "llama",
        "cow",
    )
    assert minimal.ttl == TtlSettings() and minimal.max_open_orders == 8
    monkeypatch.delenv("ONEINCH_API_KEY", raising=False)
    assert minimal.pricing.oneinch_api_key is None and minimal.pricing.coingecko_api_key is None

    config = parse_config(
        {
            "pricing": {"sources": ["cow"], "cache": 30, "cross_check": 0.2},
            "ttl": {"min": 30, "max": 600, "near_above": 0.5},
            "max_open_orders": 4,
            "networks": {
                "a": {"rpc": "r", "auctions": [AUCTION]},
                "b": {
                    "rpc": "r",
                    "auctions": [AUCTION],
                    "pricing": {
                        "sources": [],
                        "chains": {"curve": "xdai"},
                        "oneinch_api_key_env": "MY_1INCH",
                    },
                    "ttl": {"near_below": 0.05},
                    "max_open_orders": 2,
                },
            },
        }
    )
    a, b = config.network("a"), config.network("b")
    assert a.pricing == PricingSettings(sources=("cow",), cache=30, cross_check=0.2)
    assert a.ttl == TtlSettings(near_above=0.5, near_below=0.1, min_ttl=30, max_ttl=600)
    assert a.max_open_orders == 4
    assert b.pricing == PricingSettings(
        sources=(),
        cache=30,
        cross_check=0.2,
        chains={"curve": "xdai"},
        oneinch_api_key_env="MY_1INCH",
    )
    monkeypatch.setenv("MY_1INCH", "k1")
    assert b.pricing.oneinch_api_key == "k1"
    assert b.ttl == TtlSettings(near_above=0.5, near_below=0.05, min_ttl=30, max_ttl=600)
    assert b.max_open_orders == 2


@pytest.mark.parametrize(
    "section",
    [
        {"pricing": {"sources": ["binance"]}},
        {"pricing": {"sources": ["cow", "cow"]}},
        {"pricing": {"sources": "cow"}},
        {"pricing": {"feed": "x"}},
        {"pricing": {"cache": 0}},
        {"pricing": {"cross_check": 1}},
        {"pricing": {"cross_check": True}},
        {"pricing": {"chains": {"binance": "bsc"}}},
        {"pricing": {"chains": ["polygon"]}},
        {"pricing": {"coingecko_url": ""}},
        {"pricing": {"oneinch_api_key_env": 1}},
        {"ttl": {"near_above": 1.5}},
        {"ttl": {"near_below": -0.1}},
        {"ttl": {"min": 100, "max": 50}},
        {"ttl": {"ttl": 10}},
        {"max_open_orders": 0},
    ],
)
def test_pricing_ttl_and_budget_reject_bad_settings(section):
    with pytest.raises(ConfigError):
        parse_config({"networks": {"x": {"rpc": "r", "auctions": [AUCTION], **section}}})
    with pytest.raises(ConfigError):
        parse_config({**section, "networks": {"x": {"rpc": "r", "auctions": [AUCTION]}}})


def test_native_section_parses_and_needs_a_taker():
    from keepers.auction_keeper.config import NativeSettings

    taker = "0x" + "ee" * 20
    pool = "0x" + "01" * 20
    config = parse_config(
        {
            "native": {"margin": 0.01, "poll": 15},
            "networks": {
                "a": {
                    "rpc": "r",
                    "auctions": [AUCTION],
                    "auction_taker": taker,
                    "native": {
                        "enabled": True,
                        "max_impact": 0.02,
                        "chain": "fraxtal",
                        "routes": {
                            TOKEN_A: {"path": [TOKEN_A, pool, TOKEN_B], "params": [[0, 1, 1, 1, 2]]}
                        },
                    },
                },
                "b": {"rpc": "r", "auctions": [AUCTION]},
            },
        }
    )
    a, b = config.network("a"), config.network("b")
    assert a.native.enabled and a.native.margin == 0.01 and a.native.poll == 15
    assert a.native.max_impact == 0.02 and a.native.chain == "fraxtal"
    assert list(a.native.routes) == [TOKEN_A] and a.native.routes[TOKEN_A].hops == 1
    assert b.native == NativeSettings(margin=0.01, poll=15)
    with pytest.raises(ConfigError, match="auction_taker"):
        parse_config(
            {"networks": {"x": {"rpc": "r", "auctions": [AUCTION], "native": {"enabled": True}}}}
        )


@pytest.mark.parametrize(
    "section",
    [
        {"native": {"enabled": "yes"}},
        {"native": {"margin": 1}},
        {"native": {"max_impact": -0.1}},
        {"native": {"poll": 0}},
        {"native": {"router": "nope"}},
        {"native": {"routes": {TOKEN_A: {"path": [TOKEN_A], "params": []}}}},
        {"native": {"min_pool_usd": -1}},
        {"native": {"feed": 1}},
    ],
)
def test_native_section_rejects_bad_settings(section):
    with pytest.raises(ConfigError):
        parse_config({"networks": {"x": {"rpc": "r", "auctions": [AUCTION], **section}}})


@pytest.mark.parametrize(
    "raw",
    [
        {"networks": {"x": {"rpc": "r", "auctions": [AUCTION, AUCTION]}}},
        {"networks": {"x": {"rpc": "r", "auctions": [AUCTION], "start_block": -1}}},
        {"networks": {"x": {"rpc": "r", "auctions": [AUCTION], "start_block": True}}},
        {"networks": {"x": {"rpc": "r", "auctions": [AUCTION], "keeper_key_env": 3}}},
        {"networks": {"x": {"rpc": "r", "auctions": [AUCTION], "cow": {"nope": 1}}}},
        {"networks": {"x": {"rpc": "r", "auctions": [AUCTION], "cow": {"orderbook_url": 1}}}},
        {"state_path": 1, "networks": {"x": {"rpc": "r", "auctions": [AUCTION]}}},
        {"state_path": "", "networks": {"x": {"rpc": "r", "auctions": [AUCTION]}}},
    ],
)
def test_network_level_holes_are_rejected(raw):
    with pytest.raises(ConfigError):
        parse_config(raw)
