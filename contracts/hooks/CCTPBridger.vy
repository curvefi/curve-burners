# pragma version 0.4.3
"""
@title CCTPBridger
@license MIT
@author Curve Finance
@notice Circle CCTP bridge wrapper
"""


interface ERC20:
    def allowance(_owner: address, _spender: address) -> uint256: view
    def balanceOf(_owner: address) -> uint256: view
    def approve(_spender: address, _amount: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _amount: uint256) -> bool: nonpayable


interface TokenMinter:
    def burnLimitsPerMessage(_token: address) -> uint256: view


interface TokenMessenger:
    def localMinter() -> TokenMinter: view
    def depositForBurn(
        _amount: uint256,
        _destinationDomain: uint32,
        _mintRecipient: bytes32,
        _burnToken: address,
    ) -> uint64: nonpayable


TOKEN_MESSENGER: immutable(TokenMessenger)
DESTINATION_DOMAIN: immutable(uint32)


@deploy
def __init__(_token_messenger: TokenMessenger, _destination_domain: uint32):
    assert _token_messenger.address != empty(address), "Bad messenger"

    TOKEN_MESSENGER = _token_messenger
    DESTINATION_DOMAIN = _destination_domain


@external
def bridge(_token: address, _to: address, _amount: uint256, _min_amount: uint256=0) -> uint256:
    """
    @notice Bridge a CCTP burn token
    @param _token CCTP burn token address
    @param _to The receiver on the destination chain
    @param _amount The amount of `_token` to bridge
    @param _min_amount Minimum amount when to bridge
    @return Bridged amount
    """
    token: ERC20 = ERC20(_token)
    amount: uint256 = _amount

    if amount == max_value(uint256):
        amount = staticcall token.balanceOf(msg.sender)

    minter: TokenMinter = staticcall TOKEN_MESSENGER.localMinter()
    burn_limit: uint256 = staticcall minter.burnLimitsPerMessage(_token)
    assert burn_limit > 0, "Unsupported token"
    amount = min(amount, burn_limit)
    if amount < _min_amount:
        return 0

    assert extcall token.transferFrom(msg.sender, self, amount, default_return_value=True)

    if staticcall token.allowance(self, TOKEN_MESSENGER.address) < amount:
        assert extcall token.approve(TOKEN_MESSENGER.address, max_value(uint256), default_return_value=True)

    amount = staticcall token.balanceOf(self)
    extcall TOKEN_MESSENGER.depositForBurn(
        amount,
        DESTINATION_DOMAIN,
        convert(_to, bytes32),
        _token,
    )
    return amount


@pure
@external
def cost() -> uint256:
    """
    @notice Cost in ETH to bridge
    @return Amount of ETH to include
    """
    return 0


@pure
@external
def check(_account: address) -> bool:
    """
    @notice Check if `_account` may bridge via `transmit_emissions`
    @param _account The account to check
    @return True if `_account` may bridge
    """
    return True
