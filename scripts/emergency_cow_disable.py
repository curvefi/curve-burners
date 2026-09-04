"""Build the emergency adapter-disable bundle for DutchAuctionBurner.

Usage:
    python scripts/emergency_cow_disable.py <registry> <burner> <adapter> <executor> <token> [<token> ...]

Prints the (target, calldata) pairs of one multisig batch:
``registry.disable_adapter(adapter)`` and
``burner.sync_executor_approvals(executor, tokens)``. Note: the sync only
clears allowances once ``registry.is_executor_active(executor)`` is false — if
other active adapters still reference the executor, the sync leg keeps them at
max by design. Runbook: contracts/burners/README.md ("Emergency procedure").
"""

import sys

from eth_abi import encode
from eth_utils import keccak, to_checksum_address


def build_bundle(
    registry: str, burner: str, adapter: str, executor: str, tokens: list[str]
) -> list[tuple[str, str]]:
    """Return (target, calldata-hex) pairs for one atomic multisig batch."""
    registry = to_checksum_address(registry)
    burner = to_checksum_address(burner)
    disable = keccak(text="disable_adapter(address)")[:4] + encode(
        ["address"], [to_checksum_address(adapter)]
    )
    sync = keccak(text="sync_executor_approvals(address,address[])")[:4] + encode(
        ["address", "address[]"],
        [to_checksum_address(executor), [to_checksum_address(t) for t in tokens]],
    )
    return [(registry, "0x" + disable.hex()), (burner, "0x" + sync.hex())]


def main(argv: list[str]) -> None:
    if len(argv) < 5:
        raise SystemExit(__doc__)
    for target, calldata in build_bundle(argv[0], argv[1], argv[2], argv[3], argv[4:]):
        print(f"{target} {calldata}")


if __name__ == "__main__":
    main(sys.argv[1:])
