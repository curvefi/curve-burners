"""Request budgets, the node's request count and the log's size."""

import logging
import threading

import pytest

from keepers.auction_keeper.config import parse_config
from keepers.auction_keeper.limits import GATES, Gates, HostGate, RpcMeter, Throttled
from keepers.auction_keeper.logs import LogSettings, RepeatFilter, setup
from keepers.auction_keeper.pricing import MarketPricer
from keepers.auction_keeper.rails.common import ApiError, JsonClient
from tests.keeper.pure.conftest import AUCTION, token_address


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_a_host_gets_its_rate_and_no_more():
    clock = Clock()
    gate = HostGate("api", 20, clock=clock)  # 20 a minute, 5 at once
    for _ in range(5):
        gate.take()
    with pytest.raises(Throttled, match="budget spent") as held:
        gate.take()
    assert 0 < held.value.wait <= 3
    clock.now += 3  # one request's worth
    gate.take()
    with pytest.raises(Throttled):
        gate.take()
    assert (gate.sent, gate.held) == (6, 2)
    clock.now += 3600  # an idle hour is still only one burst
    for _ in range(5):
        gate.take()
    with pytest.raises(Throttled):
        gate.take()
    unlimited = HostGate("free", 0, clock=clock)
    for _ in range(1000):
        unlimited.take()


def test_quotes_leave_half_the_budget_to_orders():
    clock = Clock()
    gate = HostGate("api.cow.fi", 40, clock=clock)  # 10 at once
    for _ in range(5):
        gate.take(reserve=0.5)
    with pytest.raises(Throttled):
        gate.take(reserve=0.5)  # a quote stops at half
    for _ in range(5):
        gate.take()  # an order still goes out
    with pytest.raises(Throttled):
        gate.take()


def test_a_refusing_host_is_left_alone_longer_each_time(caplog):
    clock = Clock()
    gate = HostGate("api", 0, cooldown=600, clock=clock)
    gate.take()
    with caplog.at_level("WARNING"):
        gate.answered(403)
    assert "no requests to it for 600s" in caplog.text
    with pytest.raises(Throttled, match="left alone"):
        gate.take()
    clock.now += 601
    gate.take()
    gate.answered(429)  # again, straight after the pause: twice as long
    clock.now += 601
    with pytest.raises(Throttled):
        gate.take()
    clock.now += 600
    gate.take()
    gate.answered(200)  # an answer resets the count
    gate.answered(403)
    clock.now += 601
    gate.take()
    gate.answered(404)  # not a refusal of the client
    gate.take()


class Session:
    def __init__(self, status=200):
        self.status, self.calls = status, 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        return type("R", (), {"status_code": self.status, "content": b"{}", "json": lambda s: {}})()


def test_the_json_client_asks_the_gate_first(monkeypatch):
    gates = Gates()
    gates.configure(default=8, hosts={"slow.example": 4}, cooldown=60)
    monkeypatch.setattr("keepers.auction_keeper.rails.common.GATES", gates)
    session = Session()
    client = JsonClient(session=session)
    client.request("GET", "https://slow.example/a")
    with pytest.raises(Throttled):
        client.request("GET", "https://slow.example/b")  # 4 a minute: one at once
    assert session.calls == 1  # the second request never left
    client.request("GET", "https://other.example/a")
    client.request("GET", "https://other.example/b")
    assert gates.counts() == {"slow.example": (1, 1), "other.example": (2, 0)}
    # A ban is not hammered: the 403 is reported once, then nothing is sent.
    banned = JsonClient(session=Session(403))
    with pytest.raises(ApiError):
        banned.request("GET", "https://banned.example/a")
    with pytest.raises(Throttled):
        banned.request("GET", "https://banned.example/a")
    assert banned.session.calls == 1
    assert GATES.of("https://api.cow.fi/mainnet/api/v1/quote").per_minute == 20


class Source:
    def __init__(self, name, answer):
        self.name, self.answer, self.calls = name, answer, 0

    def quote(self, lot):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_a_throttled_source_keeps_its_level_and_is_the_only_one_asked_again():
    clock = Clock()
    feed, cow = Source("feed", 100), Source("cow", Throttled("api.cow.fi", 5, "budget spent"))
    pricer = MarketPricer([feed, cow], clock=clock)
    token = token_address(1)
    lot = type("Lot", (), {"auction": AUCTION, "token": token, "available": 10})()
    quote = pricer.refresh(lot, fresh=60)
    assert quote.levels == (("feed", 100),)
    # Not everyone has answered: the lot still counts as never asked about.
    assert pricer.age(AUCTION, token) is None
    clock.now += 12
    cow.answer = 90
    quote = pricer.refresh(lot, fresh=60)
    assert quote.levels == (("feed", 100), ("cow", 90))
    assert (feed.calls, cow.calls) == (1, 2)  # the feed was not asked twice
    assert pricer.age(AUCTION, token) == 0
    # Later the orderbook is out of budget again: its level stays.
    clock.now += 100
    cow.answer = Throttled("api.cow.fi", 5, "budget spent")
    feed.answer = 110
    quote = pricer.refresh(lot, fresh=60)
    assert quote.levels == (("feed", 110), ("cow", 90))
    assert pricer.age(AUCTION, token) == 100


