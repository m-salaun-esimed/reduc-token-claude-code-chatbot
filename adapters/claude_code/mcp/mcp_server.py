#!/usr/bin/env python3
"""
mcp_server.py — Serveur MCP (Model Context Protocol) pour reduc-token.

Expose les scripts comme outils MCP :
  - project_mapper  : analyse un projet et génère CLAUDE.md + project_map.json
  - session_summary : résumé git de la session courante
  - git_diff        : diff git structuré entre deux refs
  - llm_ask         : question au LLM (Mistral Ollama ou cloud) avec cache Redis
  - llm_history     : historique des échanges LLM
  - llm_clear_cache : vide le cache Redis

Usage :
  python3 mcp_server.py                  # stdio (Claude Code)
  python3 mcp_server.py --http 8080      # HTTP SSE (debug / autre client)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent  # reduc-token/
TOOLS_DIR = ROOT / "tools"
CACHE_DIR = ROOT / "cache"
CONTEXT_DIR = ROOT / "context"

# Ajouter cache/ au path pour l'import de llm_cache
sys.path.insert(0, str(CACHE_DIR))

# ── Routing Mistral vs Claude ──────────────────────────────────────────────────
import re
import unicodedata

# Règle 1 : ces patterns indiquent une question théorique → Mistral
_MISTRAL_PATTERNS = re.compile(
    r"\b(explique|definiti|comment fonctionne|c'?est quoi|qu'?est.?ce|"
    r"difference entre|exemple de|kesako|resum|tradui|c koi|ckoi|"
    r"qu est ce|pourquoi|comment ca|c'est quoi)\b",
    re.IGNORECASE,
)

# Règle 2 : contexte projet détecté → Claude obligatoire
_CLAUDE_PATTERNS = re.compile(
    r"\b(bug|fix|corrig|modifi|refactor|implement|ajoute|supprim|optimis|"
    r"migration|crash|erreur|exception|deploy|docker|commit|merge|"
    r"scanner\.py|gisement\.py|simulateur\.py|mesures|profils|"
    r"fastapi|sqlalchemy|redux|slice|router|endpoint)\b"
    r"|\.py\b|\.ts\b|\.tsx\b|\.json\b"   # extension de fichier → contexte projet
    r"|/[a-z_]+/[a-z_]",                  # chemin de fichier
    re.IGNORECASE,
)


def _normalize(prompt: str) -> str:
    """Normalise une question pour maximiser les cache hits.

    - Minuscules
    - Supprime accents
    - Supprime ponctuation sauf apostrophes
    - Collapse espaces multiples
    - Strip
    """
    p = prompt.lower()
    # Normalise les apostrophes
    p = p.replace("'", "'").replace("`", "'")
    # Supprime accents (NFD → ASCII)
    p = unicodedata.normalize("NFD", p)
    p = "".join(c for c in p if unicodedata.category(c) != "Mn")
    # Supprime ponctuation sauf apostrophe et lettres/chiffres/espaces
    p = re.sub(r"[^\w\s']", " ", p)
    # Collapse espaces
    p = re.sub(r"\s+", " ", p).strip()
    return p


def _route_prompt(prompt: str) -> str:
    """
    Retourne 'mistral' ou 'claude' selon 3 règles :
    1. Contexte projet détecté (fichier, fonction, action code) → Claude
    2. Question théorique/générale → Mistral
    3. Ambigu → Claude (plus sûr)
    """
    # Règle 1 : contexte projet → Claude (prioritaire)
    if _CLAUDE_PATTERNS.search(prompt):
        return "claude"
    # Règle 2 : question générale → Mistral
    if _MISTRAL_PATTERNS.search(prompt):
        return "mistral"
    # Règle 3 : ambigu → Claude
    return "claude"

# ── MCP protocol helpers ───────────────────────────────────────────────────────

def _send(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _error(code: int, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


# ── Tool definitions ───────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "project_mapper",
        "description": (
            "Analyse un projet Python/TypeScript et génère CLAUDE.md (index compact) "
            "et project_map.json (arbre complet). "
            "Retourne le contenu de CLAUDE.md généré."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Chemin absolu du projet à analyser (défaut: répertoire courant)",
                },
                "md_only": {
                    "type": "boolean",
                    "description": "Régénérer uniquement CLAUDE.md depuis le JSON existant",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "session_summary",
        "description": (
            "Génère un résumé de la session git courante : fichiers modifiés, "
            "fonctions touchées, commits récents. Écrit dans session_context.md."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": "Ref git de base (ex: HEAD~3, défaut: HEAD)",
                    "default": "HEAD",
                },
                "note": {
                    "type": "string",
                    "description": "Note manuelle à ajouter au résumé",
                    "default": "",
                },
                "path": {
                    "type": "string",
                    "description": "Chemin du projet (défaut: répertoire de mcp_server.py)",
                },
            },
        },
    },
    {
        "name": "git_diff",
        "description": (
            "Retourne un diff git structuré et lisible : fichiers modifiés, "
            "fonctions ajoutées/supprimées/modifiées, stats de lignes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref_from": {
                    "type": "string",
                    "description": "Ref de base (ex: HEAD~1). Optionnel.",
                },
                "ref_to": {
                    "type": "string",
                    "description": "Ref cible (ex: HEAD). Optionnel.",
                },
                "stat_only": {
                    "type": "boolean",
                    "description": "Stats de lignes uniquement",
                    "default": False,
                },
                "path": {
                    "type": "string",
                    "description": "Chemin du projet à analyser",
                },
            },
        },
    },
    {
        "name": "llm_ask",
        "description": (
            "Pose une question à Mistral (local via Ollama ou cloud). "
            "Utilise le cache Redis pour éviter les appels redondants. "
            "Retourne la réponse et les métadonnées (tokens, source, cached)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["prompt"],
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "La question à poser au LLM",
                },
                "model": {
                    "type": "string",
                    "description": "Modèle Ollama (défaut: mistral)",
                    "default": "mistral",
                },
                "no_cache": {
                    "type": "boolean",
                    "description": "Ignorer le cache Redis",
                    "default": False,
                },
                "online": {
                    "type": "boolean",
                    "description": "Utiliser l'API Mistral cloud",
                    "default": False,
                },
                "api_key": {
                    "type": "string",
                    "description": "Clé API pour le mode online",
                    "default": "",
                },
            },
        },
    },
    {
        "name": "llm_history",
        "description": "Retourne l'historique des N derniers échanges LLM stockés dans Redis.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "Nombre d'entrées à retourner (défaut: 10)",
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "llm_clear_cache",
        "description": "Vide le cache Redis et l'historique LLM.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "smart_ask",
        "description": (
            "Route automatiquement la question vers Mistral local (questions générales, définitions, explications) "
            "ou indique de rester sur Claude (questions sur le code du projet, bugs, implémentation). "
            "Utilise le cache Redis. Retourne la réponse + le LLM utilisé."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["prompt"],
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "La question à router automatiquement",
                },
            },
        },
    },
]


# ── Tool handlers ──────────────────────────────────────────────────────────────

def _run(cmd: list[str], cwd: Path | None = None) -> str:
    """Exécute une commande et retourne stdout ou stderr en cas d'erreur."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=str(cwd or ROOT), timeout=120,
        )
        return result.stdout or result.stderr or "(pas de sortie)"
    except subprocess.TimeoutExpired:
        return "Timeout : la commande a pris trop de temps."
    except Exception as e:
        return f"Erreur : {e}"


