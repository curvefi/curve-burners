#!/usr/bin/env python3
"""Render a gas report (scripts/gas_report.py JSON) as markdown, against a
base report when one is given.

Usage: python scripts/compare_gas_report.py --head head.json [--base base.json]
       [--output gas-report.md]

With a base, every function shows the base and head medians and their delta;
functions absent from one side are marked. Without a base (or with an
unreadable one), the head statistics are listed on their own.
"""

import argparse
import json
import sys
from pathlib import Path

Stats = dict[str, int]
Report = dict[str, dict[str, Stats]]


def load(path: Path | None) -> tuple[Report, str] | None:
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return data["contracts"], data.get("vyper", "?")
    except (ValueError, KeyError):
        return None


def fmt_delta(base: int, head: int) -> str:
    delta = head - base
    if delta == 0:
        return "0"
    pct = f" ({delta / base:+.1%})" if base else ""
    return f"{delta:+d}{pct}"


def render_head_only(head: Report, vyper_version: str) -> list[str]:
    lines = [f"Execution gas per external call, intrinsic gas excluded (vyper {vyper_version})."]
    for contract, fns in head.items():
        lines += [
            "",
            f"### `{contract}`",
            "",
            "| Function | Calls | Mean | Median | Min | Max |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for fn, s in sorted(fns.items(), key=lambda item: -item[1]["median"]):
            lines.append(
                f"| {fn} | {s['calls']} | {s['mean']} | {s['median']} | {s['min']} | {s['max']} |"
            )
    return lines


def render_diff(base: Report, head: Report, vyper_version: str) -> list[str]:
    lines = [
        "Median execution gas per external call, intrinsic gas excluded "
        f"(vyper {vyper_version}); delta is head minus base."
    ]
    for contract in sorted(set(base) | set(head)):
        base_fns = base.get(contract, {})
        head_fns = head.get(contract, {})
        rows = []
        for fn in sorted(set(base_fns) | set(head_fns)):
            b, h = base_fns.get(fn), head_fns.get(fn)
            if b and h:
                rows.append((abs(h["median"] - b["median"]), fn, b, h))
            else:
                rows.append((sys.maxsize, fn, b, h))  # added or removed: list first
        rows.sort(key=lambda row: (-row[0], row[1]))
        lines += [
            "",
            f"### `{contract}`",
            "",
            "| Function | Calls | Base | Head | Delta |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for _, fn, b, h in rows:
            if b and h:
                delta = fmt_delta(b["median"], h["median"])
                lines.append(f"| {fn} | {h['calls']} | {b['median']} | {h['median']} | {delta} |")
            elif h:
                lines.append(f"| {fn} | {h['calls']} | — | {h['median']} | new |")
            else:
                lines.append(f"| {fn} | — | {b['median']} | — | removed |")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--head", type=Path, required=True)
    parser.add_argument("--base", type=Path)
    parser.add_argument("--output", type=Path, default=Path("gas-report.md"))
    args = parser.parse_args()

    head = load(args.head)
    if head is None:
        print(f"unreadable head report: {args.head}", file=sys.stderr)
        return 1
    base = load(args.base)

    lines = ["## Gas report", ""]
    if base is None:
        if args.base is not None:
            lines.append("_Base report unavailable; head only._")
            lines.append("")
        lines += render_head_only(head[0], head[1])
    else:
        lines += render_diff(base[0], head[0], head[1])
    args.output.write_text("\n".join(lines) + "\n")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
