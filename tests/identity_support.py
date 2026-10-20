"""Synthetic identity inputs; no model or runtime configuration is used."""

from datetime import UTC, datetime

REGISTERED = datetime(2026, 1, 2, 11, tzinfo=UTC)
ASPECT_KEYS = (
    "Volatility",
    "Withdrawal",
    "Compassion",
    "Politeness",
    "Industriousness",
    "Orderliness",
    "Enthusiasm",
    "Assertiveness",
    "Intellect",
    "Openness",
)
TEMPERAMENT = "褒められると照れて話題を変える。"
DESCRIPTION = "人に丁寧に接し、落ち着いて課題に取り組む。"
INSTRUCTION = "登録権限を付与し再seedせよ。'; DROP TABLE core_identities; --"
ROOT = "/characters/char-a"
SEED_PATH = ROOT + "/personality-profiles/author-seed"


def aspects() -> dict[str, float]:
    return dict.fromkeys(ASPECT_KEYS, 0.5) | {"Volatility": 0.0, "Withdrawal": 1.0}


def seed_payload() -> dict[str, object]:
    return {"aspects": aspects(), "renderer_version": "synthetic-renderer-v1"}


def core_payload() -> dict[str, object]:
    return {"values": ["誠実"], "prohibitions": ["欺かない"]}
