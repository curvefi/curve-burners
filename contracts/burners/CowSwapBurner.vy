# @version 0.3.10
"""
@title Burner
@notice Exchange tokens using CowSwap
"""


interface ERC20:
    def approve(_to: address, _value: uint256): nonpayable
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable
    def balanceOf(_owner: address) -> uint256: view

ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE


enum Epoch:
    SLEEP  # 1
    COLLECT  # 2
    EXCHANGE  # 4
    FORWARD  # 8

interface FeeCollector:
    def target() -> ERC20: view
    def owner() -> address: view
    def emergency_owner() -> address: view
    def epoch_time_frame(epoch: Epoch, ts: uint256=block.timestamp) -> (uint256, uint256): view
    def exchange(_coins: DynArray[ERC20, MAX_COINS_LEN]) -> bool: view

MAX_COINS_LEN: constant(uint256) = 64

fee_collector: public(immutable(FeeCollector))


struct GPv2Order_Data:
    sellToken: ERC20  # token to sell
    buyToken: ERC20  # token to buy
    receiver: address  # receiver of the token to buy
    sellAmount: uint256
    buyAmount: uint256
    validTo: uint32  # timestamp until order is valid
    appData: bytes32  # extra info about the order
    feeAmount: uint256  # amount of fees in sellToken
    kind: bytes32  # buy or sell
    partiallyFillable: bool  # partially fillable (True) or fill-or-kill (False)
    sellTokenBalance: bytes32  # From where the sellToken balance is withdrawn
    buyTokenBalance: bytes32  # Where the buyToken is deposited

struct ConditionalOrderParams:
    # The contract implementing the conditional order logic
    handler: address  # self
    # Allows for multiple conditional orders of the same type and data
    salt: bytes32  # Not used for now
    # Data available to ALL discrete orders created by the conditional order
    staticData: Bytes[STATIC_DATA_LEN]  # Using coin address

interface ComposableCow:
    def create(params: ConditionalOrderParams, dispatch: bool): nonpayable

STATIC_DATA_LEN: constant(uint256) = 20
OFFCHAIN_DATA_LEN: constant(uint256) = 1

vault_relayer: public(immutable(address))
composable_cow: public(immutable(ComposableCow))
app_data: public(immutable(bytes32))
sell_kind: public(immutable(bytes32))  # Surpluss in target coin


SUPPORTED_INTERFACES: constant(bytes4[3]) = [
    # ERC165: method_id("supportsInterface(bytes4)") == 0x01ffc9a7
    0x01ffc9a7,
    # Burner:
    #   method_id("burn(address[],address)") == 0x72a436a8
    #   method_id("push_target()") == 0x2eb078cd
    # 0x5c144e65
    0x5c144e65,
    # Interface corresponding to IConditionalOrderGenerator:
    #   method_id("getTradeableOrder(address,address,bytes32,bytes,bytes)") == 0xb8296fc4
    0xb8296fc4,
]

created: public(HashMap[ERC20, bool])


@external
def __init__(_fee_collector: FeeCollector,
    _composable_cow: ComposableCow, _vault_relayer: address):
    """
    @notice Contract constructor
    @param _fee_collector FeeCollector to anchor to
    @param _composable_cow Address of ComposableCow contract
    @param _vault_relayer CowSwap's VaultRelayer contract address, all approves go there
    """
    fee_collector = _fee_collector
    vault_relayer = _vault_relayer
    composable_cow = _composable_cow

    app_data = keccak256("curve")
    sell_kind = keccak256("sell")


@external
def burn(_coins: DynArray[ERC20, MAX_COINS_LEN], _receiver: address):
    """
    @notice Post hook after collect to register coins for burn
    @dev Registers new orders in ComposableCow
    @param _coins Which coins to burn
    @param _receiver Receiver of profit. Might be needed for multiple transactions actions, here is ignored
    """
    for coin in _coins:
        if not self.created[coin]:
            composable_cow.create(ConditionalOrderParams({
                handler: self,
                salt: empty(bytes32),
                staticData: concat(b"", convert(coin.address, bytes20)),
            }), True)
            coin.approve(vault_relayer, max_value(uint256))
            self.created[coin] = True


