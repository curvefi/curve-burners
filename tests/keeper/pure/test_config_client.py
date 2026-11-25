"""The new config keys, and the client's next-block calls and private sends."""

from types import SimpleNamespace

import pytest
from eth_utils import keccak

from keepers.auction_keeper.chain import client as client_module
from keepers.auction_keeper.chain.abi import CallReverted
from keepers.auction_keeper.chain.client import NotIncluded, Web3Client, submit_raw
from keepers.auction_keeper.config import (
    BLOCK_TIMES,
    ConfigError,
    FocusSettings,
    LookaheadSettings,
    parse_config,
)

AUCTION = "0x" + "11" * 20
FLASHBOTS = "https://rpc.flashbots.net/fast"
MEV_BLOCKER = "https://rpc.mevblocker.io/fullprivacy"


def network(**settings):
    raw = {"networks": {"eth": {"rpc": "https://node", "auctions": [AUCTION], **settings}}}
    return parse_config(raw).network("eth")


def test_defaults_keep_a_big_network_cheap():
    cfg = network()
    assert cfg.focus == FocusSettings(
        threshold=0.25, nearest=5, blind=2, max_lots=40, confirm=60, audit=2
    )
    assert cfg.lookahead == LookaheadSettings(blocks=1, seconds=None, orders=False)
    assert (cfg.pricing.cache, cfg.pricing.cache_far) == (60, 3600)
    assert (cfg.pricing.budget, cfg.pricing.workers, cfg.approvals) == (5.0, 4, "startup")
    assert (cfg.native.requote, cfg.native.receipt_timeout) == (300, 60)
    assert cfg.send_rpc == () and cfg.receipt_timeout == 300 and cfg.ttl.every_step is False


def test_focus_and_lookahead_sections_parse_and_inherit():
    raw = {
        "focus": {"threshold": 0.1, "nearest": 12},
        "lookahead": {"blocks": 2},
        "pricing": {"cache_far": 7200, "budget": 0, "workers": 8},
        "approvals": "lazy",
        "networks": {
            "a": {"rpc": "x", "auctions": [AUCTION]},
            "b": {
                "rpc": "x",
                "auctions": [AUCTION],
                "focus": {"max_lots": 9, "audit": 0, "blind": 0, "confirm": 15},
                "lookahead": {"seconds": 3, "orders": True},
                "ttl": {"every_step": True},
                "native": {"requote": 120, "receipt_timeout": 45},
                "receipt_timeout": 90,
                "approvals": False,  # YAML's reading of a bare `off`
            },
        },
    }
    config = parse_config(raw)
    a, b = config.network("a"), config.network("b")
    assert (a.focus.threshold, a.focus.nearest, a.focus.max_lots) == (0.1, 12, 40)
    assert b.focus == FocusSettings(
        threshold=0.1, nearest=12, blind=0, max_lots=9, confirm=15, audit=0
    )
    assert a.lookahead.ahead(1) == 2 * BLOCK_TIMES[1] == 24 and a.lookahead.ahead(42161) == 2
    assert b.lookahead.ahead(1) == 3 and b.lookahead.orders is True
    assert (a.pricing.cache_far, a.pricing.budget) == (7200, 0.0)
    assert (a.approvals, b.approvals) == ("lazy", "off")
    assert b.ttl.every_step and (b.native.requote, b.native.receipt_timeout) == (120, 45)
    assert (a.receipt_timeout, b.receipt_timeout) == (300, 90)


@pytest.mark.parametrize(
    "section, value",
    [
        ("focus", {"nearest": -1}),
        ("focus", {"max_lots": 0}),
        ("focus", {"threshold": "close"}),
        ("focus", {"nearby": 3}),
        ("lookahead", {"blocks": 1.5}),
        ("lookahead", {"orders": "yes"}),
        ("lookahead", {"second": 1}),
        ("pricing", {"budget": -1}),
        ("pricing", {"cache_far": 0}),
        ("ttl", {"every_step": 1}),
    ],
)
def test_bad_loop_settings_are_rejected(section, value):
    with pytest.raises(ConfigError):
        network(**{section: value})


def test_send_rpc_is_a_list_of_private_endpoints(monkeypatch):
    assert network(send_rpc=FLASHBOTS).send_rpc == (FLASHBOTS,)
    assert network(send_rpc=[FLASHBOTS, MEV_BLOCKER]).send_rpc == (FLASHBOTS, MEV_BLOCKER)
    assert network(send_rpc=[]).send_rpc == ()
    monkeypatch.setenv("RELAY_KEY", "s3cret")
    assert network(send_rpc=["https://relay/${RELAY_KEY}"]).send_rpc == ("https://relay/s3cret",)
    for bad in (5, [FLASHBOTS, 7], [""], [FLASHBOTS, FLASHBOTS], {"url": FLASHBOTS}):
        with pytest.raises(ConfigError, match="send_rpc"):
            network(send_rpc=bad)


