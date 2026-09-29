"""Subprocess boundary that removes descendants when a worker times out."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=30,
        )
    else:
        try:
            getattr(os, "killpg")(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                getattr(os, "killpg")(process.pid, getattr(signal, "SIGKILL"))
            except ProcessLookupError:
                pass
        process.wait(timeout=10)


def run_process_tree(
    command: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    capture_output: bool = False,
    text: bool = False,
    encoding: str | None = None,
    errors: str | None = None,
    timeout: float | None = None,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Run one command and kill its complete process tree after timeout."""
    if capture_output:
        if "stdout" in kwargs or "stderr" in kwargs:
            raise ValueError("stdout/stderr cannot be used with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if os.name == "nt":
        kwargs["creationflags"] = (
            int(kwargs.get("creationflags", 0)) | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=env,
        text=text,
        encoding=encoding,
        errors=errors,
        **kwargs,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
        elapsed_timeout = timeout if timeout is not None else 0.0
        raise subprocess.TimeoutExpired(
            list(command), elapsed_timeout, output=stdout, stderr=stderr
        ) from None
    completed = subprocess.CompletedProcess(
        list(command), process.returncode, stdout, stderr
    )
    if check:
        completed.check_returncode()
    return completed
