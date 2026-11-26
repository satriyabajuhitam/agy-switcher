"""Process supervisor for language_server and active turn safety enforcement.

Layer 1 Core Services module: Depends only on Layer 0 (exceptions, security) and standard library.
Pure Python 3.10+ standard library. Zero external dependencies.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from agy_switcher.exceptions import ActiveTurnRunningError
from agy_switcher.security import read_latest_jsonl_record


def is_transcript_record_quiescent(record: object) -> bool:
    """Returns True only for a completed PLANNER_RESPONSE record without tool_calls."""
    if not isinstance(record, dict):
        return False

    record_type = record.get("type")
    if not isinstance(record_type, str) or record_type.strip().upper() != "PLANNER_RESPONSE":
        return False

    status = record.get("status")
    if not isinstance(status, str) or status.strip().upper() != "DONE":
        return False

    tool_calls = record.get("tool_calls")
    if tool_calls is None:
        return True
    if isinstance(tool_calls, (list, tuple, set, dict)):
        return len(tool_calls) == 0
    return False


class ProcessSupervisor:
    """Supervises the Antigravity language_server process, enforcing turn safety."""

    _MAX_ANCESTRY_HOPS = 64
    _PRESENCE_ROOTS = ("antigravity", "antigravity-cli")
    _NODE_BIN_NAMES = frozenset({"node", "nodejs"})
    _SIDECAR_ENTRYPOINT_FILES = frozenset(
        {"sidecar_sdk", "sidecar_sdk.js", "sidecar_sdk.mjs", "sidecar_sdk.cjs"}
    )
    _NODE_NON_ENTRYPOINT_FLAGS = frozenset(
        {"-e", "--eval", "-p", "--print", "-i", "--interactive", "-c", "--check"}
    )
    _NODE_VALUE_FLAGS = frozenset(
        {
            "-r",
            "--require",
            "--import",
            "--loader",
            "--experimental-loader",
            "--env-file",
            "-C",
            "--conditions",
            "--input-type",
            "--title",
            "--max-http-header-size",
            "--diagnostic-dir",
            "--heapsnapshot-signal",
            "--redirect-warnings",
            "--icu-data-dir",
            "--openssl-config",
            "--tls-cipher-list",
            "--watch-path",
            "--report-directory",
            "--report-dir",
            "--report-filename",
            "--report-signal",
            "--cpu-prof-dir",
            "--cpu-prof-name",
            "--heap-prof-dir",
            "--heap-prof-name",
            "--test-reporter",
            "--test-reporter-destination",
            "--test-shard",
        }
    )

    def __init__(
        self,
        gemini_dir: Path | str | None = None,
        proc_dir: Path | str | None = None,
    ) -> None:
        self.gemini_dir = Path(gemini_dir or Path.home() / ".gemini").expanduser()
        self.proc_dir = Path(proc_dir) if proc_dir else Path("/proc")

    @staticmethod
    def _has_language_server_cmdline(cmdline_file: Path) -> bool:
        """Returns True when cmdline contains both language_server and --standalone."""
        try:
            cmdline = cmdline_file.read_bytes().replace(b"\x00", b" ")
        except OSError:
            return False
        return b"language_server" in cmdline and b"--standalone" in cmdline

    @staticmethod
    def _parse_stat_state_and_ppid(stat_file: Path) -> tuple[str, int] | None:
        """Parses (state, ppid) from /proc/<pid>/stat via rfind(')'), failing safe to None."""
        try:
            content = stat_file.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return None
        lparen = content.find("(")
        rparen = content.rfind(")")
        if lparen == -1 or rparen <= lparen:
            return None
        rest = content[rparen + 1 :].strip().split()
        if len(rest) < 2:
            return None
        try:
            return rest[0], int(rest[1])
        except ValueError:
            return None

    def locate_language_server_pid(self) -> int | None:
        """Finds running language_server PID by scanning /proc or pgrep."""
        # 1. Scan proc_dir for exact binary command line
        proc_path = self.proc_dir
        if proc_path.is_dir():
            try:
                for pid_dir in proc_path.iterdir():
                    if not pid_dir.name.isdigit():
                        continue
                    if self._has_language_server_cmdline(pid_dir / "cmdline"):
                        try:
                            return int(pid_dir.name)
                        except ValueError:
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

    def get_cloud_code_endpoint(self, pid: int | None = None) -> str | None:
        """Extracts --cloud_code_endpoint URL from running language_server process."""
        target_pid = pid if pid is not None else self.locate_language_server_pid()
        if target_pid is None:
            return None

        # 1. Check proc_dir cmdline file
        cmdline_file = self.proc_dir / str(target_pid) / "cmdline"
        try:
            if cmdline_file.is_file():
                raw = cmdline_file.read_bytes()
                tokens = [t.decode("utf-8", errors="replace").strip() for t in raw.split(b"\x00") if t]
                if len(tokens) == 1 and " " in tokens[0]:
                    tokens = tokens[0].split()
                for i, token in enumerate(tokens):
                    if token == "--cloud_code_endpoint" and i + 1 < len(tokens):
                        val = tokens[i + 1].strip().strip("'\"").rstrip("/")
                        if val.startswith("http://") or val.startswith("https://"):
                            return val
                    elif token.startswith("--cloud_code_endpoint="):
                        val = token.split("=", 1)[1].strip().strip("'\"").rstrip("/")
                        if val.startswith("http://") or val.startswith("https://"):
                            return val
        except (OSError, ValueError):
            pass

        # 2. Fallback to ps only if proc_dir is the real /proc
        if self.proc_dir == Path("/proc"):
            try:
                res = subprocess.run(
                    ["ps", "-p", str(target_pid), "-o", "args="],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                args = res.stdout.strip().split()
                for i, token in enumerate(args):
                    if token == "--cloud_code_endpoint" and i + 1 < len(args):
                        val = args[i + 1].strip().strip("'\"").rstrip("/")
                        if val.startswith("http://") or val.startswith("https://"):
                            return val
                    elif token.startswith("--cloud_code_endpoint="):
                        val = token.split("=", 1)[1].strip().strip("'\"").rstrip("/")
                        if val.startswith("http://") or val.startswith("https://"):
                            return val
            except Exception:
                pass

        return None

    def _presence_to_brain_dirs(self) -> dict[Path, Path]:
        """Maps resolved presence directories to their corresponding brain directories."""
        mapping: dict[Path, Path] = {}
        for rel_root in self._PRESENCE_ROOTS:
            presence_dir = self.gemini_dir / rel_root / "presence"
            brain_dir = self.gemini_dir / rel_root / "brain"
            try:
                mapping[presence_dir.resolve()] = brain_dir
            except OSError:
                mapping[presence_dir] = brain_dir
        return mapping

    def get_active_presence_lock_paths(self) -> set[Path]:
        """Scans proc_dir/*/fd/* for open file descriptors pointing to presence locks."""
        active_locks: set[Path] = set()
        proc_path = self.proc_dir
        if not proc_path.is_dir():
            return active_locks

        presence_dirs = set(self._presence_to_brain_dirs().keys())

        try:
            for pid_dir in proc_path.iterdir():
                if not pid_dir.name.isdigit():
                    continue
                fd_dir = pid_dir / "fd"
                try:
                    if not fd_dir.is_dir():
                        continue
                    for fd_entry in fd_dir.iterdir():
                        try:
                            raw_target = os.readlink(fd_entry)
                            if raw_target.endswith(" (deleted)"):
                                continue
                            target_path = Path(raw_target)
                            if not target_path.is_absolute():
                                target_path = (fd_dir / target_path)

                            resolved_parent = target_path.parent.resolve()
                            if resolved_parent not in presence_dirs:
                                continue

                            if target_path.suffix == ".lock" and target_path.is_file():
                                active_locks.add(target_path.resolve())
                                active_locks.add(target_path)
                        except (OSError, ValueError):
                            continue
                except (OSError, ValueError):
                    continue
        except OSError:
            pass

        return active_locks

    def _get_active_presence_lock_paths(self) -> set[Path]:
        """Backward-compatible alias for get_active_presence_lock_paths()."""
        return self.get_active_presence_lock_paths()

    def _deduplicate_active_locks(
        self,
        active_locks: set[Path],
        known_presence_dirs: set[Path],
    ) -> list[Path]:
        """Deduplicates resolved/unresolved presence lock pairs while preferring canonical dirs."""
        if not active_locks:
            return []

        preferred: list[Path] = []
        fallback: list[Path] = []
        for lock_path in sorted(active_locks):
            try:
                parent_resolved = lock_path.parent.resolve()
            except OSError:
                parent_resolved = lock_path.parent
            if parent_resolved in known_presence_dirs:
                preferred.append(lock_path)
            else:
                fallback.append(lock_path)

        seen_resolved: set[Path] = set()
        deduped: list[Path] = []
        for lock_path in preferred + fallback:
            try:
                resolved_lock = lock_path.resolve()
            except OSError:
                resolved_lock = lock_path
            if resolved_lock in seen_resolved:
                continue
            seen_resolved.add(resolved_lock)
            deduped.append(lock_path)
        return deduped

    @staticmethod
    def _is_presence_lock_quiescent(
        lock_path: Path,
        presence_to_brain: dict[Path, Path],
    ) -> bool:
        """Returns True only when the lock's session transcript ends with a quiescent record."""
        session_id = lock_path.stem
        if not session_id or session_id in (".", "..") or "/" in session_id or "\\" in session_id:
            return False

        try:
            resolved_parent = lock_path.parent.resolve()
        except OSError:
            return False

        brain_dir = presence_to_brain.get(resolved_parent, lock_path.parent.parent / "brain")
        transcript_path = (
            brain_dir / session_id / ".system_generated" / "logs" / "transcript.jsonl"
        )
        record = read_latest_jsonl_record(transcript_path)
        return is_transcript_record_quiescent(record)

    def is_server_busy(self, pid: int | None = None) -> bool:
        """Checks if language_server has active non-sidecar children or non-quiescent presence locks."""
        active_locks = self.get_active_presence_lock_paths()

        for rel_root in self._PRESENCE_ROOTS:
            p_dir = self.gemini_dir / rel_root / "presence"
            if not p_dir.is_dir():
                continue
            try:
                for entry in p_dir.iterdir():
                    if entry.name.startswith("."):
                        continue
                    if not entry.name.endswith(".lock"):
                        continue
                    if entry.is_dir() or not entry.is_file():
                        continue
                    try:
                        resolved_entry = entry.resolve()
                    except OSError:
                        resolved_entry = entry
                    if resolved_entry in active_locks or entry in active_locks:
                        continue
                    try:
                        os.unlink(entry)
                    except OSError:
                        pass
            except OSError:
                pass

        if self.has_active_child_processes(pid):
            return True

        if not active_locks:
            return False

        presence_to_brain = self._presence_to_brain_dirs()
        for lock_path in self._deduplicate_active_locks(active_locks, set(presence_to_brain.keys())):
            if not self._is_presence_lock_quiescent(lock_path, presence_to_brain):
                return True

        return False

    @staticmethod
    def _read_cmdline_tokens(cmdline_file: Path) -> list[str] | None:
        """Reads NUL-delimited /proc/<pid>/cmdline tokens, failing safe to None on error."""
        try:
            raw = cmdline_file.read_bytes()
        except OSError:
            return None
        tokens = [
            t.decode("utf-8", errors="replace").strip()
            for t in raw.split(b"\x00")
            if t
        ]
        if len(tokens) == 1 and " " in tokens[0]:
            tokens = tokens[0].split()
        return [t for t in tokens if t]

    @classmethod
    def _is_sidecar_sdk_child(cls, cmdline_file: Path) -> bool:
        """Returns True only for Node.js runtimes invoking a sidecar_sdk entrypoint."""
        tokens = cls._read_cmdline_tokens(cmdline_file)
        if not tokens:
            return False

        if Path(tokens[0]).name not in cls._NODE_BIN_NAMES:
            return False

        entrypoint: str | None = None
        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok == "--":
                entrypoint = tokens[i + 1] if i + 1 < len(tokens) else None
                break
            if (
                tok in cls._NODE_NON_ENTRYPOINT_FLAGS
                or tok.startswith(("--eval=", "--print="))
                or (
                    tok.startswith("-")
                    and not tok.startswith("--")
                    and len(tok) > 2
                    and tok[1:].islower()
                    and any(ch in tok[1:] for ch in ("e", "p", "i", "c"))
                )
            ):
                return False
            if tok in cls._NODE_VALUE_FLAGS:
                i += 2
                continue
            if tok.startswith("-"):
                i += 1
                continue
            entrypoint = tok
            break

        if not entrypoint:
            return False

        entrypoint_path = Path(entrypoint)
        return (
            "sidecar_sdk" in entrypoint_path.parts
            or entrypoint_path.name in cls._SIDECAR_ENTRYPOINT_FILES
        )

    def has_active_child_processes(self, pid: int | None = None) -> bool:
        """Checks for non-zombie child processes of language_server excluding Node sidecar_sdk."""
        target_pid = pid if pid is not None else self.locate_language_server_pid()
        if target_pid is None or target_pid <= 0:
            return False

        proc_path = self.proc_dir
        if not proc_path.is_dir():
            return False

        try:
            for p_dir in proc_path.iterdir():
                if not p_dir.name.isdigit():
                    continue
                parsed = self._parse_stat_state_and_ppid(p_dir / "stat")
                if parsed is None:
                    continue
                state, ppid = parsed
                if ppid != target_pid or state == "Z":
                    continue
                if not self._is_sidecar_sdk_child(p_dir / "cmdline"):
                    return True
        except OSError:
            pass

        return False

    def is_invoked_by_language_server(
        self,
        pid: int | None = None,
        current_pid: int | None = None,
    ) -> bool:
        """Detects whether current_pid (or its process ancestry) descends from language_server.

        Walks up the ppid chain via ``self.proc_dir / str(cur_pid) / "stat"``,
        bounded to 64 hops with a visited-PID set for cycle protection. Returns True
        if the process or any ancestor matches ``pid`` or has a ``cmdline`` containing
        both ``language_server`` and ``--standalone``. Fails safe to False on missing,
        malformed, or unreadable ``/proc`` files.
        """
        cur_pid = current_pid if current_pid is not None else os.getpid()
        visited: set[int] = set()

        for hop in range(self._MAX_ANCESTRY_HOPS + 1):
            if cur_pid <= 0 or cur_pid in visited:
                return False
            visited.add(cur_pid)

            if pid is not None and cur_pid == pid:
                return True
            if self._has_language_server_cmdline(self.proc_dir / str(cur_pid) / "cmdline"):
                return True
            if cur_pid <= 1 or hop == self._MAX_ANCESTRY_HOPS:
                return False

            parsed = self._parse_stat_state_and_ppid(self.proc_dir / str(cur_pid) / "stat")
            if parsed is None:
                return False
            _, ppid = parsed
            if ppid <= 0 or ppid in visited:
                return False
            cur_pid = ppid

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
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except OSError:
            pass

        if self._poll_process_exit(pid, timeout=3.0):
            return True

        if not force:
            return False

        try:
            self._send_signal(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except OSError:
            pass

        return self._poll_process_exit(pid, timeout=1.0)



__all__ = ["ProcessSupervisor", "is_transcript_record_quiescent"]
