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
EMERGENCY_ADMIN = "0x6d447e544D01a59cb0774763bf15526574CffFeD"  # ALTER

BURNER = "CowSwap"  # ALTER

NETWORK = f"https://rpc.gnosischain.com"  # ALTER

EMPTY_COMPENSATION = (0, (0, 0, 0), 0, 0, False)
EMPTY_HOOK_INPUT = (0, 0, b"")

MIN_EXCHANGE_AMOUNT = 5 * 10 ** 18  # ALTER: 1 crvUSD
MIN_BRIDGE_AMOUNT = 100 * 10 ** 18  # ALTER: 100 crvUSD
ETHEREUM_FEE_DESTINATION = "0xa2Bcd1a4Efbd04B63cd03f5aFf2561106ebCCE00"  # FeeCollector on Ethereum

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# DutchAuctionBurner dependencies (verify against the target chain before deploy)
COW_SETTLEMENT = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"  # ALTER: GPv2Settlement
COMPOSABLE_COW = "0xfdaFc9d1902f4e0b84f65F49f244b32b31013b74"  # ALTER: ComposableCow
COW_VAULT_RELAYER = "0xC92E8bdf79f0507f65a392b0ab4667716BFE0110"  # ALTER: VaultRelayer (CowSwapBurner only)
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"  # ALTER: canonical Permit2, ZERO_ADDRESS to disable


def deploy():
    fee_collector = boa.load("contracts/FeeCollector.vy", TARGET, WETH, boa.env.eoa, EMERGENCY_ADMIN)
    # fee_collector = boa.load_partial("contracts/FeeCollector.vy").at("")
    print(f"FeeCollector: {fee_collector.address}")

    hooker_inputs = deploy_hooks()
    hooker = boa.load("contracts/hooks/Hooker.vy", fee_collector, *hooker_inputs)
    # hooker = boa.load_partial("contracts/hooks/Hooker.vy").at("")
    print(f"Hooker: {hooker.address}")
    fee_collector.set_hooker(hooker)

    burner = deploy_burner(fee_collector)
    print(f"Burner: {burner.address}")
    fee_collector.set_burner(burner)

    fee_collector.set_killed([("0x0000000000000000000000000000000000000000", 0)])  # FeeCollector is set
    fee_collector.set_owner(ADMIN)


def deploy_burner(fee_collector):
    if BURNER == "XYZ":
        return boa.load("contracts/burners/XYZBurner.vy", fee_collector)
        # return boa.load_partial("contracts/burners/XYZBurner.vy").at("")
    if BURNER == "CowSwap":
        return boa.load("contracts/burners/CowSwapBurner.vy",
                        fee_collector,
                        COMPOSABLE_COW,
                        COW_VAULT_RELAYER,
                        MIN_EXCHANGE_AMOUNT,
                        )
        # return boa.load_partial("contracts/burners/CowSwapBurner.vy").at("")
    if BURNER == "DutchAuction":
        # Run scripts/dutch_auction_preflight.py against the final chain config first.
        registry = boa.load("contracts/AdapterRegistry.vy", ADMIN, EMERGENCY_ADMIN)
        # registry = boa.load_partial("contracts/AdapterRegistry.vy").at("")
        print(f"AdapterRegistry: {registry.address}")

        burner = boa.load("contracts/burners/DutchAuctionBurner.vy",
                          fee_collector,
                          100_000 * 10 ** 18,  # ALTER: start_total
                          MIN_EXCHANGE_AMOUNT,  # ALTER: floor_total
                          996_566_004_328_933_169_904_721_721,  # ALTER: 30s decay over 2879 active steps
                          30,  # ALTER: step_duration
                          120,  # ALTER: cow_order_validity
                          bytes.fromhex("058315b749613051abcbf50cf2d605b4fa4a41554ec35d73fd058fc530da559f"),
                          registry.address,
                          PERMIT2,  # ALTER: ZERO_ADDRESS on chains without canonical Permit2
                          )

        cow_enabled = True  # ALTER: False on chains without CoW
        if cow_enabled:
            handler = boa.load("contracts/cow/WatchtowerHandler.vy")
            # handler = boa.load_partial("contracts/cow/WatchtowerHandler.vy").at("")
            print(f"CowWatchtowerHandler: {handler.address}")
            burner.configure_cow(COW_SETTLEMENT, COMPOSABLE_COW, handler.address)
            burner.enable_cow()
        return burner
        # return boa.load_partial("contracts/burners/DutchAuctionBurner.vy").at("")
    raise ValueError("Burner not specified")


def deploy_hooks():
    initial_oth, initial_oth_inputs, initial_hooks = [], [], []

    # Custom hooks
    if chain == "gnosis":
        bridger = boa.load("contracts/hooks/gnosis/GnosisBridger.vy")
        # bridger = boa.load_partial("contracts/hooks/gnosis/GnosisBridger.vy").at("")

    # Bridger
    if chain != "ethereum":
        target = boa.load_partial("contracts/testing/ERC20Mock.vy").at(TARGET)
        initial_oth.append((TARGET, target.approve.prepare_calldata(bridger, 2 ** 256 - 1), EMPTY_COMPENSATION, False))
        initial_oth_inputs.append(EMPTY_HOOK_INPUT)
        initial_hooks.append(
            (
                str(bridger.address),
                bridger.bridge.prepare_calldata(TARGET, ETHEREUM_FEE_DESTINATION, 2 ** 256 - 1, MIN_BRIDGE_AMOUNT).hex(),
                EMPTY_COMPENSATION,
                True,
            )
        )

    # FeeDistributor
    else:
        target = boa.load_partial("contracts/testing/ERC20Mock.vy").at(TARGET)
        fee_distributor = boa.from_etherscan("0xD16d5eC345Dd86Fb63C6a9C43c517210F1027914", name="FeeDistributor")
        initial_oth.append((target, target.approve.prepare_calldata(fee_distributor, 2 ** 256 - 1), EMPTY_COMPENSATION, False))
        initial_oth_inputs.append((0, 0, b""))
        initial_hooks.append((fee_distributor, fee_distributor.burn.prepare_calldata(target), EMPTY_COMPENSATION, True))

    return initial_oth, initial_oth_inputs, initial_hooks


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


if __name__ == "__main__":
    if '--fork' in sys.argv[1:]:
        boa.fork(NETWORK)

        boa.env.eoa = '0x71F718D3e4d1449D1502A6A7595eb84eBcCB1683'
    else:
        boa.set_network_env(NETWORK)
        boa.env.add_account(account_load('curve'))  # ALTER: account to use
        # boa.env._fork_try_prefetch_state = False
    deploy()
    print("All set!")
