"""Run from the repository root: python -m examples.demo."""
import json
from pathlib import Path

from isc_agents.engine import Engine
from isc_agents.knowledge import Knowledge
from isc_agents.store import Store


def main():
    store = Store("work/demo.db")
    try:
        engine = Engine(store, Knowledge("knowledge"))
        scope = {"client": "example", "tenant": "example_sandbox", "environment": "sandbox"}
        run = engine.plan(scope, "Simulate an authoritative delimited-file HR source configuration.",
                          json.loads(Path("examples/hr_source.json").read_text()))
        print("PLAN", run["id"], "digest", run["digest"])
        print(json.dumps(run["plan"]["change"], indent=2))
        # Demo-only automatic approval. The normal CLI requires explicit review/approval.
        engine.approve(run["id"], scope, run["digest"], "demo-operator")
        result = engine.execute(run["id"], scope)
        Path("work/demo-report.json").write_text(json.dumps(result, indent=2) + "\n")
        print("RESULT", result["status"], "— simulated tenant only")
        print("Report: work/demo-report.json")
    finally:
        store.close()


if __name__ == "__main__":
    main()