def handle_project_mapper(args: dict) -> str:
    project_path = args.get("path", "")
    md_only = args.get("md_only", False)

    cmd = [sys.executable, str(TOOLS_DIR / "project_mapper.py")]
    if md_only:
        cmd.append("--md-only")
    if project_path:
        cmd.append(project_path)

    # Forcer la sortie dans reduc-token/context/ (pas à la racine du projet)
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    cmd += ["--output", str(CONTEXT_DIR)]

    cwd = Path(project_path) if project_path else ROOT
    output = _run(cmd, cwd=cwd)

    claude_md = CONTEXT_DIR / "CLAUDE.md"
    if claude_md.exists():
        content = claude_md.read_text(encoding="utf-8")
        # Recopier CLAUDE.md à la racine du projet pour le chargement auto par Claude Code
        if project_path:
            (Path(project_path) / "CLAUDE.md").write_text(content, encoding="utf-8")
        return f"CLAUDE.md généré ({len(content)} chars) :\n\n{content[:3000]}{'…' if len(content) > 3000 else ''}"
    return output


def handle_session_summary(args: dict) -> str:
    project_path = args.get("path", "")
    since = args.get("since", "HEAD")
    note = args.get("note", "")

    cmd = [sys.executable, str(TOOLS_DIR / "session_summary.py"), "--since", since]
    if note:
        cmd += ["--append", note]

    cwd = Path(project_path) if project_path else ROOT
    _run(cmd, cwd=cwd)

    output_file = CONTEXT_DIR / "session_context.md"
    if output_file.exists():
        return output_file.read_text(encoding="utf-8")
    return "session_context.md non trouvé."


def handle_git_diff(args: dict) -> str:
    project_path = args.get("path", "")
    ref_from = args.get("ref_from", "")
    ref_to = args.get("ref_to", "")
    stat_only = args.get("stat_only", False)

    cmd = [sys.executable, str(TOOLS_DIR / "git_diff_summary.py")]
    if ref_from:
        cmd.append(ref_from)
    if ref_to:
        cmd.append(ref_to)
    if stat_only:
        cmd.append("--stat")

    cwd = Path(project_path) if project_path else ROOT
    return _run(cmd, cwd=cwd)


def handle_llm_ask(args: dict) -> str:
    try:
        from llm_cache import ask
    except ImportError:
        return "Erreur : llm_cache.py introuvable ou dépendances manquantes (pip install redis httpx)."

    try:
        result = ask(
            prompt=args["prompt"],
            model=args.get("model", "mistral"),
            use_cache=not args.get("no_cache", False),
            online=args.get("online", False),
            api_key=args.get("api_key", ""),
        )
        cached_label = "cache Redis ✓" if result["cached"] else "nouveau"
        return (
            f"{result['response']}\n\n"
            f"— {result['tokens']} tokens | {result['model']} | {result['source']} | {cached_label}"
        )
    except Exception as e:
        return f"Erreur LLM : {e}"


