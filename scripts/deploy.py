import boa
import json
import os
import sys
from getpass import getpass
from eth_account import account


TARGET = "0xaBEf652195F98A91E490f047A5006B71c85f058d"  # ALTER: crvUSD
WETH = "0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d"  # ALTER: wrapped native coin
ADMIN = "0x71F718D3e4d1449D1502A6A7595eb84eBcCB1683"  # ALTER
EMERGENCY_ADMIN = "0x71F718D3e4d1449D1502A6A7595eb84eBcCB1683"  # ALTER

BURNER = "XYZ"  # ALTER

NETWORK = f"https://rpc.gnosischain.com"  # ALTER


def deploy():
    fee_collector = boa.load("contracts/FeeCollector.vy", TARGET, WETH, boa.env.eoa, EMERGENCY_ADMIN)
    # fee_collector = boa.load_partial("contracts/FeeCollector.vy").at("")
    print(f"FeeCollector: {fee_collector.address}")

    hooker = boa.load("contracts/Hooker.vy", fee_collector)
    # hooker = boa.load_partial("contracts/Hooker.vy").at("")
    print(f"Hooker: {hooker.address}")
    # fee_collector.set_hooker(hooker)

    burner = deploy_burner(fee_collector)
    print(f"Burner: {burner.address}")
    fee_collector.set_burner(burner)

    fee_collector.set_killed([("0x0000000000000000000000000000000000000000", 0)])
    fee_collector.set_owner(ADMIN)


def deploy_burner(fee_collector):
    if BURNER == "XYZ":
        return boa.load("contracts/burners/XYZBurner.vy", fee_collector)
        # return boa.load_partial("contracts/burners/XYZBurner.vy").at("")
    if BURNER == "CowSwap":
        return boa.load("contracts/burners/CowSwapBurner.vy",
                        fee_collector,
                        "0xfdaFc9d1902f4e0b84f65F49f244b32b31013b74",  # ALTER: ComposableCow
                        "0xC92E8bdf79f0507f65a392b0ab4667716BFE0110",  # ALTER: VaultRelayer
                        )
        # return boa.load_partial("contracts/burners/CowSwapBurner.vy").at("")
    raise ValueError("Burner not specified")


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


if __name__ == "__main__":
    if '--fork' in sys.argv[1:]:
        boa.env.fork(NETWORK)

        boa.env.eoa = '0x71F718D3e4d1449D1502A6A7595eb84eBcCB1683'
    else:
        boa.set_network_env(NETWORK)
        boa.env.add_account(account_load('curve'))  # ALTER: account to use
        boa.env._fork_try_prefetch_state = False
    deploy()
    print("All set!")
