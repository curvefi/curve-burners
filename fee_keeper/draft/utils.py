import json
import os.path
from enum import Enum
from typing import Union
from time import time as timestamp  # TODO: use pending block timestamp

import yaml


class EPOCH(Enum):
    SLEEP = 0
    COLLECT = 1
    EXCHANGE = 2
    FORWARD = 3

    _START_TS = 1600300800
    _DAY = 24 * 3600
    _WEEK = 7 * 24 * 3600

    @staticmethod
    def get_epoch_time_elapsed():
        return (int(timestamp()) - EPOCH._START_TS) % EPOCH._WEEK % EPOCH._DAY

    @staticmethod
    def get_current():
        return EPOCH.COLLECT  # TODO: remove wen ready
        day = (int(timestamp()) - EPOCH._START_TS) % EPOCH._WEEK // EPOCH._DAY
        match day:
            case 0, 1, 2, 3:
                return EPOCH.SLEEP
            case 4:
                return EPOCH.COLLECT
            case 5:
                return EPOCH.EXCHANGE
            case 6:
                return EPOCH.FORWARD
            case _:
                raise ValueError("day out of range")


class Chain(Enum):
    # Arbitrum = 42161
    # Aurora = 1313161554
    # Avalanche = 43114
    # Base = 8453
    # Celo = 42220
    Ethereum = 1
    # Fantom = 250
    Gnosis = 100
    # Kava = 2222
    # Moonbeam = 1284
    # Optimism = 10
    # Polygon = 137


def load_config_from_file(path="fee_keeper/fee_keeper.yaml") -> dict:
    with open(path, "r") as stream:
        config = yaml.safe_load(stream)
    config["chain"] = Chain[config["chain"]]
    return config


def prune_config(config, cls) -> dict:
    prune_config = {"chain": config["chain"]}
    cls_specific = config.get(cls.__name__, {})
    prune_config.update(cls_specific)
    prune_config.update(cls_specific.get(config["chain"], {}))
    return prune_config


class Registrar:
    """Class for registering subclass implementations

    All subclasses have to be in the same file as Base class or imported in order to trigger `__init_subclass__`.
    """
    _subclasses = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls not in Registrar.__subclasses__():  # direct subclasses are base classes
            cls._subclasses[cls.__name__] = cls
        else:
            cls._subclasses = {}

    @classmethod
    def get_registered_types(cls):
        """Get all available types(implementations/subclasses) of Base class"""
        return list(cls._subclasses.keys())

    @classmethod
    def get_from_type(cls, subclass_type: str):
        """Get class of given type."""
        if subclass_type not in cls._subclasses:
            raise ValueError(f"No subclass type of {subclass_type} registered")
        return cls._subclasses[subclass_type]

    @classmethod
    def get_from_config(cls, config: dict):
        """Get class of given type defined in config."""
        subclass_type = config[cls.__name__ + "Type"]
        return cls.get_from_type(subclass_type)


class Cached:
    _DIR = "fee_keeper/cache"

    def __getstate__(self) -> dict:
        """Should be of type dict"""
        return self.__dict__

    def __setstate__(self, state):
        """Implement for each class"""
        pass

    @property
    def cache_file_name(self):
        return f"{self._DIR}/{self.__class__.__name__}.json"

    def save_cache(self):
        def recursive_serialize(d: Union[dict, Cached]):
            if hasattr(d, "__getstate__"):
                return recursive_serialize(d.__getstate__())
            elif isinstance(d, dict):
                for k, v in d.items():
                    d[k] = recursive_serialize(v)
            elif hasattr(d, "__iter__") and not isinstance(d, str):
                return [recursive_serialize(v) for v in d]
            return d

        initial = {}
        if os.path.isfile(self.cache_file_name):
            with open(self.cache_file_name, "r") as file:
                initial = json.load(file)

        # NOTE: not recursive since logic is: cache[chain] = whole_new_cache
        initial.update(recursive_serialize(self))
        with open(self.cache_file_name, "w+") as file:
            json.dump(recursive_serialize(self), file, indent=2)

    def load_cache(self, cache=None):
        if not cache:
            cache = {}
            if os.path.isfile(self.cache_file_name):
                with open(self.cache_file_name, 'r') as f:
                    cache = json.load(f)
        if isinstance(super(), Cached):
            super().__setstate__(cache)
        self.__setstate__(cache)
