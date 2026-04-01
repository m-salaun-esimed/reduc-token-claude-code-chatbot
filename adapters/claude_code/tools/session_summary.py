#!/usr/bin/env python3
"""
session_summary.py — Résumé structuré de la session courante pour Claude Code.

Écrit dans session_context.md :
  - Fichiers modifiés (git diff) avec fonctions touchées
  - Décisions clés prises (extraites des commits récents)

Usage:
  python3 session_summary.py              # résumé depuis dernier commit
  python3 session_summary.py --since HEAD~3  # depuis 3 commits en arrière
  python3 session_summary.py --append "note"  # ajouter une note manuelle

Intégration Claude Code (settings.json) :
  "hooks": {
    "Stop": [{ "command": "python3 session_summary.py --auto" }]
  }
"""

import subprocess
import sys
import re
import argparse
from pathlib import Path
from datetime import datetime

ROOT    = Path(__file__).parent.parent  # reduc-token/
OUTPUT  = ROOT / "context" / "session_context.md"
MAPFILE = ROOT / "context" / "project_map.json"


def _git(args: list[str]) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT)] + args,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except subprocess.CalledProcessError:
        return ""


def _load_fn_index() -> dict[str, str]:
    """Charge project_map.json et retourne {nom_fonction: fichier}."""
    if not MAPFILE.exists():
        return {}
    import json
    data = json.loads(MAPFILE.read_text(encoding="utf-8"))
    index: dict[str, str] = {}
    for f in data.get("files", []):
        for fn in f.get("functions", []):
            index[fn["name"]] = f["path"]
        for cls in f.get("classes", []):
            for m in cls.get("methods", []):
                index[m["name"]] = f["path"]
    return index


def git_diff_summary(since: str = "HEAD") -> list[dict]:
    """Retourne la liste des fichiers modifiés avec les fonctions/classes touchées."""
    raw_diff = _git(["diff", since, "--unified=0"])
    if not raw_diff:
        raw_diff = _git(["diff", "--cached", "--unified=0"])
    if not raw_diff:
        return []

    files: dict[str, dict] = {}
    current_file = None

    for line in raw_diff.splitlines():
        # Nouveau fichier
        m = re.match(r'^\+\+\+ b/(.+)$', line)
        if m:
            current_file = m.group(1)
            if current_file not in files:
                files[current_file] = {"added": [], "removed": [], "fns": set()}
            continue

        if current_file is None:
            continue

        # Ligne ajoutée — chercher def/function/const
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            # Python def
            m = re.match(r'\s*(?:async\s+)?def\s+(\w+)\s*\(', content)
            if m:
                files[current_file]["fns"].add(f"+{m.group(1)}")
                continue
            # TS function / const arrow
            m = re.match(r'\s*(?:export\s+)?(?:async\s+)?(?:function\s+|const\s+)(\w+)', content)
            if m and m.group(1)[0].islower() or (m and m.group(1)[0].isupper()):
                files[current_file]["fns"].add(f"+{m.group(1)}")

        elif line.startswith("-") and not line.startswith("---"):
            content = line[1:]
            m = re.match(r'\s*(?:async\s+)?def\s+(\w+)\s*\(', content)
            if m:
                files[current_file]["fns"].add(f"-{m.group(1)}")

    return [
        {"file": path, "fns": sorted(info["fns"])}
        for path, info in sorted(files.items())
        if path and not path.endswith((".lock", ".json")) or info["fns"]
    ]


def recent_commits(n: int = 5) -> list[str]:
    log = _git(["log", f"-{n}", "--oneline", "--no-decorate"])
    return log.splitlines() if log else []


def generate_summary(since: str = "HEAD", note: str = "") -> str:
    lines = [
        f"# Session context",
        f"> Généré : {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    if note:
        lines += ["## Note de session", note, ""]

    # Git diff
    changes = git_diff_summary(since)
    if changes:
        lines.append("## Fichiers modifiés")
        for c in changes:
            fns_str = f" — {', '.join(c['fns'])}" if c["fns"] else ""
            lines.append(f"- `{c['file']}`{fns_str}")
        lines.append("")

    # Commits récents
    commits = recent_commits()
    if commits:
        lines.append("## Commits récents")
        for c in commits:
            lines.append(f"- {c}")
        lines.append("")

    # Statut git rapide
    status = _git(["status", "--short"])
    if status:
        lines.append("## Fichiers non commités")
        for line in status.splitlines()[:15]:
            lines.append(f"- `{line.strip()}`")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since",  default="HEAD", help="Base git ref (défaut: HEAD)")
    parser.add_argument("--append", default="",     help="Ajouter une note manuelle")
    parser.add_argument("--auto",   action="store_true", help="Mode silencieux (hook Claude)")
    args = parser.parse_args()

    summary = generate_summary(since=args.since, note=args.append)
    OUTPUT.write_text(summary, encoding="utf-8")

    if not args.auto:
        print(summary)
        print(f"✅ Écrit dans {OUTPUT}")


if __name__ == "__main__":
    main()
