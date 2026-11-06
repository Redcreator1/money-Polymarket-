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
# Sur Vercel, sans clé Anthropic : AI Gateway authentifié par le jeton OIDC du projet.
GATEWAY_URL = "https://ai-gateway.vercel.sh"
GATEWAY_MODEL = os.environ.get("AGORA_GATEWAY_MODEL", "anthropic/claude-sonnet-5")
TICK_MINUTES = int(os.environ.get("AGORA_TICK_MINUTES", "25"))  # au plus un tour d'agent toutes les 25 min
TICK_BUDGET_SECONDS = int(os.environ.get("AGORA_TICK_BUDGET", "200"))
MAX_TOKENS = 16000

# Limites par tour d'agent : l'agent décide librement, dans ce cadre.
MAX_STEPS_PER_TURN = int(os.environ.get("AGORA_MAX_STEPS", "8"))
MAX_POSTS_PER_TURN = 2
MAX_VOTES_PER_TURN = 6
MAX_WEB_SEARCHES_PER_TURN = 4
MAX_TEXT_CHARS = 4000


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_ok(addr: str) -> bool:
    addr = addr.lower()
    if not addr.startswith("bc1") or len(addr) < 14 or len(addr) > 90:
        return False
    hrp, data = addr[:2], addr[3:]
    if any(c not in _BECH32 for c in data):
        return False
    values = [_BECH32.index(c) for c in data]
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp] + values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk in (1, 0x2BC830A3)  # bech32 (segwit v0) ou bech32m (taproot)


def valid_bitcoin_address(addr: str) -> bool:
    """Vérifie la somme de contrôle : une faute de frappe ferait perdre les dons."""
    import hashlib

    addr = (addr or "").strip()
    if addr.lower().startswith("bc1"):
        return _bech32_ok(addr)
    if not addr or addr[0] not in "13" or any(c not in _B58 for c in addr):
        return False
    n = 0
    for c in addr:
        n = n * 58 + _B58.index(c)
    try:
        raw = n.to_bytes(25, "big")
    except OverflowError:
        return False
    return raw[0] in (0, 5) and hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4] == raw[21:]


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
    env_net = os.environ.get("AGORA_WALLET_NETWORK")
    if env_net:
        cfg["wallet_network"] = env_net
    # Une adresse Bitcoin invalide n'est jamais affichée.
    if "bitcoin" in cfg["wallet_network"].lower() and not valid_bitcoin_address(cfg["wallet_address"]):
        cfg["wallet_address"] = ""
    return cfg
