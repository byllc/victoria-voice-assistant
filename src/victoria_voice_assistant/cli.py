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
from victoria_voice_assistant.secret_store import get_secret, set_secret
from victoria_voice_assistant.notifications.service import NotificationService
from victoria_voice_assistant.tools.registry import Services, ToolRegistry, build_registry
from victoria_voice_assistant.tools.testing import format_report, format_status, run_tool_tests
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService
from victoria_voice_assistant.web.tls import TlsError, describe, find_tls_files, log_cert, validate
from victoria_voice_assistant.launchd import (
    install_decider_launch_agent,
    install_launch_agent,
    install_searxng_launch_agent,
    install_web_launch_agent,
)
from victoria_voice_assistant.logging_config import configure_logging


def _registry(config: AppConfig) -> ToolRegistry:
    from victoria_voice_assistant.search.service import PageReader, SearchService

    services = Services(
        timers=TimerService(config.tools.timers),
        weather=WeatherService(config.tools.weather),
        search=SearchService(config.tools.search),
        reader=PageReader(config.tools.search.read if config.tools.search else None),
        notifications=NotificationService(config.tools.notifications),
    )
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

    voice = commands.add_parser("voice", help="voice pipeline tools")
    voice_subcommands = voice.add_subparsers(dest="voice_command", required=True)
    bench = voice_subcommands.add_parser("bench", help="speak 20 commands over WebRTC and report latency by stage")
    bench.add_argument("--url", default="http://127.0.0.1:8765", help="assistant service base URL")
    bench.add_argument("--sessions", type=int, default=1, help="simultaneous voice sessions")
    bench.add_argument("--log", type=Path, help="the service's log file, to join per-stage timings")

    serve = commands.add_parser("serve", help="run assistant health service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=int(os.environ.get("ASSISTANT_PORT", "8765")))

    web = commands.add_parser("web", help="run the household web chat (login + static client)")
    web.add_argument(
        "--host",
        default=os.environ.get("ASSISTANT_WEB_HOST"),
        help="bind address (default: 0.0.0.0 with a certificate, else 127.0.0.1)",
    )
    web.add_argument("--config-dir", help="configuration directory (default: config/)")

    tls = commands.add_parser("tls", help="HTTPS certificate for the web interface")
    tls_subcommands = tls.add_subparsers(dest="tls_command", required=True)
    tls_status = tls_subcommands.add_parser("status", help="check the installed certificate and key")
    tls_status.add_argument("--config-dir", help="configuration directory (default: config/)")
    web.add_argument("--port", type=int, default=int(os.environ.get("ASSISTANT_WEB_PORT", "8766")))

    push = commands.add_parser("push", help="Web Push to installed web clients")
    push_subcommands = push.add_subparsers(dest="push_command", required=True)
    push_subcommands.add_parser("keys", help="create the VAPID key in the Keychain (never printed)")
    push_status = push_subcommands.add_parser("status", help="whether push is set up, and the subscribed browsers")
    push_status.add_argument("--config-dir", help="configuration directory (default: config/)")

    secrets_parser = commands.add_parser("secrets", help="secrets: the Keychain and config/.env (docs/secrets.md)")
    secrets_parser.add_argument("--config-dir", help="configuration directory (default: config/)")
    secrets_subcommands = secrets_parser.add_subparsers(dest="secrets_command", required=True)
    secrets_subcommands.add_parser("list", help="which secrets are set, and where (never their values)")
    secret_set = secrets_subcommands.add_parser("set", help="store a secret in the Keychain and config/.env; prompts without echoing")
    secret_set.add_argument("name")
    secret_delete = secrets_subcommands.add_parser("delete", help="remove a secret from both")
    secret_delete.add_argument("name")
    secrets_subcommands.add_parser("sync", help="rewrite config/.env from the Keychain")
    secret_get = secrets_subcommands.add_parser("get", help="print a secret for a script (refuses to print to a terminal)")
    secret_get.add_argument("name")

    launchd = commands.add_parser("launchd", help="manage the macOS LaunchAgent")
    launchd_subcommands = launchd.add_subparsers(dest="launchd_command", required=True)
    install = launchd_subcommands.add_parser("install", help="write assistant LaunchAgent plist")
    install.add_argument("--output", type=Path)
    install.add_argument("--load", action="store_true", help="bootstrap LaunchAgent now")
    install_web = launchd_subcommands.add_parser("install-web", help="write web LaunchAgent plist")
    install_web.add_argument("--output", type=Path)
    install_web.add_argument("--load", action="store_true", help="bootstrap LaunchAgent now")
    install_searxng = launchd_subcommands.add_parser("install-searxng", help="write SearXNG LaunchAgent plist")
    install_searxng.add_argument("--output", type=Path)
    install_searxng.add_argument("--load", action="store_true", help="bootstrap LaunchAgent now")

    install_decider = launchd_subcommands.add_parser("install-decider", help="write decider (mlx-lm) LaunchAgent plist")
    install_decider.add_argument("--output", type=Path)
    install_decider.add_argument("--load", action="store_true", help="bootstrap LaunchAgent now")

    decider = commands.add_parser("decider", help="the local decision model (mlx-lm)")
    decider_subcommands = decider.add_subparsers(dest="decider_command", required=True)
    decider_subcommands.add_parser("install", help="build the pinned mlx-lm environment")
    decider_run = decider_subcommands.add_parser("run", help="serve the decision model from models.yaml")
    decider_run.add_argument("--config-dir", help="configuration directory (default: config/)")

    searxng = commands.add_parser("searxng", help="the local SearXNG search engine")
    searxng_subcommands = searxng.add_subparsers(dest="searxng_command", required=True)
    searxng_subcommands.add_parser("install", help="check out the pinned SearXNG and build its environment")
    searxng_run = searxng_subcommands.add_parser("run", help="run SearXNG on 127.0.0.1:8888")
    searxng_run.add_argument("--config-dir", help="configuration directory (default: config/)")
    return parser


def _secrets_command(args: argparse.Namespace) -> int:
    import getpass

    from victoria_voice_assistant.secret_store import (
        KNOWN,
        SecretsError,
        delete_secret,
        keychain_available,
        keychain_get,
        read_env_file,
        sync_env_file,
    )

    env_file = _config_dir(getattr(args, "config_dir", None)) / ".env"
    try:
        if args.secrets_command == "list":
            in_file = read_env_file(env_file)
            for name, purpose in KNOWN.items():
                places = [p for p, present in (("Keychain", keychain_get(name)), ("config/.env", name in in_file)) if present]
                print(f"  {', '.join(places) or 'not set':22}  {name}: {purpose}")
            if keychain_available():
                print("Services read config/.env; `assistant secrets sync` rewrites it from the Keychain.")
            return 0
        if args.secrets_command == "set":
            # Typed at a hidden prompt, or piped in; never taken as an argument (it would show in the process list).
            value = getpass.getpass(f"{args.name}: ") if sys.stdin.isatty() else sys.stdin.readline()
            value = value.strip()
            if not value:
                print("Nothing entered; unchanged.", file=sys.stderr)
                return 1
            where = set_secret(args.name, value, env_file)
            print(f"Stored {args.name} in {where}. Restart the assistant to use it.")
            return 0
        if args.secrets_command == "delete":
            delete_secret(args.name, env_file)
            print(f"Removed {args.name}.")
            return 0
        if args.secrets_command == "sync":
            names = sync_env_file(env_file)
            if not keychain_available():
                print("No Keychain here; config/.env is the only store.")
            else:
                print(f"Wrote {len(names)} secret(s) from the Keychain to {env_file}: {', '.join(names) or 'none'}")
            return 0
        # get: for scripts (`$(assistant secrets get NAME)`), never a terminal.
        if sys.stdout.isatty():
            print("Refusing to print a secret to the terminal; this is for scripts (`$(assistant secrets get NAME)`).", file=sys.stderr)
            return 1
        value = get_secret(args.name, env_file)
        if not value:
            print(f"{args.name} is not set.", file=sys.stderr)
            return 1
        sys.stdout.write(value)
        return 0
    except SecretsError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _sync_secrets_for_service() -> bool:
    """Before installing a service: write config/.env from the Keychain, so it starts after a reboot (docs/secrets.md)."""
    from victoria_voice_assistant.secret_store import SecretsError, keychain_available, sync_env_file

    env_file = _config_dir(None) / ".env"
    try:
        names = sync_env_file(env_file)
    except SecretsError as exc:
        print(str(exc), file=sys.stderr)
        return False
    if keychain_available():
        print(f"Secrets from the Keychain written to {env_file}: {', '.join(names) or 'none found'}")
    return True


def _push_command(args: argparse.Namespace) -> int:
    from victoria_voice_assistant.notifications.push import PushService, generate_private_key, public_key
    from victoria_voice_assistant.storage.db import default_database_path, migrate

    if args.push_command == "keys":
        if get_secret("VAPID_PRIVATE_KEY"):
            print("VAPID_PRIVATE_KEY is already set; left unchanged (a new key would cut off every subscribed browser).")
            return 0
        key = generate_private_key()
        where = set_secret("VAPID_PRIVATE_KEY", key)
        print(f"Stored VAPID_PRIVATE_KEY in {where}. Public key (safe to share): {public_key(key)}")
        print("Then set notifications.web_push: true and vapid_subject in tools.yaml, and restart the assistant.")
        return 0
    try:
        config = load_config(_config_dir(args.config_dir))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    service = PushService(config.tools.notifications, migrate(default_database_path()))
    print("Web Push: on" if service.enabled else f"Web Push: off ({service.problem})")
    for subscription in service.subscriptions():
        host = subscription.endpoint.split("/")[2]
        print(f"  {subscription.device}: browser {subscription.client_id[:8]} via {host}")
    return 0


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
        filenames = ("models.yaml", "tools.yaml", "agents.yaml", "searxng/settings.yml")
        existing = [name for name in filenames if (destination / name).exists()]
        if existing:
            print(
                f"Refusing to overwrite existing config files: {', '.join(existing)}",
                file=sys.stderr,
            )
            return 1
        (destination / "searxng").mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            shutil.copyfile(examples / filename, destination / filename)
        print(f"Example configuration copied to {destination}")
        print("Store secrets with `uv run assistant secrets set LMS_API_KEY` (Keychain and config/.env; docs/secrets.md).")
        return 0

    if args.command == "serve":
        uvicorn.run(
            "victoria_voice_assistant.app:app",
            host=args.host,
            port=args.port,
            log_config=None,
        )
        return 0

    if args.command == "voice":
        from victoria_voice_assistant.voice.bench import bench as run_bench

        logging.getLogger().setLevel(logging.ERROR)
        print(asyncio.run(run_bench(args.url, sessions=args.sessions, log_path=args.log)))
        return 0

    if args.command == "tls":
        try:
            files = find_tls_files(_config_dir(args.config_dir))
            info = validate(files) if files else None
        except TlsError as exc:
            print(f"TLS problem: {exc}", file=sys.stderr)
            return 1
        print(describe(files, info))
        return 0

    if args.command == "push":
        return _push_command(args)

    if args.command == "secrets":
        return _secrets_command(args)

    if args.command == "web":
        try:
            files = find_tls_files(_config_dir(args.config_dir))
            info = validate(files) if files else None
        except TlsError as exc:
            # Refuse to start rather than quietly serving plain HTTP after a bad update.
            print(f"TLS problem: {exc} (see docs/https.md)", file=sys.stderr)
            return 1
        tls_options = {}
        if files is not None and info is not None:
            log_cert(files, info)
            os.environ["ASSISTANT_WEB_TLS"] = "1"  # read by the app factory (Secure cookies)
            tls_options = {"ssl_certfile": str(files.cert), "ssl_keyfile": str(files.key)}
        uvicorn.run(
            "victoria_voice_assistant.web.app:create_default_web_app",
            factory=True,
            host=args.host or ("0.0.0.0" if files else "127.0.0.1"),
            port=args.port,
            log_config=None,
            **tls_options,
        )
        return 0

    if args.command == "launchd" and args.launchd_command == "install":
        if not _sync_secrets_for_service():
            return 1
        destination = install_launch_agent(args.output, load=args.load)
        print(f"LaunchAgent plist written: {destination}")
        return 0

    if args.command == "searxng":
        from victoria_voice_assistant.search import searxng as searxng_runtime

        if args.searxng_command == "install":
            print(f"SearXNG {searxng_runtime.SEARXNG_COMMIT} ready in {searxng_runtime.install()}")
            return 0
        try:
            searxng_runtime.run(_config_dir(args.config_dir))
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0

    if args.command == "decider":
        from victoria_voice_assistant.decider import mlxlm

        if args.decider_command == "install":
            print(f"mlx-lm ready in {mlxlm.install()}")
            return 0
        try:
            mlxlm.run(load_config(_config_dir(args.config_dir)).models)
        except (ConfigError, FileNotFoundError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0

    if args.command == "launchd" and args.launchd_command == "install-decider":
        destination = install_decider_launch_agent(args.output, load=args.load)
        print(f"Decider LaunchAgent plist written: {destination}")
        return 0

    if args.command == "launchd" and args.launchd_command == "install-searxng":
        destination = install_searxng_launch_agent(args.output, load=args.load)
        print(f"SearXNG LaunchAgent plist written: {destination}")
        return 0

    if args.command == "launchd" and args.launchd_command == "install-web":
        if not _sync_secrets_for_service():
            return 1
        destination = install_web_launch_agent(args.output, load=args.load)
        print(f"Web LaunchAgent plist written: {destination}")
        return 0

    logging.getLogger(__name__).error("Unsupported command", extra={"event": "invalid_command"})
    return 2
