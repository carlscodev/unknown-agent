"""Host abstraction: OS detection, executable lookup, shell and process execution."""

from unknown_agent.host.info import HostInfo, detect_host, detect_shell, find_executable, is_ci, is_interactive
from unknown_agent.host.process import ProcessResult, run_process, split_command
from unknown_agent.host.shell import CommandPlan, normalize_package_manager_command, plan_command, uses_shell_operators

__all__ = [
    "HostInfo",
    "detect_host",
    "detect_shell",
    "find_executable",
    "is_ci",
    "is_interactive",
    "ProcessResult",
    "run_process",
    "split_command",
    "CommandPlan",
    "plan_command",
    "uses_shell_operators",
    "normalize_package_manager_command",
]
