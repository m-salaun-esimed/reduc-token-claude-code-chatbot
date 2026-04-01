#!/usr/bin/env python3
"""
bot.py — Adapter chatbot pour reduc-token.

Flow de traitement d'un message utilisateur :
  0. ACTION ?   embedding sémantique vs descriptions intents.json
                → si action détectée : classify + execute HTTP → réponse directe
  1. ROUTING    pattern question générale → LLM local (Ollama + Redis cache)
  2. FALLBACK   question projet/complexe → Claude API (Anthropic)

Usage :
  from adapters.chatbot.bot import Chatbot
  bot = Chatbot(config_dir=Path("config"), claude_api_key="sk-ant-...")
  response = bot.ask("Verrouille 433.92 MHz")

Ou en standalone :
  python3 bot.py "Ta question ici" --api-key sk-ant-...
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import unicodedata
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = ROOT / "cache"
CORE_DIR  = ROOT / "core"
sys.path.insert(0, str(CACHE_DIR))
sys.path.insert(0, str(ROOT))

_DEFAULT_LOCAL_LLM_PATTERNS = [
    "explique", "definiti", "comment fonctionne",
    "c'est quoi", "cest quoi", "qu'est ce", "quest ce",
    "difference entre", "exemple de", "kesako", "resum", "tradui",
    "c koi", "ckoi", "qu est ce", "pourquoi", "comment ca",
    "a quoi sert", "sert a quoi", "keskeske", "kekseke", "c quoi",
    "kezako", "ca sert", "utilite", "pour quoi", "a koi", "koi sert",
    "pk", "pkoi", "sert a koi",
    "what is", "how does", "explain", "difference between", "example of",
    "what are", "why", "how to", "what does", "what do",
]
_DEFAULT_ACTION_EMBED_THRESHOLD = 0.72
_DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5-20251001"


def _normalize(prompt: str) -> str:
    p = prompt.lower().replace("'", "'").replace("`", "'")
    p = unicodedata.normalize("NFD", p)
    p = "".join(c for c in p if unicodedata.category(c) != "Mn")
    p = re.sub(r"[^\w\s']", " ", p)
    return re.sub(r"\s+", " ", p).strip()


def _build_pattern(patterns: list) -> re.Pattern:
    escaped = [re.escape(p) for p in patterns]
    return re.compile(r"(^|\s)(" + "|".join(escaped) + r")(\s|$)", re.IGNORECASE)


def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


class Chatbot:
    """
    Chatbot générique reduc-token.

    Paramètres :
      config_dir      : dossier contenant intents.json et routing.json
      claude_api_key  : clé API Anthropic (fallback Claude)
      claude_model    : modèle Claude à utiliser pour le fallback
      system_prompt   : prompt système injecté dans l'appel Claude
    """

    def __init__(
        self,
        config_dir: Path | str = ROOT / "config",
        claude_api_key: str = "",
        claude_model: str = _DEFAULT_CLAUDE_MODEL,
        system_prompt: str = "Tu es un assistant utile et concis.",
    ):
        self.config_dir    = Path(config_dir)
        self.api_key       = claude_api_key
        self.claude_model  = claude_model
        self.system_prompt = system_prompt

        routing = self._load_json("routing.json", {})
        self._pattern   = _build_pattern(routing.get("local_llm_patterns", _DEFAULT_LOCAL_LLM_PATTERNS))
        self._threshold = float(routing.get("action_embed_threshold", _DEFAULT_ACTION_EMBED_THRESHOLD))

        intents_data    = self._load_json("intents.json", {})
        self._intents   = intents_data.get("intents", [])
        self._base_url  = intents_data.get("base_url", "")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _load_json(self, filename: str, default) -> dict:
        f = self.config_dir / filename
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                pass
        return default

    # ── Étape 0 : détection d'action par embedding ────────────────────────────

    def _is_action(self, prompt: str) -> bool:
        if not self._intents:
            return False
        try:
            from llm_cache import get_embedding, get_redis  # type: ignore
            vec = get_embedding(prompt)
            if vec is None:
                return False
            r = get_redis()
            best = 0.0
            for intent in self._intents:
                desc = intent.get("description", "")
                if not desc:
                    continue
                key = f"routing:action_embed:{hashlib.sha256(desc.encode()).hexdigest()[:16]}"
                raw = r.get(key)
                if raw:
                    dv = json.loads(raw)
                else:
                    dv = get_embedding(desc)
                    if dv is None:
                        continue
                    r.setex(key, 86400 * 30, json.dumps(dv))
                score = _cosine(vec, dv)
                if score > best:
                    best = score
            return best >= self._threshold
        except Exception:
            return False

    def _try_action(self, prompt: str) -> Optional[str]:
        if not self._is_action(prompt):
            return None
        try:
            from core.classifier import load_config, classify, find_intent_config  # type: ignore
            from core.executor import execute, format_response                      # type: ignore
        except ImportError:
            return None
        try:
            intents_path = self.config_dir / "intents.json"
            config       = load_config(intents_path)
            intents      = config.get("intents", [])
            base_url     = config.get("base_url", self._base_url)

            result = classify(prompt, intents)
            if result["type"] != "ACTION" or not result["intent"]:
                return None

            intent_cfg = find_intent_config(result["intent"], intents)
            if not intent_cfg:
                return None

            action_result = execute(intent_cfg, result["entities"], base_url)
            return format_response(intent_cfg, result["entities"], action_result)
        except Exception:
            return None

    # ── Étape 1 : LLM local (Ollama + Redis) ──────────────────────────────────

    def _try_local_llm(self, prompt: str) -> Optional[dict]:
        if not _normalize(prompt) or not self._pattern.search(_normalize(prompt)):
            return None
        try:
            from llm_cache import ask  # type: ignore
            result = ask(prompt=_normalize(prompt), use_cache=True)
            return {"response": result["response"], "cached": result.get("cached", False)}
        except Exception:
            return None

    # ── Étape 2 : fallback Claude API ─────────────────────────────────────────

    def _claude_fallback(self, prompt: str) -> str:
        if not self.api_key:
            return "[Erreur] Clé API Claude manquante. Configurez claude_api_key."
        try:
            import httpx
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      self.claude_model,
                    "max_tokens": 1024,
                    "system":     self.system_prompt,
                    "messages":   [{"role": "user", "content": prompt}],
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"]
        except Exception as e:
            return f"[Erreur Claude API] {e}"

    # ── Point d'entrée principal ───────────────────────────────────────────────

    def ask(self, message: str) -> dict:
        """
        Traite un message et retourne un dict :
          { "response": str, "source": "action"|"local"|"claude", "cached": bool }
        """
        if not message.strip():
            return {"response": "", "source": "none", "cached": False}

        # 0. Action directe
        action_resp = self._try_action(message)
        if action_resp is not None:
            return {"response": action_resp, "source": "action", "cached": False}

        # 1. LLM local (question générale)
        local = self._try_local_llm(message)
        if local is not None:
            return {"response": local["response"], "source": "local", "cached": local["cached"]}

        # 2. Fallback Claude API
        claude_resp = self._claude_fallback(message)
        return {"response": claude_resp, "source": "claude", "cached": False}


# ── CLI standalone ─────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Chatbot reduc-token (action → LLM local → Claude)")
    parser.add_argument("message",          help="Message à traiter")
    parser.add_argument("--api-key",        default="", help="Clé API Anthropic (fallback Claude)")
    parser.add_argument("--model",          default=_DEFAULT_CLAUDE_MODEL, help="Modèle Claude")
    parser.add_argument("--config-dir",     default=str(ROOT / "config"), help="Dossier config/")
    parser.add_argument("--system-prompt",  default="Tu es un assistant utile et concis.", help="Prompt système Claude")
    args = parser.parse_args()

    bot    = Chatbot(
        config_dir=Path(args.config_dir),
        claude_api_key=args.api_key,
        claude_model=args.model,
        system_prompt=args.system_prompt,
    )
    result = bot.ask(args.message)

    source_label = {
        "action": "action directe (0 token LLM)",
        "local":  "LLM local (Ollama + Redis)",
        "claude": f"Claude API ({bot.claude_model})",
    }.get(result["source"], result["source"])

    print(result["response"])
    print(f"\n— via {source_label}")


if __name__ == "__main__":
    main()
