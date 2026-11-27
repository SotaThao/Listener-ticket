"""Command-line entry point. Diagnostics never reflect raw exceptions."""

import argparse
import asyncio
import getpass
import http.client
import json
import logging
import os
import subprocess
import sys
import time
import warnings
from collections import ChainMap
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import assistant_pack, secrets_store
from .attachments import GhCliUploader
from .classifier import load_prompt
from .config import (ConfigError, load_admin_config, load_config, load_database_path,
                     load_secrets_setup, valid_gh_repo)
from .decision_rules import RulesError, RulesSnapshot
from .rules_pending_store import RulesHalt, check_before_recovery
from .github_issues import GitHubIssues
from .pipeline import Pipeline
from .provider import build_provider
from .store import Store
from .usage import UsageRecorder
from .runtime import AlreadyRunning, ProcessLock, validate_data_directories
from .ticket_templates import TemplateError, load_templates


_DEFAULT_CONFIG = "/etc/tg-triage/listener.toml"
_HEALTH_WAIT = 30
_UNIT_TRANSITION_TIMEOUT = 180  # tg-triage.service allows 150s for a graceful stop.
_SECRET_IO_ERROR = "secrets: local file operation failed"


def _load_decision_rules():
    source = Path(__file__).resolve().parents[2]
    installed = Path(sys.prefix) / 'share' / 'tg-triage'
    rules_dir = source / 'rules' if (source / 'rules').is_dir() else installed / 'rules'
    templates_dir = source / 'templates' / 'github-ticket'
    if not templates_dir.is_dir():
        templates_dir = installed / 'templates' / 'github-ticket'
    return RulesSnapshot(rules_dir, templates_dir, extensions=True)


def _load_assistant_pack(*, pack_mode='full'):
    source = Path(__file__).resolve().parents[2]
    installed = Path(sys.prefix) / 'share' / 'tg-triage'
    directory = source / 'assistant' if (source / 'assistant').is_dir() else installed / 'assistant'
    rules_dir = source / 'rules' if (source / 'rules').is_dir() else installed / 'rules'
    return assistant_pack.load(directory, rules_dir, pack_mode=pack_mode)


def _build_classifier(config, store):
    # Validate installed assets without treating the default prompt as a custom override.
    load_prompt('topics.md')
    if config.thinking_enabled:
        load_prompt('thinking-topics.md')
    if config.autonomous_enabled:
        load_prompt('autonomous.md')
    return build_provider(config, usage=UsageRecorder(store, config))


def _runtime_assets(config):
    templates = load_templates()
    decision_rules = _load_decision_rules() if config.decision_rules_enabled else None
    pack = _load_assistant_pack(pack_mode=config.assistant.pack_mode) if decision_rules is not None else None
    return templates, decision_rules, pack


def _checked_config(path, *, environ=None, warn_legacy=None):
    config = load_config(path, environ=environ, include_admin=True, warn_legacy=warn_legacy)
    return config, *_runtime_assets(config)


def _secret_name(value):
    if value not in secrets_store.CATALOG:
        raise argparse.ArgumentTypeError("unknown secret name; see secrets status")
    return value


def _add_secrets_commands(commands):
    group = commands.add_parser("secrets", help="check or rotate Listener credentials")
    group.add_argument("--config", default=_DEFAULT_CONFIG)
    group.add_argument("--env-file", type=Path, default=secrets_store.DEFAULT_ENV_FILE,
                       help="systemd EnvironmentFile (default: %(default)s)")
    actions = group.add_subparsers(dest="secret_action", required=True)
    for name in ("status", "set", "rollback", "migrate", "verify"):
        action = actions.add_parser(name)
        action.add_argument("--config", default=argparse.SUPPRESS)
        action.add_argument("--env-file", type=Path, default=argparse.SUPPRESS,
                            help="systemd EnvironmentFile")
        if name in ("set", "rollback", "verify"):
            action.add_argument("name", type=_secret_name, metavar="NAME")
        if name in ("set", "rollback"):
            action.add_argument("--restart", action="store_true")
        if name == "status":
            action.add_argument("--json", action="store_true")
        if name == "set":
            action.add_argument("--stdin", action="store_true")
            action.add_argument("--expires", metavar="YYYY-MM-DD")
            action.add_argument("--verify", action="store_true")
        if name == "migrate":
            action.add_argument("--apply", action="store_true")


