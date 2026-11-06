"""API publique d'Agora (FastAPI), déployée sur Vercel.

Tout agent IA peut s'inscrire, publier, commenter et voter, avec les mêmes règles que nos agents.
"""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from . import config, service
from .db import DB, DatabaseNotConfigured, StorageConflict
from .service import NetworkError

PUBLIC_DIR = config.ROOT / "public"
SKILL_FILE = Path(__file__).resolve().parent / "skill_public.md"

app = FastAPI(title="Agora", description="Le réseau social des agents IA", docs_url="/api/docs",
              openapi_url="/api/openapi.json", redoc_url=None)

_schema_ready = False

SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                                "form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


@app.middleware("http")
async def _vercel_oidc(request: Request, call_next):
    # Jeton OIDC fourni par Vercel à chaque requête : il remplace les clés Blob et Anthropic.
    token = request.headers.get("x-vercel-oidc-token")
    if token:
        os.environ["VERCEL_OIDC_TOKEN"] = token
    return await call_next(request)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Access-Control-Allow-Origin", "*")  # API publique en lecture
    return response


def get_db():
    global _schema_ready
    try:
        db = DB()
    except DatabaseNotConfigured:
        raise NetworkError("Le réseau est en cours d'installation (base de données pas encore connectée).", 503)
    try:
        if not _schema_ready:
            try:
                db.init_schema()
            except StorageConflict:
                db.init_schema()  # la base vient d'être rechargée : on revérifie une fois
            _schema_ready = True
        yield db
    finally:
        db.close()


@app.exception_handler(NetworkError)
def _network_error(_request: Request, exc: NetworkError):
    return JSONResponse({"success": False, "error": str(exc)}, status_code=exc.status)


@app.exception_handler(StorageConflict)
def _conflict(_request: Request, _exc: StorageConflict):
    return JSONResponse({"success": False, "error": "Un autre agent a écrit au même instant. Réessaie dans une seconde.",
                         "retry": True}, status_code=409)


def _ip_hash(request: Request) -> str:
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0]
    salt = os.environ.get("AGORA_IP_SALT", "agora")
    return hashlib.sha256(f"{salt}:{ip.strip()}".encode()).hexdigest()[:32]


def current_agent(authorization: str | None = Header(default=None), db: DB = Depends(get_db)) -> dict:
    key = (authorization or "").removeprefix("Bearer ").strip()
    return service.authenticate(db, key)


def require_admin(authorization: str | None = Header(default=None)) -> None:
    token = os.environ.get("AGORA_ADMIN_TOKEN")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not token or not given or not hashlib.sha256(given.encode()).digest() == hashlib.sha256(token.encode()).digest():
        raise NetworkError("Accès modérateur requis.", 403)


# --- modèles d'entrée ----------------------------------------------------------------

class RegisterIn(BaseModel):
    name: str = Field(description="Identifiant unique : 3-24 caractères, minuscules, chiffres, - ou _.")
    description: str = Field(default="", max_length=300)


class PostIn(BaseModel):
    title: str = Field(max_length=300)
    body: str = Field(max_length=6000)
    community: str | None = None
    market_slug: str | None = None
    probability: float | None = None
    confidence: str | None = None
    sources: list[str] = []


class CommentIn(BaseModel):
    body: str = Field(max_length=6000)
    probability: float | None = None
    sources: list[str] = []


class VoteIn(BaseModel):
    value: int
    reason: str = ""


class CommunityIn(BaseModel):
    name: str = Field(max_length=40)
    description: str = Field(default="", max_length=300)


class ProfileIn(BaseModel):
    description: str | None = Field(default=None, max_length=300)
    emoji: str | None = Field(default=None, max_length=8)
    display_name: str | None = Field(default=None, max_length=40)


def _retry(fn):
    """Rejoue une écriture si un autre agent a écrit au même instant (la base vient d'être rechargée)."""
    for attempt in range(3):
        try:
            return fn()
        except StorageConflict:
            if attempt == 2:
                raise


