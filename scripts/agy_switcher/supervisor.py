"""Process supervisor for language_server and active turn safety enforcement.

Layer 1 Core Services module: Depends only on Layer 0 (exceptions) and standard library.
Pure Python 3.10+ standard library. Zero external dependencies.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time

from agy_switcher.exceptions import ActiveTurnRunningError


class ProcessSupervisor:
    """Supervises the Antigravity language_server process, enforcing turn safety."""

    def __init__(
        self,
        gemini_dir: Path | str | None = None,
        proc_dir: Path | str | None = None,
    ) -> None:
        self.gemini_dir = Path(gemini_dir or Path.home() / ".gemini").expanduser()
        self.proc_dir = Path(proc_dir) if proc_dir else Path("/proc")

    def locate_language_server_pid(self) -> int | None:
        """Finds running language_server PID by scanning /proc or pgrep."""
        # 1. Scan proc_dir for exact binary command line
        proc_path = self.proc_dir
        if proc_path.is_dir():
            try:
                for pid_dir in proc_path.iterdir():
                    if not pid_dir.name.isdigit():
                        continue
                    cmdline_file = pid_dir / "cmdline"
                    try:
                        cmdline = cmdline_file.read_bytes().replace(b"\x00", b" ")
                        if b"language_server" in cmdline and b"--standalone" in cmdline:
                            return int(pid_dir.name)
                    except (OSError, ValueError):
                        continue
            except OSError:
                pass

        # 2. Fallback to pgrep only if proc_dir is the real /proc
        if self.proc_dir == Path("/proc"):
            try:
                res = subprocess.run(
                    ["pgrep", "-f", "language_server.*--standalone"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                lines = res.stdout.strip().split()
                if lines and lines[0].isdigit():
                    return int(lines[0])
            except Exception:
                pass

        return None

    def is_server_busy(self, pid: int | None = None) -> bool:
        """Checks if language_server has active child processes or presence locks."""
        # 1. Check presence locks in ~/.gemini
        presence_dirs = [
            self.gemini_dir / "antigravity" / "presence",
            self.gemini_dir / "antigravity-cli" / "presence",
        ]
        for p_dir in presence_dirs:
            if p_dir.is_dir():
                try:
                    lock_files = [f for f in p_dir.iterdir() if f.is_file() and f.suffix == ".lock"]
                    if lock_files:
                        return True
                except OSError:
                    pass

        # 2. Check child processes of pid if provided
        if pid is not None:
            proc_path = self.proc_dir
            if proc_path.is_dir():
                try:
                    for p_dir in proc_path.iterdir():
                        if not p_dir.name.isdigit():
                            continue
                        stat_file = p_dir / "stat"
                        try:
                            content = stat_file.read_bytes().decode("utf-8", errors="replace")
                            rparen = content.rfind(")")
                            if rparen != -1:
                                rest = content[rparen + 1:].strip().split()
                                if len(rest) >= 2 and rest[1] == str(pid):
                                    if rest[0] == "Z":
                                        continue
                                    return True
                        except (OSError, IndexError, ValueError):
                            continue
                except OSError:
                    pass

        return False

    def _send_signal(self, pid: int, sig: int = signal.SIGTERM) -> None:
        """Dispatches signal to target process."""
        os.kill(pid, sig)

    def _poll_process_exit(self, pid: int, timeout: float = 3.0) -> bool:
        """Polls until process with given pid has terminated."""
        deadline = time.time() + max(0.0, timeout)
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except OSError as exc:
                if getattr(exc, "errno", None) == 3:  # errno.ESRCH
                    return True
            if time.time() >= deadline:
                break
            time.sleep(0.1)
        return False

    def verify_turn_safety(self, force: bool = False, pid: int | None = None) -> None:
        """Validates that language_server is not actively executing a turn.

        Raises:
            ActiveTurnRunningError: If server is busy and force is False.
        """
        target_pid = pid if pid is not None else self.locate_language_server_pid()
        if target_pid is not None and self.is_server_busy(target_pid) and not force:
            raise ActiveTurnRunningError(
                "Antigravity is currently busy processing an agent turn or background task. "
                "Use --force to switch and restart anyway."
            )

    def restart_language_server(self, force: bool = False) -> bool:
        """Restarts language_server gracefully via SIGTERM so WSL Relay auto-respawns it.

        If force=True and the process fails to exit within 3.0s, escalates to SIGKILL
        and polls for up to 1.0s.
        """
        pid = self.locate_language_server_pid()
        if pid is None:
            return False

        self.verify_turn_safety(force=force, pid=pid)

        try:
            self._send_signal(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return True

        if self._poll_process_exit(pid, timeout=3.0):
            return True

        if not force:
            return False

        try:
            self._send_signal(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except OSError:
            pass

        return self._poll_process_exit(pid, timeout=1.0)



__all__ = ["ProcessSupervisor"]
