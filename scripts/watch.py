import os
import time
import re
import glob
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

try:
    from agy_switch import AgySwitchEngine, RotationEngine
except ImportError:
    from scripts.agy_switch import AgySwitchEngine, RotationEngine

class LogWatcher:
    def __init__(self, engine: AgySwitchEngine, log_dirs: list[str] = None, cooldown_minutes: int = 60):
        self.engine = engine
        self.log_dirs = log_dirs or [
            str(self.engine.gemini_dir / "antigravity-cli" / "log"),
            str(self.engine.gemini_dir / "antigravity" / "log"),
        ]
        self.cooldown_minutes = cooldown_minutes
        self.cooldown_file = self.engine.cache_dir / "cooldown.json"
        
        self.exhaustion_regex = re.compile(
            r"RESOURCE_EXHAUSTED \(\s*code\s*429\s*\)|Individual quota reached|weekly quota reached",
            re.IGNORECASE
        )
        
        self.file_cursors = {}

    def is_exhausted_message(self, line: str) -> bool:
        return bool(self.exhaustion_regex.search(line))

    def process_line(self, line: str):
        if self.is_exhausted_message(line):
            print(f"[watch] Detected quota exhaustion: {line.strip()}", file=sys.stderr)
            self.trigger_switch()

    def get_cooldowns(self) -> dict:
        if not self.cooldown_file.is_file():
            return {}
        try:
            return json.loads(self.cooldown_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def set_cooldown(self, account_name: str):
        cooldowns = self.get_cooldowns()
        # clear old cooldowns
        now = time.time()
        cooldowns = {k: v for k, v in cooldowns.items() if now - v < self.cooldown_minutes * 60}
        cooldowns[account_name] = now
        
        self.engine.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cooldown_file.write_text(json.dumps(cooldowns, indent=2), encoding="utf-8")

    def trigger_switch(self):
        active_name = self.engine.get_active_name()
        if not active_name:
            print("[watch] No active account found. Cannot switch.", file=sys.stderr)
            return

        self.set_cooldown(active_name)
        cooldowns = self.get_cooldowns()
        now = time.time()
        
        accounts_data = self.engine.list_accounts()
        # Filter out accounts in cooldown
        valid_accounts = []
        for acc in accounts_data.get("accounts", []):
            acc_name = acc["name"]
            if acc_name == active_name:
                continue
            if acc_name in cooldowns and now - cooldowns[acc_name] < self.cooldown_minutes * 60:
                continue
            if acc["valid"]:
                valid_accounts.append(acc)

        if not valid_accounts:
            print("[watch] All other valid accounts are in cooldown. Cannot switch.", file=sys.stderr)
            return

        # Read cache to get quotas
        cache = {}
        if self.engine.cache_file.is_file():
            try:
                cache = json.loads(self.engine.cache_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        
        for acc in valid_accounts:
            acc["quota"] = cache.get(acc["name"], {})
        
        rot_engine = RotationEngine(model_name="gemini", active_account_id=active_name)
        target_account = rot_engine.evaluate(valid_accounts, current_active=active_name, target_model="gemini")
        
        if not target_account or target_account == active_name:
            # Fallback to the first available if rotation engine is strict
            target_account = valid_accounts[0]["name"]
            
        print(f"[watch] Switching from '{active_name}' to '{target_account}' mid-turn...", file=sys.stderr)
        try:
            self.engine.switch_tokens(target_account, restart_server=False, force=True)
            print(f"[watch] Successfully switched to '{target_account}'.", file=sys.stderr)
            # small sleep to avoid rapid re-triggering for the same error
            time.sleep(2)
        except Exception as e:
            print(f"[watch] Error switching accounts: {e}", file=sys.stderr)

    def scan_new_files(self) -> list[str]:
        log_files = []
        for log_dir in self.log_dirs:
            if not os.path.isdir(log_dir):
                continue
            for ext in ("*.log", "*.txt"):
                log_files.extend(glob.glob(os.path.join(log_dir, ext)))
        return log_files

    def watch_loop(self):
        print("[watch] Daemon started, tailing logs for quota exhaustion...", file=sys.stderr)
        while True:
            try:
                current_files = self.scan_new_files()
                for file_path in current_files:
                    if file_path not in self.file_cursors:
                        # Start tailing from the end of new files
                        self.file_cursors[file_path] = os.path.getsize(file_path)

                for file_path in list(self.file_cursors.keys()):
                    if not os.path.isfile(file_path):
                        del self.file_cursors[file_path]
                        continue

                    current_size = os.path.getsize(file_path)
                    cursor = self.file_cursors[file_path]

                    if current_size < cursor:
                        # File was rotated/truncated
                        cursor = 0

                    if current_size > cursor:
                        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                            f.seek(cursor)
                            lines = f.readlines()
                            self.file_cursors[file_path] = f.tell()
                            
                            for line in lines:
                                self.process_line(line)
                                
                time.sleep(0.5)
            except KeyboardInterrupt:
                print("\n[watch] Stopping daemon.", file=sys.stderr)
                break
            except Exception as e:
                print(f"[watch] Unhandled exception in loop: {e}", file=sys.stderr)
                time.sleep(5)

if __name__ == "__main__":
    engine = AgySwitchEngine()
    watcher = LogWatcher(engine)
    watcher.watch_loop()