def test_one_acceptance_is_enough_and_the_hash_is_local(caplog):
    raw = b"\x02signed"
    answers = {"https://a/key": "rate limited", "https://b": None, "https://c": "timed out"}
    posted = []

    def post(url, raw_hex, timeout):
        posted.append((url, raw_hex, timeout))
        return answers[url]

    with caplog.at_level("WARNING"):
        assert submit_raw(list(answers), raw, 3.0, post=post) == keccak(raw)
    assert sorted(posted) == sorted((url, "0x" + raw.hex(), 3.0) for url in answers)
    # The refusals are logged by host only: a path may carry a key.
    assert "a refused" in caplog.text and "key" not in caplog.text
    with pytest.raises(RuntimeError, match="no send endpoint accepted"):
        submit_raw(["https://a/key", "https://c"], raw, 3.0, post=post)


def test_an_endpoint_that_already_has_the_transaction_counts(monkeypatch):
    replies = iter(
        [
            {"error": {"code": -32000, "message": "already known"}},
            {"error": {"code": -32000, "message": "nonce too low"}},
            {"result": "0xabc"},
        ]
    )
    monkeypatch.setattr(
        "requests.post", lambda url, json, timeout: SimpleNamespace(json=lambda: next(replies))
    )
    assert client_module._post_raw("https://x", "0x01", 1.0) is None
    assert client_module._post_raw("https://x", "0x01", 1.0) == "nonce too low"
    assert client_module._post_raw("https://x", "0x01", 1.0) is None


class Provider:
    """Answers eth_call by mode; refuses the modes listed in ``refuses``."""

    def __init__(self, refuses=(), revert=None):
        self.refuses, self.revert, self.requests = set(refuses), revert, []

    def make_request(self, method, params):
        self.requests.append((method, params))
        mode = "override" if len(params) == 4 else params[1]
        if mode in self.refuses:
            return {"error": {"code": -32602, "message": "too many arguments"}}
        if self.revert is not None:
            return {"error": {"code": 3, "message": "execution reverted", "data": self.revert}}
        return {"result": "0x" + {"override": "01", "pending": "02", "latest": "03"}[mode]}


def bare_client(provider):
    client = object.__new__(Web3Client)
    client.w3 = SimpleNamespace(provider=provider)
    client._at_mode = {}
    return client


def test_next_block_calls_use_a_timestamp_override_and_fall_back():
    provider = Provider()
    client = bare_client(provider)
    assert client.call("0x" + "22" * 20, b"\xaa", at=1_800_000_000) == b"\x01"
    method, params = provider.requests[0]
    assert method == "eth_call" and params[1:] == ["latest", {}, {"time": hex(1_800_000_000)}]
    assert params[0]["data"] == "0xaa"

    # An endpoint without block overrides: the pending block, remembered.
    provider = Provider(refuses={"override"})
    client = bare_client(provider)
    assert client.call("0x" + "22" * 20, b"", at=5) == b"\x02"
    assert client.call("0x" + "22" * 20, b"", at=6) == b"\x02"
    assert [len(params) for _, params in provider.requests] == [4, 2, 2]

    # Neither: the latest block, as before the lookahead.
    client = bare_client(Provider(refuses={"override", "pending"}))
    assert client.call("0x" + "22" * 20, b"", at=5) == b"\x03"


def test_a_revert_at_the_next_block_is_a_revert_not_a_fallback():
    selector = keccak(text="ProfitShortfall()")[:4]
    provider = Provider(revert="0x" + selector.hex())
    client = bare_client(provider)
    with pytest.raises(CallReverted, match="ProfitShortfall"):
        client.call("0x" + "22" * 20, b"", at=5)
    assert len(provider.requests) == 1 and client._at_mode == {}


class Eth:
    """The node as ``Web3Client.send`` sees it."""

    def __init__(self):
        self.pending = 7  # the public pending count
        self.mined = 7
        self.receipts = {}
        self.sent = []
        self.gas_price = 10**9
        self.max_priority_fee = 10**8

    def get_transaction_count(self, address, tag):
        return self.pending if tag == "pending" else self.mined

    def estimate_gas(self, tx):
        return 100_000

    def get_block(self, tag):
        return {"baseFeePerGas": 10**9}

    def get_transaction_receipt(self, tx_hash):
        from web3.exceptions import TransactionNotFound

        if bytes(tx_hash) not in self.receipts:
            raise TransactionNotFound("not found")
        return self.receipts[bytes(tx_hash)]


