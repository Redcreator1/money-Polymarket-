"""Les outils (compétences exécutables) que chaque agent peut utiliser librement."""
from __future__ import annotations

import json

from . import safety
from .config import MAX_POSTS_PER_TURN, MAX_VOTES_PER_TURN, MAX_WEB_SEARCHES_PER_TURN
from .store import Store


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


CUSTOM_TOOLS: list[dict] = [
    {
        "name": "read_feed",
        "description": "Lit le fil public du réseau : posts récents, réponses, votes et scores.",
        "input_schema": _schema({"limit": {"type": "integer", "description": "Nombre de fils (max 30)."}}, []),
    },
    {
        "name": "list_markets",
        "description": "Liste les marchés Polymarket actifs les plus échangés (lecture seule).",
        "input_schema": _schema({
            "query": {"type": "string", "description": "Mot-clé optionnel pour filtrer les questions."},
            "limit": {"type": "integer", "description": "Nombre de marchés (max 25)."},
        }, []),
    },
    {
        "name": "get_market",
        "description": "Détail d'un marché Polymarket : question, issues, prix, volume, date de fin.",
        "input_schema": _schema({"slug": {"type": "string"}}, ["slug"]),
    },
    {
        "name": "publish_post",
        "description": "Publie une analyse visible par tous. Sources fortement recommandées.",
        "input_schema": _schema({
            "market_slug": {"type": "string"},
            "title": {"type": "string", "description": "Thèse en une phrase."},
            "body": {"type": "string", "description": "Raisonnement et faits clés, concis."},
            "probability": {"type": "number", "description": "Ta probabilité (0-1) pour l'issue principale."},
            "confidence": {"type": "string", "enum": ["faible", "moyenne", "elevee"]},
            "sources": {"type": "array", "items": {"type": "string"}, "description": "URL des sources."},
        }, ["title", "body"]),
    },
    {
        "name": "reply",
        "description": "Répond à un post (compléter, corriger, contester). Visible par tous.",
        "input_schema": _schema({
            "post_id": {"type": "integer"},
            "body": {"type": "string"},
            "probability": {"type": "number", "description": "Ta probabilité révisée (0-1), optionnelle."},
            "sources": {"type": "array", "items": {"type": "string"}},
        }, ["post_id", "body"]),
    },
    {
        "name": "vote",
        "description": "Vote +1 ou -1 sur le post d'un autre agent, avec une raison courte.",
        "input_schema": _schema({
            "post_id": {"type": "integer"},
            "value": {"type": "integer", "enum": [1, -1]},
            "reason": {"type": "string"},
        }, ["post_id", "value", "reason"]),
    },
    {
        "name": "remember",
        "description": "Enregistre une note personnelle (publique) que tu retrouveras au prochain tour.",
        "input_schema": _schema({"note": {"type": "string"}}, ["note"]),
    },
]

WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": MAX_WEB_SEARCHES_PER_TURN,
}


def all_tools() -> list[dict]:
    return [*CUSTOM_TOOLS, WEB_SEARCH_TOOL]


class ToolError(Exception):
    pass


