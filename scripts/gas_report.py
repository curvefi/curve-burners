#!/usr/bin/env python3
"""Run tests under a per-call gas recorder and write the gas statistics of the
production contracts as JSON.

Usage: python scripts/gas_report.py [--output gas-report.json] [pytest args...]

Every external call a test makes through titanoboa is recorded with the gas
the EVM charged for its execution (intrinsic transaction gas excluded).
Reverted calls are not recorded. Per contract and function the output holds
the call count and the mean, median, minimum and maximum over all recorded
calls, aggregated across every deployed instance. Render it with
scripts/compare_gas_report.py.
"""

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import pytest

DEFAULT_TESTS = [
    "tests/burners/test_dutch_auction_v2.py",
    "tests/burners/test_auction_taker.py",
    "tests/burners/test_cow_adapter.py",
    "tests/burners/test_adapter_registry.py",
    "tests/burners/test_intent_resolver.py",
]
# Only these source trees enter the report; mocks and test harnesses do not.
REPORTED_PREFIXES = ("contracts/burners/",)
EXCLUDED_PREFIXES = ("contracts/testing/",)


class GasRecorder:
    """pytest plugin: wraps titanoboa's external-call path and records gas."""

    def __init__(self) -> None:
        self.samples: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        self._root = Path.cwd()

    def _relpath(self, contract_path: str) -> str:
        try:
            return str(Path(contract_path).resolve().relative_to(self._root))
        except ValueError:
            return contract_path

    def pytest_configure(self, config) -> None:
        from boa.contracts.vyper.vyper_contract import VyperFunction

        recorder = self
        original_call = VyperFunction.__call__

        def recording_call(fn, *args, **kwargs):
            result = original_call(fn, *args, **kwargs)
            computation = fn.contract._computation
            if computation is not None and not computation.is_error:
                path = recorder._relpath(fn.contract.compiler_data.contract_path)
                recorder.samples[path][fn.fn_ast.name].append(computation.get_gas_used())
            return result

        VyperFunction.__call__ = recording_call

    def stats(self) -> dict[str, dict[str, dict[str, int]]]:
        out: dict[str, dict[str, dict[str, int]]] = {}
        for path in sorted(self.samples):
            if not path.startswith(REPORTED_PREFIXES) or path.startswith(EXCLUDED_PREFIXES):
                continue
            out[path] = {
                fn: {
                    "calls": len(gas),
                    "mean": int(statistics.mean(gas)),
                    "median": int(statistics.median(gas)),
                    "min": min(gas),
                    "max": max(gas),
                }
                for fn, gas in sorted(self.samples[path].items())
            }
        return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--output", type=Path, default=Path("gas-report.json"))
    parser.add_argument(
        "pytest_args",
        nargs="*",
        help="pytest targets and options; defaults to the Dutch auction tests",
    )
    args = parser.parse_args()

    recorder = GasRecorder()
    pytest_args = args.pytest_args or DEFAULT_TESTS
    exit_code = pytest.main(["-q", "-p", "no:cacheprovider", *pytest_args], plugins=[recorder])
    if exit_code != 0:
        print(f"pytest exited with {exit_code}; no report written", file=sys.stderr)
        return int(exit_code)

    import vyper

    report = {"vyper": vyper.__version__, "contracts": recorder.stats()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