@view
@internal
def _get_order(sell_token: ERC20) -> GPv2Order_Data:
    buy_token: ERC20 = fee_collector.target()
    return GPv2Order_Data({
        sellToken: sell_token,  # token to sell
        buyToken: buy_token,  # token to buy
        receiver: fee_collector.address,  # receiver of the token to buy
        sellAmount: 0,  # Set later
        buyAmount: 0,
        validTo: convert(fee_collector.epoch_time_frame(Epoch.EXCHANGE)[1], uint32),  # timestamp until order is valid
        appData: app_data,  # extra info about the order
        feeAmount: 0,  # amount of fees in sellToken
        kind: sell_kind,  # buy or sell
        partiallyFillable: True,  # partially fillable (True) or fill-or-kill (False)
        sellTokenBalance: convert(sell_token.address, bytes32),  # From where the sellToken balance is withdrawn
        buyTokenBalance: convert(buy_token.address, bytes32),  # Where the buyToken is deposited
    })


@view
@external
def get_current_order(sell_token: address=empty(address)) -> GPv2Order_Data:
    """
    @notice Get current order parameters
    @notice sell_token Address of possible sell token
    """
    return self._get_order(ERC20(sell_token))


@view
@external
def getTradeableOrder(_owner: address, _sender: address, _ctx: bytes32, _static_input: Bytes[STATIC_DATA_LEN], _offchain_input: Bytes[OFFCHAIN_DATA_LEN]) -> GPv2Order_Data:
    """
    @notice Generate order for WatchTower
    @param _owner Owner of order (self)
    @param _sender `msg.sender` context calling `isValidSignature`
    @param _ctx Execution context
    @param _static_input sellToken encoded as bytes(Bytes[20])
    @param _offchain_input Not used, zero-length bytes
    """
    sell_token: ERC20 = ERC20(convert(convert(_static_input, bytes20), address))
    order: GPv2Order_Data = self._get_order(sell_token)
    order.sellAmount = sell_token.balanceOf(self)
    return order


@view
@external
def verify(
    _owner: address,
    _sender: address,
    _hash: bytes32,
    _domain_separator: bytes32,
    _ctx: bytes32,
    _static_input: Bytes[STATIC_DATA_LEN],
    _offchain_input: Bytes[OFFCHAIN_DATA_LEN],
    _order: GPv2Order_Data,
):
    """
    @notice Verify order
    @dev Called from ComposableCow
    @param _owner Owner of conditional order (self)
    @param _sender `msg.sender` context calling `isValidSignature`
    @param _hash `EIP-712` order digest
    @param _domain_separator `EIP-712` domain separator
    @param _ctx Execution context
    @param _static_input ConditionalOrder's staticData (coin address)
    @param _offchain_input Conditional order type-specific data NOT known at time of creation for a specific discrete order (or zero-length bytes if not applicable)
    @param _order The proposed discrete order's `GPv2Order.Data` struct
    """
    sell_token: ERC20 = ERC20(convert(convert(_static_input, bytes20), address))
    assert fee_collector.exchange([sell_token])
    # assert _owner == self
    # Any sender
    # assert _hash == ???.hash(_order, _domain_separator)
    # _domain_separator?
    # _ctx is ok if sender is
    assert _offchain_input == b""
    order: GPv2Order_Data = self._get_order(sell_token)
    order.sellAmount = _order.sellAmount  # Any amount allowed
    order.buyAmount = _order.buyAmount  # Price is discovered within CowSwap competition
    assert _abi_encode(order) == _abi_encode(_order), "OrderNotValid()"


@external
def push_target() -> uint256:
    """
    @notice In case target coin is left in contract can be pushed to forward
    @return Amount of coin pushed further
    """
    target: ERC20 = fee_collector.target()
    amount: uint256 = target.balanceOf(self)
    if amount > 0:
        target.transfer(fee_collector.address, amount)
    return amount


@pure
@external
def supportsInterface(_interface_id: bytes4) -> bool:
    """
    @dev Interface identification is specified in ERC-165.
    @param _interface_id Id of the interface
    """
    return _interface_id in SUPPORTED_INTERFACES


@external
def recover(_coins: DynArray[ERC20, MAX_COINS_LEN]):
    """
    @notice Recover ERC20 tokens or Ether from this contract
    @dev Callable only by owner and emergency owner
    @param _coins Token addresses
    """
    assert msg.sender in [fee_collector.owner(), fee_collector.emergency_owner()], "Only owner"

    for coin in _coins:
        if coin.address == ETH_ADDRESS:
            raw_call(fee_collector.address, b"", value=self.balance)
        else:
            coin.transfer(fee_collector.address, coin.balanceOf(self))  # do not need safe transfer
