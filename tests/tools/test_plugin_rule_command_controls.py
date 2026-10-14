import pytest

from tools import approval_plugin_commands


@pytest.mark.parametrize("control", [chr(value) for value in range(32) if value != 9] + [chr(127)])
def test_ascii_controls_are_rejected_before_tokenization(monkeypatch, control):
    tokenized = []
    monkeypatch.setattr(approval_plugin_commands, "_shell_segment_tokens", lambda *args: tokenized.append(args) or ["gh"])
    assert approval_plugin_commands.command_segments(f"gh issue create --title 'text{control}data' -R yashiels/x") is None
    assert tokenized == []


def test_tab_is_valid_shell_whitespace():
    assert approval_plugin_commands.command_segments("gh\tissue\tview\t3\t-R\tyashiels/x") == [
        ["gh", "issue", "view", "3", "-R", "yashiels/x"],
    ]
