"""Layer 4 Presentation & Dispatch module: CLI argument parser and command router.

Zero external dependencies. Pure Python 3.10+ standard library.
Implements Dynamic Module Proxy Bridge for transparent test mock interception.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import Any, Callable

from agy_switcher.exceptions import (
    AccountNotFoundError,
    ActiveTurnRunningError,
    AgySwitchError,
    DaemonAlreadyRunningError,
    DaemonError,
    InvalidTokenError,
)
from agy_switcher.formatters import (
    format_accounts_listicle,
    format_accounts_table,
    format_diagnostic_report,
    format_status_report,
)
from agy_switcher.models import DiagnosticItem, DiagnosticReport, QuotaStatus
from agy_switcher.telemetry import (
    QuotaClient,
    fetch_all_accounts_quota,
    normalize_model_name,
    run_quota_pipeline,
)
from agy_switcher.vault import AgySwitchEngine
from agy_switcher.rotation import RotationEngine
from agy_switcher.watcher import DaemonManager, LogWatcher
from agy_switcher.hook import handle_pre_invocation_hook


def _resolve_symbol(symbol_name: str, fallback_factory: Callable[[], Any]) -> Any:
    """Resolves symbol from sys.modules['agy_switch'] if mocked/present; falls back to sub-package default."""
    mod = sys.modules.get("agy_switch")
    if mod and hasattr(mod, symbol_name):
        return getattr(mod, symbol_name)
    return fallback_factory()


def _get_datetime() -> Any:
    mod = sys.modules.get("agy_switch")
    if mod and hasattr(mod, "datetime"):
        return getattr(mod, "datetime")
    return datetime


def _get_quota_client_class() -> Any:
    return _resolve_symbol(
        "QuotaClient",
        lambda: QuotaClient,
    )


def _get_rotation_engine_class() -> Any:
    return _resolve_symbol(
        "RotationEngine",
        lambda: RotationEngine,
    )


def _get_fetch_all_accounts_quota() -> Any:
    return _resolve_symbol(
        "fetch_all_accounts_quota",
        lambda: fetch_all_accounts_quota,
    )


def _get_run_quota_pipeline() -> Any:
    return _resolve_symbol(
        "run_quota_pipeline",
        lambda: run_quota_pipeline,
    )


def _get_engine_class() -> Any:
    return _resolve_symbol(
        "AgySwitchEngine",
        lambda: AgySwitchEngine,
    )


def build_parser() -> argparse.ArgumentParser:
    """Builds and returns the comprehensive argparse CLI parser."""
    parser = argparse.ArgumentParser(
        prog="agy-switch",
        description="High-reliability account switcher for Antigravity 2.0 & CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    # Command: list
    list_parser = subparsers.add_parser("list", help="List registered accounts and show active account")
    list_parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    list_parser.add_argument(
        "--format",
        choices=["table", "list"],
        default="table",
        help="Output format: table (default) or list (listicle)",
    )
    list_parser.add_argument("--quota", action="store_true", help="Fetch and display quota concurrently")
    list_parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Target model family for quota sorting (e.g. gemini, claude)",
    )

    # Command: quota
    quota_parser = subparsers.add_parser(
        "quota", help="Fetch and display current quota rankings across all accounts"
    )
    quota_parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    quota_parser.add_argument(
        "--format",
        choices=["table", "list"],
        default="table",
        help="Output format: table (default) or list (listicle)",
    )
    quota_parser.add_argument(
        "--model",
        type=str,
        default="gemini",
        help="Target model family for quota sorting (e.g. gemini, claude)",
    )

    # Command: status
    subparsers.add_parser("status", help="Show system status, active account, and server health")

    # Command: doctor
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Run 5-layer self-healing diagnostics on vault, credentials, and processes",
    )
    doctor_parser.add_argument("--fix", action="store_true", help="Automatically repair fixable anomalies")
    doctor_parser.add_argument(
        "--json", action="store_true", help="Output pure JSON report for automated ingestion"
    )

    # Command: switch
    switch_parser = subparsers.add_parser(
        "switch", help="Switch active account across Antigravity 2.0 and CLI"
    )
    switch_parser.add_argument("account", nargs="?", help="Name of the account to switch to")
    switch_parser.add_argument(
        "--no-restart", action="store_true", help="Do not restart language_server process"
    )
    switch_parser.add_argument(
        "--force", action="store_true", help="Force switch even if active turns are detected"
    )
    switch_parser.add_argument("--auto", action="store_true", help="Auto-switch to optimal account")
    switch_parser.add_argument(
        "--model", type=str, default=None, help="Target model for auto-switch (e.g. gemini, claude)"
    )

    # Command: add
    add_parser = subparsers.add_parser("add", help="Register a new account from token JSON file")
    add_parser.add_argument("name", help="Account name/alias to register")
    add_parser.add_argument("--token-file", required=True, type=Path, help="Path to token JSON file")

    # Command: remove
    remove_parser = subparsers.add_parser("remove", help="Remove an account from vault")
    remove_parser.add_argument("name", help="Account name to remove")
    remove_parser.add_argument("--force", action="store_true", help="Force removal even if active")

    # Command: rename
    rename_parser = subparsers.add_parser(
        "rename",
        help="Rename an account alias across vault, active pointer, and cache",
    )
    rename_parser.add_argument("old_alias", help="Current account alias to rename")
    rename_parser.add_argument("new_alias", help="New account alias")
    rename_parser.add_argument(
        "--force",
        action="store_true",
        help="Force rename even if language_server is busy on an active turn",
    )

    # Command: migrate
    subparsers.add_parser("migrate", help="Import legacy accounts from ~/.agy-cli-manager")

    # Command: login
    login_parser = subparsers.add_parser(
        "login", help="Interactive Google OAuth login in an isolated sandbox"
    )
    login_parser.add_argument("name", help="Account name/alias to assign in the vault")
    login_parser.add_argument("--agy-binary", help="Path to agy binary (default: auto-detected)")
    login_parser.add_argument(
        "--timeout", type=int, default=600, help="Timeout in seconds (default: 600)"
    )
    login_parser.add_argument(
        "--force", action="store_true", help="Overwrite existing account without confirmation prompt"
    )

    # Command: config
    config_parser = subparsers.add_parser("config", help="Manage vault configuration")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    auto_switch_parser = config_subparsers.add_parser(
        "auto-switch", help="Configure auto-switch toggle (on/off)"
    )
    auto_switch_parser.add_argument("state", nargs="?", choices=["on", "off"], help="State (on/off)")
    default_model_parser = config_subparsers.add_parser(
        "default-model", help="Configure persistent default model (gemini or claude)"
    )
    default_model_parser.add_argument(
        "model", nargs="?", help="Default model family (gemini or claude)"
    )

    # Command: hook
    hook_parser = subparsers.add_parser("hook", help="Manage Antigravity lifecycle hooks")
    hook_parser.set_defaults(_subparser=hook_parser)
    hook_subparsers = hook_parser.add_subparsers(dest="hook_command")
    hook_subparsers.add_parser("install", help="Install PreInvocation lifecycle hook into hooks.json")

    # Command: hook-pre-invocation
    subparsers.add_parser("hook-pre-invocation", help="Antigravity PreInvocation lifecycle hook handler")

    # Command: watch
    watch_parser = subparsers.add_parser(
        "watch",
        help="Manage background daemon to tail logs and auto-switch on quota exhaustion",
    )
    watch_parser.add_argument(
        "watch_action",
        nargs="?",
        choices=["run", "start", "stop", "status"],
        default="run",
        help="Action to perform: run (default foreground), start (background daemon), stop, status",
    )
    watch_parser.add_argument(
        "--generate-systemd",
        action="store_true",
        help="Output systemd user service unit definition",
    )
    watch_parser.add_argument(
        "--install-systemd",
        action="store_true",
        help="Install systemd user service unit to ~/.config/systemd/user/agy-switch-watcher.service",
    )
    watch_parser.add_argument(
        "--cooldown",
        type=int,
        default=60,
        help="Default cooldown minutes for exhausted accounts (default: 60)",
    )

    # Command: internal-probe
    subparsers.add_parser("internal-probe", help=argparse.SUPPRESS)

    return parser


def main(argv: list[str] | None = None, engine: AgySwitchEngine | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if engine is None:
        engine_cls = _get_engine_class()
        engine = engine_cls()

    # Perform auto-migration if needed on startup
    migrated = engine.auto_migrate_if_needed()
    if migrated and len(migrated) > 0 and args.command != "migrate":
        if (
            args.command == "hook-pre-invocation"
            or getattr(args, "json", False)
            or getattr(args, "generate_systemd", False)
            or getattr(args, "install_systemd", False)
        ):
            print(
                f"[agy-switch] Auto-migrated {len(migrated)} account(s) from agy-cli-manager: {', '.join(migrated)}",
                file=sys.stderr,
            )
        else:
            print(
                f"[agy-switch] Auto-migrated {len(migrated)} account(s) from agy-cli-manager: {', '.join(migrated)}"
            )

    if args.command in ("quota", "list") or not args.command:
        data = engine.list_accounts()
        show_quota = True if args.command == "quota" else getattr(args, "quota", False)
        if show_quota:
            cli_model = getattr(args, "model", None)
            if cli_model is not None:
                try:
                    target_model = normalize_model_name(cli_model)
                except AgySwitchError:
                    print(
                        f"[agy-switch] Unsupported model '{cli_model}'. Allowed models: 'gemini', 'claude'.",
                        file=sys.stderr,
                    )
                    return 1
            else:
                target_model = "gemini"

            qc_cls = _get_quota_client_class()
            client = qc_cls(timeout=2.0, engine=engine)
            pipeline_func = _get_run_quota_pipeline()
            data = pipeline_func(engine, data, client=client, target_model=target_model)

        if getattr(args, "json", False):
            print(json.dumps(data, indent=2))
        elif getattr(args, "format", "table") == "list":
            print(format_accounts_listicle(data, show_quota=show_quota))
        else:
            print(format_accounts_table(data, show_quota=show_quota))
        return 0

    if args.command == "status":
        status_info = engine.get_status()

        qc_cls = _get_quota_client_class()
        client = qc_cls(timeout=2.0, engine=engine)
        cache_data = client.load_cache()

        dt_mod = _get_datetime()
        now = dt_mod.now(timezone.utc).timestamp()
        cache_age = now - cache_data.get("timestamp", 0)

        if cache_age > 60:
            launcher_env = os.environ.get("AGY_SWITCH_LAUNCHER")
            if launcher_env:
                script_abs_path = launcher_env
            else:
                candidate = Path(__file__).resolve().parent.parent / "agy_switch.py"
                if candidate.is_file():
                    script_abs_path = str(candidate)
                else:
                    script_abs_path = str(Path(__file__).resolve())
            subprocess.Popen(
                [sys.executable, script_abs_path, "internal-probe"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        if "accounts" in cache_data and status_info.get("active"):
            active_name = status_info["active"]
            q_data = cache_data["accounts"].get(active_name, {})
            status_info["cached_quota"] = q_data

        print(format_status_report(status_info))
        return 0

    if args.command == "doctor":
        is_json = getattr(args, "json", False)
        do_fix = getattr(args, "fix", False)

        report = engine.run_diagnostics()

        if do_fix:
            has_fixable = any(item.can_fix and not item.fixed for item in report.items)
            fixes = engine.fix_diagnostics(report) if has_fixable else []

            if not is_json:
                if fixes:
                    print(f"[agy-switch doctor] Applied {len(fixes)} automatic repair(s):")
                    for fix_desc in fixes:
                        print(f"  • {fix_desc}")
                    print()
                elif has_fixable:
                    print("[agy-switch doctor] No repairs were applied.\n")
                else:
                    if report.overall_status == "PASS":
                        print("[agy-switch doctor] All diagnostics healthy. No repairs needed.\n")
                    else:
                        print(
                            "[agy-switch doctor] No automatic repairs available for detected issues.\n"
                        )
            else:
                if fixes:
                    for fix_desc in fixes:
                        print(f"[agy-switch doctor] Applied repair: {fix_desc}", file=sys.stderr)

            # Re-run diagnostics to get clean post-repair report
            report = engine.run_diagnostics()

        if is_json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(format_diagnostic_report(report))

        return report.exit_code

    if args.command == "internal-probe":
        accounts_data = engine.list_accounts().get("accounts", [])
        qc_cls = _get_quota_client_class()
        client = qc_cls(timeout=2.0, engine=engine)
        fetch_all_fn = _get_fetch_all_accounts_quota()
        raw_quotas = fetch_all_fn(engine, accounts_data, client=client)
        valid_quotas = {k: v for k, v in raw_quotas.items() if "error" not in v and v}

        dt_mod = _get_datetime()
        client.save_cache(
            {"timestamp": dt_mod.now(timezone.utc).timestamp(), "accounts": valid_quotas}
        )
        return 0

    if args.command == "switch":
        cli_model = getattr(args, "model", None)
        if cli_model is not None:
            try:
                target_model = normalize_model_name(cli_model)
            except AgySwitchError:
                print(
                    f"[agy-switch] Unsupported model '{cli_model}'. Allowed models: 'gemini', 'claude'.",
                    file=sys.stderr,
                )
                return 1
        else:
            target_model = engine.get_default_model()

        if getattr(args, "auto", False):
            active_name = engine.get_active_name()
            accounts_data = engine.list_accounts().get("accounts", [])

            qc_cls = _get_quota_client_class()
            client = qc_cls(timeout=2.0, engine=engine)
            fetch_all_fn = _get_fetch_all_accounts_quota()
            raw_quotas = fetch_all_fn(engine, accounts_data, client=client)
            valid_quotas = {k: v for k, v in raw_quotas.items() if "error" not in v and v}

            if not valid_quotas:
                print("[agy-switch] No valid quota info available to auto-switch.", file=sys.stderr)
                return 1

            dt_mod = _get_datetime()
            client.save_cache(
                {"timestamp": dt_mod.now(timezone.utc).timestamp(), "accounts": valid_quotas}
            )

            for acc in accounts_data:
                acc["quota"] = valid_quotas.get(acc["name"], {})

            re_cls = _get_rotation_engine_class()
            rot_engine = re_cls(model_name=target_model, active_account_id=active_name)

            # Quota telemetry presence check for target_model
            accounts_with_telemetry = []
            for acc in accounts_data:
                q_data = acc.get("quota", {})
                status = rot_engine.get_quota_status(q_data, model_key=target_model)
                if status is not None:
                    accounts_with_telemetry.append((acc, status))

            if not accounts_with_telemetry:
                print(
                    f"[agy-switch] No accounts found with available quota telemetry for model '{target_model}'.",
                    file=sys.stderr,
                )
                return 1

            # Strict quota isolation: check if all accounts have verified 0% quota
            def _is_zero(st: Any) -> bool:
                try:
                    f = getattr(st, "fraction", 1.0)
                    return float(f) <= 0.0
                except (TypeError, ValueError):
                    return False

            if all(_is_zero(status) for _, status in accounts_with_telemetry):
                print(
                    f"[agy-switch] All accounts exhausted for model '{target_model}'. Cannot auto-switch.",
                    file=sys.stderr,
                )
                return 1

            cooldowns = engine.get_cooldowns()
            target_account = rot_engine.evaluate(
                accounts_data,
                current_active=active_name,
                target_model=target_model,
                cooldowns=cooldowns,
            )

            if target_account is None or target_account == active_name:
                active_status = None
                for acc, status in accounts_with_telemetry:
                    if acc.get("name") == active_name:
                        active_status = status
                        break

                active_fraction = 1.0
                try:
                    if active_status is not None:
                        active_fraction = float(getattr(active_status, "fraction", 1.0))
                except (TypeError, ValueError):
                    active_fraction = 1.0

                is_active_in_cooldown = bool(cooldowns and active_name in cooldowns)
                if is_active_in_cooldown and active_status is not None:
                    rec = cooldowns[active_name]
                    q_data = valid_quotas.get(active_name, {})
                    is_active_in_cooldown = rot_engine.is_cooldown_penalized_for_model(
                        target_model, active_fraction, rec, q_data
                    )

                if active_status is not None and active_fraction > 0.0 and not is_active_in_cooldown:
                    print(f"[agy-switch] Active account '{active_name}' is optimal. No switch needed.")
                    return 0

                print(
                    f"[agy-switch] All accounts exhausted for model '{target_model}'. Cannot auto-switch.",
                    file=sys.stderr,
                )
                return 1

            args.account = target_account

        elif getattr(args, "account", None) is None:
            print(
                "[agy-switch] Error: account name must be provided if --auto is not used.",
                file=sys.stderr,
            )
            return 1

        try:
            res = engine.switch_tokens(
                args.account,
                restart_server=not args.no_restart,
                force=args.force,
            )
            print(
                f"[agy-switch] Successfully switched active account to '{res['active']}' ({res.get('email') or 'unknown'})."
            )
            print("  - Updated: ~/.gemini/jetski-standalone-oauth-token (Antigravity 2.0)")
            print("  - Updated: ~/.gemini/antigravity-cli/antigravity-oauth-token (Antigravity CLI)")
            if res.get("server_restarted"):
                print("  - Server : Restarted language_server (new credentials loaded)")
            elif not args.no_restart:
                print("  - Server : language_server not running or no restart needed")
            return 0
        except ActiveTurnRunningError as e:
            print(f"[agy-switch] Warning: {e}", file=sys.stderr)
            return 2
        except AgySwitchError as e:
            err_msg = str(e)
            if err_msg.startswith("[agy-switch]"):
                print(err_msg, file=sys.stderr)
            else:
                print(f"[agy-switch] Error: {err_msg}", file=sys.stderr)
            return 1

    if args.command == "add":
        try:
            res = engine.add_account(args.name, args.token_file)
            print(f"[agy-switch] Account '{res['name']}' registered successfully.")
            if res.get("email"):
                print(f"  - Email: {res['email']}")
            print(f"  - Vault: {engine.accounts_dir / res['name'] / 'token.json'}")
            return 0
        except AgySwitchError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    if args.command == "login":
        try:
            res = engine.login_account(
                args.name,
                agy_binary=args.agy_binary,
                timeout=args.timeout,
                force=args.force,
            )
            email_info = f" ({res['email']})" if res.get("email") else ""
            print(
                f"[agy-switch] Account '{res['name']}'{email_info} successfully logged in and saved to vault."
            )
            print(f"  - Status: Standby (Run 'agy-switch switch {res['name']}' to activate)")
            print(f"  - Vault : {engine.accounts_dir / res['name'] / 'token.json'}")
            return 0
        except AgySwitchError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    if args.command == "remove":
        try:
            engine.remove_account(args.name, force=args.force)
            print(f"[agy-switch] Account '{args.name}' removed successfully from vault.")
            return 0
        except AgySwitchError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    if args.command == "rename":
        try:
            res = engine.rename_account(args.old_alias, args.new_alias, force=args.force)
            print(
                f"[agy-switch] Successfully renamed account '{res['old_alias']}' to '{res['new_alias']}'."
            )
            if res.get("is_active"):
                print(f"  - Active Pointer: Updated to '{res['new_alias']}'")
            if res.get("cache_migrated"):
                print(f"  - Telemetry Cache: Migrated quota keys to '{res['new_alias']}'")
            if res.get("cooldown_migrated"):
                print(f"  - Cooldown Cache: Migrated cooldown keys to '{res['new_alias']}'")
            return 0
        except ActiveTurnRunningError as e:
            print(f"Warning: {e}", file=sys.stderr)
            return 2
        except AgySwitchError as e:
            err_msg = str(e)
            if err_msg.startswith("[agy-switch]"):
                print(err_msg, file=sys.stderr)
            else:
                print(f"[agy-switch] {err_msg}", file=sys.stderr)
            return 1

    if args.command == "migrate":
        if migrated:
            print(
                f"[agy-switch] Successfully migrated {len(migrated)} account(s): {', '.join(migrated)}"
            )
        else:
            print("[agy-switch] No new accounts to migrate from legacy ~/.agy-cli-manager.")
        return 0

    if args.command == "config":
        if getattr(args, "config_command", None) == "auto-switch":
            state = getattr(args, "state", None)
            if state == "on":
                engine.set_auto_switch(True)
                print("[agy-switch] auto-switch enabled.")
            elif state == "off":
                engine.set_auto_switch(False)
                print("[agy-switch] auto-switch disabled.")
            else:
                current = engine.get_auto_switch()
                print(f"[agy-switch] auto-switch is currently {'on' if current else 'off'}.")
            return 0
        if getattr(args, "config_command", None) == "default-model":
            model_arg = getattr(args, "model", None)
            if model_arg is None:
                current = engine.get_default_model()
                print(f"[agy-switch] default-model is currently '{current}'.")
                return 0
            try:
                normalized = normalize_model_name(model_arg)
                engine.set_default_model(normalized)
                print(f"[agy-switch] default-model set to '{normalized}'.")
                return 0
            except AgySwitchError:
                print(
                    f"[agy-switch] Unsupported model '{model_arg}'. Allowed models: 'gemini', 'claude'.",
                    file=sys.stderr,
                )
                return 1
        cfg = engine.load_config()
        print(json.dumps(cfg, indent=2))
        return 0

    if args.command == "hook":
        if getattr(args, "hook_command", None) == "install":
            hooks_path = engine.install_hook()
            print(f"[agy-switch] PreInvocation hook registered successfully in {hooks_path}.")
            return 0
        subparser = getattr(args, "_subparser", parser)
        subparser.print_help()
        return 0

    if args.command == "watch":
        w_mod = sys.modules.get("watch") or sys.modules.get("scripts.watch")
        watch = w_mod if w_mod else DaemonManager.__module__
        from agy_switcher import watcher as watch_mod
        if w_mod:
            watch_mod = w_mod

        dm = watch_mod.DaemonManager(
            vault_dir=engine.vault_dir,
            proc_dir=getattr(engine, "proc_dir", None),
            gemini_dir=getattr(engine, "gemini_dir", None),
        )

        if getattr(args, "generate_systemd", False) or getattr(args, "install_systemd", False):
            target = (
                Path.home() / ".config" / "systemd" / "user" / "agy-switch-watcher.service"
                if getattr(args, "install_systemd", False)
                else None
            )
            unit_content = dm.generate_systemd(target_path=target)
            sys.stdout.write(unit_content)
            sys.stdout.flush()
            if target:
                print(f"[agy-switch watch] Installed systemd unit to {target}", file=sys.stderr)
            return 0

        action = getattr(args, "watch_action", None) or "run"

        if action == "start":
            try:
                cooldown = getattr(args, "cooldown", 60)
                pid = dm.start(cooldown=cooldown)
                print(f"[agy-switch watch] Daemon started in background (PID: {pid}).")
                print(f"  - Log: {dm.log_file}")
                print(f"  - PID: {dm.pid_file}")
                return 0
            except getattr(watch_mod, "DaemonAlreadyRunningError", DaemonAlreadyRunningError) as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            except Exception as e:
                print(f"Error starting daemon: {e}", file=sys.stderr)
                return 1

        elif action == "stop":
            try:
                stopped = dm.stop()
                if stopped:
                    print("[agy-switch watch] Daemon stopped successfully.")
                else:
                    print("[agy-switch watch] No active daemon was running.")
                return 0
            except Exception as e:
                print(f"Error stopping daemon: {e}", file=sys.stderr)
                return 1

        elif action == "status":
            try:
                st = dm.status()
                if st["running"]:
                    print(f"[agy-switch watch] Daemon is running (PID: {st['pid']}).")
                    print(f"  - Log: {st['log_file']}")
                    if st.get("targets"):
                        print("  - Monitored targets:")
                        for t in st["targets"]:
                            print(f"    * {t}")
                else:
                    print("[agy-switch watch] Daemon is stopped.")
                    print(f"  - Log: {st['log_file']}")
                return 0
            except Exception as e:
                print(f"Error checking daemon status: {e}", file=sys.stderr)
                return 1

        elif action == "run":
            cooldown_mins = getattr(args, "cooldown", 60)
            try:
                signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
            except Exception:
                pass
            try:
                watcher_cls = getattr(watch_mod, "LogWatcher", LogWatcher)
                watcher = watcher_cls(engine, cooldown_minutes=cooldown_mins)
                watcher.watch_loop()
                return 0
            except KeyboardInterrupt:
                return 0

    if args.command == "hook-pre-invocation":
        hook_fn = _resolve_symbol(
            "handle_pre_invocation_hook",
            lambda: handle_pre_invocation_hook,
        )
        return hook_fn(engine)

    return 0


if __name__ == "__main__":
    sys.exit(main())
