import typing as tp
from abc import abstractmethod
from enum import Enum

from data.brownie import BrownieData
from data.web3py import Web3PyData
from utils import Registrar


class FeeSource(Registrar):
    class _SourceType(Enum):
        STABLE_POOL = 1
        CRYPTO_POOL = 2
        STABLECOIN_CONTROLLER = 3
        PEG_KEEPER = 4

        def __getstate__(self):
            return self.name

    _ABI = {
        _SourceType.STABLE_POOL: [
            {"stateMutability": "view", "type": "function", "name": "withdraw_admin_fees", "inputs": [], "outputs":[]},
            {"stateMutability": "view", "type": "function", "name": "admin_balances",
             "inputs": [{"name": "i", "type": "uint256"}], "outputs": [{"name": "", "type": "uint256"}]},
        ],
        _SourceType.CRYPTO_POOL: [
            {"stateMutability": "view", "type": "function", "name": "claim_admin_fees", "inputs": [], "outputs": []},
        ],
        _SourceType.STABLECOIN_CONTROLLER: [
            {"stateMutability": "nonpayable", "type": "function", "name": "collect_fees", "inputs": [],
             "outputs": [{"name": "", "type": "uint256"}]},
        ],
        _SourceType.PEG_KEEPER: [
            {"stateMutability": "view", "type": "function", "name": "calc_profit", "inputs": [],
             "outputs": [{"name": "", "type": "uint256"}]},
            {"stateMutability": "nonpayable", "type": "function", "name": "withdraw_profit", "inputs": [],
             "outputs": [{"name": "", "type": "uint256"}]},
        ],
    }

    def __init__(self, source_type: tp.Union[_SourceType, str], address: str, coins: list[str], config: dict):
        self.source_type = source_type
        self.address = address
        self.coins = coins

    def __getstate__(self):
        return {
            "source_type": self.source_type,
            "address": self.address,
            "coins": self.coins,
        }

    @staticmethod
    def init_params_from_state(state) -> dict:
        return {
            "source_type": FeeSource._SourceType[state["source_type"]],
            "address": state["address"],
            "coins": state["coins"],
        }

    @abstractmethod
    def tally(self) -> dict:
        # Can be cached for some time
        return {}

    @abstractmethod
    def get_call(self) -> list[tuple]:
        return [
            ("0xCCcCccCcCcCCCCcAAAAAaaAaAAAAA18554448888", "Call", "Me", "If", "You", "Get", "Lost")
        ]

    def __hash__(self):
        return self.source_type.value + hash(self.address.lower())

    def __eq__(self, other: "FeeSource"):
        return self.source_type == other.source_type and self.address.lower() == other.address.lower()


class FeeSourceWeb3Py(FeeSource, Web3PyData):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.contract = self.web3.eth.contract(self.address, abi=self._ABI[self.source_type])

    def tally(self) -> dict:
        if self.source_type == self._SourceType.STABLE_POOL:
            return {coin: self.contract.functions.admin_balances(i).call() for i, coin in enumerate(self.coins)}
        elif self.source_type == self._SourceType.CRYPTO_POOL:
            return {}  # TODO how to count crypto pool profit?
        elif self.source_type == self._SourceType.STABLECOIN_CONTROLLER:
            return {coin: self.contract.functions.admin_fees().call() for coin in self.coins}  # only 1 coin
        elif self.source_type == self._SourceType.PEG_KEEPER:
            return {coin: self.contract.functions.calc_profit().call() for coin in self.coins}  # only 1 coin
        else:
            raise ValueError(f"Type {self.source_type} is not supported")

    def get_call(self) -> list[tuple]:
        if self.source_type == self._SourceType.STABLE_POOL:
            return [
                (self.address, self.contract.encodeABI("withdraw_admin_fees"))
            ]
        elif self.source_type == self._SourceType.CRYPTO_POOL:
            return [
                (self.address, self.contract.encodeABI("claim_admin_fees"))
            ]
        elif self.source_type == self._SourceType.STABLECOIN_CONTROLLER:
            return [
                (self.address, self.contract.encodeABI("collect_fees"))
            ]
        elif self.source_type == self._SourceType.PEG_KEEPER:
            return [
                (self.address, self.contract.encodeABI("withdraw_profit"))
            ]
        else:
            raise ValueError(f"Type {self.source_type} is not supported")


class FeeSourceBrownie(FeeSource, BrownieData):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._import_brownie(kwargs["config"])
        self.contract = self.brownie.Contract.from_abi(self.source_type.name, self.address, self._ABI[self.source_type])

    def tally(self) -> dict:
        if self.source_type == self._SourceType.STABLE_POOL:
            return {coin: self.contract.admin_balances(i) for i, coin in enumerate(self.coins)}
        elif self.source_type == self._SourceType.CRYPTO_POOL:
            return {}  # TODO how to count crypto pool profit?
        elif self.source_type == self._SourceType.STABLECOIN_CONTROLLER:
            return {coin: self.contract.admin_fees() for coin in self.coins}
        elif self.source_type == self._SourceType.PEG_KEEPER:
            return {coin: self.contract.calc_profit() for coin in self.coins}
        else:
            raise ValueError(f"Type {self.source_type} is not supported")

    def get_call(self) -> list[tuple]:
        if self.source_type == self._SourceType.STABLE_POOL:
            return [
                (self.address, self.contract.withdraw_admin_fees.encode_input())
            ]
        elif self.source_type == self._SourceType.CRYPTO_POOL:
            return [
                (self.address, self.contract.claim_admin_fees.encode_input())
            ]
        elif self.source_type == self._SourceType.STABLECOIN_CONTROLLER:
            return [
                (self.address, self.contract.collect_fees.encode_input())
            ]
        elif self.source_type == self._SourceType.PEG_KEEPER:
            return [
                (self.address, self.contract.withdraw_profit.encode_input())
            ]
        else:
            raise ValueError(f"Type {self.source_type} is not supported")
