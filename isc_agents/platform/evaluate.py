"""Repeatable retrieval baseline. Does not claim to evaluate IAM business outcomes."""
import argparse
import json
import time
from pathlib import Path

from ..models import Rejected
from .catalog import Catalog
from .db import DB


def evaluate(db, cases, spec_id, top_k=10):
    catalog, results = Catalog(db), []
    for case in cases:
        started = time.perf_counter()
        operations = catalog.operations(spec_id, case["prompt"], limit=top_k)
        names = [o["operation_id"] for o in operations]
        expected = case["expected_operation"]
        rank = names.index(expected) + 1 if expected in names else None
        results.append({"prompt": case["prompt"], "expected_operation": expected, "rank": rank,
                        "passed": rank is not None, "milliseconds": round((time.perf_counter() - started) * 1000, 2)})
    return {"kind": "API retrieval baseline, not model or connector acceptance", "spec_id": spec_id,
            "top_k": top_k, "cases": results,
            "recall": sum(r["passed"] for r in results) / len(results) if results else 0,
            "passed": all(r["passed"] for r in results)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="work/platform-v2/platform.db")
    parser.add_argument("--cases", default="evaluations/api-retrieval.json")
    parser.add_argument("--spec")
    parser.add_argument("--report", default="work/evaluation.json")
    args = parser.parse_args()
    db = DB(args.db)
    try:
        specs = Catalog(db).list()
        if not specs:
            raise Rejected("Import an API contract before running retrieval evaluations")
        report = evaluate(db, json.loads(Path(args.cases).read_text()), args.spec or specs[0]["id"])
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2) + "\n")
        print(f"API retrieval recall@{report['top_k']}: {report['recall']:.0%} ({len(report['cases'])} cases). Report: {target}")
        return 0 if report["passed"] else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
