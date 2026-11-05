"""Keeper configuration: a YAML file with one entry per network.

The minimum per network is an RPC URL and the auction addresses; everything
else has a default. ``${NAME}`` inside any string is expanded from the
environment (``~/.env`` is loaded first), so API keys stay out of the file.

.. code-block:: yaml

    poll_interval: 60       # seconds between node polls (default for every network)
    order_ttl: 300          # seconds a published order stays valid (uniform / fallback TTL)
    mode: offline           # offline: read + post only; keeper: also send on-chain syncs
    state_path: auction_keeper_state.json
    pricing:                # market estimate for the TTL schedule (per network override allowed)
      sources: [curve, llama, cow]  # priority order: first source with a price wins; [] = no market
                            # curve, llama, coingecko (USD feeds); cow, kyber, velora, oneinch (swap quotes)
      cache: 60             # seconds a lot's market estimate is reused
      cross_check: 0.25     # the winner must agree with the next source within 25%, else no market
    ttl:                    # validity by distance of the curve quote from the market
      near_above: 0.2       # band upper edge: quote at most 20% above the market
      near_below: 0.1       # band lower edge: quote at most 10% below the market
      min: 60               # validity inside the band (orderbook minimum)
      max: 7200             # longest validity far above the market
    max_open_orders: 8      # open orders per adapter at once (CoW caps outside-market orders at 10)
    native:                 # the native rail: take lots through the AuctionTaker with a Curve swap
      enabled: false        # needs auction_taker; sends only in mode keeper (offline: simulates)
      margin: 0.005         # take when the quote is this far under the Curve route quote
      max_impact: 0.01      # chunk the take so the route's price impact stays under this
      poll: 30              # poll this often while a lot is near its route quote
    networks:
      ethereum:
        rpc: https://eth-mainnet.g.alchemy.com/v2/${ALCHEMY_API_TOKEN}
        auctions: ["0x..."]
        start_block: 21000000        # LotStaged scan start for token discovery (optional)
        tokens:
          whitelist: []              # non-empty: sell only these
          blacklist: []              # never sell these
        mode: keeper                 # needs keeper_key_env
        keeper_key_env: KEEPER_PRIVATE_KEY   # env var with the key for syncs / takes
        pricing:
          sources: [cow, curve]      # this network prefers the orderbook's own quote
          chains: {curve: ethereum}  # feed chain slugs (default by chain id)
        cow:
          orderbook_url: https://api.cow.fi/mainnet   # optional: defaults by chain id
          app_data_document: '{"version":"1.1.0","appCode":"Curve","metadata":{}}'
        oneinch:                     # plain LOP, FeeTaker orderbook and Fusion publishers
          api_key_env: ONEINCH_API_KEY
          enabled: true
          post: false                # plain orders: log only (the public orderbook rejects them)

Every other settlement rail gets a section named after it (``oneinch``,
``bebop``, ...) parsed by ``parse_rail`` (``config/rails.py``) into a
``RailSettings`` subclass: an ``api_key_env``, an ``enabled`` switch and
``post`` (feed or log only) plus the rail's own fields.

The package: ``core`` (network/keeper config, loading), ``common`` (the error
type and primitive validators), ``pricing`` (``pricing`` / ``ttl``),
``native`` (the native rail) and ``rails`` (``cow`` and the rail sections).
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
from keepers.auction_keeper.config.native import NativeSettings
from keepers.auction_keeper.config.pricing import (
    DEFAULT_PRICE_CACHE,
    DEFAULT_PRICE_CROSS_CHECK,
    DEFAULT_PRICE_SOURCES,
    PRICE_SOURCES,
    PricingSettings,
    TtlSettings,
)
from keepers.auction_keeper.config.rails import CowSettings, OneInchSettings, RailSettings

__all__ = [
    "DEFAULT_LOG_CHUNK",
    "DEFAULT_MAX_OPEN_ORDERS",
    "DEFAULT_MODE",
    "DEFAULT_ORDER_TTL",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_PRICE_CACHE",
    "DEFAULT_PRICE_CROSS_CHECK",
    "DEFAULT_PRICE_SOURCES",
    "DEFAULT_STATE_PATH",
    "MODES",
    "PRICE_SOURCES",
    "ConfigError",
    "CowSettings",
    "KeeperConfig",
    "NativeSettings",
    "NetworkConfig",
    "PricingSettings",
    "RailSettings",
    "OneInchSettings",
    "TokenFilter",
    "TtlSettings",
    "expand_env",
    "load_config",
    "parse_config",
    "parse_network",
]
