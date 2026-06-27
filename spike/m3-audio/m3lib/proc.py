"""Thin async subprocess helpers.

Every external command the spike runs goes through here so it can be logged
uniformly. Commands are always passed as argument lists (never a shell string),
so there is no shell-injection surface despite the ``S`` lint relaxation for
spike code.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Result:
    """Outcome of a finished subprocess."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def command(self) -> str:
        return " ".join(self.argv)


async def run(
    *argv: str,
    timeout: float | None = 30.0,
    check: bool = False,
) -> Result:
    """Run ``argv`` to completion and capture its output.

    Args:
        argv: the command and its arguments.
        timeout: seconds before the process is killed (``None`` waits forever).
        check: if true, raise :class:`ProcessError` on a non-zero exit.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ProcessError(f"timed out after {timeout}s: {' '.join(argv)}") from None

    result = Result(
        argv=tuple(argv),
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=out.decode(errors="replace"),
        stderr=err.decode(errors="replace"),
    )
    if check and not result.ok:
        raise ProcessError(
            f"command failed ({result.returncode}): {result.command}\n{result.stderr.strip()}"
        )
    return result


async def run_json(*argv: str, timeout: float | None = 30.0) -> Any:
    """Run a command whose stdout is JSON and return the parsed value."""
    result = await run(*argv, timeout=timeout, check=True)
    return json.loads(result.stdout)


def require(*tools: str) -> list[str]:
    """Return the subset of ``tools`` that are missing from ``PATH``."""
    return [tool for tool in tools if shutil.which(tool) is None]


class ProcessError(RuntimeError):
    """A subprocess failed or timed out."""
