import json
import os
import sys
from getpass import getpass

import boa
from eth_account import account

chain = "gnosis"  # ALTER
TARGET = "0xaBEf652195F98A91E490f047A5006B71c85f058d"  # ALTER: crvUSD
WETH = "0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d"  # ALTER: wrapped native coin
ADMIN = "0x71F718D3e4d1449D1502A6A7595eb84eBcCB1683"  # ALTER
EMERGENCY_ADMIN = "0x6d447e544D01a59cb0774763bf15526574CffFeD"  # ALTER

BURNER = "CowSwap"  # ALTER

NETWORK = "https://rpc.gnosischain.com"  # ALTER

EMPTY_COMPENSATION = (0, (0, 0, 0), 0, 0, False)
EMPTY_HOOK_INPUT = (0, 0, b"")

MIN_EXCHANGE_AMOUNT = 5 * 10**18  # ALTER: 5 crvUSD
MIN_BRIDGE_AMOUNT = 100 * 10**18  # ALTER: 100 crvUSD
ETHEREUM_FEE_DESTINATION = "0xa2Bcd1a4Efbd04B63cd03f5aFf2561106ebCCE00"  # FeeCollector on Ethereum

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# CoW dependencies (verify against the target chain before deploy)
COW_SETTLEMENT = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"  # ALTER: GPv2Settlement (DutchAuctionBurner: CowAdapter reads the relayer from it)
COMPOSABLE_COW = (
    "0xfdaFc9d1902f4e0b84f65F49f244b32b31013b74"  # ALTER: ComposableCow (CowSwapBurner only)
)
# 0x Settler dependency (verify against the target chain before deploy): the
# live SettlerMetaTxn is the 0x deployer registry's ownerOf(3), SettlerIntent
# its ownerOf(4); ZeroExSettlerAdapter pins one Settler and its flavor, checks
# the registry lists it at deploy (deployInfo and ownerOf/prev, else it
# reverts) and stops signing once the registry no longer does.
ZEROX_DEPLOYER = "0x00000000000004533Fe15556B1E086BB1A72cEae"  # the same on every chain
ZEROX_SETTLER_METATXN = "0x9a6350d532ed83a970d452b736be5847b2856768"  # ALTER
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"  # ALTER: canonical Permit2

# 1inch dependency (verify against the target chain before deploy): the
# Limit Order Protocol v4 host (AggregationRouterV6, the same address on
# every supported chain); OneInchAdapter checks its EIP-712 domain at deploy.
ONEINCH_LIMIT_ORDER_PROTOCOL = "0x111111125421cA6dc452d289314280a0f8842A65"  # ALTER
# 1inch extension contracts (verify against the target chain before deploy):
# the FeeTaker the public orderbook relays orders through, and the Fusion
# settlement (Settlement on Ethereum, SimpleSettlement on Polygon, the same
# address and layout); each adapter probes its extension at deploy.
ONEINCH_FEE_TAKER = "0xc0DFdB9E7a392c3dBBE7c6FBe8FBC1789C9FE05e"  # ALTER
ONEINCH_FUSION_SETTLEMENT = "0x2Ad5004c60e16E54d5007C80CE329Adde5B51Ef5"  # ALTER

# UniswapX dependency (verify against the target chain before deploy): the v1
# ExclusiveDutchOrderReactor; UniswapXAdapter reads Permit2 from it.
UNISWAPX_REACTOR = "0x6000da47483062A0D734Ba3dc7576Ce6A0B645C4"  # ALTER
COW_VAULT_RELAYER = (
    "0xC92E8bdf79f0507f65a392b0ab4667716BFE0110"  # ALTER: VaultRelayer (CowSwapBurner only)
)
# OKX Intent Swap dependency (verify against the target chain before deploy):
# the production Settlement of the legacy schema (the same address on
# Ethereum, Arbitrum, Base, BSC and X Layer; the threshold schema lives at
# 0xC75d2cE9d0A2C67b606a7B325c16E8a1B7C15a4A); OKXIntentAdapter checks its
# EIP-712 domain at deploy and follows its tokenApproveProxy to the OKX DEX
# TokenApprove (the executor).
OKX_INTENT_SETTLEMENT = "0x25ED72C3f671b626810A6dB597DCFD50F215A423"  # ALTER

# KyberSwap DSLO dependency (verify against the target chain before deploy):
# DSLOProtocol, the same address on every chain KyberSwap serves (not
# Fraxtal); KyberDsloAdapter checks its EIP-712 domain and hashOrder at
# deploy, and the protocol itself is the executor (it pulls the maker asset).
KYBER_DSLO_PROTOCOL = "0xcab2FA2eeab7065B45CBcF6E3936dDE2506b4f6C"  # ALTER