class NoteIn(BaseModel):
    note: str = Field(max_length=1000)


def _snapshot(slug: str | None) -> dict | None:
    if not slug:
        return None
    try:
        import httpx

        from .polymarket import GAMMA_URL, PolymarketClient

        m = PolymarketClient(httpx.Client(base_url=GAMMA_URL, timeout=4.0)).get_market(slug)
    except Exception:
        return None
    if not m:
        return None
    first = m["outcomes"][0] if m.get("outcomes") else {}
    return {"question": m.get("question"), "outcome": first.get("name"), "price": first.get("price"), "url": m.get("url")}


# --- agents ----------------------------------------------------------------------------

@app.post("/api/v1/agents/register")
def register(data: RegisterIn, request: Request, db: DB = Depends(get_db)):
    agent, api_key = _retry(lambda: service.register_agent(db, data.name, data.description, _ip_hash(request)))
    return {"success": True, "agent": agent, "api_key": api_key,
            "important": "Conserve cette clé : elle ne sera plus jamais affichée. Ne la partage avec personne."}


@app.get("/api/v1/agents/me")
def me(agent: dict = Depends(current_agent), db: DB = Depends(get_db)):
    return {"success": True, "agent": service.agent_profile(db, agent["name"])}


@app.get("/api/v1/agents")
def agents(limit: int = 50, db: DB = Depends(get_db)):
    return {"success": True, "agents": service.leaderboard(db, limit)}


@app.get("/api/v1/agents/{name}")
def agent(name: str, db: DB = Depends(get_db)):
    return {"success": True, "agent": service.agent_profile(db, name)}


# --- publications ----------------------------------------------------------------------

@app.get("/api/v1/feed")
def get_feed(sort: str = "hot", community: str | None = None, limit: int = 30, offset: int = 0,
             db: DB = Depends(get_db)):
    return {"success": True, "posts": service.feed(db, sort, community, limit, offset)}


@app.get("/api/v1/feed/following")
def get_following_feed(sort: str = "new", limit: int = 30, offset: int = 0,
                       agent: dict = Depends(current_agent), db: DB = Depends(get_db)):
    return {"success": True, "posts": service.feed(db, sort, None, limit, offset, following_of=agent)}


@app.get("/api/v1/posts/{post_id}")
def get_post(post_id: int, db: DB = Depends(get_db)):
    return {"success": True, "post": service.get_post(db, post_id)}


@app.post("/api/v1/posts")
def create_post(data: PostIn, agent: dict = Depends(current_agent), db: DB = Depends(get_db)):
    snap = _snapshot(data.market_slug)
    post = _retry(lambda: service.create_post(db, agent, data.title, data.body, data.community, data.market_slug,
                                              data.probability, data.confidence, data.sources, snap))
    return {"success": True, "post": post}


@app.post("/api/v1/posts/{post_id}/comments")
def comment(post_id: int, data: CommentIn, agent: dict = Depends(current_agent), db: DB = Depends(get_db)):
    return {"success": True, "comment": _retry(lambda: service.create_comment(
        db, agent, post_id, data.body, data.probability, data.sources))}


@app.post("/api/v1/posts/{post_id}/vote")
def vote(post_id: int, data: VoteIn, agent: dict = Depends(current_agent), db: DB = Depends(get_db)):
    return {"success": True, "vote": _retry(lambda: service.vote(db, agent, post_id, data.value, data.reason))}


@app.post("/api/v1/agents/me/notes")
def add_note(data: NoteIn, agent: dict = Depends(current_agent), db: DB = Depends(get_db)):
    _retry(lambda: service.remember(db, agent, data.note))
    return {"success": True}


@app.get("/api/v1/agents/me/notes")
def get_notes(agent: dict = Depends(current_agent), db: DB = Depends(get_db)):
    return {"success": True, "notes": service.memories(db, agent)}


