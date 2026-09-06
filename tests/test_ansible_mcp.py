"""
Unit tests for ansible_mcp.py — mocks subprocess so no real Ansible run
or SSH connection happens in CI.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import ansible_mcp  # noqa: E402


def test_rejects_disallowed_group():
    result = ansible_mcp.scan_logs(target_group="not_a_real_group", pattern="error")
    assert "not in the allowed groups" in result


@patch("ansible_mcp.subprocess.run")
def test_returns_combined_output_on_success(mock_run, tmp_path):
    combined = tmp_path / "logscan_combined.txt"
    combined.write_text("=== host1 ===\nerror: disk full\n")
    ansible_mcp.CONFIG["output"]["combined_file"] = str(combined)

    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    result = ansible_mcp.scan_logs(target_group="patching_group", pattern="error")
    assert "disk full" in result
    mock_run.assert_called_once()


@patch("ansible_mcp.subprocess.run")
def test_returns_error_on_nonzero_exit(mock_run):
    mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="unreachable hosts")
    result = ansible_mcp.scan_logs(target_group="patching_group", pattern="error")
    assert "Ansible run failed" in result
    assert "unreachable hosts" in result


def test_list_allowed_groups_returns_sorted_string():
    result = ansible_mcp.list_allowed_groups()
    groups = result.split(", ")
    assert groups == sorted(groups)
    assert "patching_group" in groups
