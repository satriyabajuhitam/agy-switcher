import datetime

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
            
    def _parse_time(self, t_str):
        if not t_str:
            return datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
        if t_str.endswith("Z"):
            t_str = t_str[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(t_str)
        
    def _get_worst_case(self, quota_data):
        if not quota_data or "groups" not in quota_data:
            return None, None
            
        target_group = None
        for group in quota_data.get("groups", []):
            if group.get("group", "").startswith(self.target_group_prefix):
                target_group = group
                break
                
        if not target_group or "buckets" not in target_group:
            return None, None
            
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
                
        return lowest_fraction, earliest_reset

    def evaluate(self, accounts: dict, current_time: datetime.datetime = None):
        """
        accounts: dict mapping account_id -> quota JSON
        """
        if current_time is None:
            current_time = datetime.datetime.now(datetime.timezone.utc)
            
        evaluated = {}
        for acc_id, quota_data in accounts.items():
            frac, reset_time = self._get_worst_case(quota_data)
            if frac is not None:
                evaluated[acc_id] = (frac, reset_time)
                
        if not evaluated:
            return self.active_account_id
            
        active_frac, active_reset = evaluated.get(self.active_account_id, (None, None))
        
        # 1. cache_optimized heuristic
        if active_frac is not None and active_frac > 0.15:
            return self.active_account_id
            
        # 2. drain_window heuristic
        if active_frac is not None and active_frac > 0.05:
            reset_delta = (active_reset - current_time).total_seconds() / 60.0
            if reset_delta <= 30:
                return self.active_account_id
                
        # 3. imminent_reset priority
        best_acc = None
        best_imminent_frac = -1
        best_normal_frac = -1
        
        for acc_id, (frac, reset_time) in evaluated.items():
            reset_delta = (reset_time - current_time).total_seconds() / 60.0
            
            if reset_delta <= 45 and frac > 0.10:
                if frac > best_imminent_frac:
                    best_imminent_frac = frac
                    best_acc = acc_id
            else:
                if best_imminent_frac == -1 and frac > best_normal_frac:
                    best_normal_frac = frac
                    best_acc = acc_id
                    
        return best_acc if best_acc else self.active_account_id
