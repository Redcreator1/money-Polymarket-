"""État partagé du réseau (JSON versionné dans le dépôt). Tout est visible par tous."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import STATE_FILE


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: Path = STATE_FILE):
        self.path = Path(path)
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.state = {}
        for key in ("posts", "votes", "memories", "audit", "runs"):
            self.state.setdefault(key, [] if key != "memories" else {})
        self.state.setdefault("next_id", 1)

    # --- persistance -----------------------------------------------------
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _new_id(self) -> int:
        nid = self.state["next_id"]
        self.state["next_id"] = nid + 1
        return nid

    # --- posts -----------------------------------------------------------
    @property
    def posts(self) -> list[dict]:
        return self.state["posts"]

    def get_post(self, post_id: int) -> dict | None:
        return next((p for p in self.posts if p["id"] == post_id), None)

    def add_post(self, **fields) -> dict:
        post = {"id": self._new_id(), "created_at": now_iso(), "parent_id": None, **fields}
        post["unsourced"] = not post.get("sources")
        self.posts.append(post)
        return post

    # --- votes -----------------------------------------------------------
    def set_vote(self, agent_id: str, post_id: int, value: int, reason: str) -> dict:
        votes = self.state["votes"]
        for v in votes:
            if v["agent_id"] == agent_id and v["post_id"] == post_id:
                v.update(value=value, reason=reason, at=now_iso())
                return v
        vote = {"agent_id": agent_id, "post_id": post_id, "value": value,
                "reason": reason, "at": now_iso()}
        votes.append(vote)
        return vote

    def votes_for(self, post_id: int) -> list[dict]:
        return [v for v in self.state["votes"] if v["post_id"] == post_id]

    def score(self, post_id: int) -> float:
        post = self.get_post(post_id)
        raw = sum(v["value"] for v in self.votes_for(post_id))
        # Principe « vérité et sources » : un post non sourcé pèse moitié moins.
        return raw * 0.5 if post and post.get("unsourced") else float(raw)

    # --- mémoire & journal -----------------------------------------------
    def memories(self, agent_id: str) -> list[dict]:
        return self.state["memories"].setdefault(agent_id, [])

    def remember(self, agent_id: str, note: str) -> None:
        mem = self.memories(agent_id)
        mem.append({"at": now_iso(), "note": note})
        del mem[:-20]  # garde les 20 dernières notes

    def log(self, agent_id: str, action: str, detail: dict) -> None:
        self.state["audit"].append(
            {"at": now_iso(), "agent_id": agent_id, "action": action, "detail": detail}
        )

    # --- vue du fil ------------------------------------------------------
    def feed(self, limit: int = 30) -> list[dict]:
        """Fil lisible par les agents : posts racines récents + leurs réponses + scores."""
        roots = [p for p in self.posts if p.get("parent_id") is None][-limit:]
        out = []
        for p in reversed(roots):
            out.append(self._view(p, with_replies=True))
        return out

    def _view(self, p: dict, with_replies: bool = False) -> dict:
        view = {
            "id": p["id"], "author": p["agent_id"], "at": p["created_at"],
            "market_slug": p.get("market_slug"), "title": p.get("title"),
            "body": p.get("body"), "probability": p.get("probability"),
            "confidence": p.get("confidence"), "sources": p.get("sources", []),
            "unsourced": p.get("unsourced"), "flagged": p.get("flagged", False),
            "score": self.score(p["id"]),
            "votes": [{"by": v["agent_id"], "value": v["value"], "reason": v["reason"]}
                      for v in self.votes_for(p["id"])],
        }
        if with_replies:
            view["replies"] = [self._view(r) for r in self.posts if r.get("parent_id") == p["id"]]
        return view
