"""Background process manager for long-running commands (dev servers, watchers).

`bash_background` starts a process and returns immediately with an id; its output
is buffered by a reader thread and can be polled with `process_output`, listed
with `process_list`, and stopped with `process_kill`.
"""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field


@dataclass
class BgProc:
    id: str
    command: str
    proc: subprocess.Popen
    lines: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class BackgroundManager:
    def __init__(self) -> None:
        self._procs: dict[str, BgProc] = {}
        self._counter = 0

    def start(self, cwd: str, command: str) -> str:
        self._counter += 1
        pid = f"bg{self._counter}"
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        bg = BgProc(pid, command, proc)
        self._procs[pid] = bg

        def reader():
            try:
                if proc.stdout:
                    for line in proc.stdout:
                        with bg.lock:
                            bg.lines.append(line)
                            if len(bg.lines) > 2000:
                                del bg.lines[:1000]
            except Exception:
                pass

        threading.Thread(target=reader, daemon=True).start()
        return pid

    def output(self, pid: str, tail: int = 100) -> str:
        bg = self._procs.get(pid)
        if not bg:
            return f"no such process: {pid}"
        with bg.lock:
            text = "".join(bg.lines[-tail:])
        status = "running" if bg.proc.poll() is None else f"exited ({bg.proc.returncode})"
        return f"[{pid}] {status}\n{text}" if text else f"[{pid}] {status} (no output yet)"

    def kill(self, pid: str) -> str:
        bg = self._procs.get(pid)
        if not bg:
            return f"no such process: {pid}"
        try:
            bg.proc.kill()
        except Exception:
            pass
        return f"killed {pid}"

    def list_(self) -> str:
        if not self._procs:
            return "(no background processes)"
        rows = []
        for pid, bg in self._procs.items():
            status = "running" if bg.proc.poll() is None else f"exited({bg.proc.returncode})"
            rows.append(f"{pid}  {status}  {bg.command}")
        return "\n".join(rows)

    def kill_all(self) -> None:
        for bg in self._procs.values():
            try:
                bg.proc.kill()
            except Exception:
                pass


# One manager per process is fine; ids are unique within it.
manager = BackgroundManager()
