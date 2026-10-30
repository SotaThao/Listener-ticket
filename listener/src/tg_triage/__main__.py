"""Command-line entry point. Diagnostics never reflect raw exceptions."""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from .attachments import GhCliUploader
from .classifier import load_prompt
from .config import ConfigError, load_config, load_database_path
from .github_issues import GitHubIssues
from .pipeline import Pipeline
from .provider import build_provider
from .store import Store
from .usage import UsageRecorder
from .runtime import AlreadyRunning, ProcessLock, validate_data_directories
from .ticket_templates import TemplateError, load_templates


def _build_classifier(config, store):
    # Validate installed assets without treating the default prompt as a custom override.
    load_prompt('topics.md')
    if config.thinking_enabled:
        load_prompt('thinking-topics.md')
    if config.autonomous_enabled:
        load_prompt('autonomous.md')
    return build_provider(config, usage=UsageRecorder(store, config))


def main(argv=None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="tg-triage")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "login", "export-csv", "check-config", "trace"):
        command = commands.add_parser(name)
        command.add_argument("--config", default="/etc/tg-triage/listener.toml")
        if name == "export-csv":
            command.add_argument("--out", required=True)
            command.add_argument("--db")
        if name == "trace":
            command.add_argument("--repo")
            command.add_argument("--issue", type=int)
            command.add_argument("--chat-id", type=int)
            command.add_argument("--message-id", type=int)
            command.add_argument("--limit", type=int, default=100)
            command.add_argument("--include-text", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "trace":
            from .trace import trace, validate_repo
            repo = validate_repo(args.repo or os.environ.get("GH_REPO", ""))
            store = Store(load_database_path(args.config), read_only=True)
            try:
                print(json.dumps(trace(store, repo=repo, issue=args.issue, chat_id=args.chat_id,
                                       message_id=args.message_id, limit=args.limit,
                                       include_text=args.include_text), ensure_ascii=False, indent=2))
            finally:
                store.close()
            return 0
        if args.command == "export-csv":
            db_path = args.db or load_database_path(args.config)
            store = Store(db_path, read_only=True)
            try:
                store.export_csv(args.out)
            finally:
                store.close()
            return 0
        config = load_config(args.config)
        templates = load_templates() if args.command in ('run', 'check-config') else None
        if args.command == "check-config":
            result = config.redacted()
            result['templates'] = {'fingerprint': templates.fingerprint}
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "login":
            validate_data_directories(config)
            from .telegram_io import login
            with ProcessLock(Path(config.db_path).parent):
                asyncio.run(login(config))
        else:
            logging.getLogger(__name__).info('templates fingerprint=%s', templates.fingerprint)
            validate_data_directories(config)
            # Lock before opening the store: a second process must not recover
            # another process's in-flight records.
            with ProcessLock(Path(config.db_path).parent):
                store = Store(config.db_path)
                try:
                    if config.topic_memory_enabled or config.autonomous_enabled:
                        store.enable_topic_registry()
                    from .telegram_io import run
                    # Fail at startup, not per window, when the topics prompt is not installed.
                    classifier = _build_classifier(config, store)
                    github = (GitHubIssues(config.gh_repo, config.gh_token)
                              if config.mode == "live" else None)
                    # Issues use REST; image uploads use the gh CLI (Assistant definition 12.8).
                    uploader = (GhCliUploader(config.gh_repo, config.gh_token)
                                if config.mode == "live" else None)
                    asyncio.run(run(config, Pipeline(config, store, classifier, github,
                                                     uploader=uploader)))
                finally:
                    store.close()
        return 0
    except KeyboardInterrupt:
        return 130
    except ConfigError as error:
        print(f"ConfigError: {error}", file=sys.stderr)
        return 2
    except TemplateError as error:
        print(f"TemplateError: {error}", file=sys.stderr)
        return 2
    except AlreadyRunning:
        print("AlreadyRunning: Telegram triage service is already running", file=sys.stderr)
        return 3
    except Exception as error:
        logging.getLogger(__name__).error("Command failed exception=%s", type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