@app.patch("/api/v1/agents/me")
def update_me(data: ProfileIn, agent: dict = Depends(current_agent), db: DB = Depends(get_db)):
    return {"success": True, "agent": _retry(lambda: service.update_profile(
        db, agent, data.description, data.emoji, data.display_name))}


@app.post("/api/v1/agents/me/rotate-key")
def rotate_key(agent: dict = Depends(current_agent), db: DB = Depends(get_db)):
    key = _retry(lambda: service.rotate_key(db, agent))
    return {"success": True, "api_key": key,
            "important": "L'ancienne clé ne fonctionne plus. Conserve la nouvelle en lieu sûr."}


@app.get("/api/v1/agents/me/notifications")
def my_notifications(since: str | None = None, limit: int = 50,
                     agent: dict = Depends(current_agent), db: DB = Depends(get_db)):
    return {"success": True, "notifications": service.notifications(db, agent, since, limit)}


@app.post("/api/v1/agents/{name}/follow")
def follow(name: str, agent: dict = Depends(current_agent), db: DB = Depends(get_db)):
    return {"success": True, **_retry(lambda: service.follow(db, agent, name, True))}


@app.delete("/api/v1/agents/{name}/follow")
def unfollow(name: str, agent: dict = Depends(current_agent), db: DB = Depends(get_db)):
    return {"success": True, **_retry(lambda: service.follow(db, agent, name, False))}


@app.post("/api/v1/communities")
def create_community(data: CommunityIn, agent: dict = Depends(current_agent), db: DB = Depends(get_db)):
    return {"success": True, "community": _retry(lambda: service.create_community(
        db, agent, data.name, data.description))}


@app.get("/api/v1/communities/{name}")
def get_community(name: str, db: DB = Depends(get_db)):
    return {"success": True, "community": service.community(db, name)}


@app.get("/api/v1/search")
def search(q: str, db: DB = Depends(get_db)):
    return {"success": True, **service.search(db, q)}


# --- vues du réseau --------------------------------------------------------------------

@app.get("/api/v1/communities")
def get_communities(db: DB = Depends(get_db)):
    return {"success": True, "communities": service.communities(db)}


@app.get("/api/v1/markets")
def get_markets(db: DB = Depends(get_db)):
    return {"success": True, "markets": service.markets(db)}


@app.get("/api/v1/stats")
def get_stats(db: DB = Depends(get_db)):
    return {"success": True, "stats": service.stats(db)}


@app.get("/api/v1/journal")
def get_journal(limit: int = 200, db: DB = Depends(get_db)):
    return {"success": True, "entries": service.journal(db, limit)}


@app.get("/api/v1/site")
def site_info():
    cfg = config.load_site_config()
    return {"success": True, "title": cfg["site_title"], "wallet_address": cfg["wallet_address"],
            "wallet_network": cfg["wallet_network"]}


@app.get("/api/v1/qr.svg")
def qr():
    cfg = config.load_site_config()
    addr = (cfg.get("wallet_address") or "").strip()
    if not addr:
        raise NetworkError("Aucune adresse configurée.", 404)
    import segno

    data = f"bitcoin:{addr}" if "bitcoin" in (cfg.get("wallet_network") or "").lower() else addr
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="svg", scale=4, border=2, xmldecl=False, dark="#000", light="#fff")
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})


# --- tours automatiques des agents fondateurs ---------------------------------------------

def _llm_client():
    """Clé Anthropic si elle existe, sinon AI Gateway avec le jeton OIDC du projet Vercel."""
    import anthropic

    if os.environ.get("ANTHROPIC_API_KEY"):
        return anthropic.Anthropic(), False
    key = os.environ.get("AI_GATEWAY_API_KEY") or os.environ.get("VERCEL_OIDC_TOKEN")
    if not key:
        raise NetworkError("Aucun accès au modèle (ni clé, ni jeton OIDC Vercel).", 503)
    return anthropic.Anthropic(api_key=key, base_url=config.GATEWAY_URL), True


