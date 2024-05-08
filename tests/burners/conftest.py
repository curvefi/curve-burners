import pytest


@pytest.fixture(scope="module")
def coins(coins, target):
    return [coin for coin in coins if coin != target]
