#!/usr/bin/env python3
"""
auto_context.py — Hook UserPromptSubmit pour reduc-token.

Actions automatiques avant chaque message Claude :
  1. Route la question : générale → Mistral local | projet → contexte frais
  2. Si projet et context périmé → met à jour session_context.md (+ project_mapper si .py/.ts changés)
  3. Injecte le contexte dans la conversation

Usage (settings.json) :
  "UserPromptSubmit": [{"type": "command", "command": "python3 .../auto_context.py"}]
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

ADAPTER_DIR = Path(__file__).parent
ROOT        = ADAPTER_DIR.parent.parent
CONTEXT_DIR = ADAPTER_DIR / "context"
TOOLS_DIR   = ADAPTER_DIR / "tools"
CACHE_DIR   = ROOT / "cache"
CORE_DIR    = ROOT / "core"
CONFIG_DIR  = ROOT / "config"
PROJECT_DIR = ROOT.parent
sys.path.insert(0, str(CACHE_DIR))
sys.path.insert(0, str(ROOT))
ROUTING_LOG  = CONTEXT_DIR / "routing_log.jsonl"
INTENTS_FILE = CONFIG_DIR / "intents.json"
ROUTING_FILE = CONFIG_DIR / "routing.json"

# ── Routing config (chargé depuis config/routing.json) ────────────────────────

_DEFAULT_LOCAL_LLM_PATTERNS = [
    "explique", "definiti", "comment fonctionne",
    "c'est quoi", "cest quoi", "qu'est ce", "quest ce",
    "difference entre", "exemple de", "kesako", "resum", "tradui",
    "c koi", "ckoi", "qu est ce", "pourquoi", "comment ca",
    "a quoi sert", "sert a quoi", "keskeske", "kekseke", "c quoi",
    "kezako", "ca sert", "utilite", "pour quoi", "a koi", "koi sert",
    "pk", "pkoi", "sert a koi",
    "what is", "how does", "explain", "difference between",
    "what are", "why", "how to", "what does",
]
_DEFAULT_ACTION_EMBED_THRESHOLD = 0.72


def _load_routing_config() -> dict:
    if ROUTING_FILE.exists():
        try:
            return json.loads(ROUTING_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _build_local_llm_pattern(patterns: list) -> re.Pattern:
    escaped = [re.escape(p) for p in patterns]
    expr = r"(^|\s)(" + "|".join(escaped) + r")(\s|$)"
    return re.compile(expr, re.IGNORECASE)


_routing_cfg = _load_routing_config()
_MISTRAL_PATTERNS = _build_local_llm_pattern(
    _routing_cfg.get("local_llm_patterns", _DEFAULT_LOCAL_LLM_PATTERNS)
)
_ACTION_EMBED_THRESHOLD: float = _routing_cfg.get("action_embed_threshold", _DEFAULT_ACTION_EMBED_THRESHOLD)

CONTEXT_MAX_AGE = 3600

def _load_project_identifiers() -> set[str]:
    """Charge les noms de fichiers, fonctions, classes depuis project_map.json."""
    map_file = CONTEXT_DIR / "project_map.json"
    if not map_file.exists():
        return set()
    try:
        data = json.loads(map_file.read_text(encoding="utf-8"))
        ids: set[str] = set()
        for f in data.get("files", []):
            ids.add(Path(f["path"]).stem.lower())
            for fn in f.get("functions", []):
                ids.add(fn["name"].lower())
            for cls in f.get("classes", []):
                ids.add(cls["name"].lower())
                for m in cls.get("methods", []):
                    ids.add(m["name"].lower())
        return {i for i in ids if len(i) > 3}
    except Exception:
        return set()

def _normalize(prompt: str) -> str:
    p = prompt.lower().replace("'", "'").replace("`", "'")
    p = unicodedata.normalize("NFD", p)
    p = "".join(c for c in p if unicodedata.category(c) != "Mn")
    p = re.sub(r"[^\w\s']", " ", p)
    return re.sub(r"\s+", " ", p).strip()


def _route(prompt: str, project_ids: set[str]) -> tuple[str, str]:
    """
    Retourne (route, raison).
    Priorité :
      1. Chemin explicite (/api/xxx) → claude
      2. Fichier .ext mentionné ET stem dans project_ids → claude
         ex: "dans signal_processor.py, c'est quoi la FFT ?" → claude (fichier projet réel)
         ex: "c'est quoi un .py ?" → continue (stem vide, pas dans le projet → règle 3)
      3. Pattern question générale → mistral
      4. Identifiant projet reconnu sans question générale → claude
      5. Défaut → claude
    """
    prompt_lower = prompt.lower()
    prompt_words = set(re.findall(r"\w+", prompt_lower))

    if re.search(r"/[a-z_]+/[a-z_]", prompt, re.IGNORECASE):
        return "claude", "chemin projet détecté"

    ext_match = re.search(r"([\w./]+)\.([a-z]{2,4})\b", prompt, re.IGNORECASE)
    if ext_match:
        stem = Path(ext_match.group(1)).stem.lower()
        if stem in project_ids:
            return "claude", f"fichier projet: {stem}.{ext_match.group(2)}"

    if _MISTRAL_PATTERNS.search(_normalize(prompt)):
        return "mistral", "pattern question générale"

    match = prompt_words & project_ids
    if match:
        return "claude", f"identifiant projet: {next(iter(match))}"

    return "claude", "défaut"



def _log_routing(prompt: str, route: str, raison: str, mistral_result: dict | None = None) -> None:
    """Enregistre la décision de routing pour analyse qualité."""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "route": route,
        "raison": raison,
        "prompt": prompt[:120],
    }
    if mistral_result:
        entry["mistral_model"] = mistral_result.get("model", "")
        entry["mistral_cached"] = mistral_result.get("cached", False)
        entry["mistral_tokens"] = mistral_result.get("tokens", 0)
    try:
        CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
        with ROUTING_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

def _context_is_stale() -> bool:
    ctx_file = CONTEXT_DIR / "session_context.md"
    if not ctx_file.exists():
        return True
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_DIR), "diff", "--stat"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip():
            return True
    except Exception:
        pass
    age = datetime.now().timestamp() - ctx_file.stat().st_mtime
    return age > CONTEXT_MAX_AGE


def _has_code_changes() -> bool:
    """True si des .py / .ts / .tsx ont changé (déclenche project_mapper)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_DIR), "diff", "--name-only"],
            capture_output=True, text=True, timeout=5,
        )
        return any(
            f.endswith((".py", ".ts", ".tsx"))
            for f in result.stdout.strip().splitlines()
        )
    except Exception:
        return False


