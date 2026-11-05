"""Dutch auction keeper: publishes lots to settlement adapters and runs custom takes.

Publishing (``python -m keepers.auction_keeper run --config config.yaml``) polls every
configured auction, finds live lots, and posts signed orders to every active
registry adapter it has a publisher for (CoW today). Custom executions go
through :class:`Executor` (see ``keepers.auction_keeper.execute``).
"""

from keepers.auction_keeper.chain.abi import MAX_UINT256, WAD, CallReverted, describe_revert
from keepers.auction_keeper.chain.client import ChainClient, TxResult, Web3Client
from keepers.auction_keeper.chain.contracts import (
    AdapterRegistry,
    Auction,
    AuctionTaker,
    CowAdapterContract,
    Erc20,
    LotView,
)
from keepers.auction_keeper.config import KeeperConfig, NetworkConfig, TokenFilter, load_config
from keepers.auction_keeper.core.keeper import NetworkKeeper, Outcome, needs_publish, run_networks
from keepers.auction_keeper.core.state import KeeperState, OrderRecord
from keepers.auction_keeper.execute import (
    Call,
    Executor,
    LotEnded,
    TakePlan,
    WaitTimeout,
    approve_call,
    connect,
    contract_call,
    curve_router_exchange,
    raw_call,
)
from keepers.auction_keeper.rails import AdapterPublisher, PublishResult
from keepers.auction_keeper.rails.cow import CowPublisher, GPv2Order, Orderbook

__all__ = [
    "MAX_UINT256",
    "WAD",
    "AdapterPublisher",
    "AdapterRegistry",
    "Auction",
    "AuctionTaker",
    "Call",
    "CallReverted",
    "ChainClient",
    "CowAdapterContract",
    "CowPublisher",
    "Erc20",
    "Executor",
    "GPv2Order",
    "KeeperConfig",
    "KeeperState",
    "LotEnded",
    "LotView",
    "NetworkConfig",
    "NetworkKeeper",
    "OrderRecord",
    "Orderbook",
    "Outcome",
    "PublishResult",
    "TakePlan",
    "TokenFilter",
    "TxResult",
    "WaitTimeout",
    "Web3Client",
    "approve_call",
    "connect",
    "contract_call",
    "curve_router_exchange",
    "describe_revert",
    "load_config",
    "needs_publish",
    "raw_call",
    "run_networks",
]
