from utils import load_config_from_file, Chain


# Fetch sources like Controllers, proxy.burn(crvUSD), bridge claim transactions

def forward():
    config = load_config_from_file()

    forwarder = Forward.get_from_config(config)(config)
    tx_sender = TxSender.get_from_config(config)(config)
    if config["chain"] != Chain.Ethereum:
        txs = forwarder
        tx_sender.send()
        pass