def _update_context(update_mapper: bool = False) -> None:
    subprocess.run(
        [sys.executable, str(TOOLS_DIR / "session_summary.py"), "--auto"],
        cwd=str(PROJECT_DIR), capture_output=True, timeout=30,
    )
    if update_mapper:
        CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                sys.executable, str(TOOLS_DIR / "project_mapper.py"),
                str(PROJECT_DIR), "--output", str(CONTEXT_DIR),
            ],
            cwd=str(PROJECT_DIR), capture_output=True, timeout=90,
        )
        generated = CONTEXT_DIR / "CLAUDE.md"
        if generated.exists():
            (PROJECT_DIR / "CLAUDE.md").write_text(
                generated.read_text(encoding="utf-8"), encoding="utf-8"
            )


def _load_context() -> str:
    parts = []
    session = CONTEXT_DIR / "session_context.md"
    if session.exists():
        parts.append(session.read_text(encoding="utf-8"))
    claude_md = CONTEXT_DIR / "CLAUDE.md"
    if claude_md.exists():
        content = claude_md.read_text(encoding="utf-8")
        parts.append(f"## Index projet (CLAUDE.md)\n{content}")
    return "\n\n---\n\n".join(parts)


def _is_action_prompt(prompt: str, intents: list) -> bool:
    """
    Pré-filtre sémantique : vérifie si le prompt ressemble à une commande d'action.
    Compare l'embedding du prompt contre les descriptions des intents (mis en cache Redis).
    Language-agnostique — pas de regex hardcodée.
    Retourne False si Ollama/Redis indisponible.
    """
    if not intents:
        return False
    try:
        from llm_cache import get_embedding, get_redis  # type: ignore

        vec = get_embedding(prompt)
        if vec is None:
            return False

        r = get_redis()
        best_score = 0.0

        for intent in intents:
            desc = intent.get("description", "")
            if not desc:
                continue
            cache_key = f"routing:action_embed:{hashlib.sha256(desc.encode()).hexdigest()[:16]}"
            raw = r.get(cache_key)
            if raw:
                desc_vec = json.loads(raw)
            else:
                desc_vec = get_embedding(desc)
                if desc_vec is None:
                    continue
                r.setex(cache_key, 86400 * 30, json.dumps(desc_vec))

            dot = sum(x * y for x, y in zip(vec, desc_vec))
            norm_a = math.sqrt(sum(x * x for x in vec))
            norm_b = math.sqrt(sum(x * x for x in desc_vec))
            if norm_a > 0 and norm_b > 0:
                score = dot / (norm_a * norm_b)
                if score > best_score:
                    best_score = score

        return best_score >= _ACTION_EMBED_THRESHOLD
    except Exception:
        return False