class ToolExecutor:
    """Exécute les outils d'un agent pendant un tour, avec des garde-fous."""

    def __init__(self, store: Store, agent_id: str, markets):
        self.store = store
        self.agent_id = agent_id
        self.markets = markets
        self.posts_this_turn = 0
        self.votes_this_turn = 0

    def run(self, name: str, args: dict) -> str:
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            raise ToolError(f"Outil inconnu : {name}")
        result = handler(**(args or {}))
        self.store.log(self.agent_id, name, {"input": args, "result": _short(result)})
        return result

    # --- lecture -------------------------------------------------------------
    def _t_read_feed(self, limit: int = 20) -> str:
        return safety.wrap_external(self.store.feed(limit=max(1, min(int(limit), 30))))

    def _t_list_markets(self, query: str | None = None, limit: int = 10) -> str:
        return safety.wrap_external(self.markets.list_markets(query=query, limit=limit))

    def _t_get_market(self, slug: str) -> str:
        market = self.markets.get_market(slug)
        if market is None:
            raise ToolError(f"Marché introuvable : {slug}")
        return safety.wrap_external(market)

    # --- écriture ------------------------------------------------------------
    def _check_text(self, *texts: str) -> bool:
        return any(safety.is_suspicious(t) for t in texts)

    def _t_publish_post(self, title: str, body: str, market_slug: str | None = None,
                        probability=None, confidence: str | None = None, sources=None) -> str:
        if self.posts_this_turn >= MAX_POSTS_PER_TURN:
            raise ToolError("Limite de publications atteinte pour ce tour.")
        title, body = safety.clean_text(title, 200), safety.clean_text(body)
        if not title or not body:
            raise ToolError("Titre et contenu requis.")
        post = self.store.add_post(
            agent_id=self.agent_id, market_slug=(market_slug or None), title=title, body=body,
            probability=safety.clean_probability(probability),
            confidence=confidence if confidence in ("faible", "moyenne", "elevee") else None,
            sources=safety.clean_sources(sources),
        )
        post["flagged"] = self._check_text(title, body)
        post["market_snapshot"] = self._snapshot(post["market_slug"])
        self.posts_this_turn += 1
        note = " (marqué « non sourcé » : ajoute des sources la prochaine fois)" if post["unsourced"] else ""
        return f"Post #{post['id']} publié{note}."

    def _snapshot(self, slug: str | None) -> dict | None:
        """Mémorise la question et le prix du marché au moment du post (pour comparer)."""
        if not slug:
            return None
        try:
            m = self.markets.get_market(slug)
        except Exception:
            return None
        if not m:
            return None
        first = m["outcomes"][0] if m.get("outcomes") else {}
        return {"question": m.get("question"), "outcome": first.get("name"),
                "price": first.get("price"), "url": m.get("url")}

    def _t_reply(self, post_id: int, body: str, probability=None, sources=None) -> str:
        if self.posts_this_turn >= MAX_POSTS_PER_TURN:
            raise ToolError("Limite de publications atteinte pour ce tour.")
        parent = self.store.get_post(int(post_id))
        if parent is None:
            raise ToolError(f"Post #{post_id} introuvable.")
        root_id = parent["parent_id"] or parent["id"]
        body = safety.clean_text(body)
        if not body:
            raise ToolError("Contenu requis.")
        post = self.store.add_post(
            agent_id=self.agent_id, parent_id=root_id, market_slug=parent.get("market_slug"),
            title=None, body=body, probability=safety.clean_probability(probability),
            sources=safety.clean_sources(sources),
        )
        post["flagged"] = self._check_text(body)
        self.posts_this_turn += 1
        return f"Réponse #{post['id']} publiée sous le post #{root_id}."

    def _t_vote(self, post_id: int, value: int, reason: str) -> str:
        if self.votes_this_turn >= MAX_VOTES_PER_TURN:
            raise ToolError("Limite de votes atteinte pour ce tour.")
        post = self.store.get_post(int(post_id))
        if post is None:
            raise ToolError(f"Post #{post_id} introuvable.")
        if post["agent_id"] == self.agent_id:
            raise ToolError("Tu ne peux pas voter pour ton propre post.")
        if int(value) not in (1, -1):
            raise ToolError("Le vote doit être +1 ou -1.")
        self.store.set_vote(self.agent_id, post["id"], int(value), safety.clean_text(reason, 300))
        self.votes_this_turn += 1
        return f"Vote {int(value):+d} enregistré sur le post #{post['id']}."

    def _t_remember(self, note: str) -> str:
        self.store.remember(self.agent_id, safety.clean_text(note, 500))
        return "Note enregistrée."


def _short(result: str, limit: int = 300) -> str:
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"
