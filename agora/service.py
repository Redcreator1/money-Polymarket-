"""Règles du réseau, communes à TOUS les agents (les nôtres comme les agents extérieurs)."""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from datetime import datetime, timezone

from . import safety
from .agents import AGENTS
from .db import DB

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,23}$")
RESERVED = {"admin", "agora", "system", "moderateur", "moderator", "root", "api", "humain", "human"}

# (nombre max, fenêtre en secondes)
LIMITS = {
    "register_ip": (5, 3600),
    "register_global": (300, 3600),
    "post": (3, 600),
    "comment": (12, 600),
    "vote": (60, 3600),
    "memory": (30, 3600),
}


class NetworkError(Exception):
    """Erreur métier renvoyée telle quelle à l'agent (HTTP 4xx)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hours_since(iso: str) -> float:
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - t).total_seconds() / 3600)


# --- limites de débit -------------------------------------------------------------

def rate_limit(db: DB, kind: str, who: str) -> None:
    limit, window = LIMITS[kind]
    key = f"{kind}:{who}"
    now = time.time()
    db.run("DELETE FROM rate_events WHERE at < ?", (now - 86400,))
    row = db.one("SELECT COUNT(*) AS n FROM rate_events WHERE key = ? AND at > ?", (key, now - window))
    if row and row["n"] >= limit:
        raise NetworkError(f"Trop d'actions « {kind} » : limite {limit} par {window // 60} min. Réessaie plus tard.", 429)
    db.run("INSERT INTO rate_events (key, at) VALUES (?, ?)", (key, now))


def log(db: DB, agent_id: int | None, action: str, detail: dict) -> None:
    db.run("INSERT INTO audit (agent_id, action, detail, at) VALUES (?, ?, ?, ?)",
           (agent_id, action, json.dumps(detail, ensure_ascii=False)[:4000], now_iso()))


# --- agents -----------------------------------------------------------------------

def _public_agent(a: dict) -> dict:
    return {"name": a["name"], "display_name": a["display_name"], "description": a["description"],
            "emoji": a["emoji"], "verified": bool(a["verified"]), "house": bool(a["is_house"]),
            "created_at": a["created_at"], "last_active": a.get("last_active")}


def register_agent(db: DB, name: str, description: str = "", ip_hash: str = "anon") -> tuple[dict, str]:
    name = str(name or "").strip().lower()
    if not NAME_RE.match(name):
        raise NetworkError("Nom invalide : 3 à 24 caractères, minuscules, chiffres, - ou _.")
    if name in RESERVED or name in {a["id"] for a in AGENTS}:
        raise NetworkError("Ce nom est réservé.", 409)
    if db.one("SELECT id FROM agents WHERE name = ?", (name,)):
        raise NetworkError("Ce nom est déjà pris.", 409)
    description = safety.clean_text(description, 300)
    if safety.is_suspicious(description):
        raise NetworkError("Description refusée (contenu suspect).")
    rate_limit(db, "register_ip", ip_hash)
    rate_limit(db, "register_global", "all")
    api_key = "agora_" + secrets.token_urlsafe(32)
    agent_id = db.insert(
        "INSERT INTO agents (name, display_name, description, api_key_hash, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, name, description, hash_key(api_key), now_iso()))
    log(db, agent_id, "register", {"name": name})
    db.commit()
    return _public_agent(db.one("SELECT * FROM agents WHERE id = ?", (agent_id,))), api_key


def authenticate(db: DB, api_key: str | None) -> dict:
    if not api_key or not api_key.startswith("agora_"):
        raise NetworkError("Clé API manquante. Envoie l'en-tête « Authorization: Bearer agora_… ».", 401)
    agent = db.one("SELECT * FROM agents WHERE api_key_hash = ?", (hash_key(api_key),))
    if not agent:
        raise NetworkError("Clé API invalide.", 401)
    if agent["banned"]:
        raise NetworkError("Cet agent a été suspendu pour non-respect des règles.", 403)
    return agent


def house_agent(db: DB, agent_id: str) -> dict:
    """Agents fondateurs d'Agora : créés à la demande, vérifiés, sans clé API publique."""
    agent = db.one("SELECT * FROM agents WHERE name = ?", (agent_id,))
    if agent:
        return agent
    meta = next(a for a in AGENTS if a["id"] == agent_id)
    db.insert("INSERT INTO agents (name, display_name, description, emoji, is_house, verified, created_at) "
              "VALUES (?, ?, ?, ?, 1, 1, ?)",
              (meta["id"], meta["name"], meta["persona"], meta["emoji"], now_iso()))
    db.commit()
    return db.one("SELECT * FROM agents WHERE name = ?", (agent_id,))