def test_the_rpc_meter_counts_and_spaces_requests():
    clock, slept = Clock(), []

    def sleep(seconds):
        slept.append(round(seconds, 3))
        clock.now += seconds

    meter = RpcMeter(per_second=10, clock=clock, sleep=sleep)
    for method in ("eth_call", "eth_call", "eth_getLogs"):
        meter.note(method)
    assert meter.counts == {"eth_call": 2, "eth_getLogs": 1} and meter.total() == 3
    assert slept == [0.1, 0.1]
    free = RpcMeter()
    free.note("eth_call")
    assert free.total() == 1


def record(text, name="keeper", level=logging.INFO):
    return logging.LogRecord(name, level, __file__, 1, text, None, None)


def test_a_repeated_line_is_written_once_per_window():
    clock = Clock()
    repeat = RepeatFilter(300, clock=clock)
    assert repeat.filter(record("NEAREST "))
    for _ in range(100_000):  # a loop gone wrong
        clock.now += 0.001
        assert not repeat.filter(record("NEAREST "))
    assert repeat.filter(record("posted 0xabc"))  # other lines are not held up
    assert repeat.filter(record("NEAREST ", level=logging.WARNING))
    clock.now += 300
    again = record("NEAREST ")
    assert repeat.filter(again)
    assert again.getMessage().startswith("NEAREST  (and 100000 more like it in the last 4")
    assert RepeatFilter(0).filter(record("x")) and RepeatFilter(0).filter(record("x"))


def test_the_log_file_has_a_size_limit(tmp_path):
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    path = tmp_path / "keeper.log"
    try:
        handler = setup(LogSettings(file=str(path), max_mb=1, backups=2, repeat=0))
        log = logging.getLogger("keeper.test")
        for index in range(60_000):
            log.info("line %06d %s", index, "x" * 80)
        handler.close()
    finally:
        for added in list(root.handlers):
            root.removeHandler(added)
        for old in before:
            root.addHandler(old)
        root.setLevel(level)
    files = sorted(tmp_path.iterdir())
    assert [f.name for f in files] == ["keeper.log", "keeper.log.1", "keeper.log.2"]
    assert sum(f.stat().st_size for f in files) <= 3 * 1024 * 1024


def test_limits_log_and_several_keys_parse(monkeypatch):
    raw = {
        "limits": {"http": 30, "hosts": {"api.cow.fi": 10}, "cooldown": 900, "rpc": 20},
        "log": {"file": "keeper.log", "max_mb": 5, "backups": 1, "repeat": 60},
        "keeper_key_env": ["KEY_A", "KEY_B"],
        "mode": "keeper",
        "networks": {
            "a": {"rpc": "x", "auctions": [AUCTION]},
            "b": {"rpc": "x", "auctions": [AUCTION], "keeper_key_env": "KEY_C", "rpc_rate": 5},
        },
    }
    config = parse_config(raw)
    assert (config.limits.http, config.limits.hosts, config.limits.cooldown) == (
        30,
        {"api.cow.fi": 10},
        900,
    )
    assert config.log == LogSettings(file="keeper.log", max_mb=5, backups=1, repeat=60)
    a, b = config.network("a"), config.network("b")
    assert (a.rpc_rate, b.rpc_rate) == (20, 5)
    assert (a.keeper_key_env, a.keeper_key_envs) == ("KEY_A", ("KEY_A", "KEY_B"))
    monkeypatch.setenv("KEY_A", "0x" + "01" * 32)
    monkeypatch.setenv("KEY_C", "0x" + "03" * 32)
    monkeypatch.delenv("KEY_B", raising=False)
    with pytest.raises(ValueError, match="KEY_B"):
        a.private_keys  # noqa: B018 - a lane silently missing is an error
    monkeypatch.setenv("KEY_B", "0x" + "02" * 32)
    assert a.private_keys == ["0x" + "01" * 32, "0x" + "02" * 32]
    assert b.private_keys == ["0x" + "03" * 32] and b.private_key == "0x" + "03" * 32
    for bad in (
        {"limits": {"http": -1}},
        {"limits": {"burst": 3}},
        {"log": {"max_mb": 0}},
        {"keeper_key_env": ["KEY_A", "KEY_A"]},
        {"keeper_key_env": []},
    ):
        with pytest.raises(ValueError):
            parse_config({**raw, **bad})


def test_gates_are_safe_from_many_threads():
    gate = HostGate("api", 0)
    threads = [threading.Thread(target=lambda: [gate.take() for _ in range(500)]) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert gate.sent == 4000
