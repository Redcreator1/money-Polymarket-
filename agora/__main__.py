"""Ligne de commande : `python -m agora run` puis `python -m agora build`."""
from __future__ import annotations

import argparse
import sys

from . import config
from .store import Store


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agora", description="Réseau d'agents autonomes Agora")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Donne un tour de parole à chaque agent")
    run.add_argument("--rounds", type=int, default=1)
    run.add_argument("--agents", nargs="*", help="Limiter à certains agents (ids)")
    run.add_argument("--build", action="store_true", help="Régénérer le site ensuite")

    sub.add_parser("build", help="Génère le site statique dans docs/")
    args = parser.parse_args(argv)

    store = Store()
    if args.cmd == "run":
        import anthropic

        from .polymarket import PolymarketClient
        from .runner import run_network

        client = anthropic.Anthropic()
        records = run_network(client, store, PolymarketClient(), rounds=args.rounds,
                              agent_ids=args.agents)
        for r in records:
            print(f"[{r['agent_id']}] {r['steps']} étapes, fin={r['stop_reason']} — {r['summary'][:200]}")
        if args.build:
            from .site import build_site
            build_site(store)
    elif args.cmd == "build":
        from .site import build_site
        paths = build_site(store)
        print(f"{len(paths)} pages générées dans {config.DOCS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
