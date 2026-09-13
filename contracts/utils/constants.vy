# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title Shared protocol constants
@author Curve Finance
@license MIT
@notice Protocol-wide scales, bounds, and interface ids shared across the
        burner stack.
@dev Stateless constants-only module; import as `constants as c`.
"""

# transfer/exchange batch bound.
MAX_COINS: constant(uint256) = 64
# Fixed-point percentage scale: 100% = 10**18.
WAD: constant(uint256) = 10**18
# Bound of the AdapterRegistry catalog.
MAX_ADAPTERS: constant(uint256) = 32

# ERC-165 interface ids (XOR of the interface's selectors).
ERC165_INTERFACE_ID: constant(bytes4) = method_id("supportsInterface(bytes4)", output_type=bytes4)
# FeeCollector burner interface:
#   ^ burn(address[],address) 0x72a436a8
#   ^ push_target() 0x2eb078cd
#   ^ VERSION() 0xffa1ad74
BURNER_INTERFACE_ID: constant(bytes4) = 0xa3b5e311

# ERC-1271: the single-selector interface id doubles as the magic return value.
ERC1271_MAGIC_VALUE: constant(bytes4) = method_id(
    "isValidSignature(bytes32,bytes)", output_type=bytes4
)  # 0x1626ba7e