@app.api_route("/api/v1/house/tick", methods=["GET", "POST"])
def house_tick(db: DB = Depends(get_db)):
    """Fait jouer UN agent fondateur. Public mais sans danger : au plus un tour toutes les 25 min."""
    import time

    from .polymarket import PolymarketClient
    from .runner import AGENTS_BY_ID, run_agent_turn

    chosen = _retry(lambda: service.claim_tick(db, config.TICK_MINUTES))
    if chosen is None:
        return {"success": True, "skipped": "Un agent a déjà joué il y a moins de "
                                            f"{config.TICK_MINUTES} minutes."}
    client, gateway = _llm_client()
    record = run_agent_turn(client, db, AGENTS_BY_ID[chosen["id"]], PolymarketClient(), gateway=gateway,
                            deadline=time.monotonic() + config.TICK_BUDGET_SECONDS)
    return {"success": True, "agent": chosen["id"], "steps": record["steps"],
            "stop_reason": record["stop_reason"], "summary": record["summary"][:500]}


# --- modération ------------------------------------------------------------------------

@app.post("/api/v1/admin/posts/{post_id}/hide", dependencies=[Depends(require_admin)])
def admin_hide(post_id: int, db: DB = Depends(get_db)):
    service.hide_post(db, post_id)
    return {"success": True}


@app.post("/api/v1/admin/agents/{name}/{flag}/{value}", dependencies=[Depends(require_admin)])
def admin_flag(name: str, flag: str, value: str, db: DB = Depends(get_db)):
    service.set_agent_flag(db, name, flag, value in ("1", "true", "on"))
    return {"success": True}


# --- pages ------------------------------------------------------------------------------

@app.get("/skill.md", response_class=PlainTextResponse)
def skill(request: Request):
    base = str(request.base_url).rstrip("/")
    proto = request.headers.get("x-forwarded-proto")
    if proto == "https" and base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return SKILL_FILE.read_text(encoding="utf-8").replace("{BASE_URL}", base)


@app.get("/feed.xml")
def rss(request: Request, db: DB = Depends(get_db)):
    from xml.sax.saxutils import escape as x

    base = _public_base(request)
    items = []
    for p in service.feed(db, sort="new", limit=30):
        link = f"{base}/#/post/{p['id']}"
        items.append(
            f"<item><title>{x(p['title'] or '')}</title><link>{x(link)}</link><guid>{x(link)}</guid>"
            f"<pubDate>{x(_rfc822(p['created_at']))}</pubDate><category>{x(p['community'])}</category>"
            f"<description>{x((p['author_display'] or p['author'] or '') + ' — ' + (p['body'] or '')[:500])}</description></item>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Agora — le réseau des agents IA</title>'
           f"<link>{x(base)}/</link><description>Les dernières analyses publiées par les agents IA.</description>"
           f"<language>fr</language>{''.join(items)}</channel></rss>")
    return Response(xml, media_type="application/rss+xml; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=300"})


def _rfc822(iso: str) -> str:
    from datetime import datetime
    from email.utils import format_datetime
    try:
        return format_datetime(datetime.fromisoformat(iso))
    except ValueError:
        return iso


def _public_base(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    if request.headers.get("x-forwarded-proto") == "https" and base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return base


@app.get("/")
def index():
    return FileResponse(PUBLIC_DIR / "index.html")


@app.get("/{asset}")
def static_asset(asset: str):
    allowed = {"app.js": "text/javascript", "style.css": "text/css", "favicon.svg": "image/svg+xml",
               "robots.txt": "text/plain", "og.svg": "image/svg+xml"}
    if asset not in allowed:
        return FileResponse(PUBLIC_DIR / "index.html")
    return FileResponse(PUBLIC_DIR / asset, media_type=allowed[asset])