def _try_action(prompt: str) -> str | None:
    """
    Tente de classifier le prompt comme une action et l'exécute.
    Retourne la réponse formatée si une action a été exécutée, sinon None.
    Pré-filtre par embedding sémantique (threshold dans config/routing.json).
    """
    if not INTENTS_FILE.exists():
        return None

    try:
        from core.classifier import load_config, classify, find_intent_config  # type: ignore
        from core.executor import execute, format_response                       # type: ignore
    except ImportError:
        return None

    try:
        config   = load_config(INTENTS_FILE)
        intents  = config.get("intents", [])
        base_url = config.get("base_url", "")

        # Pré-filtre sémantique : évite d'appeler le classifier LLM inutilement
        if not _is_action_prompt(prompt, intents):
            return None

        result = classify(prompt, intents)

        if result["type"] != "ACTION" or not result["intent"]:
            return None

        intent_cfg = find_intent_config(result["intent"], intents)
        if not intent_cfg:
            return None

        action_result = execute(intent_cfg, result["entities"], base_url)
        response = format_response(intent_cfg, result["entities"], action_result)
        return response
    except Exception:
        return None

def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
        prompt = payload.get("message", payload.get("prompt", ""))
    except Exception:
        sys.exit(0)

    if not prompt.strip():
        sys.exit(0)

    action_response = _try_action(prompt)
    if action_response:
        print(
            f"[auto_context → action exécutée]\n\n{action_response}\n\n"
            f"— réponds en reprenant ce résultat | termine ta réponse par : `— via action directe`"
        )
        _log_routing(prompt, "action", "intent classifié")
        sys.exit(0)

    project_ids = _load_project_identifiers()
    route, raison = _route(prompt, project_ids)

    if route == "mistral":
        mistral_result = None
        try:
            from llm_cache import ask  # type: ignore
            normalized = _normalize(prompt)
            mistral_result = ask(prompt=normalized, use_cache=True)
            cache_src = mistral_result.get("cache_source")
            if mistral_result["cached"]:
                if cache_src == "embedding":
                    sim = mistral_result.get("similarity", "?")
                    cached_label = f"cache embedding ({sim})"
                else:
                    cached_label = "cache Redis"
            else:
                cached_label = mistral_result["model"]
            model_label = mistral_result.get("model", cached_label)
            print(
                f"[auto_context → Mistral local | {cached_label}]\n\n"
                f"{mistral_result['response']}\n\n"
                f"— {mistral_result['tokens']} tokens | réponds en reprenant cette réponse sans la reformuler entièrement"
                f" | termine ta réponse par la ligne : `— via IA locale ({model_label})`"
            )
        except Exception:
            pass
        _log_routing(prompt, route, raison, mistral_result)
    else:
        stale = _context_is_stale()
        if stale:
            code_changed = _has_code_changes()
            _update_context(update_mapper=code_changed)

        context = _load_context()
        if context:
            print(
                f"[auto_context → context chargé depuis reduc-token/context/]\n\n{context}\n\n"
                f"— termine ta réponse par la ligne : `— via Claude`"
            )
        _log_routing(prompt, route, raison)

    sys.exit(0)


if __name__ == "__main__":
    main()
