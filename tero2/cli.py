"""CLI entry point for tero2.

Commands:
    tero2 run <project_path> --plan <plan.md>   — run agent on project
    tero2 status <project_path>                  — show current state
    tero2 init <project_path>                    — initialize .sora/ structure
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)


def cmd_run(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    project_path = Path(args.project_path).expanduser().resolve()

    if not project_path.is_dir():
        print(f"error: {project_path} is not a directory")
        sys.exit(1)

    plan_file = Path(args.plan).expanduser()
    if not plan_file.is_absolute():
        plan_file = (project_path / plan_file).resolve()
    else:
        plan_file = plan_file.resolve()

    if not plan_file.is_file():
        print(f"error: plan file not found: {plan_file}")
        sys.exit(1)

    from tero2.config import load_config
    from tero2.runner import Runner

    config = None
    if args.config:
        config_path = Path(args.config).expanduser().resolve()
        if not config_path.is_file():
            print(f"error: config file not found: {config_path}")
            sys.exit(1)
        config = load_config(project_path, override_path=config_path)

    runner = Runner(project_path, plan_file, config=config)
    asyncio.run(runner.run())


def cmd_status(args: argparse.Namespace) -> None:
    project_path = Path(args.project_path).expanduser().resolve()
    state_file = project_path / ".sora" / "runtime" / "STATE.json"

    if not state_file.is_file():
        print("no state found — run `tero2 init` first")
        return

    from tero2.state import AgentState

    state = AgentState.from_file(state_file)
    print(f"phase:    {state.phase.value}")
    print(f"task:     {state.current_task or '(none)'}")
    print(f"retry:    {state.retry_count}")
    print(f"steps:    {state.steps_in_task}")
    print(f"checkpoint: {state.last_checkpoint or '(none)'}")
    if state.error_message:
        print(f"error:    {state.error_message}")


def cmd_init(args: argparse.Namespace) -> None:
    project_path = Path(args.project_path).expanduser().resolve()

    if not project_path.is_dir():
        print(f"error: {project_path} is not a directory")
        sys.exit(1)

    from tero2.disk_layer import DiskLayer

    disk = DiskLayer(project_path)
    disk.init()
    print(f"initialized .sora/ in {project_path}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tero2", description="Immortal Runner")

    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run agent on a project with a plan")
    run_parser.add_argument("project_path", help="Path to the project root")
    run_parser.add_argument("--plan", required=True, help="Path to the markdown plan file")
    run_parser.add_argument("--config", help="Override config file path")
    run_parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    run_parser.set_defaults(func=cmd_run)

    status_parser = subparsers.add_parser("status", help="Show current runner state")
    status_parser.add_argument("project_path", help="Path to the project root")
    status_parser.set_defaults(func=cmd_status)

    init_parser = subparsers.add_parser("init", help="Initialize .sora/ directory structure")
    init_parser.add_argument("project_path", help="Path to the project root")
    init_parser.set_defaults(func=cmd_init)

    return parser
