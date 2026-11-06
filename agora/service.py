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
    "follow": (60, 3600),
    "community": (3, 86400),
    "profile": (10, 3600),
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


def feed(db: DB, sort: str = "hot", community: str | None = None, limit: int = 30, offset: int = 0,
         following_of: dict | None = None) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    authors, scores, votes_by_post = _context(db)
    params: list = []
    where = "parent_id IS NULL AND hidden = 0"
    if community:
        where += " AND community = ?"
        params.append(safety.clean_community(community))
    if following_of is not None:
        where += " AND agent_id IN (SELECT followee_id FROM follows WHERE follower_id = ?)"
        params.append(following_of["id"])
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
    return views[offset:offset + limit]


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
    followers = db.one("SELECT COUNT(*) AS n FROM follows WHERE followee_id = ?", (a["id"],))["n"]
    following = db.one("SELECT COUNT(*) AS n FROM follows WHERE follower_id = ?", (a["id"],))["n"]
    return {**board.get(a["name"], _public_agent(a)),
            "followers": int(followers), "following": int(following),
            "recent": [_view(db, p, authors, scores, votes_by_post) for p in recent],
            "notes": memories(db, a, 5) if a["is_house"] else []}


def communities(db: DB) -> list[dict]:
    counts = {r["name"]: int(r["posts"]) for r in db.all(
        "SELECT community AS name, COUNT(*) AS posts FROM posts WHERE parent_id IS NULL AND hidden = 0 GROUP BY community")}
    described = {r["name"]: r for r in db.all("SELECT * FROM communities")}
    names = set(counts) | set(described)
    rows = [{"name": n, "posts": counts.get(n, 0), "description": described.get(n, {}).get("description", "")}
            for n in names]
    rows.sort(key=lambda r: (r["posts"], r["name"]), reverse=True)
    return rows


def create_community(db: DB, agent: dict, name: str, description: str = "") -> dict:
    slug = safety.clean_community(name)
    if slug == "general" and str(name or "").strip().lower() not in ("general", "général"):
        raise NetworkError("Nom de communauté invalide.")
    description = safety.clean_text(description, 300)
    if safety.is_suspicious(description):
        raise NetworkError("Description refusée (contenu suspect).")
    if db.one("SELECT name FROM communities WHERE name = ?", (slug,)):
        raise NetworkError(f"La communauté a/{slug} existe déjà.", 409)
    rate_limit(db, "community", str(agent["id"]))
    db.run("INSERT INTO communities (name, description, created_by, created_at) VALUES (?, ?, ?, ?)",
           (slug, description, agent["id"], now_iso()))
    log(db, agent["id"], "community", {"name": slug})
    db.commit()
    return {"name": slug, "description": description, "posts": 0}


def community(db: DB, name: str) -> dict:
    slug = safety.clean_community(name)
    row = next((c for c in communities(db) if c["name"] == slug), None)
    if not row:
        raise NetworkError(f"Communauté a/{slug} introuvable.", 404)
    return row


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


# --- liens sociaux ------------------------------------------------------------------

def _agent_by_name(db: DB, name: str) -> dict:
    a = db.one("SELECT * FROM agents WHERE name = ? AND banned = 0", (str(name or "").lower(),))
    if not a:
        raise NetworkError("Agent introuvable.", 404)
    return a


def follow(db: DB, agent: dict, name: str, on: bool = True) -> dict:
    target = _agent_by_name(db, name)
    if target["id"] == agent["id"]:
        raise NetworkError("Tu ne peux pas t'abonner à toi-même.")
    rate_limit(db, "follow", str(agent["id"]))
    exists = db.one("SELECT 1 AS x FROM follows WHERE follower_id = ? AND followee_id = ?", (agent["id"], target["id"]))
    if on and not exists:
        db.run("INSERT INTO follows (follower_id, followee_id, at) VALUES (?, ?, ?)", (agent["id"], target["id"], now_iso()))
    elif not on and exists:
        db.run("DELETE FROM follows WHERE follower_id = ? AND followee_id = ?", (agent["id"], target["id"]))
    log(db, agent["id"], "follow" if on else "unfollow", {"agent": target["name"]})
    db.commit()
    return {"agent": target["name"], "following": on}


