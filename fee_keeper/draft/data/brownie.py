import typing as tp
from utils import Chain


class BrownieData:
    _NETWORK_NAME = {
        Chain.Ethereum: "hardhat-fork",
        Chain.Gnosis: "gnosis-fork",
    }

    def __init__(self, config: tp.Optional[Chain] = None, chain: tp.Optional[Chain] = None):
        self._import_brownie(config=config, chain=chain)

    @staticmethod
    def _import_brownie(config: tp.Optional[Chain] = None, chain: tp.Optional[Chain] = None):
        if config and not chain:
            chain = config["chain"]
        if not hasattr(BrownieData, "brownie"):
            global brownie
            import brownie
            BrownieData.brownie = brownie
        if chain and BrownieData.brownie.network.show_active() != BrownieData._NETWORK_NAME[chain]:
            BrownieData.brownie.network.connect(BrownieData._NETWORK_NAME[chain])

    @staticmethod
    def _connect(chain: Chain):
        if chain and BrownieData.brownie.network.show_active() != BrownieData._NETWORK_NAME[chain]:
            BrownieData.brownie.network.connect(BrownieData._NETWORK_NAME[chain])
