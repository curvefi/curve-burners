"""Build the emergency adapter-disable governance bundle for DutchAuctionBurner.

``disable_adapter(verifier)`` alone only removes the adapter from the router
and releases its executor reference — ERC-20 allowances toward the executor
(the CoW vault relayer for the CowAdapter) stay at max until the
permissionless ``sync_executor_approvals`` clears them. The emergency owner is
a multisig, so the approved runbook batches both calls into a single
transaction: this script prints the (target, calldata) pairs for that batch.

The burner has no on-chain enumeration of staged tokens: the operator must
supply the full token list off-chain (any token ever staged while the adapter
was enabled). Tokens missed here can still be cleared later by anyone via the
permissionless sync.

Usage:
    python scripts/emergency_cow_disable.py <burner> <verifier> <executor> <token> [<token> ...]

The verifier is the adapter address (the CowAdapter for CoW); the executor is
``registry.get_adapter(verifier).executor`` (the vault relayer for CoW). Note:
sync only clears once the executor's refcount is zero — if other enabled
adapters still reference it, the sync leg keeps allowances at max by design.
"""

import sys

from eth_abi import encode
from eth_utils import keccak, to_checksum_address


def build_bundle(
    burner: str, verifier: str, executor: str, tokens: list[str]
) -> list[tuple[str, str]]:
    """Return (target, calldata-hex) pairs for one atomic multisig batch."""
    burner = to_checksum_address(burner)
    disable = keccak(text="disable_adapter(address)")[:4] + encode(
        ["address"], [to_checksum_address(verifier)]
    )
    sync = keccak(text="sync_executor_approvals(address,address[])")[:4] + encode(
        ["address", "address[]"],
        [to_checksum_address(executor), [to_checksum_address(t) for t in tokens]],
    )
    return [(burner, "0x" + disable.hex()), (burner, "0x" + sync.hex())]


def main(argv: list[str]) -> None:
    if len(argv) < 4:
        raise SystemExit(__doc__)
    for target, calldata in build_bundle(argv[0], argv[1], argv[2], argv[3:]):
        print(f"{target} {calldata}")


if __name__ == "__main__":
    main(sys.argv[1:])
