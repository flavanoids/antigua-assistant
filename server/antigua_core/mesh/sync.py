"""Bidirectional delta synchronization engine for memories, personality/preferences, and lists.

Features:
- Section-level SHA-256 digests to minimize network bandwidth and CPU overhead.
- Deterministic Last-Write-Wins (LWW) per preference key with timestamp tracking.
- Strict input sanitization and privacy boundary enforcement.
- Support for incremental delta payloads (transmits only sections that differ).
"""

import copy
import hashlib
import json
import logging
import time
from threading import RLock

from .. import household_memory
from ..stores import ListStore, MemoryStore

log = logging.getLogger("antigua_core.mesh.sync")


class MeshSync:
    """Manages state synchronization between paired Antigua nodes."""

    def __init__(
        self,
        hm: household_memory.HouseholdMemory | None = None,
        memory_store: MemoryStore | None = None,
        list_store: ListStore | None = None,
        node_id: str = "",
    ):
        self._hm = hm
        self._memory_store = memory_store
        self._list_store = list_store
        self.node_id = node_id
        self._lock = RLock()
        self._pref_timestamps: dict[str, float] = {}

    @property
    def hm(self) -> household_memory.HouseholdMemory:
        return self._hm or household_memory.store()

    @property
    def memory_store(self) -> MemoryStore:
        if self._memory_store is None:
            self._memory_store = MemoryStore()
        return self._memory_store

    @property
    def list_store(self) -> ListStore:
        if self._list_store is None:
            self._list_store = ListStore()
        return self._list_store

    # ── Section Hashing & Digests ───────────────────────────────────────────

    def _normalize_text(self, text: str) -> str:
        return " ".join(str(text).strip().lower().split())

    def compute_hashes(self) -> dict[str, str]:
        """Calculates fast SHA-256 hashes for each data partition to detect deltas."""
        with self._lock:
            data = self.hm.data()

            # 1. Facts hash
            facts = sorted(
                self._normalize_text(f.get("text", ""))
                for f in data.get("facts", [])
                if isinstance(f, dict) and f.get("text")
            )
            facts_hash = hashlib.sha256(json.dumps(facts).encode()).hexdigest()

            # 2. Preferences hash
            prefs = data.get("preferences", {})
            prefs_repr = sorted(
                (k, prefs[k], self._pref_timestamps.get(k, 0.0))
                for k in prefs
                if k in household_memory.PREFS or k == "call"
            )
            prefs_hash = hashlib.sha256(json.dumps(prefs_repr, default=str).encode()).hexdigest()

            # 3. Rules hash
            rules = sorted(str(r) for r in data.get("rules", []) if r)
            rules_hash = hashlib.sha256(json.dumps(rules).encode()).hexdigest()

            # 4. Learned hash
            learned = sorted((k, data.get("learned", {})[k]) for k in sorted(data.get("learned", {})))
            learned_hash = hashlib.sha256(json.dumps(learned, default=str).encode()).hexdigest()

            # 5. Notepad memories hash
            notepad = self.memory_store.active()
            mem_repr = sorted(
                (e.get("id", ""), self._normalize_text(e.get("text", "")), str(e.get("timestamp", "")))
                for e in notepad
                if isinstance(e, dict)
            )
            mem_hash = hashlib.sha256(json.dumps(mem_repr).encode()).hexdigest()

            # 6. Lists hash
            lists = self.list_store.all_lists()
            lists_repr = sorted(
                (name, sorted(self._normalize_text(i) for i in items))
                for name, items in lists.items()
            )
            lists_hash = hashlib.sha256(json.dumps(lists_repr).encode()).hexdigest()

            return {
                "facts": facts_hash,
                "preferences": prefs_hash,
                "rules": rules_hash,
                "learned": learned_hash,
                "memories": mem_hash,
                "lists": lists_hash,
            }

    def export_digest(self) -> dict:
        """Returns lightweight state hashes without transmitting payload data."""
        return {
            "node_id": self.node_id,
            "timestamp": time.time(),
            "hashes": self.compute_hashes(),
        }

    # ── Export Payload (Full or Delta) ──────────────────────────────────────

    def export_payload(self, peer_hashes: dict[str, str] | None = None) -> dict:
        """Packages local state for export.

        If peer_hashes is provided, only sections that differ from the peer
        are included, avoiding unnecessary network traffic.
        """
        with self._lock:
            local_hashes = self.compute_hashes()
            now = time.time()

            # If peer provided hashes, determine what needs sending
            if peer_hashes is not None:
                diff_sections = {
                    sec for sec, h in local_hashes.items()
                    if peer_hashes.get(sec) != h
                }
                if not diff_sections:
                    return {
                        "version": "1.0",
                        "node_id": self.node_id,
                        "timestamp": now,
                        "changed": False,
                        "hashes": local_hashes,
                    }
            else:
                diff_sections = {"facts", "preferences", "rules", "learned", "memories", "lists"}

            hm_data = copy.deepcopy(self.hm.data())
            payload = {
                "version": "1.0",
                "node_id": self.node_id,
                "timestamp": now,
                "changed": True,
                "hashes": local_hashes,
            }

            # Selective packaging
            hm_export = {}
            if "facts" in diff_sections:
                hm_export["facts"] = hm_data.get("facts", [])
            if "preferences" in diff_sections:
                prefs = hm_data.get("preferences", {})
                for k in prefs:
                    if k not in self._pref_timestamps:
                        self._pref_timestamps[k] = now
                hm_export["preferences"] = prefs
                hm_export["pref_timestamps"] = dict(self._pref_timestamps)
            if "rules" in diff_sections:
                hm_export["rules"] = hm_data.get("rules", [])
            if "learned" in diff_sections:
                hm_export["learned"] = hm_data.get("learned", {})

            if hm_export:
                payload["household_memory"] = hm_export

            if "memories" in diff_sections:
                payload["memories"] = self.memory_store.export()

            if "lists" in diff_sections:
                payload["lists"] = self.list_store.all_lists()

            return payload

    # ── Apply / Merge ───────────────────────────────────────────────────────

    def apply_payload(self, payload: dict) -> dict:
        """Merges a peer's payload into local state with strict sanitization."""
        if not isinstance(payload, dict):
            raise ValueError("Invalid payload: expected dictionary")

        if payload.get("changed") is False:
            return {
                "changed": False,
                "facts_added": 0,
                "prefs_updated": [],
                "rules_added": 0,
                "memories_added": 0,
                "lists_updated": [],
            }

        with self._lock:
            summary = {
                "changed": True,
                "facts_added": 0,
                "prefs_updated": [],
                "rules_added": 0,
                "memories_added": 0,
                "lists_updated": [],
            }

            peer_hm = payload.get("household_memory", {})
            if isinstance(peer_hm, dict):
                self._merge_household_memory(peer_hm, summary)

            peer_memories = payload.get("memories", [])
            if isinstance(peer_memories, list):
                self._merge_notepad(peer_memories, summary)

            peer_lists = payload.get("lists", {})
            if isinstance(peer_lists, dict):
                self._merge_lists(peer_lists, summary)

            log.info("MeshSync: merged payload from %s: %s",
                     payload.get("node_id", "peer"), summary)
            return summary

    def _merge_household_memory(self, peer_hm: dict, summary: dict):
        local_data = self.hm.data()
        changed = False

        # 1. Facts: Union deduplication with sanitization
        if "facts" in peer_hm:
            existing_facts = local_data.setdefault("facts", [])
            known_texts = {self._normalize_text(f.get("text", "")) for f in existing_facts if isinstance(f, dict)}

            for f in peer_hm.get("facts", []):
                if not isinstance(f, dict):
                    continue
                raw_text = str(f.get("text", ""))
                clean_text = household_memory._clean(raw_text)
                norm = self._normalize_text(clean_text)
                if norm and norm not in known_texts:
                    fact_obj = {
                        "text": clean_text,
                        "origin": f.get("origin") or "mesh",
                        "updated_at": float(f.get("updated_at") or time.time()),
                    }
                    existing_facts.append(fact_obj)
                    known_texts.add(norm)
                    summary["facts_added"] += 1
                    changed = True

        # 2. Preferences (Persona & behavior tuning): Last-Write-Wins
        if "preferences" in peer_hm:
            local_prefs = local_data.setdefault("preferences", {})
            peer_prefs = peer_hm.get("preferences", {})
            peer_ts = peer_hm.get("pref_timestamps", {})

            if isinstance(peer_prefs, dict):
                for key, val in peer_prefs.items():
                    if key == "call":
                        continue
                    if key not in household_memory.PREFS:
                        continue
                    if val not in household_memory.PREFS[key]:
                        continue

                    pts = float(peer_ts.get(key, 0.0))
                    lts = float(self._pref_timestamps.get(key, 0.0))

                    if key not in local_prefs or pts >= lts:
                        if local_prefs.get(key) != val:
                            local_prefs[key] = copy.deepcopy(val)
                            self._pref_timestamps[key] = pts or time.time()
                            summary["prefs_updated"].append(key)
                            changed = True

            # Special handling for "call" dict: {member: spoken_name}
            if "call" in peer_prefs and isinstance(peer_prefs["call"], dict):
                pts = float(peer_ts.get("call", 0.0))
                lts = float(self._pref_timestamps.get("call", 0.0))
                if "call" not in local_prefs or not isinstance(local_prefs["call"], dict):
                    local_prefs["call"] = {}
                if pts >= lts:
                    for member, name in peer_prefs["call"].items():
                        if isinstance(name, str) and household_memory._CALL_OK.match(name.strip()):
                            s_name = name.strip()
                            if local_prefs["call"].get(member) != s_name:
                                local_prefs["call"][member] = s_name
                                summary["prefs_updated"].append(f"call.{member}")
                                changed = True
                    self._pref_timestamps["call"] = pts or time.time()

        # 3. Rules: Set union with length & injection guard
        if "rules" in peer_hm:
            local_rules = local_data.setdefault("rules", [])
            known_rules = set(local_rules)
            for r in peer_hm.get("rules", []):
                r_str = household_memory._clean(str(r))
                if r_str and r_str not in known_rules and not household_memory._INJECTION_RE.search(r_str):
                    local_rules.append(r_str)
                    known_rules.add(r_str)
                    summary["rules_added"] += 1
                    changed = True

        # 4. Learned: Dict merge
        if "learned" in peer_hm:
            local_learned = local_data.setdefault("learned", {})
            peer_learned = peer_hm.get("learned", {})
            if isinstance(peer_learned, dict):
                for k, v in peer_learned.items():
                    if k not in local_learned:
                        local_learned[k] = copy.deepcopy(v)
                        changed = True

        if changed and not self.hm.read_only:
            self.hm._save()

    def _merge_notepad(self, peer_entries: list[dict], summary: dict):
        local_entries = self.memory_store.export()
        known = set()
        for e in local_entries:
            key = e.get("id") or (self._normalize_text(e.get("text", "")), str(e.get("timestamp", "")))
            known.add(key)

        to_add = []
        for e in peer_entries:
            if not isinstance(e, dict):
                continue
            text = household_memory._clean(str(e.get("text", "")))
            if not text:
                continue
            key = e.get("id") or (self._normalize_text(text), str(e.get("timestamp", "")))
            if key not in known:
                entry = copy.deepcopy(e)
                entry["text"] = text
                to_add.append(entry)
                known.add(key)
                summary["memories_added"] += 1

        if to_add:
            merged = local_entries + to_add
            self.memory_store.replace(merged)

    def _merge_lists(self, peer_lists: dict, summary: dict):
        changed = False
        all_local = self.list_store.all_lists()

        for name, peer_items in peer_lists.items():
            if not isinstance(peer_items, list):
                continue
            norm_name = name.strip().lower()
            local_items = all_local.get(norm_name, [])
            known_items = {self._normalize_text(x) for x in local_items}

            items_to_add = []
            for item in peer_items:
                clean_item = household_memory._clean(str(item))
                norm_item = self._normalize_text(clean_item)
                if norm_item and norm_item not in known_items:
                    items_to_add.append(clean_item)
                    known_items.add(norm_item)

            if items_to_add:
                self.list_store.add(norm_name, items_to_add)
                summary["lists_updated"].append(norm_name)
                changed = True

        if changed:
            self.list_store.reload()