# Velora Delta dependency (verify against the target chain before deploy): the
# Delta contract (Portikus V2 adapter, the same address on every supported
# chain; the approval target of a Delta order). VeloraDeltaAdapter checks its
# EIP-712 domain at deploy; the Delta contract itself is the executor.
VELORA_DELTA = "0x0000000000bbf5c5fd284e657f01bd000933c96d"  # ALTER

# OIF escrow dependencies (verify against the target chain before deploy): the
# escrow input settler (LI.FI's InputSettlerEscrowLIFI on Ethereum), the
# same-chain OutputSettlerSimple it attests with, and Permit2 (the executor).
OIF_INPUT_SETTLER = "0x00fC00edbe7C003b006f870068c548940000223e"  # ALTER
OIF_OUTPUT_SETTLER = "0x75220B7600c300005038432a0000f308e0000068"  # ALTER
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"  # ALTER: canonical Permit2

# Hashflow dependency (verify against the target chain before deploy): the
# HashflowRouter (not upgradeable; HashflowAdapter checks its EIP-712 domain
# through eip712Domain() at deploy). The router is the executor: it pulls the
# base token from the burner itself, and the proceeds land on the burner.
HASHFLOW_ROUTER = "0x55084eE0fEf03f14a305cd24286359A35D735151"  # ALTER: Ethereum

# Bebop JAM dependency (verify against the target chain before deploy): the
# JamSettlement v2 (the same address on every supported chain but zkSync Era;
# the `settlementAddress` of a JAM quote); BebopJamAdapter checks its EIP-712
# domain at deploy and reads the balance manager (the `approvalTarget`) from it.
BEBOP_JAM_SETTLEMENT = "0xbeb0b0623f66bE8cE162EbDfA2ec543A522F4ea6"  # ALTER


def deploy():
    fee_collector = boa.load(
        "contracts/FeeCollector.vy", TARGET, WETH, boa.env.eoa, EMERGENCY_ADMIN
    )
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

    fee_collector.set_killed(
        [("0x0000000000000000000000000000000000000000", 0)]
    )  # FeeCollector is set
    fee_collector.set_owner(ADMIN)


