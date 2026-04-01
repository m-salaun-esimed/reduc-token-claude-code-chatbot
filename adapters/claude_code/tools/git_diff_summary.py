#!/usr/bin/env python3
"""
git_diff_summary.py — Transforme git diff en liste structurée lisible par Claude.

Sortie compacte :
  fichier.py  +create_profil  -old_func  ~modified_func
  slice.ts    +fetchProfils

Usage:
  python3 git_diff_summary.py              # diff non commité (working tree)
  python3 git_diff_summary.py HEAD~1       # depuis le dernier commit
  python3 git_diff_summary.py HEAD~3 HEAD  # entre deux refs
  python3 git_diff_summary.py --stat       # stats seulement (lignes +/-)
"""

import subprocess
import sys
import re
import argparse
from pathlib import Path

ROOT = Path(__file__).parent


def _git(args: list[str]) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT)] + args,
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except subprocess.CalledProcessError:
        return ""


def parse_diff(raw: str) -> list[dict]:
    """Parse un git diff unifié et retourne les changements par fichier."""
    files: dict[str, dict] = {}
    current = None

    for line in raw.splitlines():
        m = re.match(r'^\+\+\+ b/(.+)$', line)
        if m:
            current = m.group(1)
            files.setdefault(current, {"added": set(), "removed": set(), "lines_add": 0, "lines_del": 0})
            continue

        if current is None:
            continue

        # Stats lignes
        if line.startswith("+") and not line.startswith("+++"):
            files[current]["lines_add"] += 1
            content = line[1:]
            fn = _extract_fn_name(content)
            if fn:
                files[current]["added"].add(fn)

        elif line.startswith("-") and not line.startswith("---"):
            files[current]["lines_del"] += 1
            content = line[1:]
            fn = _extract_fn_name(content)
            if fn:
                files[current]["removed"].add(fn)

    # Classer les fonctions : +ajouté, -supprimé, ~modifié
    result = []
    for path, info in sorted(files.items()):
        added   = info["added"] - info["removed"]
        removed = info["removed"] - info["added"]
        modified = info["added"] & info["removed"]

        symbols = (
            [f"+{f}" for f in sorted(added)] +
            [f"-{f}" for f in sorted(removed)] +
            [f"~{f}" for f in sorted(modified)]
        )
        result.append({
            "file": path,
            "symbols": symbols,
            "lines_add": info["lines_add"],
            "lines_del": info["lines_del"],
        })
    return result


def _extract_fn_name(line: str) -> str | None:
    """Extrait le nom de fonction/méthode d'une ligne de code."""
    line = line.strip()
    # Python : def / async def
    m = re.match(r'(?:async\s+)?def\s+([a-zA-Z_]\w*)\s*\(', line)
    if m and not m.group(1).startswith("__"):
        return m.group(1)
    # TS : function foo / const foo = / export function foo
    m = re.match(r'(?:export\s+)?(?:async\s+)?(?:function\s+|const\s+)([a-zA-Z_]\w*)\s*[=(]', line)
    if m:
        return m.group(1)
    # TS : méthode de classe
    m = re.match(r'(?:async\s+|static\s+|public\s+|private\s+)*([a-zA-Z_]\w*)\s*\(', line)
    if m and m.group(1) not in ("if", "for", "while", "switch", "return", "class", "import"):
        return m.group(1)
    return None


def format_output(changes: list[dict], stat_only: bool = False) -> str:
    if not changes:
        return "Aucun changement détecté."

    lines = []
    for c in changes:
        fname = c["file"]
        add, rm = c["lines_add"], c["lines_del"]
        stat = f"(+{add}/-{rm})"

        if stat_only or not c["symbols"]:
            lines.append(f"{fname}  {stat}")
        else:
            sym_str = "  ".join(c["symbols"][:12])
            lines.append(f"{fname}  {sym_str}  {stat}")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ref_from", nargs="?", default=None, help="Ref de base (ex: HEAD~1)")
    parser.add_argument("ref_to",   nargs="?", default=None, help="Ref cible  (ex: HEAD)")
    parser.add_argument("--stat",   action="store_true",     help="Stats de lignes uniquement")
    args = parser.parse_args()

    if args.ref_from and args.ref_to:
        raw = _git(["diff", "--unified=3", args.ref_from, args.ref_to])
    elif args.ref_from:
        raw = _git(["diff", "--unified=3", args.ref_from])
    else:
        # Working tree non commité
        raw = _git(["diff", "--unified=3"])
        if not raw:
            raw = _git(["diff", "--unified=3", "--cached"])

    if not raw:
        print("Aucune différence trouvée.")
        sys.exit(0)

    changes = parse_diff(raw)
    print(format_output(changes, stat_only=args.stat))


if __name__ == "__main__":
    main()