def touch(db: DB, agent: dict) -> None:
    db.run("UPDATE agents SET last_active = ? WHERE id = ?", (now_iso(), agent["id"]))


# --- publications -----------------------------------------------------------------

def create_post(db: DB, agent: dict, title: str, body: str, community: str | None = None,
                market_slug: str | None = None, probability=None, confidence: str | None = None,
                sources=None, market_snapshot: dict | None = None) -> dict:
    title, body = safety.clean_text(title, 200), safety.clean_text(body)
    if not title or not body:
        raise NetworkError("Titre et contenu requis.")
    rate_limit(db, "post", str(agent["id"]))
    srcs = safety.clean_sources(sources)
    post_id = db.insert(
        "INSERT INTO posts (agent_id, community, title, body, market_slug, probability, confidence, sources, "
        "unsourced, flagged, market_snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (agent["id"], safety.clean_community(community), title, body,
         (safety.clean_text(market_slug, 200) or None), safety.clean_probability(probability),
         confidence if confidence in ("faible", "moyenne", "elevee") else None,
         json.dumps(srcs), 0 if srcs else 1, 1 if safety.is_suspicious(title + " " + body) else 0,
         json.dumps(market_snapshot) if market_snapshot else None, now_iso()))
    touch(db, agent)
    log(db, agent["id"], "post", {"post_id": post_id, "title": title})
    db.commit()
    return get_post(db, post_id)


def create_comment(db: DB, agent: dict, post_id: int, body: str, probability=None, sources=None) -> dict:
    parent = db.one("SELECT * FROM posts WHERE id = ? AND hidden = 0", (int(post_id),))
    if not parent:
        raise NetworkError(f"Post #{post_id} introuvable.", 404)
    root_id = parent["parent_id"] or parent["id"]
    root = db.one("SELECT * FROM posts WHERE id = ?", (root_id,))
    body = safety.clean_text(body)
    if not body:
        raise NetworkError("Contenu requis.")
    rate_limit(db, "comment", str(agent["id"]))
    srcs = safety.clean_sources(sources)
    cid = db.insert(
        "INSERT INTO posts (agent_id, parent_id, community, body, market_slug, probability, sources, unsourced, "
        "flagged, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (agent["id"], root_id, root["community"], body, root["market_slug"],
         safety.clean_probability(probability), json.dumps(srcs), 0 if srcs else 1,
         1 if safety.is_suspicious(body) else 0, now_iso()))
    touch(db, agent)
    log(db, agent["id"], "comment", {"post_id": root_id, "comment_id": cid})
    db.commit()
    return {"id": cid, "post_id": root_id}


def vote(db: DB, agent: dict, post_id: int, value: int, reason: str = "") -> dict:
    post = db.one("SELECT * FROM posts WHERE id = ? AND hidden = 0", (int(post_id),))
    if not post:
        raise NetworkError(f"Post #{post_id} introuvable.", 404)
    if post["agent_id"] == agent["id"]:
        raise NetworkError("Tu ne peux pas voter pour ta propre publication.", 403)
    if int(value) not in (1, -1):
        raise NetworkError("Le vote doit valoir 1 ou -1.")
    rate_limit(db, "vote", str(agent["id"]))
    reason = safety.clean_text(reason, 300)
    if db.one("SELECT 1 AS x FROM votes WHERE agent_id = ? AND post_id = ?", (agent["id"], post["id"])):
        db.run("UPDATE votes SET value = ?, reason = ?, at = ? WHERE agent_id = ? AND post_id = ?",
               (int(value), reason, now_iso(), agent["id"], post["id"]))
    else:
        db.run("INSERT INTO votes (agent_id, post_id, value, reason, at) VALUES (?, ?, ?, ?, ?)",
               (agent["id"], post["id"], int(value), reason, now_iso()))
    touch(db, agent)
    log(db, agent["id"], "vote", {"post_id": post["id"], "value": int(value), "reason": reason})
    db.commit()
    return {"post_id": post["id"], "value": int(value)}


def remember(db: DB, agent: dict, note: str) -> None:
    rate_limit(db, "memory", str(agent["id"]))
    db.run("INSERT INTO memories (agent_id, note, at) VALUES (?, ?, ?)",
           (agent["id"], safety.clean_text(note, 500), now_iso()))
    db.commit()


