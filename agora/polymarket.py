"""Client Polymarket en LECTURE SEULE (Gamma API publique, sans authentification).

Aucune fonction de trading ici, volontairement.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

GAMMA_URL = "https://gamma-api.polymarket.com"


def _parse_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def simplify_market(m: dict) -> dict:
    outcomes = _parse_list(m.get("outcomes"))
    prices = _parse_list(m.get("outcomePrices"))
    return {
        "slug": m.get("slug"),
        "question": m.get("question"),
        "outcomes": [
            {"name": str(o), "price": float(p) if p not in (None, "") else None}
            for o, p in zip(outcomes, prices)
        ],
        "volume_24h": m.get("volume24hr"),
        "volume": m.get("volumeNum") or m.get("volume"),
        "end_date": m.get("endDate"),
        "description": (m.get("description") or "")[:1500],
        "url": f"https://polymarket.com/market/{m.get('slug')}" if m.get("slug") else None,
    }


class PolymarketClient:
    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(base_url=GAMMA_URL, timeout=20.0)

    def list_markets(self, query: str | None = None, limit: int = 10) -> list[dict]:
        limit = max(1, min(int(limit), 25))
        params = {
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
            "limit": 100 if query else limit,
        }
        resp = self.http.get("/markets", params=params)
        resp.raise_for_status()
        markets = [simplify_market(m) for m in resp.json()]
        if query:
            q = query.lower()
            markets = [m for m in markets if q in (m["question"] or "").lower()]
        for m in markets:
            m.pop("description", None)
        return markets[:limit]

    def get_market(self, slug: str) -> dict | None:
        resp = self.http.get("/markets", params={"slug": slug})
        resp.raise_for_status()
        data = resp.json()
        return simplify_market(data[0]) if data else None
