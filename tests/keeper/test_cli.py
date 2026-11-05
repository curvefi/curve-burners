"""The command line: ``python -m keepers.auction_keeper run|status``."""

import pytest

from keepers.auction_keeper.cli import build_parser, cmd_run, cmd_status, main


def test_run_arguments():
    args = build_parser().parse_args(
        ["run", "--config", "configs/x.yaml", "--network", "a", "--network", "b", "--once"]
    )
    assert args.func is cmd_run and args.config == "configs/x.yaml"
    assert args.network == ["a", "b"] and args.once and not args.dry_run


def test_status_arguments_and_defaults():
    args = build_parser().parse_args(["status", "--config", "x.yaml"])
    assert args.func is cmd_status and args.network == [] and not args.verbose
    assert build_parser().parse_args(["-v", "run", "--config", "x.yaml", "--dry-run"]).verbose


@pytest.mark.parametrize("argv", [[], ["run"], ["status"], ["serve", "--config", "x.yaml"]])
def test_missing_command_or_config_is_a_usage_error(argv):
    with pytest.raises(SystemExit):
        main(argv)
