"""Run untrusted repository code (installs, tests, imports) in a bubblewrap sandbox.

Inside the sandbox the host filesystem is read-only, home directories and
removable media are hidden, only the per-PR work directory (and the shared
package cache) is writable, and the network is off unless explicitly allowed
(dependency installation). Credentials in the environment are never passed in.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

_SAFE_ENV = ("PATH", "LANG", "LC_ALL", "TERM")


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool = False


class Sandbox:
    def __init__(self, writable: list[Path], *, enabled: bool = True, env: dict[str, str] | None = None,
                 readonly: list[Path] | None = None):
        self.writable = [Path(p).resolve() for p in writable]
        # Tool directories (e.g. where uv lives) that may sit under a hidden mount such as /dev/shm.
        self.readonly = [Path(p).resolve() for p in (readonly or []) if Path(p).exists()]
        self.enabled = enabled
        if enabled and not shutil.which("bwrap"):
            raise RuntimeError("bubblewrap (bwrap) not found; install it or pass --no-sandbox")
        self.env = {k: os.environ[k] for k in _SAFE_ENV if k in os.environ}
        self.env.update(env or {})

    def _prefix(self, network: bool) -> list[str]:
        if not self.enabled:
            return []
        args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                "--tmpfs", "/tmp", "--tmpfs", "/home", "--tmpfs", "/root", "--tmpfs", "/media",
                "--tmpfs", "/mnt", "--tmpfs", "/run", "--tmpfs", "/var/tmp"]
        if network and Path("/run/systemd/resolve").is_dir():
            args += ["--ro-bind", "/run/systemd/resolve", "/run/systemd/resolve"]
        for p in self.readonly:
            args += ["--ro-bind", str(p), str(p)]
        for p in self.writable:
            args += ["--bind", str(p), str(p)]
        args += ["--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc"]
        if not network:
            args.append("--unshare-net")
        return args

    def run(self, cmd: list[str], *, cwd: Path, timeout: int, network: bool = False,
            env: dict[str, str] | None = None) -> Result:
        full_env = dict(self.env)
        full_env.update(env or {})
        start = time.monotonic()
        try:
            p = subprocess.run(self._prefix(network) + ["--chdir", str(cwd)] + cmd
                               if self.enabled else cmd,
                               cwd=cwd, env=full_env, capture_output=True, text=True,
                               timeout=timeout, errors="replace")
            return Result(p.returncode, p.stdout, p.stderr, time.monotonic() - start)
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            return Result(-9, out, err, time.monotonic() - start, timed_out=True)
