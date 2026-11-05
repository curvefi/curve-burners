"""LZOFTBridger against an OFT mock that folds in the endpoint's fee and refund rules."""

import boa
import pytest

WAD = 10**18
DST_EID = 30255
RATE = 10**12  # 18 local decimals, 6 shared
NATIVE_FEE = 10**16
QUOTE_AMOUNT = 1000 * WAD
OPTIONS = bytes.fromhex("0003010011010000000000000000000000000000030d40")


@pytest.fixture(autouse=True)
def anchor():
    with boa.env.anchor():
        yield


@pytest.fixture(scope="module")
def keeper():
    keeper = boa.env.generate_address("keeper")
    boa.env.set_balance(keeper, 100 * WAD)
    return keeper


@pytest.fixture(scope="module")
def token():
    return boa.load("contracts/testing/ERC20Mock.vy", "Frax USD", "frxUSD", 18)


@pytest.fixture(scope="module")
def oft(token):
    oft = boa.load("contracts/testing/hooks/OFTMock.vy", token.address, True, RATE, NATIVE_FEE)
    oft.set_peer(DST_EID, bytes(12) + bytes.fromhex("96a394058e2b84a89bac9667b19661ed003cf5d4"))
    oft.set_enforced_options(DST_EID, 1, OPTIONS)
    return oft


@pytest.fixture(scope="module")
def bridger(oft):
    return boa.load("contracts/hooks/LZOFTBridger.vy", oft.address, DST_EID, QUOTE_AMOUNT, b"")


@pytest.fixture(scope="module")
def hooker():
    """A Hooker-like caller: forwards value to bridge() and cannot receive ETH."""
    return boa.loads(
        """
# pragma version 0.5.0b1
from ethereum.ercs import IERC20

interface Bridger:
    def bridge(_token: address, _to: address, _amount: uint256, _min_amount: uint256, _refund: address) -> uint256: payable
    def bridge_default(_token: address, _to: address, _amount: uint256, _min_amount: uint256) -> uint256: payable

@external
@payable
def call_bridge(_bridger: address, _token: address, _to: address, _amount: uint256, _min_amount: uint256, _refund: address) -> uint256:
    assert extcall IERC20(_token).approve(_bridger, max_value(uint256), default_return_value=True)
    return extcall Bridger(_bridger).bridge(_token, _to, _amount, _min_amount, _refund, value=msg.value)

@external
@payable
def call_bridge_default_refund(_bridger: address, _token: address, _to: address, _amount: uint256, _min_amount: uint256) -> uint256:
    assert extcall IERC20(_token).approve(_bridger, max_value(uint256), default_return_value=True)
    data: Bytes[256] = abi_encode(_token, _to, _amount, _min_amount, method_id=method_id("bridge(address,address,uint256,uint256)"))
    response: Bytes[32] = raw_call(_bridger, data, value=msg.value, max_outsize=32)
    return abi_decode(response, uint256)
"""
    )


@pytest.fixture
def funded_hooker(hooker, token):
    token._mint_for_testing(hooker.address, 10 * WAD + 123_456)
    return hooker


def receiver_word(address: str) -> bytes:
    return bytes(12) + bytes.fromhex(address[2:])


# Deployment


def test_deploy_pins_oft_wiring(bridger, oft, token):
    assert bridger.OFT_CONTRACT() == oft.address
    assert bridger.DST_EID() == DST_EID
    assert bridger.TOKEN() == token.address
    assert bridger.EXTRA_OPTIONS() == b""


def test_deploy_rejects_missing_peer(oft):
    with boa.reverts("No peer"):
        boa.load("contracts/hooks/LZOFTBridger.vy", oft.address, 30101, QUOTE_AMOUNT, b"")


def test_deploy_requires_options_from_somewhere(token):
    bare = boa.load("contracts/testing/hooks/OFTMock.vy", token.address, True, RATE, NATIVE_FEE)
    bare.set_peer(DST_EID, bytes(32)[:-1] + b"\x01")
    with boa.reverts("No options"):
        boa.load("contracts/hooks/LZOFTBridger.vy", bare.address, DST_EID, QUOTE_AMOUNT, b"")
    with_options = boa.load(
        "contracts/hooks/LZOFTBridger.vy", bare.address, DST_EID, QUOTE_AMOUNT, OPTIONS
    )
    assert with_options.EXTRA_OPTIONS() == OPTIONS


# Bridging


def test_bridge_exact_fee_from_contract_caller(funded_hooker, bridger, oft, token, keeper):
    receiver = boa.env.generate_address("receiver")
    with boa.env.prank(keeper):
        sent = funded_hooker.call_bridge(
            bridger.address, token.address, receiver, 4 * WAD + 999, 0, keeper, value=NATIVE_FEE
        )
    assert sent == 4 * WAD  # dust below the shared decimals stays with the caller
    assert token.balanceOf(oft.address) == 4 * WAD
    assert token.balanceOf(funded_hooker.address) == 6 * WAD + 123_456
    param = oft.last_send()
    assert param.dstEid == DST_EID
    assert bytes(param.to) == receiver_word(receiver)
    assert param.amountLD == param.minAmountLD == 4 * WAD
    assert bytes(param.extraOptions) == b""
    assert oft.last_fee().nativeFee == NATIVE_FEE
    assert oft.last_refund() == keeper


