"""Mainnet fork simulation of the Hashflow RFQ-m rail: the real HashflowRouter
fills a quote built from HashflowAdapter.order_for against a
DutchAuctionBurner behind a FeeCollector, through a pool created with the
real HashflowFactory (its owner pranked onto the pool-creator allowlist) and
a market-maker key this test controls; the proceeds land on the burner and
push_target forwards them to the FeeCollector.

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
from eth_keys import keys
from eth_utils import keccak, to_checksum_address

from tests.burners.adapters.hashflow.test_module import (
    DOMAIN_FIELDS,
    DOMAIN_NAME,
    DOMAIN_VERSION,
    FACTORY,
    MAINNET_DOMAIN_SEPARATOR,
    QUOTE_ABI,
    ROUTER,
    domain_separator_reference,
    quote_digest_reference,
    sign_maker,
    to_calldata,
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
MAKER_KEY = keys.PrivateKey(keccak(b"fork market maker"))
INVALID_SIGNER = "HashflowRouter::_validateRFQMSignature Invalid signer."

RPC_ENV_KEYS = ("ETH_RPC_URL", "MAINNET_RPC_URL", "FORK_MAINNET_URL")

RFQM_QUOTE_COMPONENTS = [
    {"name": "pool", "type": "address"},
    {"name": "externalAccount", "type": "address"},
    {"name": "trader", "type": "address"},
    {"name": "baseToken", "type": "address"},
    {"name": "quoteToken", "type": "address"},
    {"name": "baseTokenAmount", "type": "uint256"},
    {"name": "quoteTokenAmount", "type": "uint256"},
    {"name": "quoteExpiry", "type": "uint256"},
    {"name": "txid", "type": "bytes32"},
    {"name": "takerSignature", "type": "bytes"},
    {"name": "makerSignature", "type": "bytes"},
]
ROUTER_ABI = [
    {
        "type": "function",
        "name": "tradeRFQM",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "quote", "type": "tuple", "components": RFQM_QUOTE_COMPONENTS}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "eip712Domain",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "fields", "type": "bytes1"},
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
            {"name": "salt", "type": "bytes32"},
            {"name": "extensions", "type": "uint256[]"},
        ],
    },
    {
        "type": "function",
        "name": "factory",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "name": "authorizedPools",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "address"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
]
FACTORY_ABI = [
    {
        "type": "function",
        "name": "owner",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "name": "allowedPoolCreators",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "address"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "updatePoolCreatorAuthorization",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "poolCreator", "type": "address"}, {"name": "status", "type": "bool"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "createPool",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "name", "type": "string"}, {"name": "signer", "type": "address"}],
        "outputs": [],
    },
]
# HashflowFactory.CreatePool(address pool, address operations), neither indexed.
CREATE_POOL_TOPIC = keccak(text="CreatePool(address,address)")


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


def _timestamp() -> int:
    return boa.env.evm.vm.state.timestamp


def _as_tuple(quote: Any) -> tuple:
    """A Quote as the adapter returns it, txid as bytes."""
    quote = tuple(quote)
    return (bytes(quote[0]), *quote[1:])


def _create_pool(factory: Any, creator: str, signer: str) -> str:
    """createPool through the real factory; the pool address from its CreatePool log."""
    with boa.env.prank(creator):
        factory.createPool("curve-burner-test", signer)
    for _, log_address, topics, data in factory._computation.get_raw_log_entries():
        if bytes(log_address) == bytes.fromhex(FACTORY[2:]) and topics[0].to_bytes(32, "big") == (
            CREATE_POOL_TOPIC
        ):
            return to_checksum_address(data[12:32])
    raise AssertionError("CreatePool not emitted")


def test_mainnet_real_router_fills_a_quote_and_push_target_forwards():
    rpc_url = _rpc_url()
    if rpc_url is None:
        message = f"Mainnet fork RPC unavailable; set one of {', '.join(RPC_ENV_KEYS)}"
        if os.environ.get("REQUIRE_MAINNET_FORK"):
            pytest.fail(message)
        pytest.skip(message)

    fork_env = boa.Env()
    with boa.swap_env(fork_env):
        _fork(rpc_url)

        assert boa.env.get_code(ROUTER), f"Missing deployed code at {ROUTER}"
        router = _abi_contract(ROUTER_ABI, "HashflowRouter", ROUTER)
        fields, name, version, chain_id, verifying_contract, salt, extensions = (
            router.eip712Domain()
        )
        assert (bytes(fields), name, version, chain_id, verifying_contract) == (
            DOMAIN_FIELDS,
            DOMAIN_NAME,
            DOMAIN_VERSION,
            1,
            ROUTER,
        )
        assert bytes(salt) == bytes(32) and list(extensions) == []
        assert router.factory() == FACTORY
        factory = _abi_contract(FACTORY_ABI, "HashflowFactory", FACTORY)

        owner = boa.env.generate_address("owner")
        emergency_owner = boa.env.generate_address("emergency_owner")
        keeper = boa.env.generate_address("keeper")
        relayer = boa.env.generate_address("relayer")
        pool_creator = boa.env.generate_address("market_maker_operations")
        maker_signer = MAKER_KEY.public_key.to_checksum_address()

        erc20 = boa.load_partial("contracts/testing/ERC20Mock.vy")
        target = erc20.deploy("Fork Target", "TARGET", 18)
        sell_token = erc20.deploy("Fork Sell Token", "SELL", 18)
        weth = boa.load("contracts/testing/WETH.vy")
        fee_collector = boa.load("contracts/FeeCollector.vy", target, weth, owner, emergency_owner)
        registry = boa.load("contracts/burners/adapters/AdapterRegistry.vy", fee_collector.address)
        # The deployment cross-checks the router's eip712Domain.
        adapter = boa.load("contracts/burners/adapters/hashflow/HashflowAdapter.vy", ROUTER)
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
            # The router is the executor: a fill pulls the base token through it.
            registry.set_adapter(adapter, adapter.router())
            registry.activate_adapter(adapter)
        assert adapter.router() == ROUTER
        assert bytes(adapter.domain_separator()) == MAINNET_DOMAIN_SEPARATOR
        assert bytes(adapter.domain_separator()) == domain_separator_reference(1, ROUTER)
        assert registry.is_executor_active(ROUTER)

        # A market maker: pool creation is allowlisted by the factory owner;
        # the pool holds its own quote-token liquidity (no external account).
        factory_owner = factory.owner()
        with boa.env.prank(factory_owner):
            factory.updatePoolCreatorAuthorization(pool_creator, True)
        pool = _create_pool(factory, pool_creator, maker_signer)
        assert router.authorizedPools(pool)
        target._mint_for_testing(pool, 1_000_000 * WAD)

        _move_to_epoch(fee_collector, Epoch.COLLECT)
        amount = 1_000 * WAD
        sell_token._mint_for_testing(fee_collector, amount)
        with boa.env.prank(keeper):
            fee_collector.collect([sell_token.address], keeper)
            burner.sync_executor_approvals(ROUTER, [sell_token.address])
        assert sell_token.allowance(burner, ROUTER) == MAX_UINT256
        lot_start, lot_end = burner.window(sell_token)
        initial_amount = burner.lots(sell_token).initial_amount
        assert initial_amount > 0
        _move_to_timestamp(lot_start + 3 * STEP_DURATION)

        # The trader side of the quote request, straight from the adapter: a
        # quarter of the lot at the live quote; the market maker fills its
        # pool, a txid and a short expiry, keeps the price, and signs.
        quarter = initial_amount // 4
        template = _as_tuple(adapter.order_for(burner.address, sell_token.address, quarter))
        quote = burner.getAmountNeeded(sell_token, quarter)
        assert template == (
            bytes(32),
            burner.address,
            ZERO_ADDRESS,
            ZERO_ADDRESS,
            sell_token.address,
            target.address,
            quarter,
            quote,
            lot_end,
        )
        txid = keccak(b"quote 1")
        signed = (txid, burner.address, pool, ZERO_ADDRESS, *template[4:8], _timestamp() + 120)
        taker_signature = bytes(adapter.signature_for(signed))
        assert taker_signature == bytes.fromhex(adapter.address[2:]) + encode([QUOTE_ABI], [signed])
        digest = quote_digest_reference(signed, MAINNET_DOMAIN_SEPARATOR)
        described, described_digest = adapter.describe(taker_signature[20:])
        assert _as_tuple(described) == signed and bytes(described_digest) == digest
        with boa.env.prank(ROUTER):
            assert burner.isValidSignature(digest, taker_signature) == bytes.fromhex("1626ba7e")
            assert burner.isValidSignature(digest, taker_signature[20:]) == bytes.fromhex(
                "ffffffff"
            )

        def calldata(fields: tuple) -> tuple:
            return to_calldata(
                fields, bytes(adapter.signature_for(fields)), sign_maker(MAKER_KEY, fields, 1)
            )

        # An underpriced quote: the burner's eth_call carries the auction's
        # typed reason, the real router swallows it (SignatureChecker) and
        # reports its own invalid-signer string.
        cheaper = (*signed[:7], quote - 1, signed[8])
        with boa.env.prank(ROUTER), boa.reverts(custom_err("BadBuyAmount()")):
            burner.isValidSignature(
                quote_digest_reference(cheaper, MAINNET_DOMAIN_SEPARATOR),
                bytes(adapter.signature_for(cheaper)),
            )
        with boa.env.prank(relayer), boa.reverts(INVALID_SIGNER):
            router.tradeRFQM(calldata(cheaper))

        # tradeRFQM by any relayer: the real router verifies the digest
        # through the burner's ERC-1271 (proving the adapter's hashing),
        # pulls the quarter from the burner, and the real pool pays the
        # quote to the burner — the trader, the only account in the quote.
        with boa.env.prank(relayer):
            router.tradeRFQM(calldata(signed))
        assert sell_token.balanceOf(pool) == quarter
        assert target.balanceOf(burner) == quote
        assert target.balanceOf(fee_collector) == 0
        assert burner.available(sell_token) == initial_amount - quarter
        # Target on the burner is not inventory.
        assert burner.available(target) == 0
        # Exact fill: the txid is burnt.
        with (
            boa.env.prank(relayer),
            boa.reverts("HashflowRouter::_validateRFQMQuote txid has already been used."),
        ):
            router.tradeRFQM(calldata(signed))

        # The permissionless push forwards the proceeds to the FeeCollector.
        with boa.env.prank(keeper):
            assert burner.push_target() == quote
        assert target.balanceOf(fee_collector) == quote
        assert target.balanceOf(burner) == 0

        # The router checks the contract signature on every fill: once the
        # adapter is disabled, a fresh quote over the rest answers 0xffffffff
        # through the burner's router and the real router reports an invalid
        # signer. The allowance sync then completes the emergency procedure.
        rest = initial_amount - quarter
        fresh_template = _as_tuple(adapter.order_for(burner.address, sell_token.address))
        assert fresh_template[6] == rest
        fresh = (
            keccak(b"quote 2"),
            burner.address,
            pool,
            ZERO_ADDRESS,
            *fresh_template[4:8],
            _timestamp() + 120,
        )
        with boa.env.prank(emergency_owner):
            registry.disable_adapter(adapter)
        with boa.env.prank(relayer), boa.reverts(INVALID_SIGNER):
            router.tradeRFQM(calldata(fresh))
        with boa.env.prank(keeper):
            burner.sync_executor_approvals(ROUTER, [sell_token.address])
        assert sell_token.allowance(burner, ROUTER) == 0
        assert sell_token.balanceOf(burner) == rest
