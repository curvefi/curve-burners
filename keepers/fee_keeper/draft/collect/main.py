from collect.calculator.calculator import Calculator
from tx_sender import TxSender
from collect.source_fetcher import SourceFetcher
from utils import load_config_from_file


def collect():
    config = load_config_from_file()

    source_fetcher = SourceFetcher.get_from_config(config)(config)
    fee_sources = source_fetcher.sources
    # fee_sources = source_fetcher.fetch()
    calculator = Calculator.get_from_config(config)(config)
    tx_sender = TxSender.get_from_config(config)(config)
    while True:
        # tally = {source: source.tally() for source in fee_sources}  # How to multicall?
        sources, txs = calculator.calculate(fee_sources)

        # Combine calls
        proxy_withdraw = [[]]
        collector_withdraw = [[]]

        remaining = []
        for source_calls in txs:
            for call in source_calls:
                if call[1] == "withdraw_admin_fees":
                    if call[0] == self.proxy.address:
                        proxy_withdraw.append(call[2])
                        continue
                    elif call[0] == self.collector.address:
                        collector_withdraw.append(call[2])
                        continue
                remaining.append(call)

        # Batch withdraw TODO: remove
        for to_withdraw in proxy_withdraw:
            proxy.withdraw_many(to_withdraw + [self.brownie.ZERO_ADDRESS] * (20 - len(to_withdraw)))
        for to_withdraw in collector_withdraw:
            collector.withdraw_many(to_withdraw + [self.brownie.ZERO_ADDRESS] * (20 - len(to_withdraw)))
        collect_call = ("collector", "collect", [source.address for source in sources])

        tx_sender.send(txs + collect_call)
        for source in sources:
            fee_sources.remove(source)
