"""Mainnet fork simulation of the OIF escrow rail as deployed by LI.FI: the
real InputSettlerEscrowLIFI, OutputSettlerSimple and Permit2 settle intents
built by OifEscrowAdapter.order_for against a DutchAuctionBurner behind a
FeeCollector, through the atomic openForAndFinalise (a solver fills and
attests the output, then opens and claims the input in one go), the
two-step openFor (anyone opens, the input sits in the escrow) plus finalise
by the solver, and the refund of an expired open intent.

Skips without a mainnet RPC (ETH_RPC_URL / MAINNET_RPC_URL, or an
ALCHEMY_API_TOKEN / DRPC_API_KEY to build one); set REQUIRE_MAINNET_FORK=1 to
turn the skip into a hard failure.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import boa
import pytest
from dotenv import load_dotenv
from eth_abi import encode

from tests.burners.conftest import custom_err
from tests.burners.test_dutch_auction_v2 import (
    FLOOR_TOTAL,
    START_TOTAL,
    STEP_DURATION,
    WAD,
    _move_to_epoch,
    _move_to_timestamp,
)
from tests.burners.test_oif_module import (
    LIFI_INPUT_SETTLER,
    LIFI_OUTPUT_SETTLER,
    ORDER_ABI,
    PERMIT2,
    PERMIT2_MAINNET_DOMAIN,
    identifier,
    permit_digest_reference,
)
from tests.conftest import ZERO_ADDRESS, Epoch

MAX_UINT256 = 2**256 - 1
NONCE = 0x5AFE_C0DE
# InputSettlerEscrow.OrderStatus
STATUS_NONE, STATUS_DEPOSITED, STATUS_CLAIMED, STATUS_REFUNDED = 0, 1, 2, 3

RPC_ENV_KEYS = ("ETH_RPC_URL", "MAINNET_RPC_URL", "FORK_MAINNET_URL")

OUTPUT_COMPONENTS = [
    {"name": "oracle", "type": "bytes32"},
    {"name": "settler", "type": "bytes32"},
    {"name": "chainId", "type": "uint256"},
    {"name": "token", "type": "bytes32"},
    {"name": "amount", "type": "uint256"},
    {"name": "recipient", "type": "bytes32"},
    {"name": "callbackData", "type": "bytes"},
    {"name": "context", "type": "bytes"},
]
ORDER_COMPONENTS = [
    {"name": "user", "type": "address"},
    {"name": "nonce", "type": "uint256"},
    {"name": "originChainId", "type": "uint256"},
    {"name": "expires", "type": "uint32"},
    {"name": "fillDeadline", "type": "uint32"},
    {"name": "inputOracle", "type": "address"},
    {"name": "inputs", "type": "uint256[2][]"},
    {"name": "outputs", "type": "tuple[]", "components": OUTPUT_COMPONENTS},
]
ORDER_INPUT = {"name": "order", "type": "tuple", "components": ORDER_COMPONENTS}
ESCROW_ABI = [
    {
        "type": "function",
        "name": "openFor",
        "stateMutability": "payable",
        "inputs": [
            ORDER_INPUT,
            {"name": "sponsor", "type": "address"},
            {"name": "signature", "type": "bytes"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "openForAndFinalise",
        "stateMutability": "nonpayable",
        "inputs": [
            ORDER_INPUT,
            {"name": "sponsor", "type": "address"},
            {"name": "signature", "type": "bytes"},
            {"name": "destination", "type": "address"},
            {"name": "call", "type": "bytes"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "finalise",
        "stateMutability": "nonpayable",
        "inputs": [
            ORDER_INPUT,
            {
                "name": "solveParams",
                "type": "tuple[]",
                "components": [
                    {"name": "timestamp", "type": "uint32"},
                    {"name": "solver", "type": "bytes32"},
                ],
            },
            {"name": "destination", "type": "bytes32"},
            {"name": "call", "type": "bytes"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "refund",
        "stateMutability": "nonpayable",
        "inputs": [ORDER_INPUT],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "orderIdentifier",
        "stateMutability": "view",
        "inputs": [ORDER_INPUT],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "orderStatus",
        "stateMutability": "view",
        "inputs": [{"name": "orderId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint8"}],
    },
]
OUTPUT_SETTLER_ABI = [
    {
        "type": "function",
        "name": "fill",
        "stateMutability": "payable",
        "inputs": [
            {"name": "orderId", "type": "bytes32"},
            {"name": "output", "type": "tuple", "components": OUTPUT_COMPONENTS},
            {"name": "fillDeadline", "type": "uint48"},
            {"name": "fillerData", "type": "bytes"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "setAttestation",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "orderId", "type": "bytes32"},
            {"name": "solver", "type": "bytes32"},
            {"name": "timestamp", "type": "uint32"},
            {"name": "output", "type": "tuple", "components": OUTPUT_COMPONENTS},
        ],
        "outputs": [],
    },
]
PERMIT2_ABI = [
    {
        "type": "function",
        "name": "DOMAIN_SEPARATOR",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "nonceBitmap",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}, {"name": "word", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


def _rpc_url() -> str | None:
    load_dotenv(Path.home() / ".env", override=False)
    url = next((os.environ[key] for key in RPC_ENV_KEYS if os.environ.get(key)), None)
    if url:
        return url
    if os.environ.get("ALCHEMY_API_TOKEN"):
        return f"https://eth-mainnet.g.alchemy.com/v2/{os.environ['ALCHEMY_API_TOKEN']}"
    if os.environ.get("DRPC_API_KEY"):
        return f"https://lb.drpc.org/ogrpc?network=ethereum&dkey={os.environ['DRPC_API_KEY']}"
    return None


def _fork(rpc_url: str) -> None:
    if hasattr(boa, "fork"):
        boa.fork(rpc_url, block_identifier="latest")
    else:
        boa.env.fork(rpc_url, block_identifier="latest")


def _abi_contract(abi: list[dict[str, Any]], name: str, address: str) -> Any:
    return boa.loads_abi(json.dumps(abi), name=name).at(address)


def _normalized(order: Any) -> tuple:
    """The order as the adapter returns it, bytes fields as bytes."""
    order = tuple(order)
    outputs = [
        (bytes(o[0]), bytes(o[1]), o[2], bytes(o[3]), o[4], bytes(o[5]), bytes(o[6]), bytes(o[7]))
        for o in order[7]
    ]
    return (*order[:6], [list(pair) for pair in order[6]], outputs)


def _nonce_used(permit2: Any, owner: str, nonce: int) -> bool:
    return permit2.nonceBitmap(owner, nonce >> 8) & (1 << (nonce & 0xFF)) != 0


def test_mainnet_real_escrow_settles_published_intents():
    rpc_url = _rpc_url()
    if rpc_url is None:
        message = f"Mainnet fork RPC unavailable; set one of {', '.join(RPC_ENV_KEYS)}"
        if os.environ.get("REQUIRE_MAINNET_FORK"):
            pytest.fail(message)
        pytest.skip(message)

    fork_env = boa.Env()
    with boa.swap_env(fork_env):
        _fork(rpc_url)

        for address in (LIFI_INPUT_SETTLER, LIFI_OUTPUT_SETTLER, PERMIT2):
            assert boa.env.get_code(address), f"Missing deployed code at {address}"
        escrow = _abi_contract(ESCROW_ABI, "InputSettlerEscrowLIFI", LIFI_INPUT_SETTLER)
        output_settler = _abi_contract(
            OUTPUT_SETTLER_ABI, "OutputSettlerSimple", LIFI_OUTPUT_SETTLER
        )
        permit2 = _abi_contract(PERMIT2_ABI, "Permit2", PERMIT2)
        assert bytes(permit2.DOMAIN_SEPARATOR()) == PERMIT2_MAINNET_DOMAIN

        owner = boa.env.generate_address("owner")
        emergency_owner = boa.env.generate_address("emergency_owner")
        keeper = boa.env.generate_address("keeper")
        solver = boa.env.generate_address("solver")
        solver_id = identifier(solver)

        erc20 = boa.load_partial("contracts/testing/ERC20Mock.vy")
        target = erc20.deploy("Fork Target", "TARGET", 18)
        sell_token = erc20.deploy("Fork Sell Token", "SELL", 18)
        weth = boa.load("contracts/testing/WETH.vy")
        fee_collector = boa.load("contracts/FeeCollector.vy", target, weth, owner, emergency_owner)
        registry = boa.load("contracts/burners/adapters/AdapterRegistry.vy", fee_collector.address)
        adapter = boa.load(
            "contracts/burners/adapters/oif/OifEscrowAdapter.vy",
            LIFI_INPUT_SETTLER,
            LIFI_OUTPUT_SETTLER,
            PERMIT2,
        )
        burner = boa.load(
            "contracts/burners/DutchAuctionBurner.vy",
            fee_collector,
            START_TOTAL,
            FLOOR_TOTAL,
            STEP_DURATION,
            registry,
        )
        with boa.env.prank(owner):
            fee_collector.set_burner(burner)
            fee_collector.set_killed([(ZERO_ADDRESS, 0)])
            # Permit2 pulls the sold tokens for the escrow: it is the executor.
            registry.set_adapter(adapter, PERMIT2)
            registry.activate_adapter(adapter)
        assert bytes(adapter.domain_separator()) == PERMIT2_MAINNET_DOMAIN
        assert registry.is_executor_active(PERMIT2)

        _move_to_epoch(fee_collector, Epoch.COLLECT)
        amount = 1_000 * WAD
        sell_token._mint_for_testing(fee_collector, amount)
        with boa.env.prank(keeper):
            fee_collector.collect([sell_token.address], keeper)
            burner.sync_executor_approvals(PERMIT2, [sell_token.address])
        assert sell_token.allowance(burner, PERMIT2) == MAX_UINT256
        lot_start, lot_end = burner.window(sell_token)
        initial_amount = burner.lots(sell_token).initial_amount
        assert initial_amount > 0
        _move_to_timestamp(lot_start + 3 * STEP_DURATION)
        with boa.env.prank(solver):
            target.approve(LIFI_OUTPUT_SETTLER, MAX_UINT256)

        # What a publisher hands to solvers, straight from the adapter: a
        # quarter of the lot at the live quote to the FeeCollector, and the
        # escrow signature (type byte, adapter, order).
        quarter = initial_amount // 4
        order, signature = adapter.order_for(burner.address, sell_token.address, NONCE, quarter)
        order = _normalized(order)
        signature = bytes(signature)
        output = order[7][0]
        quote = output[4]
        assert quote == burner.getAmountNeeded(sell_token, quarter)
        assert order[0] == burner.address and order[4] == lot_end
        assert signature[0] == 0
        digest = permit_digest_reference(order, LIFI_INPUT_SETTLER, PERMIT2_MAINNET_DOMAIN)
        assert bytes(burner.isValidSignature(digest, signature[1:])) == bytes.fromhex("1626ba7e")
        order_id = bytes(escrow.orderIdentifier(order))

        # Atomic settlement: the solver fills the output (paying the
        # FeeCollector the signed amount), attests its own fill on the output
        # settler, and opens-and-claims the input in the same block.
        target._mint_for_testing(solver, quote)
        now = boa.env.evm.vm.state.timestamp
        with boa.env.prank(solver):
            output_settler.fill(order_id, output, order[4], solver_id)
            output_settler.setAttestation(order_id, solver_id, now, output)
            escrow.openForAndFinalise(order, burner.address, signature, solver, b"")
        assert target.balanceOf(fee_collector) == quote
        assert target.balanceOf(solver) == 0
        assert sell_token.balanceOf(solver) == quarter
        assert sell_token.balanceOf(burner) == initial_amount - quarter
        assert escrow.orderStatus(order_id) == STATUS_CLAIMED
        assert _nonce_used(permit2, burner.address, NONCE)

        # Two-step settlement later on the curve: anyone opens the intent
        # (the input moves into the escrow, nothing is paid yet), then the
        # solver fills, attests and finalises to claim the input.
        _move_to_timestamp(lot_start + 20 * STEP_DURATION)
        order2, signature2 = adapter.order_for(
            burner.address, sell_token.address, NONCE + 1, quarter
        )
        order2 = _normalized(order2)
        signature2 = bytes(signature2)
        output2 = order2[7][0]
        quote2 = output2[4]
        assert quote2 < quote
        order_id2 = bytes(escrow.orderIdentifier(order2))
        with boa.env.prank(keeper):
            escrow.openFor(order2, burner.address, signature2)
        assert sell_token.balanceOf(burner) == initial_amount - 2 * quarter
        assert sell_token.balanceOf(LIFI_INPUT_SETTLER) >= quarter
        assert escrow.orderStatus(order_id2) == STATUS_DEPOSITED
        assert _nonce_used(permit2, burner.address, NONCE + 1)
        # The same signature cannot open twice: Permit2 burnt the nonce.
        with boa.env.prank(keeper), boa.reverts():
            escrow.openFor(order2, burner.address, signature2)
        boa.env.time_travel(seconds=STEP_DURATION)
        target._mint_for_testing(solver, quote2)
        now = boa.env.evm.vm.state.timestamp
        with boa.env.prank(solver):
            output_settler.fill(order_id2, output2, order2[4], solver_id)
            output_settler.setAttestation(order_id2, solver_id, now, output2)
            escrow.finalise(order2, [(now, solver_id)], solver_id, b"")
        assert target.balanceOf(fee_collector) == quote + quote2
        assert sell_token.balanceOf(solver) == 2 * quarter
        assert escrow.orderStatus(order_id2) == STATUS_CLAIMED

        # An opened intent nobody fills is refundable after it expires.
        now = boa.env.evm.vm.state.timestamp
        order3, signature3 = adapter.order_for(
            burner.address, sell_token.address, NONCE + 2, quarter, now + 600
        )
        order3 = _normalized(order3)
        order_id3 = bytes(escrow.orderIdentifier(order3))
        with boa.env.prank(keeper):
            escrow.openFor(order3, burner.address, bytes(signature3))
        assert sell_token.balanceOf(burner) == initial_amount - 3 * quarter
        boa.env.time_travel(seconds=601)
        with boa.env.prank(keeper):
            escrow.refund(order3)
        assert sell_token.balanceOf(burner) == initial_amount - 2 * quarter
        assert escrow.orderStatus(order_id3) == STATUS_REFUNDED

        # The burner's typed economic revert reaches the solver through the
        # real escrow and Permit2: an underpriced copy is rejected before any
        # pull.
        live, live_signature = adapter.order_for(
            burner.address, sell_token.address, NONCE + 3, quarter
        )
        live = _normalized(live)
        live_output = live[7][0]
        underpriced = (*live[:7], [(*live_output[:4], live_output[4] - 1, *live_output[5:])])
        bad_signature = bytes(live_signature)[:21] + encode([ORDER_ABI], [underpriced])
        with boa.env.prank(keeper), boa.reverts(custom_err("BadBuyAmount()", nested=True)):
            escrow.openFor(underpriced, burner.address, bad_signature)

        # Emergency: a registry disable turns the router's answer into
        # 0xffffffff, which Permit2 reports as InvalidContractSignature.
        order4, signature4 = adapter.order_for(
            burner.address, sell_token.address, NONCE + 4, quarter
        )
        with boa.env.prank(emergency_owner):
            registry.disable_adapter(adapter)
        with (
            boa.env.prank(keeper),
            boa.reverts(custom_err("InvalidContractSignature()", nested=True)),
        ):
            escrow.openFor(_normalized(order4), burner.address, bytes(signature4))
