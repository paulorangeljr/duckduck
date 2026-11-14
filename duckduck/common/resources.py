"""How much memory this process is really allowed — the container's limit, not the host's.

DuckDB (and anything reading ``/proc/meminfo``) sizes itself to the *machine's* RAM, which on a
serverless container (AWS Lambda, Azure Functions, a cgroup-limited pod) is far more than the
function may use: it then over-commits and the platform kills it (OOM) long before DuckDB would
spill. ``container_memory_bytes`` reads the cgroup limit that actually applies so the pipeline can
cap DuckDB's ``memory_limit`` (and switch reads to batches) to fit — only when there is a real
limit; on a normal machine it returns ``None`` and nothing changes.
"""

from __future__ import annotations

import os
from typing import Optional

#: a cgroup "no limit" sentinel is a huge number near 2**63; treat anything this big as unlimited
_UNLIMITED = 1 << 62


def _read_int(path: str) -> Optional[int]:
    try:
        with open(path) as f:
            text = f.read().strip()
    except (OSError, ValueError):
        return None
    if text in ("max", ""):
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if 0 < value < _UNLIMITED else None


def _host_memory_bytes() -> Optional[int]:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _own_cgroup_paths() -> "list[str]":
    """The process's cgroup paths (``/proc/self/cgroup``), leaf-most first, so the limit that actually applies
    to this process is found — not the root hierarchy's (which is usually unlimited)."""
    try:
        with open("/proc/self/cgroup") as f:
            lines = f.read().splitlines()
    except OSError:
        return [""]
    rel = ""
    for line in lines:  # v2 is a single "0::<path>" line; v1 has a "...:memory:<path>" line
        parts = line.split(":", 2)
        if len(parts) == 3 and (parts[1] == "" or "memory" in parts[1].split(",")):
            rel = parts[2]
            break
    rel = rel.strip("/")
    out = []
    while True:
        out.append(rel)
        if not rel:
            break
        rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
    return out


def container_memory_bytes() -> Optional[int]:
    """The memory this container is limited to — the tightest cgroup limit on this process (v2 ``memory.max``
    then v1 ``memory.limit_in_bytes``), or ``None`` when there is no real limit: unset, unlimited, or the host's
    whole RAM (a normal machine, where DuckDB's own default is right). The env override
    ``DUCKDUCK_CONTAINER_MEMORY`` (bytes) wins, for platforms whose cgroup files don't reflect the real cap."""
    override = _read_int_env("DUCKDUCK_CONTAINER_MEMORY")
    if override is not None:
        return override
    limit = None
    for rel in _own_cgroup_paths():
        for base in (f"/sys/fs/cgroup/{rel}/memory.max" if rel else "/sys/fs/cgroup/memory.max",  # v2
                     f"/sys/fs/cgroup/memory/{rel}/memory.limit_in_bytes" if rel  # v1
                     else "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
            found = _read_int(base)
            if found is not None:
                limit = found if limit is None else min(limit, found)
    if limit is None:
        return None
    host = _host_memory_bytes()
    if host is not None and limit >= host * 0.9:  # the whole machine: not a container limit
        return None
    return limit


def _read_int_env(name: str) -> Optional[int]:
    text = os.environ.get(name)
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value > 0 else None


def human_bytes(n: int) -> str:
    """``n`` bytes as a DuckDB ``memory_limit`` string (``"512MB"``, ``"2GB"``)."""
    mb = max(1, int(n) // (1024 * 1024))
    if mb >= 1024 and mb % 1024 == 0:
        return f"{mb // 1024}GB"
    return f"{mb}MB"
