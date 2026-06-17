"""Checkpoint / undo for file mutations.

Before any mutating file tool runs, the loop records the affected file's prior
state here. `undo_last_turn()` restores everything changed during the most recent
user turn — the "git checkpoint/revert" capability, without requiring git.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class FileSnapshot:
    path: Path
    existed: bool
    content: str  # empty when the file didn't exist


class Checkpointer:
    def __init__(self) -> None:
        # one list of snapshots per user turn (a stack of turns)
        self._turns: list[list[FileSnapshot]] = []

    def begin_turn(self) -> None:
        self._turns.append([])

    def record(self, full_path: Path) -> None:
        """Snapshot a file's current state before it is mutated."""
        if not self._turns:
            self.begin_turn()
        # avoid duplicate snapshots of the same path within a turn
        for snap in self._turns[-1]:
            if snap.path == full_path:
                return
        if full_path.exists():
            try:
                content = full_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                content = ""
            self._turns[-1].append(FileSnapshot(full_path, True, content))
        else:
            self._turns[-1].append(FileSnapshot(full_path, False, ""))

    @property
    def can_undo(self) -> bool:
        return any(turn for turn in self._turns)

    def undo_last_turn(self) -> list[str]:
        """Revert the most recent non-empty turn. Returns reverted paths."""
        while self._turns:
            turn = self._turns.pop()
            if not turn:
                continue
            reverted: list[str] = []
            for snap in reversed(turn):
                try:
                    if snap.existed:
                        snap.path.parent.mkdir(parents=True, exist_ok=True)
                        snap.path.write_text(snap.content, encoding="utf-8")
                    elif snap.path.exists():
                        snap.path.unlink()
                    reverted.append(str(snap.path))
                except Exception:
                    continue
            return reverted
        return []
