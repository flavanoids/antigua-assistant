"""Node identity and hardware capability detection."""

import logging
import os
import socket
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .. import settings

log = logging.getLogger("antigua_core.mesh")

BASE_CAPABILITIES = [
    "chat",
    "llm",
    "memory",
    "lists",
    "timers",
    "persona",
]

MAC_CAPABILITIES = [
    "macos_shortcuts",
    "macos_calendar",
    "macos_reminders",
    "macos_system",
]


@dataclass
class NodeInfo:
    node_id: str
    name: str
    platform: str
    version: str = "1.0.0"
    capabilities: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NodeInfo":
        return cls(
            node_id=str(data.get("node_id", "")),
            name=str(data.get("name", "Antigua Node")),
            platform=str(data.get("platform", "generic")),
            version=str(data.get("version", "1.0.0")),
            capabilities=list(data.get("capabilities", [])),
            created_at=float(data.get("created_at", time.time())),
        )


class MeshIdentity:
    """Manages the identity and capabilities of this Antigua node."""

    def __init__(self, path: Path | None = None, name: str | None = None):
        self.path = Path(path or (settings.DATA_DIR / "mesh_node.yaml"))
        self._custom_name = name
        self._info: NodeInfo | None = None
        self._secret: str | None = None
        self.load_or_create()

    def _detect_platform(self) -> str:
        if sys.platform == "darwin":
            return "mac"
        return "linux"

    def _detect_capabilities(self) -> list[str]:
        caps = list(BASE_CAPABILITIES)
        plat = self._detect_platform()
        if plat == "mac":
            caps.extend(MAC_CAPABILITIES)
        else:
            # Linux / Home Server capabilities
            if getattr(settings, "GOVEE_API_KEY", None) or getattr(settings, "GOVEE_DEVICES", None):
                caps.append("govee_lights")
            if getattr(settings, "CLIMATE_URL", None):
                caps.append("climate_ac")
            if getattr(settings, "SECURITY_ENABLED", False) or getattr(settings, "HA_URL", None):
                caps.append("security_cams")
            if getattr(settings, "APPLE_TV_ID", None) or getattr(settings, "ROKU_IP", None):
                caps.append("living_room_tv")
        # Deduplicate while preserving order
        seen = set()
        out = []
        for c in caps:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def _default_name(self) -> str:
        plat = self._detect_platform()
        host = socket.gethostname().split(".")[0]
        if plat == "mac":
            return f"{host} (Mac)"
        return f"{host} (Home Server)"

    def load_or_create(self) -> NodeInfo:
        if self.path.exists():
            try:
                data = yaml.safe_load(self.path.read_text()) or {}
                if isinstance(data, dict) and "node_id" in data:
                    self._info = NodeInfo.from_dict(data)
                    self._secret = str(data.get("node_secret", ""))
                    # Refresh dynamic capabilities if platform changed
                    cur_caps = self._detect_capabilities()
                    for c in cur_caps:
                        if c not in self._info.capabilities:
                            self._info.capabilities.append(c)
                    if self._custom_name and self._info.name != self._custom_name:
                        self._info.name = self._custom_name
                        self.save()
                    return self._info
            except Exception as e:
                log.warning("MeshIdentity: failed to load %s (%s), recreating", self.path, e)

        # Generate new identity
        plat = self._detect_platform()
        node_id = f"antigua-{plat}-{uuid.uuid4().hex[:8]}"
        name = self._custom_name or self._default_name()
        caps = self._detect_capabilities()
        self._secret = uuid.uuid4().hex
        self._info = NodeInfo(
            node_id=node_id,
            name=name,
            platform=plat,
            capabilities=caps,
            created_at=time.time(),
        )
        self.save()
        log.info("MeshIdentity: created new identity %s (%s)", node_id, name)
        return self._info

    def save(self):
        if not self._info:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self._info.to_dict()
        data["node_secret"] = self._secret
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(data, sort_keys=False))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    @property
    def info(self) -> NodeInfo:
        if self._info is None:
            return self.load_or_create()
        return self._info

    @property
    def node_id(self) -> str:
        return self.info.node_id

    @property
    def name(self) -> str:
        return self.info.name

    @property
    def node_secret(self) -> str:
        return self._secret or ""

    @property
    def capabilities(self) -> list[str]:
        return list(self.info.capabilities)
