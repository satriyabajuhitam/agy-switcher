import datetime
from dataclasses import dataclass
from typing import Optional

@dataclass
class QuotaStatus:
    fraction: float
    reset_time: datetime.datetime

    def get_reset_delta_minutes(self, current_time: datetime.datetime) -> float:
        """Calculates reset delta in minutes relative to current_time."""
        return (self.reset_time - current_time).total_seconds() / 60.0

    def is_exhausted(self) -> bool:
        """Returns True if the quota is exhausted (<= 0% remaining)."""
        return self.fraction <= 0.0

    def has_available_quota(self) -> bool:
        """Returns True if there is positive quota remaining (> 0%)."""
        return self.fraction > 0.0

    def remaining_percentage(self) -> int:
        """Returns remaining quota as an integer percentage (0-100)."""
        return int(self.fraction * 100)

class RotationEngine:
    def __init__(self, model_name: str, active_account_id: str):
        self.model_name = model_name
        self.active_account_id = active_account_id
        
        if "gemini" in self.model_name.lower():
            self.target_group_prefix = "Gemini"
        elif "claude" in self.model_name.lower():
            self.target_group_prefix = "Claude"
        else:
            self.target_group_prefix = "Gemini"
            
    def _parse_time(self, t_str: str) -> datetime.datetime:
        if not t_str:
            return datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
        if t_str.endswith("Z"):
            t_str = t_str[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(t_str)
        
    def get_quota_status(self, quota_data: dict) -> Optional[QuotaStatus]:
        if not quota_data or "groups" not in quota_data:
            return None
            
        target_group = None
        for group in quota_data.get("groups", []):
            if group.get("group", "").startswith(self.target_group_prefix):
                target_group = group
                break
                
        if not target_group or "buckets" not in target_group:
            return None
            
        lowest_fraction = 1.0
        earliest_reset = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
        
        for bucket in target_group["buckets"]:
            frac = bucket.get("remainingFraction", 1.0)
            reset_time = self._parse_time(bucket.get("resetTime"))
            if frac < lowest_fraction:
                lowest_fraction = frac
                earliest_reset = reset_time
            elif frac == lowest_fraction and reset_time < earliest_reset:
                earliest_reset = reset_time
                
        return QuotaStatus(fraction=lowest_fraction, reset_time=earliest_reset)

    def is_exhausted(self, quota_data: dict) -> bool:
        """Returns True if the relevant quota is completely exhausted (0% remaining)."""
        status = self.get_quota_status(quota_data)
        if status is None:
            return False
        return status.is_exhausted()

    def has_available_quota(self, quota_data: dict) -> bool:
        """Returns True if the relevant quota has positive remaining quota."""
        status = self.get_quota_status(quota_data)
        if status is None:
            return False
        return status.has_available_quota()

    def calculate_reset_delta(self, reset_time: datetime.datetime, current_time: datetime.datetime) -> float:
        """Calculates reset delta in minutes."""
        return QuotaStatus(fraction=0.0, reset_time=reset_time).get_reset_delta_minutes(current_time)

    def evaluate(self, accounts: dict, current_time: datetime.datetime = None) -> str:
        """
        accounts: dict mapping account_id -> quota JSON
        """
        if current_time is None:
            current_time = datetime.datetime.now(datetime.timezone.utc)
            
        evaluated = {}
        reset_deltas = {}
        for acc_id, quota_data in accounts.items():
            status = self.get_quota_status(quota_data)
            if status is not None:
                evaluated[acc_id] = status
                reset_deltas[acc_id] = self.calculate_reset_delta(status.reset_time, current_time)
                
        if not evaluated:
            return self.active_account_id
            
        active_status = evaluated.get(self.active_account_id)
        
        # 1. cache_optimized heuristic
        if active_status is not None and active_status.fraction > 0.15:
            return self.active_account_id
            
        # 2. drain_window heuristic
        if active_status is not None and active_status.fraction > 0.05:
            reset_delta = reset_deltas.get(self.active_account_id, float("inf"))
            if reset_delta <= 30:
                return self.active_account_id
                
        # 3. imminent_reset priority
        best_acc = None
        best_imminent_frac = -1.0
        best_normal_frac = -1.0
        
        for acc_id, status in evaluated.items():
            reset_delta = reset_deltas[acc_id]
            
            if reset_delta <= 45 and status.fraction > 0.10:
                if status.fraction > best_imminent_frac:
                    best_imminent_frac = status.fraction
                    best_acc = acc_id
            else:
                if best_imminent_frac == -1.0 and status.fraction > best_normal_frac:
                    best_normal_frac = status.fraction
                    best_acc = acc_id
                    
        return best_acc if best_acc else self.active_account_id
