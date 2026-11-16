"""Mainnet fork simulation of the Kyber rail: the real DSLOProtocol fills
orders built for a DutchAuctionBurner behind a FeeCollector through
KyberDsloAdapter, including the API's `and(nonceEquals, timestampBelow)`
predicate and a taker-asset fee.

Every fill needs the operator co-signature of the protocol's dsOrderSigner;
the simulation points it at a test key through the protocol owner
(updateDSOrderSigner is onlyOwner) and co-signs each fill itself.

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
from eth_account import Account

from tests.burners.adapters.kyber.test_module import (
    MAINNET_ORDER_DIGEST,
    ORDER_ABI,
    PROTOCOL,
    PROTOCOL_DOMAIN_SEPARATOR,
    api_predicate,
    linear_getters,
    mainnet_order,
    order_digest_reference,
    pack_fee_config,
    timestamp_below,
    with_fields,
)
from tests.burners.conftest import custom_err
from tests.burners.test_dutch_auction_v2 import (
    FLOOR_TOTAL,
    START_TOTAL,
    STEP_DURATION,
    WAD,
    _move_to_epoch,
    _move_to_timestamp,
)
from tests.conftest import ZERO_ADDRESS, Epoch

MAX_UINT256 = 2**256 - 1
SALT = 0x5AFE_C0DE
OP_SIGNER_KEY = bytes.fromhex("5a" * 32)

RPC_ENV_KEYS = ("ETH_RPC_URL", "MAINNET_RPC_URL", "FORK_MAINNET_URL")

ORDER_COMPONENTS = [
    {"name": name, "type": kind}
    for name, kind in (
        ("salt", "uint256"),
        ("makerAsset", "address"),
        ("takerAsset", "address"),
        ("maker", "address"),
        ("receiver", "address"),
        ("allowedSender", "address"),
        ("makingAmount", "uint256"),
        ("takingAmount", "uint256"),
        ("feeConfig", "uint256"),
        ("makerAssetData", "bytes"),
        ("takerAssetData", "bytes"),
        ("getMakerAmount", "bytes"),
        ("getTakerAmount", "bytes"),
        ("predicate", "bytes"),
        ("interaction", "bytes"),
    )
]
PROTOCOL_ABI = [
    {
        "type": "function",
        "name": "fillOrderTo",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "order", "type": "tuple", "components": ORDER_COMPONENTS},
                    {
                        "name": "signature",
                        "type": "tuple",
                        "components": [
                            {"name": "orderSignature", "type": "bytes"},
                            {"name": "opSignature", "type": "bytes"},
                        ],
                    },
                    {"name": "opExpireTime", "type": "uint32"},
                    {"name": "target", "type": "address"},
                    {"name": "callbackData", "type": "bytes"},
                ],
            },
            {
                "name": "amountData",
                "type": "tuple",
                "components": [
                    {"name": "makingAmount", "type": "uint256"},
                    {"name": "takingAmount", "type": "uint256"},
                    {"name": "thresholdAmount", "type": "uint256"},
                ],
            },
            {"name": "sender", "type": "address"},
        ],
        "outputs": [
            {"name": "actualMakingAmount", "type": "uint256"},
            {"name": "actualTakingAmount", "type": "uint256"},
            {"name": "feeCollected", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "name": "hashOrder",
        "stateMutability": "view",
        "inputs": [{"name": "order", "type": "tuple", "components": ORDER_COMPONENTS}],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "hashDSOrder",
        "stateMutability": "view",
        "inputs": [
            {
                "name": "dsOrder",
                "type": "tuple",
                "components": [
                    {"name": "orderHash", "type": "bytes32"},
                    {"name": "opExpireTime", "type": "uint32"},
                ],
            }
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "DOMAIN_SEPARATOR",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "owner",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "name": "paused",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "updateDSOrderSigner",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_address", "type": "address"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "remainingRaw",
        "stateMutability": "view",
        "inputs": [{"name": "orderHash", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "nonce",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "address"}],
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


def _sign_digest(key: bytes, digest: bytes) -> bytes:
    signer = Account.from_key(key)
    if hasattr(signer, "unsafe_sign_hash"):
        return bytes(signer.unsafe_sign_hash(digest).signature)
    return bytes(signer.signHash(digest).signature)


class Filler:
    """A taker of the real protocol co-signing every fill as the operator."""

    def __init__(self, protocol: Any, adapter: Any, taker: str):
        self.protocol = protocol
        self.adapter = adapter
        self.taker = taker

    def params(self, order: tuple, order_signature: bytes) -> tuple:
        op_expire = boa.env.evm.vm.state.timestamp + 300
        digest = bytes(self.protocol.hashOrder(order))
        ds_digest = bytes(self.protocol.hashDSOrder((digest, op_expire)))
        op_signature = _sign_digest(OP_SIGNER_KEY, ds_digest)
        return (order, (order_signature, op_signature), op_expire, self.taker, b"")

    def fill(self, order: tuple, order_signature: bytes, making: int = 0, taking: int = 0):
        threshold = order[7] if making else 0
        with boa.env.prank(self.taker):
            return self.protocol.fillOrderTo(
                self.params(order, order_signature), (making, taking, threshold), ZERO_ADDRESS
            )


def test_mainnet_real_protocol_fills_published_orders():
    rpc_url = _rpc_url()
    if rpc_url is None:
        message = f"Mainnet fork RPC unavailable; set one of {', '.join(RPC_ENV_KEYS)}"
        if os.environ.get("REQUIRE_MAINNET_FORK"):
            pytest.fail(message)
        pytest.skip(message)

    fork_env = boa.Env()
    with boa.swap_env(fork_env):
        _fork(rpc_url)

        assert boa.env.get_code(PROTOCOL), f"Missing deployed code at {PROTOCOL}"
        protocol = _abi_contract(PROTOCOL_ABI, "DSLOProtocol", PROTOCOL)
        assert not protocol.paused()
        # The recorded vectors against the live contract: its domain and the
        # digest of the order a real fill carried.
        assert bytes(protocol.DOMAIN_SEPARATOR()) == PROTOCOL_DOMAIN_SEPARATOR
        assert bytes(protocol.hashOrder(mainnet_order())) == MAINNET_ORDER_DIGEST

        owner = boa.env.generate_address("owner")
        emergency_owner = boa.env.generate_address("emergency_owner")
        keeper = boa.env.generate_address("keeper")
        taker = boa.env.generate_address("taker")
        fee_recipient = boa.env.generate_address("fee_recipient")
        op_signer = Account.from_key(OP_SIGNER_KEY).address

        erc20 = boa.load_partial("contracts/testing/ERC20Mock.vy")
        target = erc20.deploy("Fork Target", "TARGET", 18)
        sell_token = erc20.deploy("Fork Sell Token", "SELL", 18)
        weth = boa.load("contracts/testing/WETH.vy")
        fee_collector = boa.load("contracts/FeeCollector.vy", target, weth, owner, emergency_owner)
        registry = boa.load("contracts/burners/adapters/AdapterRegistry.vy", fee_collector.address)
        # The deployment cross-checks its domain against the protocol's
        # DOMAIN_SEPARATOR and its hashing against hashOrder.
        adapter = boa.load("contracts/burners/adapters/kyber/KyberDsloAdapter.vy", PROTOCOL)
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
            # The protocol is the executor: a fill pulls the maker asset itself.
            registry.set_adapter(adapter, adapter.protocol())
            registry.activate_adapter(adapter)
        assert adapter.protocol() == PROTOCOL
        assert bytes(adapter.domain_separator()) == PROTOCOL_DOMAIN_SEPARATOR
        assert registry.is_executor_active(PROTOCOL)

        # The operator co-signature: point the protocol's signer at a test key.
        protocol_owner = protocol.owner()
        boa.env.set_balance(protocol_owner, 10**18)
        with boa.env.prank(protocol_owner):
            protocol.updateDSOrderSigner(op_signer)
        filler = Filler(protocol, adapter, taker)

        _move_to_epoch(fee_collector, Epoch.COLLECT)
        amount = 1_000 * WAD
        sell_token._mint_for_testing(fee_collector, amount)
        with boa.env.prank(keeper):
            fee_collector.collect([sell_token.address], keeper)
            burner.sync_executor_approvals(PROTOCOL, [sell_token.address])
        assert sell_token.allowance(burner, PROTOCOL) == MAX_UINT256
        lot_start, lot_end = burner.window(sell_token)
        initial_amount = burner.lots(sell_token).initial_amount
        assert initial_amount > 0
        _move_to_timestamp(lot_start + 3 * STEP_DURATION)

        # What a publisher hands to takers, straight from the adapter: a
        # quarter of the lot at the live quote to the FeeCollector,
        # fill-or-kill, and the signature the protocol will forward to the
        # burner.
        quarter = initial_amount // 4
        order, signature = adapter.order_for(burner.address, sell_token.address, SALT, quarter)
        order = tuple(order)
        signature = bytes(signature)
        quote = burner.getAmountNeeded(sell_token, quarter)
        assert order == (
            SALT,
            sell_token.address,
            target.address,
            burner.address,
            fee_collector.address,
            ZERO_ADDRESS,
            quarter,
            quote,
            0,
            b"",
            b"",
            # No getters: the protocol takes the exact order amounts only.
            b"",
            b"",
            timestamp_below(lot_end + 1),
            b"",
        )
        assert signature == bytes.fromhex(adapter.address[2:]) + encode([ORDER_ABI], [order])
        digest = bytes(protocol.hashOrder(order))
        assert digest == order_digest_reference(order, PROTOCOL_DOMAIN_SEPARATOR)
        with boa.env.prank(PROTOCOL):
            assert burner.isValidSignature(digest, signature) == bytes.fromhex("1626ba7e")
            assert burner.isValidSignature(digest, signature[20:]) == bytes.fromhex("ffffffff")

        target._mint_for_testing(taker, 12 * quote)
        with boa.env.prank(taker):
            target.approve(PROTOCOL, MAX_UINT256)

        # An underpriced order of the taker's own making fails inside the
        # real fill: the protocol reports every rejected contract signature
        # as its own 'LOP: bad signature' (the auction's typed reason is for
        # eth_call).
        cheaper = with_fields(order, takingAmount=quote - 1)
        cheaper_sig = bytes.fromhex(adapter.address[2:]) + encode([ORDER_ABI], [cheaper])
        with boa.reverts():
            filler.fill(cheaper, cheaper_sig, making=quarter)

        # No getters: the real protocol refuses a part of the order
        # ('LOP: wrong amount'), so there is never a remainder for it to
        # fill from its cache without the burner's signature.
        with boa.reverts():
            filler.fill(order, signature, making=quarter // 2)

        # The real protocol fills the published order in whole: it verifies
        # the digest through the burner's router, runs the predicate, pulls
        # the lot slice from the burner, and moves the taker's payment to
        # the FeeCollector.
        making, taking, fee = filler.fill(order, signature, making=quarter)
        assert (making, taking, fee) == (quarter, quote, 0)
        assert sell_token.balanceOf(taker) == quarter
        assert target.balanceOf(fee_collector) == quote
        assert burner.available(sell_token) == initial_amount - quarter
        # DSOrderMixin._ORDER_FILLED: nothing left under this hash.
        assert protocol.remainingRaw(digest) == 1
        with boa.reverts():
            filler.fill(order, signature, making=quarter)

        # An order in the shape the KyberSwap API signs: its salt, a
        # taker-asset fee and the `and(nonceEquals(maker, nonce),
        # timestampBelow(expiry))` predicate run by the real PredicateHelper.
        # The burner never advances its protocol nonce, so it reads 0.
        assert protocol.nonce(burner.address) == 0
        api_quote = burner.getAmountNeeded(sell_token, quarter)
        api_order = with_fields(
            order,
            salt=SALT + 1,
            takingAmount=api_quote,
            feeConfig=pack_fee_config(fee_recipient, 300, True),
            predicate=api_predicate(PROTOCOL, burner.address, 0, lot_end + 1),
        )
        assert adapter.describe(api_order) == (
            fee_collector.address,
            fee_recipient,
            300,
            True,
            lot_end,
        )
        # With the linear getters the API builds today the order could be
        # filled in parts, the remainder without a signature check: rejected
        # by name on an eth_call and inside the real fill.
        api_getters = linear_getters(quarter, api_quote)
        partial_order = with_fields(
            api_order, getMakerAmount=api_getters[0], getTakerAmount=api_getters[1]
        )
        partial_sig = bytes.fromhex(adapter.address[2:]) + encode([ORDER_ABI], [partial_order])
        with (
            boa.env.prank(PROTOCOL),
            boa.reverts(custom_err("OrderNotValid(string)", "BadAmountGetter")),
        ):
            burner.isValidSignature(bytes(protocol.hashOrder(partial_order)), partial_sig)
        with boa.reverts():
            filler.fill(partial_order, partial_sig, making=quarter // 2)

        # Every fill is validated live: a FeeCollector kill of the token
        # stops the fill-or-kill API-shaped order, with the allowance
        # untouched...
        api_sig = bytes.fromhex(adapter.address[2:]) + encode([ORDER_ABI], [api_order])
        api_digest = bytes(protocol.hashOrder(api_order))
        with boa.env.prank(PROTOCOL):
            assert burner.isValidSignature(api_digest, api_sig) == bytes.fromhex("1626ba7e")
        with boa.env.prank(emergency_owner):
            fee_collector.set_killed([(sell_token.address, Epoch.EXCHANGE)])
        assert sell_token.allowance(burner, PROTOCOL) >= quarter
        with boa.reverts():
            filler.fill(api_order, api_sig, taking=api_quote)
        # ...and it fills once the kill is lifted: a taking-amount fill of
        # the exact order amount, the fee charged on top, to the recipient.
        with boa.env.prank(owner):
            fee_collector.set_killed([(sell_token.address, 0)])
        making, taking, fee = filler.fill(api_order, api_sig, taking=api_quote)
        expected_fee = -(-api_quote * 300 // 100_000)
        assert (making, taking, fee) == (quarter, api_quote + expected_fee, expected_fee)
        assert sell_token.balanceOf(taker) == 2 * quarter
        assert target.balanceOf(fee_collector) == quote + api_quote
        assert target.balanceOf(fee_recipient) == expected_fee

        # So does a registry disable, for an order published before it.
        fresh, fresh_sig = adapter.order_for(burner.address, sell_token.address, SALT + 2, quarter)
        with boa.env.prank(emergency_owner):
            registry.disable_adapter(adapter)
        with boa.reverts():
            filler.fill(tuple(fresh), bytes(fresh_sig), making=quarter)

        # The allowance sync completes the emergency procedure as hygiene.
        with boa.env.prank(keeper):
            burner.sync_executor_approvals(PROTOCOL, [sell_token.address])
        assert sell_token.allowance(burner, PROTOCOL) == 0
        assert burner.available(sell_token) == initial_amount - 2 * quarter
