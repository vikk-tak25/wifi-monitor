"""The unit agent (unit/app.py) that the server offers to units as a remote update."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

VERSION_RE = re.compile(rb'^AGENT_VERSION = "([A-Za-z0-9._+-]{1,32})"$', re.MULTILINE)
MAX_AGENT_BYTES = 256 * 1024


@dataclass(frozen=True)
class AgentBundle:
    version: str
    sha256: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)

    def offer(self) -> dict:
        return {"version": self.version, "sha256": self.sha256, "size": self.size, "url": "/api/unit/v1/agent"}


def read_bundle(path: Path) -> AgentBundle | None:
    try:
        data = path.read_bytes()
    except OSError as exc:
        log.warning("agent updates disabled: cannot read %s (%s)", path, exc)
        return None
    if len(data) > MAX_AGENT_BYTES:
        log.warning("agent updates disabled: %s is larger than %d bytes", path, MAX_AGENT_BYTES)
        return None
    match = VERSION_RE.search(data)
    if not match:
        log.warning('agent updates disabled: no AGENT_VERSION = "..." line in %s', path)
        return None
    return AgentBundle(version=match.group(1).decode(), sha256=hashlib.sha256(data).hexdigest(), data=data)


class AgentSource:
    """Reads the agent file and re-reads it when it changes on disk."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._bundle: AgentBundle | None = None

    def get(self) -> AgentBundle | None:
        if self.path is None:
            return None
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        with self._lock:
            if mtime != self._mtime:
                self._mtime = mtime
                self._bundle = read_bundle(self.path) if mtime is not None else None
            return self._bundle