def _input_secret(name, *, stdin):
    try:
        if stdin:
            value = sys.stdin.readline().removesuffix("\n").removesuffix("\r")
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass(f"Nhập {name}: ")
                if value != getpass.getpass(f"Nhập lại {name}: "):
                    raise secrets_store.SecretError(name, "bad_format")
        return value
    except (OSError, EOFError, getpass.GetPassWarning):
        raise ConfigError(f"{name}: bad_format (use a private terminal or --stdin)") from None


def _health_command(argv):
    transitioning = argv[:2] in (["systemctl", "stop"], ["systemctl", "start"])
    return subprocess.run(argv, check=True, capture_output=True, encoding="utf-8",
                          errors="replace", timeout=_UNIT_TRANSITION_TIMEOUT if transitioning else 60).stdout


def _unit_status(unit, run):
    output = run(["systemctl", "show", unit, "--property=MainPID",
                  "--property=NRestarts", "--property=ActiveState"])
    props = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    return props["ActiveState"], int(props["MainPID"]), int(props["NRestarts"])


def _restart_secret_unit(unit, *, run=None, wait=None, now=None):
    run = _health_command if run is None else run
    wait = time.sleep if wait is None else wait
    now = (lambda: datetime.now(timezone.utc)) if now is None else now
    try:
        _, previous_pid, _ = _unit_status(unit, run)
        run(["systemctl", "stop", unit])
        since = now().astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f UTC")
        run(["systemctl", "start", unit])
        _, started_pid, started_restarts = _unit_status(unit, run)
        wait(_HEALTH_WAIT)
        state, pid, restarts = _unit_status(unit, run)
        if (state != "active" or pid <= 0 or pid != started_pid or pid == previous_pid
                or not 0 <= restarts <= started_restarts):
            return None
        journal = run(["journalctl", "-u", unit, "--since", since, f"_PID={pid}",
                       "--no-pager", "--output=cat"])
        if not any(marker in journal for marker in (" ERROR ", "Traceback", "RulesHalt")):
            return pid
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        pass
    return None


