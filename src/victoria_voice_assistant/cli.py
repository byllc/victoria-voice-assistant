from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

import uvicorn

from victoria_voice_assistant.config.loader import (
    ConfigError,
    default_config_dir,
    load_config,
)
from victoria_voice_assistant.config.schema import AppConfig
from victoria_voice_assistant.tools.registry import Services, ToolRegistry, build_registry
from victoria_voice_assistant.tools.testing import format_report, format_status, run_tool_tests
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService
from victoria_voice_assistant.launchd import install_launch_agent, install_web_launch_agent
from victoria_voice_assistant.logging_config import configure_logging


def _registry(config: AppConfig) -> ToolRegistry:
    services = Services(timers=TimerService(config.tools.timers), weather=WeatherService(config.tools.weather))
    return build_registry(config.tools, config.agents, services)


def _config_dir(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else default_config_dir()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="assistant")
    commands = parser.add_subparsers(dest="command", required=True)

    config_command = commands.add_parser("config", help="configuration operations")
    config_subcommands = config_command.add_subparsers(dest="config_command", required=True)
    init = config_subcommands.add_parser("init", help="copy example YAML into config/")
    init.add_argument("--config-dir", help="destination directory (default: config/)")
    check = config_subcommands.add_parser("check", help="validate all configuration files")
    check.add_argument("--config-dir", help="configuration directory (default: config/)")

    tools = commands.add_parser("tools", help="tool registry operations")
    tools_subcommands = tools.add_subparsers(dest="tools_command", required=True)
    status = tools_subcommands.add_parser("status", help="list tools, stubs, and why each stub is one")
    status.add_argument("--config-dir", help="configuration directory (default: config/)")
    test = tools_subcommands.add_parser("test", help="run example phrases and tests against the router model")
    test.add_argument("--config-dir", help="configuration directory (default: config/)")
    test.add_argument("--only", help="only this tool's examples and tests")
    test.add_argument(
        "--min-accuracy",
        type=float,
        default=1.0,
        help="exit with an error below this pass rate, 0-1 (default: 1.0)",
    )

    serve = commands.add_parser("serve", help="run assistant health service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=int(os.environ.get("ASSISTANT_PORT", "8765")))

    web = commands.add_parser("web", help="run the household web chat (login + static client)")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=int(os.environ.get("ASSISTANT_WEB_PORT", "8766")))

    launchd = commands.add_parser("launchd", help="manage the macOS LaunchAgent")
    launchd_subcommands = launchd.add_subparsers(dest="launchd_command", required=True)
    install = launchd_subcommands.add_parser("install", help="write assistant LaunchAgent plist")
    install.add_argument("--output", type=Path)
    install.add_argument("--load", action="store_true", help="bootstrap LaunchAgent now")
    install_web = launchd_subcommands.add_parser("install-web", help="write web LaunchAgent plist")
    install_web.add_argument("--output", type=Path)
    install_web.add_argument("--load", action="store_true", help="bootstrap LaunchAgent now")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "config" and args.config_command == "check":
        directory = _config_dir(args.config_dir)
        try:
            load_config(directory)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Configuration valid: {directory}")
        for entry in _registry(load_config(directory)).stubs():
            if entry.status == "auto_stub":
                print(f"Warning: tool {entry.name!r} is an automatic stub: {entry.reason}")
        return 0

    if args.command == "tools":
        logging.getLogger().setLevel(logging.ERROR)
        directory = _config_dir(args.config_dir)
        try:
            config = load_config(directory)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.tools_command == "status":
            print(format_status(_registry(config)))
            return 0
        results = asyncio.run(run_tool_tests(config, only=args.only))
        if not results:
            print("No examples or tests found in tools.yaml.")
            return 1
        print(format_report(results))
        accuracy = sum(result.passed for result in results) / len(results)
        return 0 if accuracy >= args.min_accuracy else 1

    if args.command == "config" and args.config_command == "init":
        destination = _config_dir(args.config_dir)
        examples = Path(__file__).resolve().parents[2] / "config" / "examples"
        filenames = ("models.yaml", "tools.yaml", "agents.yaml")
        existing = [name for name in filenames if (destination / name).exists()]
        if existing:
            print(
                f"Refusing to overwrite existing config files: {', '.join(existing)}",
                file=sys.stderr,
            )
            return 1
        destination.mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            shutil.copyfile(examples / filename, destination / filename)
        print(f"Example configuration copied to {destination}")
        print(f"Copy {examples / '.env.example'} to {destination / '.env'} and set secrets.")
        return 0

    if args.command == "serve":
        uvicorn.run(
            "victoria_voice_assistant.app:app",
            host=args.host,
            port=args.port,
            log_config=None,
        )
        return 0

    if args.command == "web":
        uvicorn.run(
            "victoria_voice_assistant.web.app:create_default_web_app",
            factory=True,
            host=args.host,
            port=args.port,
            log_config=None,
        )
        return 0

    if args.command == "launchd" and args.launchd_command == "install":
        destination = install_launch_agent(args.output, load=args.load)
        print(f"LaunchAgent plist written: {destination}")
        return 0

    if args.command == "launchd" and args.launchd_command == "install-web":
        destination = install_web_launch_agent(args.output, load=args.load)
        print(f"Web LaunchAgent plist written: {destination}")
        return 0

    logging.getLogger(__name__).error("Unsupported command", extra={"event": "invalid_command"})
    return 2
