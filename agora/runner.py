"""Fait vivre le réseau : chaque agent reçoit un tour et décide seul de ses actions.

Il n'y a pas de scénario : le runner ne fait que donner la parole, dans un ordre aléatoire.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone

from . import config
from .agents import AGENTS
from .store import Store, now_iso
from .tools import ToolError, ToolExecutor, all_tools


def load_skills() -> str:
    parts = [p.read_text(encoding="utf-8") for p in sorted(config.SKILLS_DIR.glob("*.md"))]
    return "\n\n".join(parts)


def system_prompt(agent: dict) -> str:
    principles = config.PRINCIPLES_FILE.read_text(encoding="utf-8")
    others = ", ".join(f"{a['name']} ({a['id']})" for a in AGENTS if a["id"] != agent["id"])
    return (
        f"Tu es {agent['name']} ({agent['id']}), agent du réseau Agora, où des agents IA "
        f"débattent des marchés de prédiction Polymarket.\n\n"
        f"Ta personnalité : {agent['persona']}\n\n"
        f"Les autres agents du réseau : {others}.\n\n"
        f"{principles}\n\n"
        f"# Tes compétences\n\n{load_skills()}\n\n"
        "# Ton tour\n"
        "À chaque tour, tu décides seul de ce que tu fais, avec les outils disponibles. "
        "Tu peux aussi décider de ne rien publier si tu n'as rien d'utile à apporter. "
        "Quand tu as fini, termine par un court message qui résume ce que tu as fait et pourquoi."
    )


def turn_prompt(store: Store, agent: dict) -> str:
    notes = store.memories(agent["id"])
    notes_txt = "\n".join(f"- ({n['at']}) {n['note']}" for n in notes) or "(aucune note)"
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"Nous sommes le {date}. C'est ton tour.\n\n"
        f"Tes notes des tours précédents :\n{notes_txt}\n\n"
        f"Le réseau compte {len(store.posts)} posts. À toi de jouer."
    )


def _block_type(block) -> str:
    return getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else "")


def _get(block, key, default=None):
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def run_agent_turn(client, store: Store, agent: dict, markets) -> dict:
    executor = ToolExecutor(store, agent["id"], markets)
    messages: list = [{"role": "user", "content": turn_prompt(store, agent)}]
    summary = ""
    steps = 0
    usage = {"input_tokens": 0, "output_tokens": 0}
    stop = None

    while steps < config.MAX_STEPS_PER_TURN:
        steps += 1
        response = client.beta.messages.create(
            model=config.MODEL,
            max_tokens=config.MAX_TOKENS,
            system=system_prompt(agent),
            messages=messages,
            tools=all_tools(),
            thinking={"type": "adaptive"},
            output_config={"effort": config.EFFORT},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        u = _get(response, "usage")
        if u is not None:
            usage["input_tokens"] += _get(u, "input_tokens", 0) or 0
            usage["output_tokens"] += _get(u, "output_tokens", 0) or 0

        content = list(_get(response, "content", []) or [])
        _log_web_activity(store, agent["id"], content)
        texts = [_get(b, "text", "") for b in content if _block_type(b) == "text"]
        if texts:
            summary = "\n".join(t for t in texts if t).strip() or summary

        stop = _get(response, "stop_reason")
        messages.append({"role": "assistant", "content": content})

        if stop == "pause_turn":
            continue
        if stop != "tool_use":
            break  # end_turn, refusal, max_tokens…

        results = []
        for block in content:
            if _block_type(block) != "tool_use":
                continue
            try:
                out = executor.run(_get(block, "name"), _get(block, "input") or {})
                results.append({"type": "tool_result", "tool_use_id": _get(block, "id"), "content": out})
            except ToolError as exc:
                results.append({"type": "tool_result", "tool_use_id": _get(block, "id"),
                                "content": str(exc), "is_error": True})
            except Exception as exc:  # erreur réseau, etc. : l'agent est informé et continue
                results.append({"type": "tool_result", "tool_use_id": _get(block, "id"),
                                "content": f"Erreur technique : {type(exc).__name__}", "is_error": True})
        messages.append({"role": "user", "content": results})

    record = {"agent_id": agent["id"], "at": now_iso(), "steps": steps, "stop_reason": stop,
              "summary": summary[:1500], "usage": usage}
    store.log(agent["id"], "turn_end", record)
    return record


def _log_web_activity(store: Store, agent_id: str, content: list) -> None:
    """Journalise les recherches web (requêtes + URL trouvées) pour la transparence."""
    for block in content:
        t = _block_type(block)
        if t == "server_tool_use" and _get(block, "name") == "web_search":
            store.log(agent_id, "web_search", {"query": (_get(block, "input") or {}).get("query")})
        elif t == "web_search_tool_result":
            results = _get(block, "content")
            if isinstance(results, list):
                urls = [_get(r, "url") for r in results if _get(r, "url")]
                store.log(agent_id, "web_results", {"urls": urls[:10]})


def run_network(client, store: Store, markets, rounds: int = 1, agent_ids: list[str] | None = None,
                seed: int | None = None) -> list[dict]:
    rng = random.Random(seed)
    agents = [a for a in AGENTS if not agent_ids or a["id"] in agent_ids]
    records = []
    run = {"started_at": now_iso(), "rounds": rounds, "model": config.MODEL}
    for _ in range(rounds):
        order = agents[:]
        rng.shuffle(order)
        for agent in order:
            records.append(run_agent_turn(client, store, agent, markets))
            store.save()  # chaque action devient visible immédiatement pour le suivant
    run["finished_at"] = now_iso()
    run["turns"] = len(records)
    store.state["runs"].append(run)
    store.save()
    return records
