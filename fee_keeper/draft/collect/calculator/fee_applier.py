import typing as tp
from abc import abstractmethod

from utils import Registrar, EPOCH


class FeeApplier(Registrar):
    def __init__(self, config: dict):
        pass

    @abstractmethod
    def get_profit(self, coin: str, amount: int = None) -> tp.Union[float, int]:
        pass


class OfflineFeeApplier(FeeApplier):
    """
    Reproduce `FeeCollector.fee()`
    """
    # TODO fetch updates of max_fee
    _COLLECT_MAX_FEE: int = 2 * 10 ** (18 - 2)  # 2%
    _FORWARD_MAX_FEE: int = 1 * 10 ** (18 - 2)  # 1%

    def get_profit(self, coin: str, amount: int = None) -> tp.Union[float, int]:
        match EPOCH.get_current():
            case EPOCH.COLLECT:
                max_fee = self._COLLECT_MAX_FEE
            case EPOCH.FORWARD:
                max_fee = self._FORWARD_MAX_FEE
            case _:
                max_fee = 0

        time_elapsed: int = EPOCH.get_epoch_time_elapsed()  # Might be less than actual block time execution
        return int(amount * time_elapsed * max_fee // 10 ** 18 // (24 * 3600)) if amount is not None\
            else time_elapsed * max_fee / (10 ** 18 * 24 * 3600)
