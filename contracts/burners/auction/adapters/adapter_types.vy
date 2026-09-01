# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title Adapter types
@author Curve Finance
@license MIT
@notice Shared adapter-registry types and signature bounds.
@dev Stateless module. Signature routing is shape-based: a signature whose
     first 20 bytes name an enabled verifier is forwarded to it with the
     prefix stripped; anything else (the historical CoW encodings, which
     start with the zero padding of an ABI address head) goes to the
     configured fallback adapter verbatim. Signature bytes carry no
     authority by themselves: verifiers prove protocol digests and the
     auction's check_order view prices every fill.
"""


# The registry maps a verifier address (the routing key and the adapter's
# identity — a new adapter version is a new verifier deployment) to this
# config. The executor is the protocol contract that pulls sold tokens and
# therefore the approval target while any enabled adapter references it.
struct AdapterConfig:
    executor: address
    active: bool


# Upper bound for ERC-1271 signature bytes accepted by the router and its
# verifiers. Covers the largest historical CoW wrapper encoding with headroom
# for prefixed adapter payloads.
MAX_SIGNATURE_LEN: constant(uint256) = 4096
ADAPTER_PREFIX_LEN: constant(uint256) = 20
