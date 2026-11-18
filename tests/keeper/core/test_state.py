"""KeeperState: the file round trip, damaged or foreign files, and concurrent writers."""

import json
import threading

from keepers.auction_keeper.core.state import KeeperState, OrderRecord

RECORD = OrderRecord(
    uid="0xabc", valid_to=10, sell_amount=1, buy_amount=2, posted_at=3, lot_start=4
)


def test_unreadable_file_starts_empty(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    state = KeeperState(path)
    assert state.orders() == {} and "unreadable" in caplog.text
    state.set_order("net", "0xA", "0xB", "0xC", RECORD)
    assert json.loads(path.read_text())["orders"]


def test_records_from_another_layout_are_read(tmp_path):
    path = tmp_path / "state.json"
    raw = {"orders": {"net:0xa:0xb:0xc": {**RECORD.__dict__, "extra": 1}}, "cursors": {}}
    path.write_text(json.dumps(raw))
    state = KeeperState(path)
    assert state.order("net", "0xA", "0xB", "0xC") == RECORD
    assert state.known_tokens("net", "0xA") == set()


def test_open_orders_by_adapter_and_expiry(tmp_path):
    state = KeeperState(None)
    state.set_order("net", "0xA", "0xB", "0xC", RECORD)
    state.set_order("net", "0xA", "0xB", "0xD", OrderRecord(**{**RECORD.__dict__, "valid_to": 5}))
    state.set_order("net", "0xA", "0xE", "0xC", RECORD)
    assert len(state.open_orders("net", "0xA", "0xB", 0)) == 2
    assert len(state.open_orders("net", "0xA", "0xB", 5)) == 1
    assert len(state.open_orders("net", "0xA", "0xb", 10)) == 0
    state.clear_order("net", "0xA", "0xB", "0xC")
    assert state.order("net", "0xA", "0xB", "0xC") is None


def test_concurrent_networks_share_one_file(tmp_path):
    """One process runs one thread per network over one state file: every
    write lands and the file stays valid JSON throughout."""
    path = tmp_path / "state.json"
    state = KeeperState(path)

    def writer(network):
        for i in range(25):
            state.set_order(network, "0xA", "0xB", f"0x{i:02x}", RECORD)
            state.record_scan(network, "0xA", i, {f"0x{i:02x}"})

    threads = [threading.Thread(target=writer, args=(f"net{n}",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    reloaded = KeeperState(path)
    assert len(reloaded.orders()) == 100
    assert all(reloaded.scan_cursor(f"net{n}", "0xA") == 24 for n in range(4))
    assert all(len(reloaded.known_tokens(f"net{n}", "0xA")) == 25 for n in range(4))
