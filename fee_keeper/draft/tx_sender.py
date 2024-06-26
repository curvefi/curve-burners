import time
from abc import abstractmethod

from fee_keeper import BrownieData
from utils import Registrar, prune_config


class TxSender(Registrar):
    def __init__(self, config):
        self.chain = config["chain"]

    @abstractmethod
    def send(self, txs: list):
        return []


class TxPrinter(TxSender):
    def send(self, txs: list):
        print(txs)
        time.sleep(3)  # Wait for propagate


class TxSenderBrownie(TxSender, BrownieData):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        config = prune_config(kwargs["config"], self.__class__)
        self._import_brownie(config)
        self.sender = self.brownie.accounts.load(config["sender"], password=config.get("sender_password", None))
        self.send_params = config["send_params"]

    def send(self, txs: list):
        for target, fn_name, *args in txs:
            contract = self.brownie.Contract(target)
            fn = getattr(contract, fn_name)
            # calldata = fn.encode_input(*args)
            fn(*args, {"from": self.sender} | self.send_params)
            time.sleep(3)  # Wait for propagate
