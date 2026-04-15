"""Provider abstraction layer."""

from tero2.providers.cli import CLIProvider
from tero2.providers.shell import ShellProvider
from tero2.providers.registry import create_provider as create_provider, register

register("shell", ShellProvider)
register("bash", ShellProvider)
register("opencode", CLIProvider)
register("codex", CLIProvider)
register("kilo", CLIProvider)
register("claude", CLIProvider)
