from __future__ import annotations

import argparse
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
from victoria_voice_assistant.launchd import install_launch_agent, install_web_launch_agent
from victoria_voice_assistant.logging_config import configure_logging


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
        return 0

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