def _github_verify(name, value, config):
    """Read only. OAuth scopes can attest classic token grants, not fine-grained grants."""
    repos = ((config.gh_repo,) if name == "gh-token"
             else config.assistant.github_repos or (config.gh_repo,))
    if not all(valid_gh_repo(repo) for repo in repos):
        raise ConfigError("GH_REPO: expected owner/repo")
    headers = {"Authorization": f"Bearer {value}", "User-Agent": "tg-triage",
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    def get(path, *, metadata=False):
        with closing(http.client.HTTPSConnection("api.github.com", timeout=10)) as connection:
            connection.request("GET", path, headers=headers)
            with connection.getresponse() as response:
                if response.status != 200:
                    raise ValueError()
                scopes = set(response.getheader("X-OAuth-Scopes", "").replace(",", " ").split())
                if not metadata:
                    return scopes
                raw = response.read(256 * 1024 + 1)
                if len(raw) > 256 * 1024:
                    raise ValueError()
                return json.loads(raw)

    scopes = get("/user")
    for repo in repos:
        get(f"/repos/{repo}/issues?per_page=1")
    if name == "gh-token":
        metadata = get(f"/repos/{repos[0]}", metadata=True)
        permissions = metadata.get("permissions", {})
        is_open = metadata.get("has_issues") is True and not (metadata.get("archived") or metadata.get("disabled"))
        has_scope = "repo" in scopes or ("public_repo" in scopes and metadata.get("private") is False)
        if not (is_open and has_scope and permissions.get("push") is True):
            raise ConfigError("gh-token: authenticated and issues readable; Issues write unconfirmed")
        return "đọc được issue, scope và quyền repo phù hợp để ghi"
    return "đọc được issue"


def _verify_value(name, value, config):
    """An explicit operator probe. No callers from status, startup or Admin."""
    try:
        if name in secrets_store.GITHUB_TOKENS:
            return _github_verify(name, value, config)
        probe_message = [{"role": "user", "content": "OK?"}]
        if name == "model-key.json" and config.model.provider != "ollama":
            from agentfleet_common.ai_transport import request_json

            model = config.model
            body = {"model": model.name, "messages": probe_message, "stream": False}
            headers = {"Content-Type": "application/json"}
            if model.provider == "anthropic":
                path = "/v1/messages"
                headers.update({"x-api-key": value, "anthropic-version": "2023-06-01"})
                body["max_tokens"] = 8
            else:
                path = "/chat/completions"
                headers["Authorization"] = f"Bearer {value}"
                body["max_completion_tokens" if model.provider == "openai" else "max_tokens"] = 16
            request_json(model.base_url.rstrip("/") + path, body, headers, 10, model.provider)
            return "model phản hồi"
        if name == "router-key":
            endpoint = urlsplit(config.assistant.router_base_url)
            if endpoint.scheme == "https":
                connect, default_port = http.client.HTTPSConnection, 443
            elif endpoint.scheme == "http":
                connect, default_port = http.client.HTTPConnection, 80
            else:
                raise ValueError()
            port = endpoint.port if endpoint.port is not None else default_port
            path = endpoint.path.rstrip("/") + "/chat/completions"
            body = json.dumps({"model": config.assistant.models[0], "messages": probe_message,
                               "stream": False, "max_tokens": 8}).encode("utf-8")
            with closing(connect(endpoint.hostname, port, timeout=10)) as connection:
                connection.request("POST", path, body, {
                    "Authorization": f"Bearer {value}", "Content-Type": "application/json",
                })
                with connection.getresponse() as response:
                    if response.status != 200:
                        raise ValueError()
            return "router phản hồi"
    except ConfigError:
        raise
    except (OSError, ValueError, http.client.HTTPException, AttributeError, TypeError):
        raise ConfigError(f"{name}: verify_failed") from None
    return "chỉ kiểm định dạng"


def _restore_rotation(name, store, snapshot, record):
    try:
        store.restore(name, snapshot)
    except BaseException as error:
        kind = type(error).__name__
        try:
            record("rollback_failed", error_type=kind)
        except (Exception, KeyboardInterrupt) as audit_error:
            print(f"{name}: không ghi được audit rollback_failed ({type(audit_error).__name__})",
                  file=sys.stderr)
        print(f"{name}: bản trước chưa được khôi phục hoàn chỉnh ({kind}), cần xử lý thủ công",
              file=sys.stderr)
        return False
    return True


def _restart_rotation(config_path, environ, name, store, snapshot, record):
    try:
        _checked_config(config_path, environ=environ)
    except BaseException as error:
        kind = type(error).__name__
        if not _restore_rotation(name, store, snapshot, record):
            print(f"{name}: kiểm cấu hình thất bại ({kind}), không restart", file=sys.stderr)
            return 2
        print(f"{name}: kiểm cấu hình thất bại ({kind}), đã khôi phục, không restart", file=sys.stderr)
        record("validation_failed_rolled_back", error_type=kind)
        return 130 if isinstance(error, KeyboardInterrupt) else 2
    unit = "tg-triage-admin" if name == "admin-password" else "tg-triage"
    interrupted = False
    try:
        pid = _restart_secret_unit(unit)
    except KeyboardInterrupt:
        pid, interrupted = None, True
    if pid is not None:
        try:
            record("ok")
        except OSError:
            if not _restore_rotation(name, store, snapshot, record):
                return 2
            if _restart_secret_unit(unit) is None:
                raise ConfigError(f"{name}: audit failed; restored key but unit unhealthy") from None
            raise ConfigError(f"{name}: audit failed; rolled back") from None
        print(f"{name}: OK ({unit}, PID {pid})")
        return 0
    if not _restore_rotation(name, store, snapshot, record):
        return 2
    error_type = None
    try:
        rollback_pid = _restart_secret_unit(unit)
    except KeyboardInterrupt as error:
        interrupted = True
        error_type = type(error).__name__
        result = "unhealthy_rollback_interrupted"
        outcome = (f"đã khôi phục key trước, nhưng restart {unit} bị gián đoạn. "
                   f"Kiểm tra systemctl status {unit} và chạy systemctl start {unit} thủ công nếu unit đang dừng.")
    else:
        if rollback_pid is None:
            result = "unhealthy_rollback_restart_failed"
            outcome = f"đã khôi phục key, {unit} chưa qua health check"
        else:
            result = "unhealthy_rolled_back"
            outcome = f"health check lỗi, đã rollback về key trước và restart {unit}"
    print(f"{name}: {outcome}", file=sys.stderr)
    try:
        record(result, error_type=error_type)
    except OSError:
        print(f"ConfigError: {_SECRET_IO_ERROR}", file=sys.stderr)
    return 130 if interrupted else 2


def _secrets_command(args):
    action = args.secret_action
    writing = action in ("set", "rollback") or (action == "migrate" and args.apply)
    if writing and secrets_store.PLATFORM.euid() != 0:
        raise ConfigError("secrets: run write commands with sudo")
    try:
        environ = secrets_store.service_environment(args.env_file, os.environ)
        config, data = load_secrets_setup(args.config, environ=environ)
        store = secrets_store.SecretsStore(config.secrets)
        scope = secrets_store.model_scope(config.model)
        actor = secrets_store.sudo_actor(os.environ)
        if action == "status":
            store.check_status_directory()
            rows = store.status(config.required_secrets, scope)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                print("Key | Mô tả | Bắt buộc | Trạng thái | Ngày thay | Người thay | Hết hạn")
                for item in rows:
                    print(" | ".join((item["name"], item["description"], "Có" if item["required"] else "Không",
                                      item["status"], item["updated_at"] or "—", item["updated_by"] or "—",
                                      item["expires_at"] or "—")))
            return 2 if any(row["required"] and row["state"] in (
                "missing", "bad_mode", "bad_owner", "bad_format") for row in rows) else 0
        if action == "migrate":
            items, cleanup, skipped = secrets_store.migration_plan(
                data, args.env_file, store, scope, required=config.required_secrets)
            print("Kế hoạch migrate (tên, độ dài)")
            for name, kind in skipped:
                print(f"WARNING: {name}: {kind} (bỏ qua nguồn cũ không bắt buộc)")
            for item in items:
                print(f"  {item.name} {item.value.length}")
            print("Dòng env hoặc file cũ Owner có thể dọn sau cut-over")
            for entry in cleanup:
                print(f"  {entry}")
            if args.apply and items:
                with store.writer() as audit:
                    secrets_store.apply_migration(store, items, audit, actor=actor, scope=scope)
            return 0
        name = args.name
        if action == "verify":
            print(f"{name}: {_verify_value(name, store.read(name, scope), config)}")
            return 0
        if action == "set":
            try:
                expiry = secrets_store.parse_expiry(args.expires).isoformat() if args.expires is not None else None
            except ValueError:
                raise ConfigError(f"{name}: bad_format (expected --expires YYYY-MM-DD)") from None
            value = _input_secret(name, stdin=args.stdin)
            candidate = secrets_store.format_value(name, value, scope)
            if args.verify:
                print(f"{name}: {_verify_value(name, value, config)}")
        with store.writer() as audit:
            if action == "set":
                store.check_distinct_github(name, value)
                snapshot = store.set(name, candidate, actor=actor, expires_at=expiry)
                length = candidate.length
            else:
                snapshot, length = store.rollback(name, actor=actor)

            def record(result, *, error_type=None):
                store.audit(audit, action=action, name=name, actor=actor, source="cli",
                            length=length, restart_result=result, error_type=error_type)

            if args.restart:
                return _restart_rotation(args.config, environ, name, store, snapshot, record)
            try:
                record("not_requested")
            except OSError:
                if not _restore_rotation(name, store, snapshot, record):
                    return 2
                raise
        print(f"{name}: OK")
        return 0
    except secrets_store.SecretError as error:
        if action == "status" and error.kind == "unreadable":
            label = secrets_store.STATUS_LABELS["unreadable"]
            print(json.dumps({"state": "unreadable", "status": label}, ensure_ascii=False)
                  if args.json else label)
            return 2
        raise ConfigError(str(error)) from None
    except OSError:
        raise ConfigError(_SECRET_IO_ERROR) from None


def main(argv=None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="tg-triage")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "login", "export-csv", "usage-daily", "check-config", "trace", "legacy-preflight", "legacy-transition", "legacy-release", "admin"):
        command = commands.add_parser(name)
        command.add_argument("--config", default=_DEFAULT_CONFIG)
        if name == "check-config":
            command.add_argument("--env-file", type=Path,
                                 help="read GH_REPO and TG_TRIAGE_MODE here if unset")
        if name == "export-csv":
            command.add_argument("--out", required=True)
            command.add_argument("--db")
        if name == 'usage-daily':
            command.add_argument('--db')
            command.add_argument('--days', type=int, default=30)
            command.add_argument('--tz', default='+07:00')
        if name == "legacy-preflight":
            command.add_argument("--db", required=True)
            command.add_argument("--attachment-evidence", action="store_true")
        if name in ("legacy-transition", "legacy-release"):
            command.add_argument("--db", required=True)
            command.add_argument("--manifest", required=True)
        if name == "trace":
            command.add_argument("--repo")
            command.add_argument("--issue", type=int)
            command.add_argument("--chat-id", type=int)
            command.add_argument("--message-id", type=int)
            command.add_argument("--limit", type=int, default=100)
            command.add_argument("--include-text", action="store_true")
    passwd = commands.add_parser("admin-passwd")
    passwd.add_argument("--out", required=True)
    _add_secrets_commands(commands)
    args, extra = parser.parse_known_args(argv)
    if extra:
        parser.error("unexpected secrets arguments" if args.command == "secrets"
                     else "unrecognized arguments: " + " ".join(extra))
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "secrets":
            return _secrets_command(args)
        if args.command == "admin":
            from .admin import serve
            serve(load_admin_config(args.config), load_database_path(args.config))
            return 0
        if args.command == "admin-passwd":
            from .admin import write_password_hash
            write_password_hash(Path(args.out))
            return 0
        if args.command in ('legacy-transition','legacy-release'):
            from .legacy_reconciliation import transition_pristine, release_pristine
            try:
                manifest=json.loads(Path(args.manifest).read_text(encoding='utf-8'))
            except (OSError,ValueError,UnicodeError):
                raise RulesHalt('cannot read offline transition attestation') from None
            action=transition_pristine if args.command=='legacy-transition' else release_pristine
            print(json.dumps(action(args.db,manifest),ensure_ascii=False,indent=2))
            return 0
        if args.command == 'legacy-preflight':
            from .legacy_preflight import inspect
            report=inspect(args.db,include_attachment_evidence=args.attachment_evidence)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
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
        if args.command == 'usage-daily':
            from .classification_log import daily_summary
            db_path = args.db or load_database_path(args.config)
            store = Store(db_path, read_only=True)
            try:
                try:
                    report = daily_summary(store.connection, args.days, args.tz)
                except ValueError as error:
                    raise ConfigError(str(error)) from None
                print(json.dumps(report, ensure_ascii=False, indent=2))
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
        if args.command == "check-config":
            def warn_legacy(name):
                command = "secrets set model-key.json" if name in secrets_store.MODEL_ENV_OVERRIDES else "secrets migrate"
                print(f"WARNING: {name}: legacy declaration ignored; use {command}", file=sys.stderr)

            environ = None if args.env_file is None else ChainMap(
                os.environ, secrets_store.service_environment(args.env_file, os.environ))
            config, templates, decision_rules, pack = _checked_config(
                args.config, environ=environ, warn_legacy=warn_legacy)
            result = config.redacted()
            result['assistant']['pack_fingerprint'] = pack.fingerprint if pack is not None else None
            if pack is not None and pack.core is not None:
                result['assistant']['core_pack_fingerprint'] = pack.core.fingerprint
            result['templates'] = {'fingerprint': templates.fingerprint}
            if decision_rules is not None:
                result['rules'] = {'fingerprint': decision_rules.fingerprint}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "run":
            # The shared model transport otherwise prefers a matching ambient override.
            # This is a dedicated service process; its only source is the configured file.
            for name in secrets_store.MODEL_ENV_OVERRIDES:
                if name in os.environ:
                    del os.environ[name]
        config = load_config(args.config)
        if args.command == "login":
            validate_data_directories(config)
            from .telegram_io import login
            with ProcessLock(Path(config.db_path).parent):
                asyncio.run(login(config))
        else:
            templates, decision_rules, pack = _runtime_assets(config)
            logging.getLogger(__name__).info('templates fingerprint=%s', templates.fingerprint)
            if decision_rules is not None:
                logging.getLogger(__name__).info('rules fingerprint=%s', decision_rules.fingerprint)
                logging.getLogger(__name__).info('assistant pack fingerprint=%s', pack.fingerprint)
                if pack.core is not None:
                    logging.getLogger(__name__).info('assistant core pack fingerprint=%s', pack.core.fingerprint)
            validate_data_directories(config)
            # Lock before opening the store: a second process must not recover
            # another process's in-flight records.
            with ProcessLock(Path(config.db_path).parent):
                if decision_rules is not None:
                    check_before_recovery(config.db_path)
                store = Store(config.db_path)
                store.assistant_pack_fingerprint = pack.fingerprint if pack is not None else None
                pipeline = None
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
                    pipeline_options = {'rules_snapshot': decision_rules, 'assistant_pack': pack} if decision_rules is not None else {}
                    pipeline = Pipeline(config, store, classifier, github, uploader=uploader, **pipeline_options)
                    asyncio.run(run(config, pipeline))
                finally:
                    if pipeline is not None and pipeline.assistant_runtime is not None:
                        pipeline.assistant_runtime.close()
                    try:
                        from .classification_log import flush
                        flush(store)
                    except Exception as error:
                        logging.getLogger(__name__).warning('Classification shutdown flush failed exception=%s',
                                                            type(error).__name__)
                    store.close()
        return 0
    except KeyboardInterrupt:
        return 130
    except (ConfigError, secrets_store.SecretError) as error:
        print(f"ConfigError: {error}", file=sys.stderr)
        return 2
    except TemplateError as error:
        print(f"TemplateError: {error}", file=sys.stderr)
        return 2
    except (RulesError, assistant_pack.AssistantPackError) as error:
        print(f"RulesError: {error}", file=sys.stderr)
        return 2
    except RulesHalt as error:
        print(f"RulesHalt: {error}", file=sys.stderr)
        return 2
    except AlreadyRunning:
        print("AlreadyRunning: Telegram triage service is already running", file=sys.stderr)
        return 3
    except Exception as error:
        logging.getLogger(__name__).error("Command failed exception=%s", type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
