"""Keeper tests run the real package against Titanoboa through ``BoaClient``.

The chain client protocol is the only seam: everything above it (contract
readers, publishers, the keeper loop, executions) is exercised unchanged.
"""

from __future__ import annotations

import json as json_module
from typing import Any

import boa
import pytest
from eth_utils import keccak

from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.config import CowSettings, NetworkConfig, PricingSettings, TokenFilter
from keepers.auction_keeper.core.keeper import NetworkKeeper
from keepers.auction_keeper.core.state import KeeperState
from keepers.auction_keeper.rails.cow import CowPublisher

WAD = 10**18
WEEK = 7 * 24 * 60 * 60

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
AUCTION_LENGTH = 24 * 60 * 60
LOT_AMOUNT = 250 * WAD

APP_DATA = keccak(b"CURVE_DUTCH_AUCTION_KEEPER_TEST_APP_DATA")
DOMAIN_SEPARATOR = keccak(b"GPV2_KEEPER_TEST_DOMAIN")


def timestamp() -> int:
    return boa.env.evm.patch.timestamp


class FakeOrderbook:
    """Collects posted orders; answers with the uid the real orderbook would."""

    def __init__(self, domain_separator: bytes):
        self.domain_separator = domain_separator
        self.posted: list[dict[str, Any]] = []
        self.app_data: dict[str, str] = {}

    def post_order(self, body: dict[str, Any]) -> str:
        from keepers.auction_keeper.rails.cow import GPv2Order

        self.posted.append(body)
        order = GPv2Order(
            sell_token=body["sellToken"],
            buy_token=body["buyToken"],
            receiver=body["receiver"],
            sell_amount=int(body["sellAmount"]),
            buy_amount=int(body["buyAmount"]),
            valid_to=body["validTo"],
            app_data=bytes.fromhex(body["appData"][2:]),
            fee_amount=int(body["feeAmount"]),
        )
        return order.uid(self.domain_separator, body["from"])

    def register_app_data(self, app_data_hash: bytes, document: str) -> None:
        self.app_data["0x" + app_data_hash.hex()] = document


@pytest.fixture(autouse=True)
def anchor():
    with boa.env.anchor():
        yield


@pytest.fixture(scope="module")
def owner():
    return boa.env.generate_address("owner")


@pytest.fixture(scope="module")
def keeper_account():
    return boa.env.generate_address("keeper")


@pytest.fixture(scope="module")
def relayer():
    return boa.env.generate_address("relayer")


@pytest.fixture(scope="module")
def proceeds_receiver():
    return boa.env.generate_address("proceeds_receiver")


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
def other_token(erc20_deployer):
    return erc20_deployer.deploy("Wrapped Ether", "WETH", 18)


@pytest.fixture(scope="module")
def role_source(owner):
    return boa.load(
        "contracts/testing/dutch_auction/RoleSourceMock.vy",
        owner,
        boa.env.generate_address("emergency_owner"),
    )


@pytest.fixture(scope="module")
def settlement(relayer):
    return boa.load("contracts/testing/dutch_auction/SettlementMock.vy", DOMAIN_SEPARATOR, relayer)


@pytest.fixture
def registry(role_source):
    return boa.load("contracts/burners/adapters/AdapterRegistry.vy", role_source.address)


@pytest.fixture
def cow_adapter(settlement):
    return boa.load("contracts/burners/adapters/cow/CowAdapter.vy", settlement, APP_DATA)


@pytest.fixture
def harness(want, proceeds_receiver, registry, role_source, cow_adapter, owner, relayer):
    # Deploy shortly after a week boundary: the harness places a lot's window
    # at its frame start within the staging week, so staging and short time
    # travels stay inside one frame.
    boa.env.time_travel(seconds=WEEK - timestamp() % WEEK + 3600)
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
        registry.set_adapter(cow_adapter, relayer)
        registry.activate_adapter(cow_adapter)
    # Lots staged in the configuration block are fenced: stage from the next second.
    boa.env.time_travel(seconds=1)
    return harness


@pytest.fixture
def stage(harness):
    def _stage(token, amount: int = LOT_AMOUNT) -> None:
        token._mint_for_testing(harness.address, amount)
        harness.stage(token.address)

    return _stage


@pytest.fixture
def lot(stage, sell_token):
    stage(sell_token)


@pytest.fixture
def network_config(harness):
    def _config(**overrides) -> NetworkConfig:
        fields = {
            "name": "boa",
            "rpc": "boa://",
            "auctions": (harness.address,),
            "tokens": TokenFilter(),
            "poll_interval": 30,
            "order_ttl": 300,
            # No market sources: the schedule is blind (uniform TTL) unless a
            # test passes its own pricer to the keeper.
            "pricing": PricingSettings(sources=()),
            "cow": CowSettings(),
        }
        fields.update(overrides)
        return NetworkConfig(**fields)

    return _config


@pytest.fixture
def state():
    return KeeperState(None)


@pytest.fixture
def orderbook():
    return FakeOrderbook(DOMAIN_SEPARATOR)


# The other rails: a fake HTTP layer and registry helpers; each rail's test
# module deploys its own settlement mocks and adapter.


class FakeHttp:
    """Records every request; answers from handlers matched on (method, url substring).

    A handler is a response payload or a callable ``(params, json) -> payload``;
    raising ``ApiError`` from it simulates a backend rejection.
    """

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.handlers: list[tuple[str, str, Any]] = []

    def on(self, method: str, url_part: str, response: Any) -> None:
        self.handlers.append((method, url_part, response))

    def request(self, method, url, *, params=None, json=None, headers=None, data=None):
        if data is not None and json is None:
            json = json_module.loads(data)
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "json": json,
                "headers": headers,
                "data": data,
            }
        )
        for handler_method, part, response in self.handlers:
            if handler_method == method and part in url:
                return response(params, json) if callable(response) else response
        raise AssertionError(f"unexpected {method} {url}")

    def requests(self, method: str, url_part: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == method and url_part in c["url"]]


@pytest.fixture
def http():
    return FakeHttp()


@pytest.fixture
def register(registry, owner):
    def _register(adapter, executor) -> None:
        with boa.env.prank(owner):
            registry.set_adapter(adapter, executor)
            registry.activate_adapter(adapter)

    return _register


@pytest.fixture
def rail_keeper(keeper_account, state, orderbook, http):
    """A keeper in keeper mode over every active adapter: CoW through the fake
    orderbook, every other rail through the fake HTTP layer."""

    def factory(cls, client, adapter, network):
        if cls is CowPublisher:
            return cls(client, adapter, network, orderbook=orderbook)
        return cls(client, adapter, network, http=http)

    def _make(config: NetworkConfig, *, dry_run: bool = False) -> NetworkKeeper:
        return NetworkKeeper(
            config,
            state,
            client=BoaClient(sender=keeper_account),
            dry_run=dry_run,
            publisher_factory=factory,
        )

    return _make


def by_adapter(outcomes) -> dict[str | None, str]:
    return {o.adapter: o.action for o in outcomes}
