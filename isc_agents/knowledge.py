"""Local, versioned evidence retrieval. Documents are data, never instructions."""
import json
import re
from pathlib import Path
from .models import Rejected, digest


class Knowledge:
    def __init__(self, directory):
        self.directory = Path(directory)

    def search(self, query, limit=5, *, scope=None):
        words = set(re.findall(r"[a-z0-9]+", query.lower()))
        hits = []
        for path in sorted(self.directory.glob("*.json")):
            if path.stat().st_size > 32000:
                raise Rejected(f"Knowledge document exceeds size budget: {path.name}")
            doc = json.loads(path.read_text())
            required = {"id", "title", "origin", "version", "trust", "content"}
            if not required <= doc.keys() or not all(isinstance(doc[k], str) for k in required):
                raise Rejected(f"Invalid knowledge document: {path.name}")
            if doc["trust"] not in {"official", "client", "fixture", "community"}:
                raise Rejected("Unknown evidence trust classification")
            if doc["trust"] == "client" and (scope is None or doc.get("scope") != scope):
                continue
            tokens = set(re.findall(r"[a-z0-9]+", (doc["title"] + " " + doc["content"]).lower()))
            score = len(words & tokens)
            if score:
                hits.append({**doc, "content_hash": digest(doc), "score": score})
        hits.sort(key=lambda d: (-d["score"], d["id"]))
        selected, budget = [], 24000
        for hit in hits[:limit]:
            if len(hit["content"]) <= budget:
                selected.append(hit)
                budget -= len(hit["content"])
        return selected
