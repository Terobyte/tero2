"""Provider abstraction layer."""

from tero2.providers.cli import CLIProvider
from tero2.providers.shell import ShellProvider
from tero2.providers.registry import create_provider as create_provider, register
from tero2.providers.zai import ZaiProvider

register("shell", ShellProvider)
register("bash", ShellProvider)
register("opencode", CLIProvider)
register("codex", CLIProvider)
register("kilo", CLIProvider)
register("claude", CLIProvider)
register("zai", ZaiProvider)