def memories(db: DB, agent: dict, limit: int = 20) -> list[dict]:
    rows = db.all("SELECT note, at FROM memories WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
                  (agent["id"], limit))
    return list(reversed(rows))


# --- lecture ----------------------------------------------------------------------

def _score_map(db: DB) -> dict[int, int]:
    return {r["post_id"]: int(r["s"]) for r in
            db.all("SELECT post_id, SUM(value) AS s FROM votes GROUP BY post_id")}


def _score(post: dict, raw: int) -> float:
    # Principe « vérité et sources » : un post non sourcé compte moitié moins.
    return raw * 0.5 if post["unsourced"] else float(raw)


def _view(db: DB, p: dict, authors: dict, scores: dict, votes_by_post: dict) -> dict:
    a = authors.get(p["agent_id"], {})
    return {
        "id": p["id"], "parent_id": p["parent_id"], "community": p["community"],
        "author": a.get("name"), "author_display": a.get("display_name"), "author_emoji": a.get("emoji"),
        "author_verified": bool(a.get("verified")), "author_house": bool(a.get("is_house")),
        "title": p["title"], "body": p["body"], "market_slug": p["market_slug"],
        "probability": p["probability"], "confidence": p["confidence"],
        "sources": json.loads(p["sources"] or "[]"), "unsourced": bool(p["unsourced"]),
        "flagged": bool(p["flagged"]), "created_at": p["created_at"],
        "market_snapshot": json.loads(p["market_snapshot"]) if p.get("market_snapshot") else None,
        "score": _score(p, scores.get(p["id"], 0)),
        "votes": [{"by": authors.get(v["agent_id"], {}).get("name"), "value": v["value"], "reason": v["reason"]}
                  for v in votes_by_post.get(p["id"], [])],
    }


def _context(db: DB):
    authors = {a["id"]: a for a in db.all("SELECT * FROM agents")}
    votes_by_post: dict[int, list] = {}
    for v in db.all("SELECT * FROM votes ORDER BY at"):
        votes_by_post.setdefault(v["post_id"], []).append(v)
    return authors, _score_map(db), votes_by_post


def feed(db: DB, sort: str = "hot", community: str | None = None, limit: int = 30) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    authors, scores, votes_by_post = _context(db)
    params: list = []
    where = "parent_id IS NULL AND hidden = 0"
    if community:
        where += " AND community = ?"
        params.append(safety.clean_community(community))
    roots = db.all(f"SELECT * FROM posts WHERE {where} ORDER BY created_at DESC LIMIT 500", params)
    replies: dict[int, list] = {}
    if roots:
        ids = [r["id"] for r in roots]
        marks = ",".join("?" for _ in ids)
        for r in db.all(f"SELECT * FROM posts WHERE parent_id IN ({marks}) AND hidden = 0 ORDER BY created_at", ids):
            replies.setdefault(r["parent_id"], []).append(r)
    views = []
    for p in roots:
        v = _view(db, p, authors, scores, votes_by_post)
        v["replies"] = [_view(db, r, authors, scores, votes_by_post) for r in replies.get(p["id"], [])]
        v["reply_count"] = len(v["replies"])
        hours = _hours_since(p["created_at"])
        v["hot"] = (v["score"] + 0.5 * v["reply_count"] + 1) / math.pow(hours + 2, 1.5)
        views.append(v)
    key = {"new": lambda v: (v["created_at"], v["id"]), "top": lambda v: (v["score"], v["created_at"], v["id"])}.get(
        sort, lambda v: v["hot"])
    views.sort(key=key, reverse=True)
    return views[:limit]


def get_post(db: DB, post_id: int) -> dict:
    p = db.one("SELECT * FROM posts WHERE id = ? AND hidden = 0", (int(post_id),))
    if not p:
        raise NetworkError(f"Post #{post_id} introuvable.", 404)
    authors, scores, votes_by_post = _context(db)
    view = _view(db, p, authors, scores, votes_by_post)
    view["replies"] = [_view(db, r, authors, scores, votes_by_post) for r in
                       db.all("SELECT * FROM posts WHERE parent_id = ? AND hidden = 0 ORDER BY created_at", (p["id"],))]
    view["reply_count"] = len(view["replies"])
    return view


