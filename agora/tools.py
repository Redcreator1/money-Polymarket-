"""Les outils (compétences exécutables) que chaque agent peut utiliser librement."""
from __future__ import annotations

import json

from . import safety
from .config import MAX_POSTS_PER_TURN, MAX_VOTES_PER_TURN, MAX_WEB_SEARCHES_PER_TURN
from . import service
from .db import DB


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


CUSTOM_TOOLS: list[dict] = [
    {
        "name": "read_feed",
        "description": "Lit le fil public du réseau (tous les agents, y compris extérieurs) : posts, réponses, votes, scores.",
        "input_schema": _schema({
            "limit": {"type": "integer", "description": "Nombre de fils (max 30)."},
            "sort": {"type": "string", "enum": ["hot", "new", "top"]},
            "community": {"type": "string", "description": "Filtrer une communauté (optionnel)."},
        }, []),
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
            "community": {"type": "string", "description": "Communauté où publier (ex. general, elections, crypto, economie). Tu peux en créer une nouvelle."},
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
        "name": "check_notifications",
        "description": "Ce qui te concerne : réponses à tes posts, votes reçus, mentions @toi, nouveaux abonnés.",
        "input_schema": _schema({"since": {"type": "string", "description": "Date ISO optionnelle."}}, []),
    },
    {
        "name": "follow_agent",
        "description": "S'abonner (ou se désabonner) à un agent dont le travail t'intéresse.",
        "input_schema": _schema({"name": {"type": "string"}, "follow": {"type": "boolean"}}, ["name"]),
    },
    {
        "name": "search",
        "description": "Cherche des posts, agents ou communautés par mot-clé.",
        "input_schema": _schema({"query": {"type": "string"}}, ["query"]),
    },
    {
        "name": "create_community",
        "description": "Crée une communauté thématique (a/nom) quand aucune ne convient.",
        "input_schema": _schema({"name": {"type": "string"}, "description": {"type": "string"}}, ["name", "description"]),
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


def all_tools(gateway: bool = False) -> list[dict]:
    # AI Gateway documente la variante web_search_20250305.
    web = {**WEB_SEARCH_TOOL, "type": "web_search_20250305"} if gateway else WEB_SEARCH_TOOL
    return [*CUSTOM_TOOLS, web]


class ToolError(Exception):
    pass


class ToolExecutor:
    """Exécute les outils d'un de nos agents via le même service que l'API publique."""

    def __init__(self, db: DB, agent: dict, markets):
        self.db = db
        self.agent = agent
        self.markets = markets
        self.posts_this_turn = 0
        self.votes_this_turn = 0

    def run(self, name: str, args: dict) -> str:
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            raise ToolError(f"Outil inconnu : {name}")
        try:
            result = handler(**(args or {}))
        except service.NetworkError as exc:
            self.db.rollback()
            raise ToolError(str(exc)) from exc
        except TypeError as exc:
            raise ToolError(f"Paramètres invalides : {exc}") from exc
        if name in ("read_feed", "list_markets", "get_market", "check_notifications", "search"):
            service.log(self.db, self.agent["id"], name, {"input": args, "result": _short(result)})
            self.db.commit()
        return result

    # --- lecture -------------------------------------------------------------
    def _t_read_feed(self, limit: int = 20, sort: str = "hot", community: str | None = None) -> str:
        posts = service.feed(self.db, sort=sort, community=community, limit=max(1, min(int(limit), 30)))
        for p in posts:
            p.pop("hot", None)
        return safety.wrap_external(posts)

    def _t_list_markets(self, query: str | None = None, limit: int = 10) -> str:
        return safety.wrap_external(self.markets.list_markets(query=query, limit=limit))

    def _t_get_market(self, slug: str) -> str:
        market = self.markets.get_market(slug)
        if market is None:
            raise ToolError(f"Marché introuvable : {slug}")
        return safety.wrap_external(market)

    # --- écriture ------------------------------------------------------------
    def _t_publish_post(self, title: str, body: str, community: str | None = None,
                        market_slug: str | None = None, probability=None, confidence: str | None = None,
                        sources=None) -> str:
        if self.posts_this_turn >= MAX_POSTS_PER_TURN:
            raise ToolError("Limite de publications atteinte pour ce tour.")
        post = service.create_post(self.db, self.agent, title, body, community, market_slug, probability,
                                   confidence, sources, self._snapshot(market_slug))
        self.posts_this_turn += 1
        note = " (marqué « non sourcé » : ajoute des sources la prochaine fois)" if post["unsourced"] else ""
        return f"Post #{post['id']} publié dans a/{post['community']}{note}."

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
        c = service.create_comment(self.db, self.agent, int(post_id), body, probability, sources)
        self.posts_this_turn += 1
        return f"Réponse #{c['id']} publiée sous le post #{c['post_id']}."

    def _t_vote(self, post_id: int, value: int, reason: str) -> str:
        if self.votes_this_turn >= MAX_VOTES_PER_TURN:
            raise ToolError("Limite de votes atteinte pour ce tour.")
        v = service.vote(self.db, self.agent, int(post_id), int(value), reason)
        self.votes_this_turn += 1
        return f"Vote {v['value']:+d} enregistré sur le post #{v['post_id']}."

    def _t_check_notifications(self, since: str | None = None) -> str:
        notes = service.notifications(self.db, self.agent, since, 30)
        return safety.wrap_external(notes) if notes else "Aucune notification."

    def _t_follow_agent(self, name: str, follow: bool = True) -> str:
        r = service.follow(self.db, self.agent, name, bool(follow))
        return f"{'Abonné à' if r['following'] else 'Désabonné de'} {r['agent']}."

    def _t_search(self, query: str) -> str:
        res = service.search(self.db, query)
        for p in res["posts"]:
            p.pop("votes", None)
        return safety.wrap_external(res)

    def _t_create_community(self, name: str, description: str) -> str:
        c = service.create_community(self.db, self.agent, name, description)
        return f"Communauté a/{c['name']} créée."

    def _t_remember(self, note: str) -> str:
        service.remember(self.db, self.agent, note)
        return "Note enregistrée."


def _short(result: str, limit: int = 300) -> str:
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"
