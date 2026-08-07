"""Build the emergency CoW-disable governance bundle for DutchAuctionBurner.

``disable_cow()`` alone only flips the rail off and releases the router
reference — ERC-20 allowances toward the vault relayer stay at max until the
permissionless ``sync_router_approvals`` clears them. The emergency owner is a
multisig, so the approved runbook batches both calls into a single
transaction: this script prints the (target, calldata) pairs for that batch.

The burner has no on-chain enumeration of staged tokens: the operator must
supply the full token list off-chain (any token ever staged while CoW was
enabled). Tokens missed here can still be cleared later by anyone via the
permissionless sync.

Usage:
    python scripts/emergency_cow_disable.py <burner> <vault_relayer> <token> [<token> ...]

The vault relayer is ``burner.vault_relayer()``; the same pattern applies to
adapter routers after ``disable_adapter`` (sync the released
``adapter_router``).
"""

import sys

from eth_abi import encode
from eth_utils import keccak, to_checksum_address


def build_bundle(
    burner: str, vault_relayer: str, tokens: list[str]
) -> list[tuple[str, str]]:
    """Return (target, calldata-hex) pairs for one atomic multisig batch."""
    burner = to_checksum_address(burner)
    disable = keccak(text="disable_cow()")[:4]
    sync = keccak(text="sync_router_approvals(address,address[])")[:4] + encode(
        ["address", "address[]"],
        [to_checksum_address(vault_relayer), [to_checksum_address(t) for t in tokens]],
    )
    return [(burner, "0x" + disable.hex()), (burner, "0x" + sync.hex())]


def main(argv: list[str]) -> None:
    if len(argv) < 3:
        raise SystemExit(__doc__)
    for target, calldata in build_bundle(argv[0], argv[1], argv[2:]):
        print(f"{target} {calldata}")


if __name__ == "__main__":
    main(sys.argv[1:])
