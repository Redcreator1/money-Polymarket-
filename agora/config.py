"""Configuration centrale d'Agora."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_FILE = DATA_DIR / "state.json"
DOCS_DIR = ROOT / "docs"
SKILLS_DIR = Path(__file__).resolve().parent / "skills"
PRINCIPLES_FILE = Path(__file__).resolve().parent / "principles.md"
SITE_CONFIG_FILE = ROOT / "site_config.json"

MODEL = os.environ.get("AGORA_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("AGORA_EFFORT", "medium")
MAX_TOKENS = 16000

# Limites par tour d'agent : l'agent décide librement, dans ce cadre.
MAX_STEPS_PER_TURN = int(os.environ.get("AGORA_MAX_STEPS", "8"))
MAX_POSTS_PER_TURN = 2
MAX_VOTES_PER_TURN = 6
MAX_WEB_SEARCHES_PER_TURN = 4
MAX_TEXT_CHARS = 4000


def load_site_config() -> dict:
    """Adresse publique du portefeuille + infos du site. Jamais de clé privée ici."""
    cfg = {
        "site_title": "Agora",
        "wallet_address": "",
        "wallet_network": "Polygon (USDC)",
    }
    if SITE_CONFIG_FILE.exists():
        cfg.update(json.loads(SITE_CONFIG_FILE.read_text(encoding="utf-8")))
    env_addr = os.environ.get("AGORA_WALLET_ADDRESS")
    if env_addr:
        cfg["wallet_address"] = env_addr
    return cfg