@pytest.fixture
def sending(monkeypatch):
    from eth_account import Account

    eth = Eth()
    client = bare_client(Provider())
    client.w3.eth = eth
    client._set_accounts([Account.from_key("0x" + "01" * 32)])
    client._chain_id = 1
    client.gas_margin = 1.25
    client.receipt_timeout = 0.0
    client.receipt_poll = 0.0
    client.send_rpc = (FLASHBOTS, MEV_BLOCKER)
    client.send_timeout = 2.0
    included = {"next": True}

    def submit(urls, raw, timeout):
        assert urls == (FLASHBOTS, MEV_BLOCKER)
        tx_hash = keccak(bytes(raw))
        eth.sent.append(raw)
        if included["next"]:
            eth.receipts[tx_hash] = {"blockNumber": 5, "gasUsed": 90_000, "status": 1}
            eth.mined += 1
        return tx_hash

    monkeypatch.setattr(client_module, "submit_raw", submit)
    return client, eth, included


def signed(raw) -> dict:
    from eth_account.typed_transactions import TypedTransaction
    from hexbytes import HexBytes

    return TypedTransaction.from_bytes(HexBytes(bytes(raw))).as_dict()


def nonce_of(raw) -> int:
    return signed(raw)["nonce"]


def test_private_sends_keep_their_own_nonce_count(sending):
    client, eth, included = sending
    to = "0x" + "22" * 20
    assert client.send(to, b"\x01").ok
    # The public node never saw the private transaction: its pending count
    # still says 7, and the next send must not reuse that nonce.
    assert client.send(to, b"\x02").ok
    assert [nonce_of(raw) for raw in eth.sent] == [7, 8]

    # A relay does not include a take that would revert: no receipt, reported
    # at once (receipt_timeout), and its nonce goes to the next transaction.
    included["next"] = False
    with pytest.raises(NotIncluded, match="no receipt"):
        client.send(to, b"\x03", receipt_timeout=0)
    included["next"] = True
    assert client.send(to, b"\x04").ok
    assert [nonce_of(raw) for raw in eth.sent] == [7, 8, 9, 9]


def test_a_nonce_used_by_another_transaction_ends_the_wait(sending):
    client, eth, included = sending
    included["next"] = False
    eth.mined = 8  # the account's nonce 7 was mined, but not with this hash
    with pytest.raises(NotIncluded, match="nonce was used"):
        client.send("0x" + "22" * 20, b"\x01", receipt_timeout=600)
    assert client._lanes[0].nonce_floor == 8


def test_the_gas_estimate_of_a_take_runs_with_the_next_block_clock(sending):
    client, eth, _ = sending
    provider = client.w3.provider
    provider.make_request = lambda method, params: (
        provider.requests.append((method, params)) or {"result": hex(200_000)}
    )
    client.send("0x" + "22" * 20, b"\x01", at=1_800_000_012)
    method, params = provider.requests[-1]
    assert method == "eth_estimateGas" and params[3] == {"time": hex(1_800_000_012)}
    assert signed(eth.sent[-1])["gas"] == 250_000


def test_several_accounts_send_at_once_and_take_turns(sending):
    import threading

    from eth_account import Account

    client, eth, _ = sending
    accounts = [Account.from_key("0x" + f"{i:02x}" * 32) for i in (1, 2, 3)]
    client._set_accounts(accounts)
    assert client.sender == accounts[0].address
    assert client.senders == [a.address for a in accounts]
    to = "0x" + "22" * 20
    for payload in (b"\x01", b"\x02", b"\x03", b"\x04"):
        assert client.send(to, payload).ok
    senders = [Account.recover_transaction(raw) for raw in eth.sent]
    # They rotate: the account that waited longest goes next.
    assert senders == [a.address for a in (*accounts, accounts[0])]
    # Each account counts its own nonces.
    assert [lane.nonce_floor for lane in client._lanes] == [9, 8, 8]

    # While every account has a transaction in flight, the next send waits.
    gate, entered = threading.Event(), threading.Semaphore(0)
    wait = client._wait_receipt

    def held(lane, tx_hash, nonce, timeout):
        entered.release()
        gate.wait(5)
        return wait(lane, tx_hash, nonce, timeout)

    client._wait_receipt = held
    threads = [threading.Thread(target=client.send, args=(to, b"\x05")) for _ in range(4)]
    for thread in threads:
        thread.start()
    for _ in range(3):
        assert entered.acquire(timeout=5)
    assert not entered.acquire(timeout=0.2)  # the fourth has no account yet
    gate.set()
    for thread in threads:
        thread.join(5)
    assert len(eth.sent) == 8

    with pytest.raises(ValueError, match="twice"):
        client._set_accounts([accounts[0], accounts[0]])
