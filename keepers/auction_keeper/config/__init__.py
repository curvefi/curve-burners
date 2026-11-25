"""Keeper configuration: a YAML file with one entry per network.

The minimum per network is an RPC URL and the auction addresses; everything
else has a default. ``${NAME}`` inside any string is expanded from the
environment (``~/.env`` is loaded first), so API keys stay out of the file.

.. code-block:: yaml

    poll_interval: 60       # longest wait between cycles (default for every network)
    order_ttl: 300          # seconds a published order stays valid (uniform / fallback TTL)
    mode: offline           # offline: read + post only; keeper: also send on-chain syncs
    approvals: startup      # executor allowances: startup (all lots once, in batches), lazy (per order), off
    state_path: auction_keeper_state.json
    pricing:                # market estimate for the TTL schedule (per network override allowed)
      sources: [curve, llama, cow]  # all are asked, each answer is a price level of its own; [] = no market
                            # curve, llama, coingecko (USD feeds); cow, kyber, velora, oneinch (swap quotes)
      cache: 60             # seconds an estimate is reused near the market
      cache_far: 3600       # ... and far above it
      budget: 5             # seconds a cycle may spend refreshing estimates
    ttl:                    # validity by distance of the curve quote from each source's price
      near_above: 0.05      # band upper edge: quote at most 5% above a price
      near_below: 0.03      # band lower edge: quote at most 3% below a price
      min: 60               # validity inside a band (orderbook minimum)
      max: 7200             # longest validity outside the bands
      every_step: false     # inside the band, repost at every curve step
    focus:                  # the lots a cycle works on; all are ranked locally for free
      threshold: 0.25       # every lot whose ask is within 25% above its highest estimate
      nearest: 5            # plus the nearest of the rest
      blind: 2              # plus unpriced lots, in rotation
      max_lots: 40
      confirm: 60           # seconds an `available` read is reused
      audit: 2              # lots read back from the chain per cycle
    lookahead:              # price lots for the block a take lands in
      blocks: 1             # x the chain's block time; or `seconds`
      orders: false         # also price adapter orders ahead
    fees:                   # the tip of a take: a share of its profit
      priority_floor: 0     # gwei
      priority_cap: 5       # gwei
      profit_share: 0.5
    max_open_orders: 8      # open orders per adapter at once (CoW caps outside-market orders at 10)
    limits:                 # request budgets, for the whole process
      http: 60              # requests per minute to one HTTP host (hosts: {api.cow.fi: 20})
      cooldown: 600         # seconds a host is left alone after a 403 / 429
      rpc: 0                # requests per second to a node (0: no limit)
    log:                    # file (rotated at max_mb, `backups` kept), repeat (same line held back, seconds)
      max_mb: 20
    dust: 0.01              # lots the market values under this many want tokens are left alone (0: off)
    native:                 # the native rail: take lots through the AuctionTaker with a Curve swap
      enabled: false        # needs auction_taker; sends only in mode keeper (offline: simulates)
      backends: [curve]     # routers: curve (router NG), electric (michwill/electric-router); the best quote wins
      margin: 0.005         # take when the quote is this far under the Curve route quote
      max_impact: 0.01      # chunk the take so the route's price impact stays under this
      requote: 300          # seconds between route quotes of a lot away from its route quote
      receipt_timeout: 60   # seconds to wait for a take's receipt
      min_profit: 0         # want tokens a take must earn after its gas
      gas_refill: true      # a take buys its own gas back in the native token, in the same transaction
      refill_below: 0       # native balance under which an account also gets a top-up ...
      refill_amount: 0      # ... of this many native tokens' worth of a take's profit (0: off)
    networks:
      ethereum:
        rpc: https://eth-mainnet.g.alchemy.com/v2/${ALCHEMY_API_TOKEN}
        send_rpc: [https://rpc.flashbots.net/fast]   # private endpoints for transactions (optional)
        auctions: ["0x..."]
        start_block: 21000000        # where the first scan of the auction's logs starts (optional)
        tokens:
          whitelist: []              # non-empty: sell only these
          blacklist: []              # never sell these
        mode: keeper                 # needs keeper_key_env
        keeper_key_env: KEEPER_PRIVATE_KEY   # env var with the key for syncs / takes; a list: several accounts
        pricing:
          sources: [cow, curve]      # this network prefers the orderbook's own quote
          chains: {curve: ethereum}  # feed chain slugs (default by chain id)
        cow:
          orderbook_url: https://api.cow.fi/mainnet   # optional: defaults by chain id
          app_data_document: '{"version":"1.1.0","appCode":"Curve","metadata":{}}'

Every other settlement rail gets a section named after it (``oneinch``,
``bebop``, ...) parsed by ``parse_rail`` (``config/rails.py``) into a
``RailSettings`` subclass: an ``api_key_env``, an ``enabled`` switch and
``post`` (feed or log only) plus the rail's own fields.

The package: ``core`` (network/keeper config, loading), ``common`` (the error
type and primitive validators), ``pricing`` (``pricing`` / ``ttl``),
``loop`` (``focus`` / ``lookahead`` / ``fees``), ``native`` (the native rail)
and ``rails`` (``cow`` and the rail sections).
"""

from keepers.auction_keeper.config.common import ConfigError
from keepers.auction_keeper.config.core import (
    DEFAULT_LOG_CHUNK,
    DEFAULT_MAX_OPEN_ORDERS,
    DEFAULT_MODE,
    DEFAULT_ORDER_TTL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_STATE_PATH,
    MODES,
    KeeperConfig,
    NetworkConfig,
    TokenFilter,
    expand_env,
    load_config,
    parse_config,
    parse_network,
)
from keepers.auction_keeper.config.loop import (
    BLOCK_TIMES,
    FeeSettings,
    FocusSettings,
    LimitSettings,
    LogSettings,
    LookaheadSettings,
)
from keepers.auction_keeper.config.native import ElectricSettings, NativeSettings
from keepers.auction_keeper.config.pricing import (
    DEFAULT_PRICE_CACHE,
    DEFAULT_PRICE_SOURCES,
    PRICE_SOURCES,
    PricingSettings,
    TtlSettings,
)
from keepers.auction_keeper.config.rails import CowSettings, RailSettings

__all__ = [
    "DEFAULT_LOG_CHUNK",
    "DEFAULT_MAX_OPEN_ORDERS",
    "DEFAULT_MODE",
    "DEFAULT_ORDER_TTL",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_PRICE_CACHE",
    "DEFAULT_PRICE_SOURCES",
    "DEFAULT_STATE_PATH",
    "MODES",
    "PRICE_SOURCES",
    "BLOCK_TIMES",
    "ConfigError",
    "CowSettings",
    "FeeSettings",
    "FocusSettings",
    "LimitSettings",
    "LogSettings",
    "LookaheadSettings",
    "KeeperConfig",
    "ElectricSettings",
    "NativeSettings",
    "NetworkConfig",
    "PricingSettings",
    "RailSettings",
    "TokenFilter",
    "TtlSettings",
    "expand_env",
    "load_config",
    "parse_config",
    "parse_network",
]
