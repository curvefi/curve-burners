from utils import Registrar, prune_config
from collect.calculator.price_source import PriceSource
from collect.calculator.fee_applier import FeeApplier

from collect.fee_source import FeeSource


class Calculator(Registrar):
    def __init__(self, config):
        self.price_source = PriceSource.get_from_config(config)(config)
        self.fee_applier = FeeApplier.get_from_config(config)(config)

    def calculate(self, fee_sources: set[FeeSource]) -> (list, list):
        return []


class ThresholdCalculator(Calculator):
    def __init__(self, config):
        super().__init__(config)
        config = prune_config(config, self.__class__)
        self.threshold = config["threshold"]  # USD
        self.max_n_sources = config["max_n_sources"]

    def calculate(self, fee_sources: set[FeeSource]) -> (list, list):
        to_execute = []
        for source in fee_sources:
            gain = source.tally()
            mass = 0
            for coin, amount in gain.items():
                profit = self.fee_applier.get_profit(coin, amount)
                mass += self.price_source.get_amount(coin, profit)
            if mass >= self.threshold:
                to_execute.append(source)
            if len(to_execute) >= self.max_n_sources:
                break
        return to_execute, [source.get_call() for source in to_execute]
