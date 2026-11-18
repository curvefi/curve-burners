from data.curve_api import CurveAPIData
from utils import Cached, Registrar
from collect.fee_source import FeeSource


class SourceFetcher(Registrar, Cached):
    def __init__(self, config: dict):
        self.chain = config["chain"]
        self.config = config
        self.fee_source = FeeSource.get_from_config(config)
        self.sources = set()

    def __getstate__(self) -> dict:
        return {self.chain.name: self.sources}

    def __setstate__(self, state):
        self.sources = set([self.fee_source(**self.fee_source.init_params_from_state(source_data), config=self.config)
                            for source_data in state.get(self.chain.name, [])])

    def fetch(self) -> set[FeeSource]:
        return self.sources


class CurveAPISourceFetcher(SourceFetcher, CurveAPIData):
    _TYPE_MAP = {
        "stable": FeeSource._SourceType.STABLE_POOL,
        "crypto": FeeSource._SourceType.CRYPTO_POOL,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.load_cache()

    def _constant_sources(self) -> set[FeeSource]:
        sources = set()
        if self.chain == "ethereum":
            for pk, pool in [
                ("0x5B49b9adD1ecfe53E19cc2cFc8a33127cD6bA4C6", "0x4DEcE678ceceb27446b35C672dC7d61F30bAD69E"),  # USDC
                ("0xFF78468340EE322ed63C432BF74D817742b392Bf", "0x390f3595bCa2Df7d23783dFd126427CCeb997BF4"),  # USDT
                ("0x68e31e1eDD641B13cAEAb1Ac1BE661B19CC021ca", "0x625E92624Bc2D88619ACCc1788365A69767f6200"),  # pyUSD
                ("0x0B502e48E950095d93E8b739aD146C72b4f6C820", "0x34D655069F4cAc1547E4C8cA284FfFF5ad4A8db0"),  # TUSD
            ]:
                sources.add(self.fee_source(
                    source_type=FeeSource._SourceType.PEG_KEEPER,
                    address=pk,
                    coins=[pool],
                    config=self.config,
                ))

            for controller in [
                "0xa920de414ea4ab66b97da1bfe9e6eca7d4219635",  # ETH
                "0x4e59541306910ad6dc1dac0ac9dfb29bd9f15c67",  # wBTC
                "0x100daa78fc509db39ef7d04de0c1abd299f4c6ce",  # wstETH
                "0xec0820efafc41d8943ee8de495fc9ba8495b15cf",  # sfrxETH 2
                "0x1c91da0223c763d2e0173243eadaa0a2ea47e704",  # tBTC
                "0x8472a9a7632b173c8cf3a86d3afec50c35548e76",  # sfrxETH
            ]:
                sources.add(self.fee_source(
                    source_type=FeeSource._SourceType.STABLECOIN_CONTROLLER,
                    address=controller,
                    coins=["0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"],  # crvUSD
                    config=self.config,
                ))
        return sources

    def fetch_from_api(self):
        self.sources.update(self._constant_sources())
        initial_len = len(self.sources)
        for type_name, pool_dict in self.iterate_over_all_pool_data(self.chain):
            # if int(pool_dict["totalSupply"]) <= 10 ** 9 or pool_dict["address"] in burn_config.borked_pools:
            #     continue
            self.sources.add(self.fee_source(
                source_type=self._TYPE_MAP[type_name],
                address=pool_dict["address"],
                coins=[coin_data["address"] for coin_data in pool_dict["coins"]],
                config=self.config,
            ))
            self.save_cache()
        print(f"Loaded {len(self.sources) - initial_len} new sources")

    def fetch(self, force=False) -> set[FeeSource]:
        if force:
            self.sources = set()
        self.fetch_from_api()
        return self.sources
