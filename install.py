#!/usr/bin/env python3
"""
install.py — Configure automatiquement les hooks Claude Code dans ~/.claude/settings.json.

Usage :
  python3 install.py              # installe les hooks
  python3 install.py --check      # vérifie si les hooks sont déjà configurés
  python3 install.py --uninstall  # supprime les hooks reduc-token
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REDUC_TOKEN_DIR = Path(__file__).resolve().parent
SETTINGS_FILE   = Path.home() / ".claude" / "settings.json"

# Chemins absolus calculés depuis l'emplacement réel du script
_PYTHON = sys.executable
_HOOK   = str(REDUC_TOKEN_DIR / "adapters" / "claude_code" / "hook.py")
_SESS   = str(REDUC_TOKEN_DIR / "adapters" / "claude_code" / "tools" / "session_summary.py")
_CTX    = str(REDUC_TOKEN_DIR / "adapters" / "claude_code" / "context" / "session_context.md")

HOOKS_TO_INSTALL = {
    "UserPromptSubmit": {
        "hooks": [{"type": "command", "command": f"{_PYTHON} {_HOOK}", "async": False}]
    },
    "Stop": {
        "hooks": [{"type": "command", "command": f"{_PYTHON} {_SESS} --auto", "async": True}]
    },
    "SessionStart": {
        "hooks": [{"type": "command", "command": f"cat {_CTX} 2>/dev/null || echo 'Pas de contexte de session précédente.'", "async": False}]
    },
}

# Marqueur pour identifier les hooks installés par reduc-token
_MARKER = str(REDUC_TOKEN_DIR)


def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[reduc-token] Attention : {SETTINGS_FILE} est invalide, il sera réinitialisé.")
    return {}


def _save_settings(data: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _hook_already_installed(hook_list: list, marker: str) -> bool:
    """Vérifie si un hook reduc-token est déjà dans la liste."""
    for entry in hook_list:
        for h in entry.get("hooks", []):
            if marker in h.get("command", ""):
                return True
    return False


def check() -> bool:
    """Retourne True si tous les hooks sont configurés."""
    settings = _load_settings()
    hooks = settings.get("hooks", {})
    all_ok = True
    for event in HOOKS_TO_INSTALL:
        installed = _hook_already_installed(hooks.get(event, []), _MARKER)
        status = "✓" if installed else "✗"
        print(f"  {status} {event}")
        if not installed:
            all_ok = False
    return all_ok


def install() -> None:
    settings = _load_settings()
    hooks = settings.setdefault("hooks", {})
    changed = False

    for event, hook_block in HOOKS_TO_INSTALL.items():
        event_list = hooks.setdefault(event, [])
        if _hook_already_installed(event_list, _MARKER):
            print(f"  [déjà installé] {event}")
        else:
            event_list.append(hook_block)
            print(f"  [ajouté]        {event}")
            changed = True

    if changed:
        # Backup avant écriture
        if SETTINGS_FILE.exists():
            shutil.copy(SETTINGS_FILE, SETTINGS_FILE.with_suffix(".json.bak"))
        _save_settings(settings)
        print(f"\n✓ {SETTINGS_FILE} mis à jour (backup : settings.json.bak)")
    else:
        print("\n✓ Rien à faire, hooks déjà présents.")


def uninstall() -> None:
    settings = _load_settings()
    hooks = settings.get("hooks", {})
    changed = False

    for event in list(hooks.keys()):
        before = hooks[event]
        after = [
            entry for entry in before
            if not _hook_already_installed([entry], _MARKER)
        ]
        if len(after) != len(before):
            hooks[event] = after
            print(f"  [retiré] {event}")
            changed = True
        # Supprimer la clé si vide
        if not hooks[event]:
            del hooks[event]

    if changed:
        _save_settings(settings)
        print(f"\n✓ Hooks reduc-token supprimés de {SETTINGS_FILE}")
    else:
        print("Aucun hook reduc-token trouvé.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check",     action="store_true", help="Vérifie si les hooks sont configurés")
    parser.add_argument("--uninstall", action="store_true", help="Supprime les hooks reduc-token")
    args = parser.parse_args()

    print(f"reduc-token : {REDUC_TOKEN_DIR}")
    print(f"settings    : {SETTINGS_FILE}\n")

    if args.check:
        ok = check()
        sys.exit(0 if ok else 1)
    elif args.uninstall:
        uninstall()
    else:
        install()


if __name__ == "__main__":
    main()
