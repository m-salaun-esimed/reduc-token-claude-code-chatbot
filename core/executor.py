#!/usr/bin/env python3
"""
executor.py — Exécution d'actions HTTP depuis l'intent registry.

Remplace les {placeholders} dans URL et body par les entités extraites,
puis effectue l'appel HTTP et formate la réponse.
"""
from __future__ import annotations

import json
from typing import Any

import httpx


def _fill(template: Any, entities: dict) -> Any:
    """Remplace {entity} dans les templates string ou dict récursivement."""
    if isinstance(template, str):
        for k, v in entities.items():
            template = template.replace(f"{{{k}}}", str(v))
        return template
    if isinstance(template, dict):
        return {k: _fill(v, entities) for k, v in template.items()}
    if isinstance(template, list):
        return [_fill(item, entities) for item in template]
    return template


def execute(intent_config: dict, entities: dict, base_url: str = "") -> dict:
    """
    Exécute l'action HTTP définie dans intent_config.

    Retourne:
        {"status": int, "data": dict, "ok": bool, "error": str|None}
    """
    action = intent_config.get("action", {})
    method = action.get("method", "GET").upper()
    url    = base_url + _fill(action.get("url", ""), entities)
    body   = _fill(action.get("body", {}), entities) or None
    params = _fill(action.get("params", {}), entities) or None

    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.request(method, url, json=body, params=params)
        data = {}
        if resp.content:
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text}
        return {"status": resp.status_code, "data": data, "ok": resp.is_success, "error": None}
    except Exception as exc:
        return {"status": 0, "data": {}, "ok": False, "error": str(exc)}


def format_response(intent_config: dict, entities: dict, result: dict) -> str:
    """
    Formate la réponse finale à afficher à l'utilisateur.
    Utilise response_template si défini, sinon affiche les données brutes.
    """
    if not result["ok"]:
        error = result.get("error") or f"HTTP {result['status']}"
        return f"Erreur lors de l'action '{intent_config['name']}' : {error}"

    template = intent_config.get("response_template", "")
    if not template:
        return json.dumps(result["data"], ensure_ascii=False, indent=2)

    # Fusionner entités + données retournées pour les placeholders
    merged = {**entities}
    data = result.get("data", {})
    # Aplatir un niveau si la réponse est un dict
    if isinstance(data, dict):
        merged.update(data)

    return _fill(template, merged)
