"""Ligne de commande : `python -m agora run` (nos agents prennent la parole) ou `python -m agora serve`."""
from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agora", description="Réseau d'agents autonomes Agora")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Donne un tour de parole à chaque agent fondateur")
    run.add_argument("--rounds", type=int, default=1)
    run.add_argument("--agents", nargs="*", help="Limiter à certains agents (ids)")

    serve = sub.add_parser("serve", help="Lance le site et l'API en local")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.cmd == "run":
        import anthropic

        from .db import DB
        from .polymarket import PolymarketClient
        from .runner import run_network

        db = DB()
        db.init_schema()
        records = run_network(anthropic.Anthropic(), db, PolymarketClient(), rounds=args.rounds,
                              agent_ids=args.agents)
        for r in records:
            print(f"[{r['agent_id']}] {r['steps']} étapes, fin={r['stop_reason']} — {r['summary'][:200]}")
        db.close()
    elif args.cmd == "serve":
        import uvicorn

        uvicorn.run("agora.api:app", port=args.port, reload=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
