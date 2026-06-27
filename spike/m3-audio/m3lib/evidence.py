"""Structured evidence recording.

M3 is about producing *evidence*, not features (ADR-014). Every run writes:

* ``events.jsonl`` — one JSON object per line, the full timeline of the run.
* ``summary.json`` / ``summary.md`` — the headline result a human reads first.
* ``environment.json`` — the Pi's BlueZ / PipeWire / kernel versions.

Scripts call :meth:`Recorder.event` liberally. The point is that when something
goes wrong on real hardware we have a timeline to read, rather than having to
reproduce a transient fault.
"""

from __future__ import annotations

import json
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from . import proc


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Recorder:
    """Collects timeline events and run artefacts under one run directory."""

    def __init__(self, run_name: str, base_dir: Path | None = None) -> None:
        root = base_dir or Path(__file__).resolve().parent.parent / "evidence"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_name = run_name
        self.dir = root / f"{stamp}-{run_name}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._events_path = self.dir / "events.jsonl"
        self._events = self._events_path.open("a", encoding="utf-8")
        self._t0 = time.monotonic()
        self.event("run_start", run=run_name)

    def event(self, kind: str, **fields: Any) -> None:
        """Append one timeline event and echo a concise line to the console."""
        record = {
            "ts": _now_iso(),
            "t": round(time.monotonic() - self._t0, 3),
            "kind": kind,
            **fields,
        }
        self._events.write(json.dumps(record) + "\n")
        self._events.flush()
        detail = " ".join(f"{k}={v}" for k, v in fields.items())
        print(f"[{record['t']:8.3f}] {kind:<22} {detail}")

    async def capture_environment(self) -> None:
        """Record the Pi's software baseline once per run."""
        env: dict[str, Any] = {
            "python": platform.python_version(),
            "uname": platform.uname()._asdict(),
        }
        for label, argv in {
            "bluetoothctl": ("bluetoothctl", "--version"),
            "pipewire": ("pipewire", "--version"),
            "wireplumber": ("wireplumber", "--version"),
            "bluez_adapter": ("bluetoothctl", "show"),
        }.items():
            try:
                result = await proc.run(*argv, timeout=10.0)
                env[label] = result.stdout.strip()
            except proc.ProcessError as exc:
                env[label] = f"<error: {exc}>"
        (self.dir / "environment.json").write_text(json.dumps(env, indent=2))
        self.event("environment_captured")

    def summary(self, verdict: str, **fields: Any) -> None:
        """Write the run's headline result as JSON and Markdown."""
        data = {"run": self.run_name, "verdict": verdict, "finished": _now_iso(), **fields}
        (self.dir / "summary.json").write_text(json.dumps(data, indent=2))

        lines = [f"# M3 run: {self.run_name}", "", f"**Verdict:** {verdict}", ""]
        for key, value in fields.items():
            lines.append(f"- **{key}:** {value}")
        lines += ["", f"_Timeline: `{self._events_path.name}`_", ""]
        (self.dir / "summary.md").write_text("\n".join(lines))
        self.event("run_end", verdict=verdict)

    def close(self) -> None:
        if not self._events.closed:
            self._events.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc is not None:
            self.event("run_exception", error=repr(exc))
        self.close()