def test_overpaid_fee_is_refunded_to_the_keeper(funded_hooker, bridger, token, keeper):
    before = boa.env.get_balance(keeper)
    with boa.env.prank(keeper):
        funded_hooker.call_bridge(
            bridger.address, token.address, keeper, WAD, 0, keeper, value=NATIVE_FEE + 10**15
        )
    assert before - boa.env.get_balance(keeper) == NATIVE_FEE
    assert boa.env.get_balance(bridger.address) == 0


def test_default_refund_to_a_contract_without_default_reverts(
    funded_hooker, bridger, token, keeper
):
    # Exact fee: nothing to refund, the Hooker-style caller works.
    with boa.env.prank(keeper):
        funded_hooker.call_bridge_default_refund(
            bridger.address, token.address, keeper, WAD, 0, value=NATIVE_FEE
        )
    # Any excess must land on msg.sender, which rejects ETH.
    with boa.env.prank(keeper), boa.reverts():
        funded_hooker.call_bridge_default_refund(
            bridger.address, token.address, keeper, WAD, 0, value=NATIVE_FEE + 1
        )


def test_underpaid_fee_reverts(funded_hooker, bridger, token, keeper):
    with boa.env.prank(keeper), boa.reverts("Bad msg.value"):
        funded_hooker.call_bridge(
            bridger.address, token.address, keeper, WAD, 0, keeper, value=NATIVE_FEE - 1
        )


def test_max_amount_bridges_the_caller_balance(funded_hooker, bridger, oft, token, keeper):
    with boa.env.prank(keeper):
        sent = funded_hooker.call_bridge(
            bridger.address, token.address, keeper, 2**256 - 1, 0, keeper, value=NATIVE_FEE
        )
    assert sent == 10 * WAD
    assert token.balanceOf(funded_hooker.address) == 123_456  # dust
    assert oft.sends() == 1


@pytest.mark.parametrize("amount", [5 * WAD, 123])  # below min_amount; dust only
def test_nothing_to_bridge_returns_the_whole_value(
    funded_hooker, bridger, oft, token, keeper, amount
):
    refund = boa.env.generate_address("refund")
    with boa.env.prank(keeper):
        out = funded_hooker.call_bridge(
            bridger.address, token.address, keeper, amount, 20 * WAD, refund, value=NATIVE_FEE
        )
    assert out == 0
    assert boa.env.get_balance(refund) == NATIVE_FEE
    assert boa.env.get_balance(bridger.address) == 0
    assert oft.sends() == 0


def test_nothing_to_bridge_with_unreachable_refund_reverts(funded_hooker, bridger, token, keeper):
    with boa.env.prank(keeper), boa.reverts():
        funded_hooker.call_bridge(
            bridger.address,
            token.address,
            keeper,
            5 * WAD,
            20 * WAD,
            funded_hooker.address,
            value=NATIVE_FEE,
        )


def test_zero_refund_address_rejected(funded_hooker, bridger, token, keeper):
    with boa.env.prank(keeper), boa.reverts("Bad refund"):
        funded_hooker.call_bridge(
            bridger.address, token.address, keeper, WAD, 0, "0x" + "00" * 20, value=NATIVE_FEE
        )


def test_wrong_token_rejected(funded_hooker, bridger, keeper):
    other = boa.load("contracts/testing/ERC20Mock.vy", "Other", "OTH", 18)
    with boa.env.prank(keeper), boa.reverts("Unsupported token"):
        funded_hooker.call_bridge(
            bridger.address, other.address, keeper, WAD, 0, keeper, value=NATIVE_FEE
        )


def test_oft_fee_cannot_shave_the_transfer(funded_hooker, bridger, oft, token, keeper):
    oft.set_fee_bps(10)
    with boa.env.prank(keeper), boa.reverts("SlippageExceeded"):
        funded_hooker.call_bridge(
            bridger.address, token.address, keeper, WAD, 0, keeper, value=NATIVE_FEE
        )


def test_extra_options_are_sent(token, oft, funded_hooker, keeper):
    bridger = boa.load(
        "contracts/hooks/LZOFTBridger.vy", oft.address, DST_EID, QUOTE_AMOUNT, OPTIONS
    )
    with boa.env.prank(keeper):
        funded_hooker.call_bridge(
            bridger.address, token.address, keeper, WAD, 0, keeper, value=NATIVE_FEE
        )
    assert bytes(oft.last_send().extraOptions) == OPTIONS


# Views


def test_cost_quotes_default_and_explicit_amounts(bridger, oft):
    assert bridger.cost() == NATIVE_FEE
    assert bridger.cost(5 * WAD) == NATIVE_FEE
    oft.set_native_fee(2 * NATIVE_FEE)
    assert bridger.cost() == 2 * NATIVE_FEE
    with boa.reverts("Dust amount"):
        bridger.cost(RATE - 1)


def test_check_is_permissionless(bridger, keeper):
    assert bridger.check(keeper)
