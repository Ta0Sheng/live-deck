"""命令行：子命令存在、版本号、persona 能打印。"""

from __future__ import annotations

import pytest

from live_deck import cli


def test_parser_has_expected_commands():
    parser = cli.build_parser()
    # argparse 把子命令放在 _subparsers 里，不依赖内部结构也能验证行为
    args = parser.parse_args(["doctor"])
    assert args.func is cli.cmd_doctor
    args = parser.parse_args(["demo", "--no-open"])
    assert args.func is cli.cmd_demo


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "live-deck" in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_persona_command(capsys):
    assert cli.main(["persona", "--lines", "3"]) == 0
    out = capsys.readouterr().out
    assert "人格文件" in out
    assert "大肥鱼" in out


def test_eval_subcommands_parse():
    parser = cli.build_parser()
    args = parser.parse_args(["eval", "make"])
    assert args.func is cli.cmd_eval
    assert args.eval_action == "make"
    args = parser.parse_args(["eval", "run", "--auto"])
    assert args.auto is True
    args = parser.parse_args(["eval", "report", "--runs", "5"])
    assert args.runs == 5
