# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title Adapter types
@author Curve Finance
@license MIT
@notice Shared adapter-registry types and the adapter signature format.
@dev Stateless module. Every ERC-1271 signature an auction accepts follows one
     template:

         signature = verifier_address (20 bytes) ++ payload

     The verifier address is the registry adapter's identity; the auction's
     router strips it and forwards `payload` to that verifier's
     isValidSignature(hash, payload). The payload is whatever the verifier's
     protocol needs to rebuild the digest it is asked about (for CoW: the
     abi-encoded GPv2Order), so signature bytes carry no authority by
     themselves — verifiers prove protocol digests and the auction's
     check_order view prices every fill. Unprefixed bytes select no adapter
     and are invalid.
"""


# The registry maps a verifier address (the routing key and the adapter's
# identity — a new adapter version is a new verifier deployment) to this
# config. The executor is the protocol contract that pulls sold tokens and
# therefore the approval target while any enabled adapter references it.
struct AdapterConfig:
    executor: address
    active: bool


# Upper bound for ERC-1271 signature bytes accepted by the router and its
# verifiers; generous headroom over prefixed adapter payloads.
MAX_SIGNATURE_LEN: constant(uint256) = 4096
# The verifier address opening every adapter signature.
ADAPTER_PREFIX_LEN: constant(uint256) = 20
