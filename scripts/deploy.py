import boa
import json
import os
import sys
from getpass import getpass
from eth_account import account


chain = "gnosis"  # ALTER
TARGET = "0xaBEf652195F98A91E490f047A5006B71c85f058d"  # ALTER: crvUSD
WETH = "0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d"  # ALTER: wrapped native coin
ADMIN = "0x71F718D3e4d1449D1502A6A7595eb84eBcCB1683"  # ALTER
EMERGENCY_ADMIN = "0x71F718D3e4d1449D1502A6A7595eb84eBcCB1683"  # ALTER

BURNER = "CowSwap"  # ALTER

NETWORK = f"https://rpc.gnosischain.com"  # ALTER

EMPTY_COMPENSATION = (0, 0, 0, 0, False)
EMPTY_HOOK_INPUT = (0, 0, b"")

MIN_BRIDGE_AMOUNT = 100 * 10 ** 18  # ALTER: 100 crvUSD
ETHEREUM_FEE_DESTINATION = "0xeCb456EA5365865EbAb8a2661B0c503410e9B347"  # FeeDistributor on Ethereum


def deploy():
    fee_collector = boa.load("contracts/FeeCollector.vy", TARGET, WETH, boa.env.eoa, EMERGENCY_ADMIN)
    # fee_collector = boa.load_partial("contracts/FeeCollector.vy").at("")
    print(f"FeeCollector: {fee_collector.address}")

    hooker_inputs = deploy_hooks()
    hooker = boa.load("contracts/hooks/Hooker.vy", fee_collector, *hooker_inputs)
    # hooker = boa.load_partial("contracts/Hooker.vy").at("")
    print(f"Hooker: {hooker.address}")
    fee_collector.set_hooker(hooker)

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


def deploy_hooks():
    initial_oth, initial_oth_inputs, initial_hooks = [], [], []

    # Custom hooks
    if chain == "gnosis":
        # bridger = boa.load("contracts/hooks/gnosis/GnosisBridger.vy")
        bridger = boa.load_partial("contracts/hooks/gnosis/GnosisBridger.vy").at("0xc4AA2fB0A8837a06d296b1c0DE1990E401659449")

    # Bridger
    if chain != "ethereum":
        target = boa.load_partial("contracts/testing/ERC20Mock.vy").at(TARGET)
        initial_oth.append((TARGET, target.approve.prepare_calldata(bridger, 2 ** 256 - 1), EMPTY_COMPENSATION, False))
        initial_oth_inputs.append(EMPTY_HOOK_INPUT)
        initial_hooks.append(
            (
                bridger,
                bridger.bridge.prepare_calldata(TARGET, ETHEREUM_FEE_DESTINATION, 2 ** 256 - 1, MIN_BRIDGE_AMOUNT),
                EMPTY_COMPENSATION,
                True,
            )
        )

    # FeeDistributor
    else:
        pass

    return initial_oth, initial_oth_inputs, initial_hooks


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