def deploy_burner(fee_collector):
    if BURNER == "XYZ":
        return boa.load("contracts/burners/XYZBurner.vy", fee_collector)
        # return boa.load_partial("contracts/burners/XYZBurner.vy").at("")
    if BURNER == "CowSwap":
        return boa.load(
            "contracts/deprecated/CowSwapBurner.vy",
            fee_collector,
            COMPOSABLE_COW,
            COW_VAULT_RELAYER,
            MIN_EXCHANGE_AMOUNT,
        )
        # return boa.load_partial("contracts/deprecated/CowSwapBurner.vy").at("")
    if BURNER == "DutchAuction":
        # Run scripts/dutch_auction_preflight.py against the final chain config first.
        registry = boa.load(
            "contracts/burners/adapters/AdapterRegistry.vy", fee_collector
        )  # roles follow FeeCollector owner/emergency_owner
        # registry = boa.load_partial("contracts/burners/adapters/AdapterRegistry.vy").at("")
        print(f"AdapterRegistry: {registry.address}")

        burner = boa.load(
            "contracts/burners/DutchAuctionBurner.vy",
            fee_collector,
            100_000 * 10**18,  # ALTER: start_total
            MIN_EXCHANGE_AMOUNT,  # ALTER: floor_total
            # The decay factor is solved on-chain from start_total, floor_total,
            # step_duration and the EXCHANGE frame (floor reached at its last
            # active second); the preflight recomputes and checks it.
            30,  # ALTER: step_duration
            registry.address,
        )
        # burner = boa.load_partial("contracts/burners/DutchAuctionBurner.vy").at("")

        cow_enabled = True  # ALTER: False on chains without CoW
        if cow_enabled:
            # Registry-managed; keepers run sync_executor_approvals(relayer, tokens) after collect.
            cow_adapter = boa.load(
                "contracts/burners/adapters/cow/CowAdapter.vy",
                COW_SETTLEMENT,
                bytes.fromhex(
                    "058315b749613051abcbf50cf2d605b4fa4a41554ec35d73fd058fc530da559f"
                ),  # ALTER: appData
            )
            # cow_adapter = boa.load_partial("contracts/burners/adapters/cow/CowAdapter.vy").at("")
            print(f"CowAdapter: {cow_adapter.address}")
            registry.set_adapter(cow_adapter.address, cow_adapter.vault_relayer())
            registry.activate_adapter(cow_adapter.address)

        okx_enabled = True  # ALTER: False on chains without OKX Intent Swap
        if okx_enabled:
            # Registry-managed; keepers run sync_executor_approvals(token_approve, tokens) after collect.
            okx_adapter = boa.load(
                "contracts/burners/adapters/okx/OKXIntentAdapter.vy",
                OKX_INTENT_SETTLEMENT,
            )
            # okx_adapter = boa.load_partial("contracts/burners/adapters/okx/OKXIntentAdapter.vy").at("")
            print(f"OKXIntentAdapter: {okx_adapter.address}")
            registry.set_adapter(okx_adapter.address, okx_adapter.token_approve())
            registry.activate_adapter(okx_adapter.address)

        kyber_enabled = True  # ALTER: False on chains without KyberSwap DSLO
        if kyber_enabled:
            # Registry-managed; keepers run sync_executor_approvals(protocol, tokens) after collect.
            kyber_adapter = boa.load(
                "contracts/burners/adapters/kyber/KyberDsloAdapter.vy",
                KYBER_DSLO_PROTOCOL,
            )
            # kyber_adapter = boa.load_partial("contracts/burners/adapters/kyber/KyberDsloAdapter.vy").at("")
            print(f"KyberDsloAdapter: {kyber_adapter.address}")
            registry.set_adapter(kyber_adapter.address, kyber_adapter.protocol())
            registry.activate_adapter(kyber_adapter.address)

        velora_enabled = True  # ALTER: False on chains without Velora Delta
        if velora_enabled:
            # Registry-managed; keepers run sync_executor_approvals(delta, tokens) after collect.
            velora_adapter = boa.load(
                "contracts/burners/adapters/velora/VeloraDeltaAdapter.vy",
                VELORA_DELTA,
            )
            # velora_adapter = boa.load_partial("contracts/burners/adapters/velora/VeloraDeltaAdapter.vy").at("")
            print(f"VeloraDeltaAdapter: {velora_adapter.address}")
            registry.set_adapter(velora_adapter.address, velora_adapter.settlement())
            registry.activate_adapter(velora_adapter.address)

        oif_enabled = True  # ALTER: False on chains without an OIF escrow deployment
        if oif_enabled:
            # Registry-managed; keepers run sync_executor_approvals(permit2, tokens) after collect.
            oif_adapter = boa.load(
                "contracts/burners/adapters/oif/OifEscrowAdapter.vy",
                OIF_INPUT_SETTLER,
                OIF_OUTPUT_SETTLER,
                PERMIT2,
            )
            # oif_adapter = boa.load_partial("contracts/burners/adapters/oif/OifEscrowAdapter.vy").at("")
            print(f"OifEscrowAdapter: {oif_adapter.address}")
            registry.set_adapter(oif_adapter.address, oif_adapter.permit2())
            registry.activate_adapter(oif_adapter.address)

        zerox_enabled = True  # ALTER: False on chains without a 0x Settler
        if zerox_enabled:
            # Registry-managed; keepers run sync_executor_approvals(permit2, tokens) after collect.
            # One deployment per Settler contract: a SettlerIntent adapter is the same call
            # with ownerOf(4) and intent=True.
            zerox_adapter = boa.load(
                "contracts/burners/adapters/zerox/ZeroExSettlerAdapter.vy",
                ZEROX_SETTLER_METATXN,
                ZEROX_DEPLOYER,
                PERMIT2,
                False,  # intent
            )
            # zerox_adapter = boa.load_partial("contracts/burners/adapters/zerox/ZeroExSettlerAdapter.vy").at("")
            print(f"ZeroExSettlerAdapter: {zerox_adapter.address}")
            registry.set_adapter(zerox_adapter.address, zerox_adapter.permit2())
            registry.activate_adapter(zerox_adapter.address)

        hashflow_enabled = True  # ALTER: False on chains without Hashflow
        if hashflow_enabled:
            # Registry-managed; keepers run sync_executor_approvals(router, tokens) after
            # collect and push_target() after every fill (proceeds land on the burner).
            hashflow_adapter = boa.load(
                "contracts/burners/adapters/hashflow/HashflowAdapter.vy",
                HASHFLOW_ROUTER,
            )
            # hashflow_adapter = boa.load_partial("contracts/burners/adapters/hashflow/HashflowAdapter.vy").at("")
            print(f"HashflowAdapter: {hashflow_adapter.address}")
            registry.set_adapter(hashflow_adapter.address, hashflow_adapter.router())
            registry.activate_adapter(hashflow_adapter.address)

        oneinch_enabled = True  # ALTER: False on chains without 1inch LOP v4
        if oneinch_enabled:
            # Registry-managed; keepers run sync_executor_approvals(router, tokens) after collect.
            oneinch_adapter = boa.load(
                "contracts/burners/adapters/oneinch/OneInchAdapter.vy",
                ONEINCH_LIMIT_ORDER_PROTOCOL,
            )
            # oneinch_adapter = boa.load_partial("contracts/burners/adapters/oneinch/OneInchAdapter.vy").at("")
            print(f"OneInchAdapter: {oneinch_adapter.address}")
            registry.set_adapter(oneinch_adapter.address, oneinch_adapter.limit_order_protocol())
            registry.activate_adapter(oneinch_adapter.address)

            # The fee-taking 1inch rails share the router as executor.
            fee_taker_adapter = boa.load(
                "contracts/burners/adapters/oneinch/OneInchFeeTakerAdapter.vy",
                ONEINCH_LIMIT_ORDER_PROTOCOL,
                ONEINCH_FEE_TAKER,
            )
            # fee_taker_adapter = boa.load_partial("contracts/burners/adapters/oneinch/OneInchFeeTakerAdapter.vy").at("")
            print(f"OneInchFeeTakerAdapter: {fee_taker_adapter.address}")
            registry.set_adapter(
                fee_taker_adapter.address, fee_taker_adapter.limit_order_protocol()
            )
            registry.activate_adapter(fee_taker_adapter.address)

            fusion_adapter = boa.load(
                "contracts/burners/adapters/oneinch/OneInchFusionAdapter.vy",
                ONEINCH_LIMIT_ORDER_PROTOCOL,
                ONEINCH_FUSION_SETTLEMENT,
            )
            # fusion_adapter = boa.load_partial("contracts/burners/adapters/oneinch/OneInchFusionAdapter.vy").at("")
            print(f"OneInchFusionAdapter: {fusion_adapter.address}")
            registry.set_adapter(fusion_adapter.address, fusion_adapter.limit_order_protocol())
            registry.activate_adapter(fusion_adapter.address)

        uniswapx_enabled = True  # ALTER: False on chains without a UniswapX v1 reactor
        if uniswapx_enabled:
            # Registry-managed; keepers run sync_executor_approvals(permit2, tokens) after collect.
            uniswapx_adapter = boa.load(
                "contracts/burners/adapters/uniswapx/UniswapXAdapter.vy",
                UNISWAPX_REACTOR,
            )
            # uniswapx_adapter = boa.load_partial("contracts/burners/adapters/uniswapx/UniswapXAdapter.vy").at("")
            print(f"UniswapXAdapter: {uniswapx_adapter.address}")
            registry.set_adapter(uniswapx_adapter.address, uniswapx_adapter.permit2())
            registry.activate_adapter(uniswapx_adapter.address)

        bebop_enabled = True  # ALTER: False on chains without Bebop JAM
        if bebop_enabled:
            # Registry-managed; keepers run sync_executor_approvals(balance_manager, tokens) after collect.
            bebop_adapter = boa.load(
                "contracts/burners/adapters/bebop/BebopJamAdapter.vy",
                BEBOP_JAM_SETTLEMENT,
            )
            # bebop_adapter = boa.load_partial("contracts/burners/adapters/bebop/BebopJamAdapter.vy").at("")
            print(f"BebopJamAdapter: {bebop_adapter.address}")
            registry.set_adapter(bebop_adapter.address, bebop_adapter.balance_manager())
            registry.activate_adapter(bebop_adapter.address)
        return burner
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
        initial_oth.append(
            (
                TARGET,
                target.approve.prepare_calldata(bridger, 2**256 - 1),
                EMPTY_COMPENSATION,
                False,
            )
        )
        initial_oth_inputs.append(EMPTY_HOOK_INPUT)
        initial_hooks.append(
            (
                str(bridger.address),
                bridger.bridge.prepare_calldata(
                    TARGET, ETHEREUM_FEE_DESTINATION, 2**256 - 1, MIN_BRIDGE_AMOUNT
                ).hex(),
                EMPTY_COMPENSATION,
                True,
            )
        )

    # FeeDistributor
    else:
        target = boa.load_partial("contracts/testing/ERC20Mock.vy").at(TARGET)
        fee_distributor = boa.from_etherscan(
            "0xD16d5eC345Dd86Fb63C6a9C43c517210F1027914", name="FeeDistributor"
        )
        initial_oth.append(
            (
                target,
                target.approve.prepare_calldata(fee_distributor, 2**256 - 1),
                EMPTY_COMPENSATION,
                False,
            )
        )
        initial_oth_inputs.append((0, 0, b""))
        initial_hooks.append(
            (
                fee_distributor,
                fee_distributor.burn.prepare_calldata(target),
                EMPTY_COMPENSATION,
                True,
            )
        )

    return initial_oth, initial_oth_inputs, initial_hooks


def account_load(fname):
    path = os.path.expanduser(os.path.join("~", ".brownie", "accounts", fname + ".json"))
    with open(path) as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


if __name__ == "__main__":
    if "--fork" in sys.argv[1:]:
        boa.fork(NETWORK)

        boa.env.eoa = ADMIN
    else:
        boa.set_network_env(NETWORK)
        boa.env.add_account(account_load("curve"))  # ALTER: account to use
        # boa.env._fork_try_prefetch_state = False
    deploy()
    print("All set!")