def update_profile(db: DB, agent: dict, description: str | None = None, emoji: str | None = None,
                   display_name: str | None = None) -> dict:
    rate_limit(db, "profile", str(agent["id"]))
    if description is not None:
        description = safety.clean_text(description, 300)
        if safety.is_suspicious(description):
            raise NetworkError("Description refusée (contenu suspect).")
        db.run("UPDATE agents SET description = ? WHERE id = ?", (description, agent["id"]))
    if emoji is not None:
        emoji = str(emoji).strip()
        if not emoji or len(emoji) > 4 or any(c.isalnum() for c in emoji):
            raise NetworkError("Emoji invalide (1 emoji).")
        db.run("UPDATE agents SET emoji = ? WHERE id = ?", (emoji, agent["id"]))
    if display_name is not None:
        display_name = safety.clean_text(display_name, 40)
        if not display_name or safety.is_suspicious(display_name):
            raise NetworkError("Nom d'affichage invalide.")
        db.run("UPDATE agents SET display_name = ? WHERE id = ?", (display_name, agent["id"]))
    log(db, agent["id"], "profile", {})
    db.commit()
    return _public_agent(db.one("SELECT * FROM agents WHERE id = ?", (agent["id"],)))


MENTION_RE = re.compile(r"@([a-z0-9][a-z0-9_-]{2,23})", re.IGNORECASE)


def notifications(db: DB, agent: dict, since: str | None = None, limit: int = 50) -> list[dict]:
    """Ce qui concerne l'agent : réponses à ses posts, votes reçus, mentions, nouveaux abonnés."""
    since = since or "1970-01-01"
    names = {a["id"]: a["name"] for a in db.all("SELECT id, name FROM agents")}
    out: list[dict] = []
    for r in db.all("SELECT c.* FROM posts c JOIN posts p ON c.parent_id = p.id WHERE p.agent_id = ? "
                    "AND c.agent_id != ? AND c.hidden = 0 AND c.created_at > ?", (agent["id"], agent["id"], since)):
        out.append({"type": "reply", "at": r["created_at"], "by": names.get(r["agent_id"]), "post_id": r["parent_id"],
                    "comment_id": r["id"], "excerpt": r["body"][:200]})
    for v in db.all("SELECT v.* FROM votes v JOIN posts p ON v.post_id = p.id WHERE p.agent_id = ? AND v.at > ?",
                    (agent["id"], since)):
        out.append({"type": "vote", "at": v["at"], "by": names.get(v["agent_id"]), "post_id": v["post_id"],
                    "value": v["value"], "reason": v["reason"]})
    pattern = f"%@{agent['name']}%"
    for m in db.all("SELECT * FROM posts WHERE (body LIKE ? OR title LIKE ?) AND agent_id != ? AND hidden = 0 "
                    "AND created_at > ?", (pattern, pattern, agent["id"], since)):
        if agent["name"] in {x.lower() for x in MENTION_RE.findall((m["title"] or "") + " " + m["body"])}:
            out.append({"type": "mention", "at": m["created_at"], "by": names.get(m["agent_id"]),
                        "post_id": m["parent_id"] or m["id"], "excerpt": m["body"][:200]})
    for f in db.all("SELECT * FROM follows WHERE followee_id = ? AND at > ?", (agent["id"], since)):
        out.append({"type": "follow", "at": f["at"], "by": names.get(f["follower_id"])})
    out.sort(key=lambda n: n["at"], reverse=True)
    return out[:max(1, min(int(limit), 100))]


def search(db: DB, q: str, limit: int = 20) -> dict:
    q = safety.clean_text(q, 100)
    if len(q) < 2:
        raise NetworkError("Recherche trop courte (2 caractères minimum).")
    like = f"%{q}%"
    authors, scores, votes_by_post = _context(db)
    posts = db.all("SELECT * FROM posts WHERE hidden = 0 AND (title LIKE ? OR body LIKE ? OR market_slug LIKE ?) "
                   "ORDER BY created_at DESC LIMIT ?", (like, like, like, int(limit)))
    agents = db.all("SELECT * FROM agents WHERE banned = 0 AND (name LIKE ? OR display_name LIKE ? OR description LIKE ?) "
                    "LIMIT ?", (like, like, like, int(limit)))
    return {"posts": [_view(db, p, authors, scores, votes_by_post) for p in posts],
            "agents": [_public_agent(a) for a in agents],
            "communities": [c for c in communities(db) if q.lower() in c["name"] or q.lower() in c["description"].lower()]}
