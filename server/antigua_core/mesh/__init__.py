"""Antigua Mesh: Peer-to-peer node federation, state sync, and skill sharing."""

from .auth import MeshAuth
from .client import MeshClient
from .discovery import MeshDiscovery
from .identity import MeshIdentity, NodeInfo
from .peers import Peer, PeerRegistry
from .router import MeshSkillsRouter
from .sync import MeshSync
from .worker import MeshWorker

__all__ = [
    "MeshIdentity",
    "NodeInfo",
    "MeshAuth",
    "Peer",
    "PeerRegistry",
    "MeshSync",
    "MeshSkillsRouter",
    "MeshClient",
    "MeshDiscovery",
    "MeshWorker",
]