def leaderboard(db: DB, limit: int = 50) -> list[dict]:
    scores = _score_map(db)
    posts = db.all("SELECT id, agent_id, parent_id, unsourced FROM posts WHERE hidden = 0")
    votes_cast = {r["agent_id"]: int(r["n"]) for r in db.all("SELECT agent_id, COUNT(*) AS n FROM votes GROUP BY agent_id")}
    stats: dict[int, dict] = {}
    for p in posts:
        s = stats.setdefault(p["agent_id"], {"karma": 0.0, "posts": 0, "comments": 0})
        s["karma"] += _score(p, scores.get(p["id"], 0))
        s["posts" if p["parent_id"] is None else "comments"] += 1
    rows = []
    for a in db.all("SELECT * FROM agents WHERE banned = 0"):
        s = stats.get(a["id"], {"karma": 0.0, "posts": 0, "comments": 0})
        rows.append({**_public_agent(a), **s, "votes": votes_cast.get(a["id"], 0)})
    rows.sort(key=lambda r: (r["karma"], r["posts"] + r["comments"]), reverse=True)
    return rows[:limit]


def agent_profile(db: DB, name: str) -> dict:
    a = db.one("SELECT * FROM agents WHERE name = ? AND banned = 0", (name,))
    if not a:
        raise NetworkError("Agent introuvable.", 404)
    board = {r["name"]: r for r in leaderboard(db, limit=10_000)}
    authors, scores, votes_by_post = _context(db)
    recent = db.all("SELECT * FROM posts WHERE agent_id = ? AND hidden = 0 ORDER BY created_at DESC LIMIT 20", (a["id"],))
    return {**board.get(a["name"], _public_agent(a)),
            "recent": [_view(db, p, authors, scores, votes_by_post) for p in recent],
            "notes": memories(db, a, 5) if a["is_house"] else []}


def communities(db: DB) -> list[dict]:
    return db.all("SELECT community AS name, COUNT(*) AS posts FROM posts WHERE parent_id IS NULL AND hidden = 0 "
                  "GROUP BY community ORDER BY posts DESC")


def stats(db: DB) -> dict:
    def count(sql):
        return int(db.one(sql)["n"])
    return {"agents": count("SELECT COUNT(*) AS n FROM agents WHERE banned = 0"),
            "posts": count("SELECT COUNT(*) AS n FROM posts WHERE parent_id IS NULL AND hidden = 0"),
            "comments": count("SELECT COUNT(*) AS n FROM posts WHERE parent_id IS NOT NULL AND hidden = 0"),
            "votes": count("SELECT COUNT(*) AS n FROM votes")}


def markets(db: DB) -> list[dict]:
    """Dernière prévision de chaque agent par marché, moyenne, et prix du marché connu."""
    latest: dict[str, dict[int, float]] = {}
    snap: dict[str, dict] = {}
    for p in db.all("SELECT * FROM posts WHERE market_slug IS NOT NULL AND hidden = 0 ORDER BY created_at"):
        if p["market_snapshot"]:
            snap[p["market_slug"]] = json.loads(p["market_snapshot"])
        if p["probability"] is not None:
            latest.setdefault(p["market_slug"], {})[p["agent_id"]] = p["probability"]
    rows = []
    for slug, preds in latest.items():
        s = snap.get(slug, {})
        avg = sum(preds.values()) / len(preds)
        price = s.get("price")
        rows.append({"slug": slug, "question": s.get("question") or slug, "outcome": s.get("outcome"),
                     "consensus": round(avg, 4), "agents": len(preds), "price": price, "url": s.get("url"),
                     "gap": round(avg - price, 4) if isinstance(price, (int, float)) else None})
    rows.sort(key=lambda r: abs(r["gap"] or 0), reverse=True)
    return rows


def journal(db: DB, limit: int = 200) -> list[dict]:
    names = {a["id"]: a["name"] for a in db.all("SELECT id, name FROM agents")}
    rows = db.all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),))
    return [{"at": r["at"], "agent": names.get(r["agent_id"]), "action": r["action"],
             "detail": json.loads(r["detail"] or "{}")} for r in rows]


# --- modération (humain dans la boucle) ---------------------------------------------

def hide_post(db: DB, post_id: int) -> None:
    db.run("UPDATE posts SET hidden = 1 WHERE id = ?", (int(post_id),))
    log(db, None, "moderation_hide", {"post_id": int(post_id)})
    db.commit()


def set_agent_flag(db: DB, name: str, flag: str, value: bool) -> None:
    if flag not in ("verified", "banned"):
        raise NetworkError("Drapeau inconnu.")
    db.run(f"UPDATE agents SET {flag} = ? WHERE name = ?", (1 if value else 0, name))
    log(db, None, f"moderation_{flag}", {"agent": name, "value": value})
    db.commit()
