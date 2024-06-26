import typing as tp
import time
from abc import abstractmethod
# from cachetools import TTLCache  coingecko

from utils import Cached, Registrar, prune_config
from data.curve_api import CurveAPIData


class PriceSource(Registrar):
    def __init__(self, config: dict):
        pass

    @abstractmethod
    def get_price(self, coin: str) -> float:
        pass

    @abstractmethod
    def get_amount(self, coin: str, amount: int) -> tp.Union[int, float]:
        pass


class IdPriceSource(PriceSource):
    def get_price(self, coin: str) -> float:
        return 1.

    def get_amount(self, coin: str, amount: int) -> tp.Union[float, int]:
        return amount


class CurveAPIPrices(PriceSource, Cached, CurveAPIData):
    def __init__(self, config):
        super().__init__(config)
        config = prune_config(config, self.__class__)
        self.chain = config["chain"]
        self.ttl = config["ttl"]
        self.last_fetch_ts = 0
        self.prices: dict[str, float] = {}
        self.decimals: dict[str, int] = {}
        self.load_cache()

    def __getstate__(self):
        return {
            self.chain.name: {
                "prices": dict(self.cache),
                "decimals": self.decimals,
            },
        }

    def __setstate__(self, state):
        self.decimals = state.get(self.chain.name, {}).get("decimals", {})
        self.cache = {}  # prices are outdated

    def _fetch_prices(self):
        new_prices = {}
        self.last_fetch_ts = time.time()  # Before in case requests will halt
        for _, pool_dict in self.iterate_over_all_pool_data(self.chain):
            for coin in pool_dict["coins"]:
                coin_address = coin["address"].lower()
                new_prices[coin_address] = coin["usdPrice"] or 0
                if coin_address not in self.decimals:
                    self.decimals[coin_address] = int(coin["decimals"])

            lp_address = pool_dict["lpTokenAddress"].lower()
            if lp_address not in new_prices:
                if int(pool_dict["totalSupply"]) == 0:
                    new_prices[lp_address] = 0
                else:
                    # Simple LP token approximation
                    new_prices[lp_address] = pool_dict["usdTotal"] * 10 ** 18 / int(pool_dict["totalSupply"])
                if lp_address not in self.decimals:
                    self.decimals[lp_address] = 18
        self.cache.update(new_prices)
        self.save_cache()

    def get_price(self, coin: str) -> float:
        coin = coin.lower()

        if self.last_fetch_ts + self.ttl < time.time():
            self._fetch_prices()
        return self.cache.get(coin, 0.)

    def get_amount(self, coin: str, amount: int) -> tp.Union[float, int]:
        coin = coin.lower()
        price = self.get_price(coin)
        amount = amount / 10 ** self.decimals[coin]
        return amount * price
