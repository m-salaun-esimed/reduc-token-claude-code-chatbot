#!/usr/bin/env python3
"""
classifier.py — Classification d'intent via LLM local (Ollama JSON mode).

Retourne l'intent + entités extraites depuis un message utilisateur.
Cache in-process pour ne pas re-classifier le même message.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

OLLAMA_URL     = "http://localhost:11435"
CLASSIFY_MODEL = "qwen2.5:3b"

_CACHE: dict[str, dict] = {}  # cache in-process (durée de vie = 1 hook call)


def load_intents(config_path: Path) -> list[dict]:
    """Charge la liste des intents depuis intents.json."""
    if not config_path.exists():
        return []
    return json.loads(config_path.read_text(encoding="utf-8")).get("intents", [])


def load_config(config_path: Path) -> dict:
    """Charge la config complète (base_url + intents)."""
    if not config_path.exists():
        return {"base_url": "", "intents": []}
    return json.loads(config_path.read_text(encoding="utf-8"))


def _build_prompt(message: str, intents: list[dict]) -> str:
    lines = []
    for i in intents:
        entities_str = ""
        if i.get("entities"):
            entities_str = ". Entities: " + ", ".join(
                f'{e["name"]} ({e["type"]})' for e in i["entities"]
            )
        lines.append(f'- {i["name"]} ({i["type"]}): {i["description"]}{entities_str}')

    intents_block = "\n".join(lines)

    return (
        f"Classify this user message and extract entities. Return ONLY valid JSON.\n\n"
        f"Available intents:\n{intents_block}\n\n"
        f'Message: "{message}"\n\n'
        f"Return exactly this JSON structure:\n"
        f'{{"intent": "<intent_name or null if none matches>", '
        f'"entities": {{}}, '
        f'"type": "<ACTION or KNOWLEDGE or UNKNOWN>"}}\n\n'
        f"Entity extraction rules:\n"
        f"- Frequencies: extract as float in MHz (ex: '433 MHz' → 433.0)\n"
        f"- IDs: extract as integer\n"
        f"- If no intent matches, return intent: null and type: UNKNOWN"
    )


def classify(message: str, intents: list[dict]) -> dict:
    """
    Classifie le message et extrait les entités.

    Retourne:
        {"intent": str|None, "entities": dict, "type": "ACTION"|"KNOWLEDGE"|"UNKNOWN"}
    """
    if not intents:
        return {"intent": None, "entities": {}, "type": "UNKNOWN"}

    key = hashlib.md5(message.lower().strip().encode()).hexdigest()
    if key in _CACHE:
        return _CACHE[key]

    prompt = _build_prompt(message, intents)

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": CLASSIFY_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                },
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "{}")
            result = json.loads(raw)
    except Exception:
        result = {}

    result = {
        "intent": result.get("intent") or None,
        "entities": result.get("entities") or {},
        "type": result.get("type") or "UNKNOWN",
    }

    _CACHE[key] = result
    return result


def find_intent_config(intent_name: str, intents: list[dict]) -> dict | None:
    """Retrouve la config complète d'un intent par son nom."""
    return next((i for i in intents if i["name"] == intent_name), None)
