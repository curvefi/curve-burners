"""Executor allowances are synced once per token, in batches."""

from eth_abi import decode, encode
from eth_utils import to_checksum_address

from keepers.auction_keeper.chain.abi import CallReverted
from keepers.auction_keeper.chain.contracts import Auction, Erc20
from keepers.auction_keeper.core.approvals import BATCH, ApprovalSync
from tests.keeper.pure.conftest import AUCTION, token_address

EXECUTOR = token_address(10**6 + 7)


class Chain:
    """Allowances of the auction to the executor, and the syncs sent."""

    sender = "0x" + "11" * 20

    def __init__(self, approved=(), broken=(), down=False):
        self.approved = set(approved)
        self.broken = set(broken)
        self.down = down
        self.reads = 0
        self.batches: list[list[str]] = []

    def call(self, to, data, *, sender=None, at=None):
        assert bytes(data[:4]) == Erc20.ALLOWANCE.selector
        self.reads += 1
        return encode(["uint256"], [2**256 - 1 if to in self.approved else 0])

    def send(self, to, data, **kwargs):
        assert to == AUCTION and bytes(data[:4]) == Auction.SYNC_EXECUTOR_APPROVALS.selector
        executor, tokens = decode(["address", "address[]"], bytes(data[4:]))
        assert to_checksum_address(executor) == EXECUTOR
        tokens = [to_checksum_address(token) for token in tokens]
        if self.down:
            raise RuntimeError("not included")
        if self.broken & set(tokens):
            raise CallReverted(b"")
        self.batches.append(tokens)
        self.approved.update(tokens)


def sync_of(chain, **kwargs):
    return ApprovalSync(chain, send=True, **kwargs), Auction(chain, AUCTION)


def test_every_lot_is_synced_once_in_batches_of_64():
    tokens = [token_address(i) for i in range(150)]
    chain = Chain(approved=tokens[:10])
    sync, auction = sync_of(chain)
    amounts = dict.fromkeys(tokens, 5)
    assert sync.sync(auction, EXECUTOR, amounts) == 140
    assert [len(batch) for batch in chain.batches] == [BATCH, BATCH, 12]
    assert chain.reads == 150
    # Never looked at again: no read, no send.
    assert sync.sync(auction, EXECUTOR, amounts) == 0
    assert chain.reads == 150 and len(chain.batches) == 3
    # A lot staged later is new, and is all the next pass touches.
    late = token_address(1000)
    assert sync.sync(auction, EXECUTOR, {**amounts, late: 5}) == 1
    assert chain.reads == 151 and chain.batches[-1] == [late]


def test_a_token_that_cannot_be_approved_is_singled_out_and_left():
    tokens = [token_address(i) for i in range(10)]
    chain = Chain(broken=[tokens[6]])
    sync, auction = sync_of(chain)
    amounts = dict.fromkeys(tokens, 5)
    assert sync.sync(auction, EXECUTOR, amounts) == 9
    assert sorted(sum(chain.batches, [])) == sorted(t for t in tokens if t != tokens[6])
    assert sync.blocked(AUCTION, EXECUTOR, tokens[6]) and not sync.blocked(
        AUCTION, EXECUTOR, tokens[5]
    )
    assert sync.sync(auction, EXECUTOR, amounts) == 0 and chain.reads == 10


def test_a_failed_send_is_tried_again_and_offline_only_reports(caplog):
    tokens = [token_address(i) for i in range(3)]
    amounts = dict.fromkeys(tokens, 5)
    chain = Chain(down=True)
    sync, auction = sync_of(chain)
    assert sync.sync(auction, EXECUTOR, amounts) == 0
    chain.down = False
    assert sync.sync(auction, EXECUTOR, amounts) == 3 and len(chain.batches) == 1

    for kwargs, why in (
        ({"send": False}, "offline mode"),
        ({"send": True, "dry_run": True}, "dry run"),
    ):
        idle = Chain()
        reporter = ApprovalSync(idle, **kwargs)
        with caplog.at_level("WARNING"):
            assert reporter.sync(auction.__class__(idle, AUCTION), EXECUTOR, amounts) == 0
        assert idle.batches == [] and why in caplog.text and tokens[0] in caplog.text
        assert reporter.sync(Auction(idle, AUCTION), EXECUTOR, amounts) == 0 and idle.reads == 3
