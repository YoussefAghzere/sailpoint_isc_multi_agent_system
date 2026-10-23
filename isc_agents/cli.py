"""Trusted local operator CLI. No remote server or production IAM boundary."""
import argparse
import json
import sys
from pathlib import Path

from .agents import OpenAI
from .engine import Engine
from .knowledge import Knowledge
from .models import Rejected
from .store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description="ISC integration engineering prototype (simulation only)")
    parser.add_argument("--db", default="work/platform.db")
    parser.add_argument("--knowledge", default="knowledge")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "show", "approve", "revoke", "execute"):
        sub = subs.add_parser(name)
        for field in ("client", "tenant", "environment"):
            sub.add_argument("--" + field, required=True,
                             **({"choices": ["sandbox", "production"]} if field == "environment" else {}))
        if name == "plan":
            sub.add_argument("--prompt", required=True)
            sub.add_argument("--requirements", required=True, type=Path)
            sub.add_argument("--provider", choices=["offline", "openai"], default="offline")
            sub.add_argument("--model")
        else:
            sub.add_argument("--run", required=True)
        if name == "approve":
            sub.add_argument("--digest", required=True)
            sub.add_argument("--actor", required=True)
    args = parser.parse_args(argv)
    store = None
    try:
        scope = {k: getattr(args, k) for k in ("client", "tenant", "environment")}
        model = OpenAI(args.model) if getattr(args, "provider", None) == "openai" else None
        store = Store(args.db)
        engine = Engine(store, Knowledge(args.knowledge), model)
        if args.command == "plan":
            result = engine.plan(scope, args.prompt, json.loads(args.requirements.read_text()))
        elif args.command == "approve":
            result = engine.approve(args.run, scope, args.digest, args.actor)
        else:
            result = getattr(engine, args.command)(args.run, scope)
        print(json.dumps(result, indent=2))
        return 0
    except (Rejected, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(2)
    finally:
        if store:
            store.close()