def handle_llm_history(args: dict) -> str:
    try:
        from llm_cache import get_history
    except ImportError:
        return "Erreur : llm_cache.py introuvable."

    n = args.get("n", 10)
    entries = get_history(n)
    if not entries:
        return "Aucun historique."

    lines = []
    for e in entries:
        ts = e.get("timestamp", "")[:16].replace("T", " ")
        src = e.get("source", "?")
        tok = e.get("tokens", 0)
        cached = "cache" if e.get("cached") else src
        lines.append(f"[{ts}] {e['model']} ({cached}) — {tok} tokens")
        lines.append(f"  Q: {e['prompt'][:100]}")
        lines.append(f"  R: {e['response'][:200]}…\n")
    return "\n".join(lines)


def handle_smart_ask(args: dict) -> str:
    prompt = args["prompt"]
    normalized = _normalize(prompt)
    target = _route_prompt(prompt)  # routing sur prompt original (meilleur signal)

    if target == "claude":
        return (
            f"[smart_ask → Claude | raison: contexte projet détecté]\n"
            f"Réponds directement avec tes outils habituels.\n\n"
            f"Question : {prompt}"
        )

    # Mistral local — on passe la version normalisée pour maximiser les cache hits
    try:
        from llm_cache import ask
        result = ask(prompt=normalized, use_cache=True)
        cached_label = "cache Redis ✓" if result["cached"] else "Mistral local"
        return (
            f"[smart_ask → {cached_label} | question normalisée: \"{normalized[:60]}…\"]\n\n"
            f"{result['response']}\n\n"
            f"— {result['tokens']} tokens | {result['model']}"
        )
    except Exception as e:
        return (
            f"[smart_ask → Mistral indisponible, réponds toi-même]\n"
            f"Erreur : {e}\n\nQuestion : {prompt}"
        )


def handle_llm_clear_cache(args: dict) -> str:
    try:
        from llm_cache import clear_cache
    except ImportError:
        return "Erreur : llm_cache.py introuvable."
    n = clear_cache()
    return f"Cache vidé : {n} entrées supprimées."


HANDLERS = {
    "project_mapper": handle_project_mapper,
    "session_summary": handle_session_summary,
    "git_diff": handle_git_diff,
    "llm_ask": handle_llm_ask,
    "llm_history": handle_llm_history,
    "llm_clear_cache": handle_llm_clear_cache,
    "smart_ask": handle_smart_ask,
}


# ── MCP stdio loop ─────────────────────────────────────────────────────────────

def run_stdio() -> None:
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError:
            _send({"jsonrpc": "2.0", "id": None, **_error(-32700, "Parse error")})
            continue

        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {})

        # initialize
        if method == "initialize":
            _send({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "reduc-token-mcp", "version": "1.0.0"},
                    "capabilities": {"tools": {}},
                },
            })

        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})

        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            handler = HANDLERS.get(tool_name)

            if not handler:
                _send({"jsonrpc": "2.0", "id": msg_id, **_error(-32601, f"Outil inconnu : {tool_name}")})
                continue

            try:
                result_text = handler(tool_args)
                _send({
                    "jsonrpc": "2.0", "id": msg_id,
                    "result": {"content": [{"type": "text", "text": result_text}]},
                })
            except Exception as e:
                _send({"jsonrpc": "2.0", "id": msg_id, **_error(-32603, str(e))})

        elif method == "notifications/initialized":
            pass  # ack silencieux

        else:
            _send({"jsonrpc": "2.0", "id": msg_id, **_error(-32601, f"Méthode inconnue : {method}")})


# ── HTTP SSE (debug) ───────────────────────────────────────────────────────────

def run_http(port: int) -> None:
    """Mode HTTP simple pour tester les outils sans client MCP."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass  # silencieux

        def do_GET(self):
            if self.path == "/tools":
                self._json({"tools": TOOLS})
            else:
                self._json({"status": "reduc-token MCP", "endpoints": ["/tools", "POST /call"]})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            tool_name = body.get("name", "")
            tool_args = body.get("arguments", {})
            handler = HANDLERS.get(tool_name)
            if not handler:
                self._json({"error": f"Outil inconnu : {tool_name}"}, 404)
                return
            try:
                result = handler(tool_args)
                self._json({"result": result})
            except Exception as e:
                self._json({"error": str(e)}, 500)

        def _json(self, obj: dict, code: int = 200):
            body = json.dumps(obj, ensure_ascii=False, indent=2).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    print(f"MCP HTTP debug sur http://localhost:{port}", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--http", type=int, metavar="PORT", help="Mode HTTP debug (ex: 8080)")
    args = parser.parse_args()

    if args.http:
        run_http(args.http)
    else:
        run_stdio()


if __name__ == "__main__":
    main()
