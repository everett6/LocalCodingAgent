"""Read-only retrieval audit inspector, served by the authenticated API bridge."""
from __future__ import annotations

from local_coder.harness_api import _integer, _object, _query


class MemoryAuditAPI:
    def _memory_audit_get(self, path, query):
        if path == "/memory/retrievals":
            _object(query, {"limit"})
            return True, {"retrievals": self.context_memory.retrievals(
                _integer(_query(query, "limit", "20"), 0, 100))}
        if path.startswith("/memory/retrievals/"):
            _object(query, set())
            return True, {"retrieval": self.context_memory.retrieval(path.removeprefix("/memory/retrievals/"))}
        return False, None
