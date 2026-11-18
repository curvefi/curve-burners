import requests

from utils import Chain


class CurveAPIData:
    _CURVE_API = {
        "endpoint": "https://api.curve.fi/api",
        "network_name": {
            Chain.Ethereum: "ethereum",
            Chain.Gnosis: "xdai",
        },
        "types": {
            "stable": ["main", "factory", "factory-stable-ng", "factory-crvusd"],
            "crypto": ["crypto", "factory-crypto", "factory-tricrypto"],
            "stablecoin": [],
        },
    }
    _REGISTRY_ERRORS = {
        "copy": {  # Will be omitted
            "ethereum": {"main": [
                "0xEd279fDD11cA84bEef15AF5D39BB4d4bEE23F0cA",  # Metapool Liquity
                "0xd632f22692FaC7611d2AA1C0D552930D43CAEd3B",  # Metapool Frax
                "0x43b4FdFD4Ff969587185cDB6f0BD875c5Fc83f8c",  # Metapool Alchemix USD
                "0x5a6A4D54456819380173272A5E8E9B9904BdF41B",  # Metapool MIM
                "0xFD5dB7463a3aB53fD211b4af195c5BCCC1A03890",  # Plain Euro Tether
            ]},
            "arbitrum": {"main": [
                "0x30dF229cefa463e991e29D42DB0bae2e122B2AC7",  # Metapool MIM
                "0xC9B8a3FDECB9D5b218d02555a8Baf332E5B740d5",  # Metapool FRAXBP
                "0x960ea3e3C7FB317332d990873d354E18d7645590",  # tricrypto
            ]},
            "avalanche": {"main": [
                "0xAEA2E71b631fA93683BCF256A8689dFa0e094fcD",  # Plain 3poolV2
                "0x0149123760957395f283AF81fE8c904348aA33FC",  # Plain avax-3pool
            ]},
            "optimism": {"main": [
                "0x29A3d66B30Bc4AD674A4FDAF27578B64f6afbFe7",  # Plain FRXABP
            ]},
        },
        "update": {
            "ethereum": {
                # address: {key: value}, "type" for type_name
                "0x80466c64868E1ab14a1Ddf27A676C3fcBE638Fe5": {"type": "crypto"},  # 1st tricrypto
                "0xEcd5e75AFb02eFa118AF914515D6521aaBd189F1": {"type": "stable"},  # Metapool TrueUSD
                "0x4807862AA8b2bF68830e4C8dc86D0e9A998e085a": {"type": "stable"},  # Metapool BUSD
            },
        }
    }

    def iterate_over_all_pool_data(self, chain: Chain) -> list:
        api_network_name = self._CURVE_API['network_name'][chain]
        pool_datas = []
        for type_name, chunks in self._CURVE_API["types"].items():
            for chunk in chunks:
                response = requests.get(
                    f"{self._CURVE_API['endpoint']}/getPools/{api_network_name}/{chunk}",
                ).json()["data"]["poolData"]
                for pool_dict in response:
                    if pool_dict["address"] in self._REGISTRY_ERRORS["copy"].get(api_network_name, {}).get(type_name, []):
                        continue
                    update = self._REGISTRY_ERRORS["update"].get(api_network_name, {}).get(pool_dict["address"], {})
                    pool_dict.update(update)
                    pool_datas.append((update.get("type", type_name), pool_dict))
        return pool_datas
