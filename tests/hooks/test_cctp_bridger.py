"""CCTPBridger against a TokenMessenger mock with a per-message burn limit."""

import boa
import pytest

USDC = 10**6
DOMAIN = 0
LIMIT = 1_000_000 * USDC


@pytest.fixture(autouse=True)
def anchor():
    with boa.env.anchor():
        yield


@pytest.fixture(scope="module")
def hooker():
    return boa.env.generate_address("hooker")


@pytest.fixture(scope="module")
def token():
    return boa.load("contracts/testing/ERC20Mock.vy", "USD Coin", "USDC", 6)


@pytest.fixture(scope="module")
def messenger(token):
    messenger = boa.load("contracts/testing/hooks/TokenMessengerMock.vy")
    messenger.set_burn_limit(token.address, LIMIT)
    return messenger


@pytest.fixture(scope="module")
def bridger(messenger):
    return boa.load("contracts/hooks/CCTPBridger.vy", messenger.address, DOMAIN)


@pytest.fixture
def funded(hooker, token, bridger):
    token._mint_for_testing(hooker, 2_500_000 * USDC)
    with boa.env.prank(hooker):
        token.approve(bridger.address, 2**256 - 1)


def test_deploy(bridger, messenger):
    assert bridger.TOKEN_MESSENGER() == messenger.address
    assert bridger.DESTINATION_DOMAIN() == DOMAIN
    assert bridger.cost() == 0
    with boa.reverts("Bad messenger"):
        boa.load("contracts/hooks/CCTPBridger.vy", "0x" + "00" * 20, DOMAIN)


def test_bridge_deposits_for_burn(funded, bridger, messenger, token, hooker):
    receiver = boa.env.generate_address("receiver")
    with boa.env.prank(hooker):
        sent = bridger.bridge(token.address, receiver, 100 * USDC, 0)
    assert sent == 100 * USDC
    assert messenger.last_amount() == 100 * USDC
    assert messenger.last_domain() == DOMAIN
    assert bytes(messenger.last_recipient()) == bytes(12) + bytes.fromhex(receiver[2:])
    assert messenger.last_token() == token.address
    assert token.balanceOf(bridger.address) == 0


def test_bridge_caps_at_burn_limit(funded, bridger, messenger, token, hooker):
    with boa.env.prank(hooker):
        sent = bridger.bridge(token.address, hooker, 2**256 - 1, 0)
    assert sent == LIMIT
    assert token.balanceOf(hooker) == 1_500_000 * USDC


def test_donation_cannot_push_the_deposit_over_the_limit(funded, bridger, messenger, token, hooker):
    token._mint_for_testing(bridger.address, 5 * USDC)  # stuck tokens
    with boa.env.prank(hooker):
        sent = bridger.bridge(token.address, hooker, LIMIT, 0)
    assert sent == LIMIT
    assert messenger.last_amount() == LIMIT
    assert token.balanceOf(bridger.address) == 5 * USDC  # swept by a later call
    with boa.env.prank(hooker):
        sent = bridger.bridge(token.address, hooker, 10 * USDC, 0)
    assert sent == 15 * USDC
    assert token.balanceOf(bridger.address) == 0


def test_below_min_amount_and_zero_return_zero(funded, bridger, messenger, token, hooker):
    with boa.env.prank(hooker):
        assert bridger.bridge(token.address, hooker, 50 * USDC, 100 * USDC) == 0
        assert bridger.bridge(token.address, hooker, 0, 0) == 0
    assert messenger.nonce() == 0


def test_unsupported_token_rejected(bridger, hooker):
    other = boa.load("contracts/testing/ERC20Mock.vy", "Other", "OTH", 18)
    with boa.env.prank(hooker), boa.reverts("Unsupported token"):
        bridger.bridge(other.address, hooker, 10**18, 0)
