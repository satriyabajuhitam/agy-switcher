"""Rotation Engine: Multi-account quota ranking, heuristic evaluation, and model steering.

Layer 2 Domain Engine: Depends strictly on Layer 0 (exceptions, models) and Layer 1 (telemetry).
Zero external dependencies. Pure Python 3.10+ standard library.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from typing import Any, Optional, Union

from agy_switcher.exceptions import AgySwitchError
from agy_switcher.models import EvaluationCandidate, QuotaStatus
from agy_switcher.telemetry import normalize_model_name


DEFAULT_QUOTA_THRESHOLD: float = 0.25


class RotationEngine:
    """Intelligent multi-account rotation engine supporting quota heuristics and model steering."""

    def __init__(
        self,
        vault_or_model: Path | str | None = None,
        client_or_active: Any | None = None,
        cooldown_manager: Any | None = None,
        *,
        model_name: str | None = None,
        active_account_id: str | None = None,
        vault_dir: Path | str | None = None,
        client: Any | None = None,
        target_model: str | None = None,
        threshold: float | int | None = None,
    ) -> None:
        resolved_vault: Path | None = Path(vault_dir).expanduser() if vault_dir else None
        resolved_client = client
        resolved_active = active_account_id
        resolved_model = model_name or target_model
        resolved_cm = cooldown_manager

        if vault_or_model is not None:
            if isinstance(vault_or_model, Path):
                resolved_vault = vault_or_model
            elif isinstance(vault_or_model, str):
                is_explicit_path = (
                    "/" in vault_or_model
                    or "\\" in vault_or_model
                    or vault_or_model.startswith((".", "~"))
                )
                is_known_model = False
                try:
                    normalize_model_name(vault_or_model)
                    is_known_model = True
                except Exception:
                    is_known_model = False

                if not is_explicit_path and is_known_model:
                    if resolved_model is None:
                        resolved_model = vault_or_model
                elif is_explicit_path or os.path.isdir(vault_or_model):
                    resolved_vault = Path(vault_or_model).expanduser()
                else:
                    if resolved_model is None:
                        resolved_model = vault_or_model

        if client_or_active is not None:
            if hasattr(client_or_active, "fetch_quota") or hasattr(client_or_active, "load_cache"):
                if resolved_client is None:
                    resolved_client = client_or_active
            else:
                if resolved_active is None:
                    resolved_active = str(client_or_active)

        self.vault_dir = resolved_vault
        self.client = resolved_client
        self.cooldown_manager = resolved_cm
        self.active_account_id = resolved_active
        self.model_name = resolved_model or "gemini"

        norm_model = (self.model_name or "gemini").strip()
        try:
            self.target_model_key = normalize_model_name(norm_model)
        except Exception:
            self.target_model_key = "claude" if "claude" in norm_model.lower() else "gemini"

        if self.target_model_key == "claude":
            self.target_group_prefix = "Claude"
        else:
            self.target_group_prefix = "Gemini"

        if threshold is not None:
            try:
                t_val = float(threshold)
                self.threshold = t_val / 100.0 if t_val > 1.0 else t_val
            except (ValueError, TypeError):
                self.threshold = DEFAULT_QUOTA_THRESHOLD
        elif resolved_vault:
            cfg_file = resolved_vault / "config.json"
            cfg_threshold = None
            if cfg_file.is_file():
                try:
                    cfg_data = json.loads(cfg_file.read_text(encoding="utf-8"))
                    if isinstance(cfg_data, dict) and "quota_threshold" in cfg_data:
                        raw_t = float(cfg_data["quota_threshold"])
                        cfg_threshold = raw_t / 100.0 if raw_t > 1.0 else raw_t
                except Exception:
                    pass
            self.threshold = cfg_threshold if cfg_threshold is not None else DEFAULT_QUOTA_THRESHOLD
        else:
            self.threshold = DEFAULT_QUOTA_THRESHOLD

    def _parse_time(self, t_val: Any) -> datetime:
        """Parses various timestamp representations into a timezone-aware UTC datetime."""
        if not t_val:
            return datetime.max.replace(tzinfo=timezone.utc)
        if isinstance(t_val, datetime):
            return t_val if t_val.tzinfo else t_val.replace(tzinfo=timezone.utc)
        if isinstance(t_val, (int, float)):
            try:
                return datetime.fromtimestamp(t_val, tz=timezone.utc)
            except Exception:
                return datetime.max.replace(tzinfo=timezone.utc)
        if isinstance(t_val, str):
            if t_val.isdigit():
                try:
                    return datetime.fromtimestamp(int(t_val), tz=timezone.utc)
                except Exception:
                    pass
            t_str = t_val
            if t_str.endswith("Z"):
                t_str = t_str[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(t_str)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                return datetime.max.replace(tzinfo=timezone.utc)
        return datetime.max.replace(tzinfo=timezone.utc)

    def get_quota_status(self, quota_data: dict, model_key: str | None = None) -> Optional[QuotaStatus]:
        """Extracts QuotaStatus for the target model_key from quota payload dictionary."""
        if not quota_data or not isinstance(quota_data, dict):
            return None

        m_key = model_key or self.target_model_key

        # 1. Check last_known_quota.json cache schema: {"gemini": {"quota": X, "reset_time": Y}}
        target_info = None
        if m_key in quota_data and isinstance(quota_data[m_key], dict):
            target_info = quota_data[m_key]
        elif "quota" in quota_data and not isinstance(quota_data["quota"], dict) and m_key == "gemini":
            target_info = quota_data

        if target_info is not None and isinstance(target_info, dict):
            q_val = target_info.get("quota")
            r_val = target_info.get("reset_time")
            if q_val is not None:
                if isinstance(q_val, int):
                    fraction = max(0.0, min(1.0, float(q_val) / 100.0))
                elif isinstance(q_val, float):
                    if q_val > 1.0:
                        fraction = float(q_val) / 100.0
                    elif q_val >= 0.0:
                        fraction = float(q_val)
                    else:
                        fraction = 0.0
                else:
                    fraction = 0.0
                reset_time = self._parse_time(r_val)
                return QuotaStatus(fraction=fraction, reset_time=reset_time)

        # 2. Check CloudCode raw groups schema: {"groups": [{"group": "...", "buckets": [...]}]}
        if "groups" in quota_data:
            target_prefix = "Claude" if "claude" in m_key.lower() else "Gemini"
            target_group = None
            for group in quota_data.get("groups", []):
                g_name = group.get("displayName") or group.get("group", "")
                if g_name.startswith(target_prefix):
                    target_group = group
                    break

            if target_group and "buckets" in target_group:
                lowest_fraction = 1.0
                earliest_reset = datetime.max.replace(tzinfo=timezone.utc)
                for bucket in target_group["buckets"]:
                    frac = bucket.get("remainingFraction", 1.0)
                    reset_time = self._parse_time(bucket.get("resetTime"))
                    if frac < lowest_fraction:
                        lowest_fraction = frac
                        earliest_reset = reset_time
                    elif frac == lowest_fraction and reset_time < earliest_reset:
                        earliest_reset = reset_time
                return QuotaStatus(fraction=lowest_fraction, reset_time=earliest_reset)

        return None

    def is_exhausted(self, quota_data: dict) -> bool:
        """Returns True if the relevant quota is completely exhausted (0% remaining)."""
        status = self.get_quota_status(quota_data)
        if status is None:
            return False
        return status.is_exhausted()

    def has_available_quota(self, quota_data: dict) -> bool:
        """Returns True if the relevant quota has positive remaining quota (> 0%)."""
        status = self.get_quota_status(quota_data)
        if status is None:
            return False
        return status.has_available_quota()

    def calculate_reset_delta(self, reset_time: datetime, current_time: datetime) -> float:
        """Calculates reset delta in minutes relative to current_time."""
        return QuotaStatus(fraction=0.0, reset_time=reset_time).get_reset_delta_minutes(current_time)

    def is_cooldown_penalized_for_model(
        self,
        model_key: str,
        fraction: float,
        rec: Any,
        q_data: dict,
    ) -> bool:
        """Determines if an account in cooldown is penalized for the target model_key.

        An account in cooldown is exempt from penalty for model_key ONLY if:
        1. It has healthy quota (> self.threshold) for the target model.
        2. The cooldown was triggered by exhaustion of the alternate model family.
        3. The cooldown does not explicitly penalize the target model family.
        """
        if fraction <= self.threshold:
            return True

        norm_key = "claude" if "claude" in model_key.lower() else "gemini"
        if norm_key == "claude":
            target_tokens = ("claude", "anthropic")
            alt_model_key = "gemini"
            alt_tokens = ("gemini",)
        else:
            target_tokens = ("gemini",)
            alt_model_key = "claude"
            alt_tokens = ("claude", "anthropic")

        is_alt_exhausted = False
        is_explicitly_target = False

        if isinstance(rec, dict):
            r_str = str(rec.get("reason", "")).lower()
            m_str = str(rec.get("model", "")).lower()
            if any(tok in r_str or tok in m_str for tok in target_tokens):
                is_explicitly_target = True
            if any(tok in r_str or tok in m_str for tok in alt_tokens):
                is_alt_exhausted = True

        if not is_explicitly_target and not is_alt_exhausted:
            alt_status = self.get_quota_status(q_data, model_key=alt_model_key)
            if alt_status is not None and alt_status.fraction <= 0.0:
                is_alt_exhausted = True

        if is_alt_exhausted and not is_explicitly_target:
            return False

        return True

    _is_cooldown_penalized_for_model = is_cooldown_penalized_for_model

    def _load_quota_cache(self) -> dict[str, Any]:
        """Loads quota cache from client or vault_dir/cache/last_known_quota.json."""
        if self.client is not None and hasattr(self.client, "load_cache"):
            try:
                data = self.client.load_cache()
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        if self.vault_dir is not None:
            cache_file = self.vault_dir / "cache" / "last_known_quota.json"
            if cache_file.is_file():
                try:
                    data = json.loads(cache_file.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
        return {}

    def evaluate(
        self,
        accounts: list[dict] | list[str] | dict,
        current_active: str | None = None,
        target_model: str | None = None,
        current_time: datetime | None = None,
        cooldowns: dict | None = None,
        strategy: str = "auto_highest",
        *,
        active: str | None = None,
    ) -> str | None:
        """Evaluates accounts and returns the optimal account to switch to, or None if current_active is optimal.

        Applies heuristics:
        1. cache_optimized: retain current active if remaining quota > self.threshold.
        2. drain_window: retain current active if remaining quota > 5% and reset in <= 30 minutes.
        3. auto_highest: select standby account with highest quota, prioritizing accounts nearing reset
           (<= 45m with >= self.threshold quota), tie-breaking by earliest reset delta.
        """
        if current_active is None and active is not None:
            current_active = active

        if current_time is None:
            current_time = datetime.now(timezone.utc)
        elif current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        target_model = target_model or self.model_name or "gemini"
        try:
            model_key = normalize_model_name(target_model)
        except Exception:
            model_key = "claude" if "claude" in str(target_model).lower() else "gemini"

        # Resolve cooldowns from cooldown_manager if not provided explicitly
        if cooldowns is None and self.cooldown_manager is not None:
            if hasattr(self.cooldown_manager, "get_active_cooldowns"):
                try:
                    cooldowns = self.cooldown_manager.get_active_cooldowns()
                except Exception:
                    pass
            elif hasattr(self.cooldown_manager, "cooldowns"):
                cooldowns = getattr(self.cooldown_manager, "cooldowns")

        # Normalize accounts into a list of (account_id, quota_dict)
        cached_data: dict[str, Any] | None = None

        def _get_cached_map() -> dict[str, Any]:
            nonlocal cached_data
            if cached_data is None:
                raw_cache = self._load_quota_cache()
                if isinstance(raw_cache, dict):
                    accounts_field = raw_cache.get("accounts")
                    cached_data = accounts_field if isinstance(accounts_field, dict) else raw_cache
                else:
                    cached_data = {}
            return cached_data if isinstance(cached_data, dict) else {}

        items: list[tuple[str, dict]] = []
        raw_quota_map: dict[str, dict] = {}

        if isinstance(accounts, dict):
            if current_active is None and isinstance(accounts.get("active"), str):
                current_active = accounts["active"]

            acc_list_or_map = accounts.get("accounts")
            if isinstance(acc_list_or_map, list):
                # Standard vault or quota pipeline structure: {"active": "...", "accounts": [...]}
                accounts_to_process = acc_list_or_map
            elif isinstance(acc_list_or_map, dict):
                # Cache structure: {"timestamp": ..., "accounts": {"acc1": {...}}}
                accounts_to_process = acc_list_or_map
            else:
                accounts_to_process = accounts
        else:
            accounts_to_process = accounts

        if isinstance(accounts_to_process, dict):
            for acc_id, q_data in accounts_to_process.items():
                if not isinstance(acc_id, str) or not acc_id.strip():
                    continue
                # Skip non-account metadata keys if legacy root dict passed
                if acc_id in ("timestamp", "active", "error") and not isinstance(q_data, dict):
                    continue
                acc_name = acc_id.strip()
                if isinstance(q_data, dict):
                    has_quota = any(k in q_data for k in ("gemini", "claude", "quota", "groups"))
                    if not has_quota:
                        q_data = _get_cached_map().get(acc_name, q_data)
                else:
                    q_data = _get_cached_map().get(acc_name, {})
                items.append((acc_name, q_data if isinstance(q_data, dict) else {}))
        elif isinstance(accounts_to_process, list):
            for acc in accounts_to_process:
                if isinstance(acc, str):
                    acc_name = acc.strip()
                    if not acc_name:
                        continue
                    q_data = _get_cached_map().get(acc_name, {})
                    items.append((acc_name, q_data if isinstance(q_data, dict) else {}))
                elif isinstance(acc, dict):
                    acc_name = acc.get("name") or acc.get("account") or acc.get("id")
                    if not acc_name or not isinstance(acc_name, str):
                        continue
                    acc_name = acc_name.strip()
                    if current_active is None and acc.get("is_active"):
                        current_active = acc_name
                    q_data = acc.get("quota") if "quota" in acc and isinstance(acc["quota"], dict) else acc
                    has_quota = any(k in q_data for k in ("gemini", "claude", "quota", "groups"))
                    if not has_quota:
                        q_data = _get_cached_map().get(acc_name, q_data)
                    items.append((acc_name, q_data if isinstance(q_data, dict) else {}))

        if current_active is None:
            current_active = self.active_account_id

        evaluated: dict[str, QuotaStatus] = {}
        reset_deltas: dict[str, float] = {}

        for acc_id, q_data in items:
            raw_quota_map[acc_id] = q_data
            status = self.get_quota_status(q_data, model_key=model_key)
            if status is not None:
                evaluated[acc_id] = status
                # Encapsulate reset delta calculation strictly once per account
                reset_deltas[acc_id] = self.calculate_reset_delta(status.reset_time, current_time)

        if not evaluated:
            return None

        active_status = evaluated.get(current_active) if current_active else None
        is_active_in_cooldown = bool(cooldowns and current_active and current_active in cooldowns)
        if is_active_in_cooldown and active_status is not None:
            rec = cooldowns[current_active]
            q_data = raw_quota_map.get(current_active, {})
            is_active_in_cooldown = self._is_cooldown_penalized_for_model(
                model_key, active_status.fraction, rec, q_data
            )

        if not is_active_in_cooldown:
            # 1. cache_optimized heuristic: retain current active if fraction > self.threshold
            if active_status is not None and active_status.fraction > self.threshold:
                return None

            # 2. drain_window heuristic: retain current active if fraction > 0.05 and reset in <= 30m
            if active_status is not None and active_status.fraction > 0.05:
                reset_delta = reset_deltas.get(current_active, float("inf"))
                if 0 <= reset_delta <= 30:
                    return None

        # 3. Candidate evaluation
        # Active account is explicitly excluded from being selected as the fallback.
        # Candidates must strictly have positive quota (fraction > 0.0).
        candidates: list[EvaluationCandidate] = []
        for acc_id, status in evaluated.items():
            if acc_id == current_active:
                continue
            if status.fraction <= 0.0 or not status.has_available_quota():
                continue

            # Model-aware cooldown evaluation
            if cooldowns and acc_id in cooldowns:
                rec = cooldowns[acc_id]
                q_data = raw_quota_map.get(acc_id, {})
                if self._is_cooldown_penalized_for_model(model_key, status.fraction, rec, q_data):
                    continue

            candidates.append(
                EvaluationCandidate(
                    account_id=acc_id,
                    status=status,
                    reset_delta=reset_deltas.get(acc_id, float("inf")),
                )
            )

        if not candidates:
            return None

        # Imminent reset priority: accounts nearing reset (reset_delta <= 45m)
        # with healthy quota (fraction >= self.threshold).
        # Imminent accounts with very low quota (< self.threshold) do not qualify for imminent priority
        # so they do not starve accounts with higher / 100% quota.
        imminent_candidates = [
            c for c in candidates
            if 0 <= c.reset_delta <= 45 and c.status.fraction >= self.threshold
        ]

        if imminent_candidates:
            # Sort by highest fraction first, tie-break by earliest reset delta
            imminent_candidates.sort(key=lambda c: (-c.status.fraction, c.reset_delta))
            return imminent_candidates[0].account_id

        # Prioritize candidate with highest absolute quota, tie-break by earliest reset delta
        candidates.sort(key=lambda c: (-c.status.fraction, c.reset_delta))
        return candidates[0].account_id


__all__ = ["RotationEngine"]
