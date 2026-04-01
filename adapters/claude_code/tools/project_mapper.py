#!/usr/bin/env python3
"""
project_mapper.py — Analyse un projet Python/FastAPI + TypeScript/React et génère :
  - project_map.json  : arbre complet fichiers/classes/fonctions + signatures + descriptions
  - CLAUDE.md         : version compacte optimisée pour Claude Code (réduction tokens)

Usage:
  python project_mapper.py                    # analyse le répertoire courant
  python project_mapper.py /chemin/projet     # analyse un répertoire spécifique
  python project_mapper.py --md-only          # régénère uniquement CLAUDE.md depuis le JSON
  python project_mapper.py --output ./docs    # répertoire de sortie personnalisé

Langages supportés:
  - Python (.py)         : AST natif — fonctions, classes, décorateurs FastAPI
  - TypeScript (.ts)     : tree-sitter — fonctions, interfaces, types, exports
  - TSX (.tsx)           : tree-sitter — composants React, hooks, props
  - Config (.json, .yaml, .toml, .env) : extraction structure/clés
  - Scripts (.sh)        : extraction commentaires de section
"""

import ast
import json
import os
import sys
import re
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional

# ── Dépendances optionnelles ──────────────────────────────────────────────────
try:
    import tree_sitter_typescript as tsts
    from tree_sitter import Language, Parser as TSParser
    TS_AVAILABLE = True
except ImportError as _ts_err:
    TS_AVAILABLE = False
    _ts_err_msg = str(_ts_err)

# ── Configuration ─────────────────────────────────────────────────────────────
IGNORE_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "env", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".tox",
    "eggs", ".next", "coverage", ".turbo", "out"
}

SUPPORTED_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".sh"}
CONFIG_EXTENSIONS    = {".json", ".yaml", ".yml", ".toml", ".env"}

# Fichiers config importants à inclure (même sans parsing profond)
IMPORTANT_CONFIG_FILES = {
    "package.json", "tsconfig.json", "requirements.txt",
    "pyproject.toml", "docker-compose.yml", "docker-compose.yaml",
    ".env.example", "vite.config.ts", "vite.config.js"
}


# ══════════════════════════════════════════════════════════════════════════════
# PYTHON PARSER (AST natif)
# ══════════════════════════════════════════════════════════════════════════════

def _get_docstring(node: ast.AST) -> str:
    try:
        doc = ast.get_docstring(node)
        return doc.strip().split("\n")[0] if doc else ""
    except Exception:
        return ""


def _format_annotation(annotation) -> str:
    if annotation is None:
        return ""
    try:
        return ast.unparse(annotation)
    except Exception:
        return "?"


def _extract_py_calls(func_node) -> list[str]:
    """Extrait tous les noms de fonctions appelées dans un nœud fonction Python."""
    calls = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.append(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                try:
                    obj = ast.unparse(node.func.value)
                    calls.append(f"{obj}.{node.func.attr}")
                except Exception:
                    calls.append(node.func.attr)
    return list(dict.fromkeys(calls))[:25]


def _extract_py_import_aliases(tree) -> dict[str, str]:
    """Extrait {symbole_ou_alias: module_source} depuis les imports Python."""
    aliases: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                name = alias.asname or alias.name
                aliases[name] = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name
                aliases[name] = alias.name
    return aliases


def _get_decorators(node) -> list[str]:
    decorators = []
    for dec in getattr(node, "decorator_list", []):
        try:
            decorators.append(ast.unparse(dec))
        except Exception:
            pass
    return decorators


def _extract_py_function(node) -> dict:
    args = []
    defaults_offset = len(node.args.args) - len(node.args.defaults)
    for i, arg in enumerate(node.args.args):
        if arg.arg == "self":
            continue
        ann = _format_annotation(arg.annotation)
        param = f"{arg.arg}: {ann}" if ann else arg.arg
        di = i - defaults_offset
        if di >= 0:
            try:
                param += f" = {ast.unparse(node.args.defaults[di])}"
            except Exception:
                param += " = ?"
        args.append(param)

    if node.args.vararg:
        v = node.args.vararg
        ann = _format_annotation(v.annotation)
        args.append(f"*{v.arg}: {ann}" if ann else f"*{v.arg}")
    if node.args.kwarg:
        k = node.args.kwarg
        ann = _format_annotation(k.annotation)
        args.append(f"**{k.arg}: {ann}" if ann else f"**{k.arg}")

    ret = _format_annotation(node.returns)
    sig = f"{'async ' if isinstance(node, ast.AsyncFunctionDef) else ''}{node.name}({', '.join(args)})"
    if ret:
        sig += f" -> {ret}"

    decs = _get_decorators(node)
    # Marquer les routes FastAPI
    route_info = None
    for d in decs:
        m = re.match(r'(router|app)\.(get|post|put|delete|patch|websocket)\(["\']([^"\']+)', d)
        if m:
            route_info = f"{m.group(2).upper()} {m.group(3)}"
            break

    return {
        "name": node.name,
        "signature": sig,
        "description": _get_docstring(node),
        "line": node.lineno,
        "is_async": isinstance(node, ast.AsyncFunctionDef),
        "is_private": node.name.startswith("_") and not node.name.startswith("__"),
        "decorators": decs,
        "route": route_info,
        "calls": _extract_py_calls(node),
    }


def _extract_py_class(node: ast.ClassDef) -> dict:
    bases = []
    for base in node.bases:
        try:
            bases.append(ast.unparse(base))
        except Exception:
            pass

    methods = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods.append(_extract_py_function(child))

    return {
        "name": node.name,
        "bases": bases,
        "description": _get_docstring(node),
        "line": node.lineno,
        "methods": methods,
    }


def parse_python_file(filepath: Path, root: Path) -> dict:
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError as e:
        return {"path": str(filepath.relative_to(root)), "lang": "python", "error": str(e)}
    except Exception as e:
        return {"path": str(filepath.relative_to(root)), "lang": "python", "error": str(e)}

    functions, classes, imports = [], [], []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(_extract_py_function(node))
        elif isinstance(node, ast.ClassDef):
            classes.append(_extract_py_class(node))
        elif isinstance(node, ast.Import):
            imports += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)

    # Détecter le type de fichier FastAPI
    file_role = None
    src = source[:2000]
    if "FastAPI" in src or "APIRouter" in src:
        file_role = "fastapi-router" if "APIRouter" in src else "fastapi-app"
    elif "BaseModel" in src:
        file_role = "pydantic-models"
    elif "pytest" in src or "def test_" in src:
        file_role = "tests"

    return {
        "path": str(filepath.relative_to(root)),
        "lang": "python",
        "role": file_role,
        "description": _get_docstring(tree),
        "lines": source.count("\n") + 1,
        "imports": list(dict.fromkeys(imports))[:12],
        "import_aliases": _extract_py_import_aliases(tree),
        "classes": classes,
        "functions": functions,
    }


# ══════════════════════════════════════════════════════════════════════════════
# TYPESCRIPT / TSX PARSER (tree-sitter)
# ══════════════════════════════════════════════════════════════════════════════

_ts_parser_cache: dict = {}

def _get_ts_parser(is_tsx: bool):
    key = "tsx" if is_tsx else "ts"
    if key not in _ts_parser_cache and TS_AVAILABLE:
        lang = Language(tsts.language_tsx() if is_tsx else tsts.language_typescript())
        _ts_parser_cache[key] = TSParser(lang)
    return _ts_parser_cache.get(key)


def _extract_ts_import_aliases(src_text: str) -> dict[str, str]:
    """Extrait {alias: chemin_import} depuis les imports TypeScript."""
    aliases: dict[str, str] = {}
    # import * as X from 'Y'
    for m in re.finditer(r'import\s+\*\s+as\s+(\w+)\s+from\s+["\']([^"\']+)["\']', src_text):
        aliases[m.group(1)] = m.group(2)
    # import { a, b as c } from 'Y'
    for m in re.finditer(r'import\s+(?:type\s+)?\{([^}]+)\}\s+from\s+["\']([^"\']+)["\']', src_text):
        src = m.group(2)
        for item in m.group(1).split(','):
            item = item.strip()
            if not item:
                continue
            if ' as ' in item:
                aliases[item.split(' as ')[1].strip()] = src
            else:
                aliases[item.strip()] = src
    # import X from 'Y' (default import, pas type ni React)
    for m in re.finditer(r'import\s+(?!type\s+|\{|\*)(\w+)\s+from\s+["\']([^"\']+)["\']', src_text):
        if m.group(1) not in ('React',):
            aliases[m.group(1)] = m.group(2)
    return aliases


def _extract_ts_calls_from_node(body_node, source: bytes) -> list[str]:
    """Extrait les call_expression depuis un nœud tree-sitter."""
    if body_node is None:
        return []
    calls: list[str] = []

    def walk(n: object, depth: int = 0) -> None:
        if depth > 12:
            return
        if n.type == "call_expression":  # type: ignore[attr-defined]
            fn_node = n.child_by_field_name("function")  # type: ignore[attr-defined]
            if fn_node:
                if fn_node.type == "identifier":
                    calls.append(_node_text(fn_node, source))
                elif fn_node.type == "member_expression":
                    obj  = fn_node.child_by_field_name("object")
                    prop = fn_node.child_by_field_name("property")
                    if obj and prop:
                        calls.append(f"{_node_text(obj, source)}.{_node_text(prop, source)}")
        for child in n.named_children:  # type: ignore[attr-defined]
            walk(child, depth + 1)

    walk(body_node)
    return list(dict.fromkeys(calls))[:25]


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _find_jsdoc(source: bytes, line: int) -> str:
    """Cherche un commentaire JSDoc au-dessus d'une ligne."""
    lines = source.split(b"\n")
    idx = line - 2  # line est 1-indexed
    if idx < 0:
        return ""
    block = []
    i = idx
    while i >= 0 and (lines[i].strip().startswith(b"*") or
                       lines[i].strip().startswith(b"/**") or
                       lines[i].strip().startswith(b"*/")):
        block.insert(0, lines[i].strip().decode("utf-8", errors="replace"))
        i -= 1
    if not block:
        return ""
    text = " ".join(block).replace("/**", "").replace("*/", "").replace("*", "").strip()
    return text.split("\n")[0].strip()


def _extract_ts_params(params_node, source: bytes) -> str:
    """Extrait les paramètres d'une fonction TS."""
    if params_node is None:
        return ""
    params = []
    for child in params_node.named_children:
        t = child.type
        if t in ("required_parameter", "optional_parameter", "rest_parameter"):
            params.append(_node_text(child, source))
        elif t == "identifier":
            params.append(_node_text(child, source))
    return ", ".join(params)


def _walk_ts(node, source: bytes, results: dict, depth: int = 0):
    """Parcourt l'AST TypeScript et collecte fonctions/composants/interfaces/types/routes."""
    if depth > 8:
        return

    ntype = node.type

    # ── Fonction nommée ──────────────────────────────────────────────────────
    if ntype in ("function_declaration", "generator_function_declaration"):
        name_node = node.child_by_field_name("name")
        name = _node_text(name_node, source) if name_node else "?"
        params_node = node.child_by_field_name("parameters")
        params = _extract_ts_params(params_node, source)
        ret_node = node.child_by_field_name("return_type")
        ret = _node_text(ret_node, source).lstrip(": ") if ret_node else ""
        is_async = any(c.type == "async" for c in node.children)
        sig = f"{'async ' if is_async else ''}{name}({params})"
        if ret:
            sig += f": {ret}"
        body_node = node.child_by_field_name("body")
        results["functions"].append({
            "name": name,
            "signature": sig,
            "description": _find_jsdoc(source, node.start_point[0] + 1),
            "line": node.start_point[0] + 1,
            "is_private": name.startswith("_"),
            "calls": _extract_ts_calls_from_node(body_node, source),
        })

    # ── Variable avec arrow function ─────────────────────────────────────────
    elif ntype == "lexical_declaration":
        for child in node.named_children:
            if child.type == "variable_declarator":
                name_node = child.child_by_field_name("name")
                val_node  = child.child_by_field_name("value")
                if name_node and val_node and val_node.type in (
                    "arrow_function", "function", "async_arrow_function"
                ):
                    name = _node_text(name_node, source)
                    params_node = val_node.child_by_field_name("parameters")
                    params = _extract_ts_params(params_node, source) if params_node else ""
                    ret_node = val_node.child_by_field_name("return_type")
                    ret = _node_text(ret_node, source).lstrip(": ") if ret_node else ""
                    is_async = any(c.type == "async" for c in val_node.children)
                    sig = f"{'async ' if is_async else ''}{name}({params})"
                    if ret:
                        sig += f": {ret}"
                    # Déterminer si c'est un composant React (majuscule + JSX)
                    txt = _node_text(val_node, source)
                    is_component = name[0].isupper() and ("<" in txt or "JSX" in ret)
                    body_node2 = val_node.child_by_field_name("body")
                    entry = {
                        "name": name,
                        "signature": sig,
                        "description": _find_jsdoc(source, node.start_point[0] + 1),
                        "line": node.start_point[0] + 1,
                        "is_private": name.startswith("_"),
                        "calls": _extract_ts_calls_from_node(body_node2, source),
                    }
                    if is_component:
                        results["components"].append(entry)
                    else:
                        results["functions"].append(entry)

    # ── Interface ────────────────────────────────────────────────────────────
    elif ntype == "interface_declaration":
        name_node = node.child_by_field_name("name")
        name = _node_text(name_node, source) if name_node else "?"
        body_node = node.child_by_field_name("body")
        fields = []
        if body_node:
            for prop in body_node.named_children:
                if prop.type in ("property_signature", "method_signature"):
                    fields.append(_node_text(prop, source).strip())
        results["interfaces"].append({
            "name": name,
            "fields": fields[:10],
            "description": _find_jsdoc(source, node.start_point[0] + 1),
            "line": node.start_point[0] + 1,
        })

    # ── Type alias ───────────────────────────────────────────────────────────
    elif ntype == "type_alias_declaration":
        name_node = node.child_by_field_name("name")
        val_node  = node.child_by_field_name("value")
        name = _node_text(name_node, source) if name_node else "?"
        val  = _node_text(val_node, source)[:80] if val_node else ""
        results["types"].append({
            "name": name,
            "definition": val,
            "line": node.start_point[0] + 1,
        })

    # ── Classe ───────────────────────────────────────────────────────────────
    elif ntype == "class_declaration":
        name_node = node.child_by_field_name("name")
        name = _node_text(name_node, source) if name_node else "?"
        methods = []
        body_node = node.child_by_field_name("body")
        if body_node:
            for child in body_node.named_children:
                if child.type == "method_definition":
                    mn = child.child_by_field_name("name")
                    mp = child.child_by_field_name("parameters")
                    mname  = _node_text(mn, source) if mn else "?"
                    mparams = _extract_ts_params(mp, source) if mp else ""
                    is_async = any(c.type == "async" for c in child.children)
                    msig = f"{'async ' if is_async else ''}{mname}({mparams})"
                    methods.append({
                        "name": mname,
                        "signature": msig,
                        "description": _find_jsdoc(source, child.start_point[0] + 1),
                        "line": child.start_point[0] + 1,
                        "is_private": mname.startswith("_") and mname != "__init__",
                    })
        results["classes"].append({
            "name": name,
            "description": _find_jsdoc(source, node.start_point[0] + 1),
            "line": node.start_point[0] + 1,
            "methods": methods,
        })

    # Récursion
    for child in node.named_children:
        _walk_ts(child, source, results, depth + 1)


def parse_ts_file(filepath: Path, root: Path) -> dict:
    is_tsx = filepath.suffix == ".tsx"
    parser = _get_ts_parser(is_tsx)
    if parser is None:
        return {
            "path": str(filepath.relative_to(root)),
            "lang": "tsx" if is_tsx else "ts",
            "error": "tree-sitter not available — run: pip install tree-sitter tree-sitter-typescript"
        }

    try:
        source = filepath.read_bytes()
        tree = parser.parse(source)
    except Exception as e:
        return {"path": str(filepath.relative_to(root)), "lang": "tsx" if is_tsx else "ts", "error": str(e)}

    results = {
        "functions": [],
        "components": [],
        "classes": [],
        "interfaces": [],
        "types": [],
    }
    _walk_ts(tree.root_node, source, results)

    # Détecter imports/exports principaux
    src_text = source.decode("utf-8", errors="replace")
    imports = re.findall(r'import\s+.*?\s+from\s+["\']([^"\']+)["\']', src_text)
    imports = list(dict.fromkeys(imports))[:12]
    import_aliases = _extract_ts_import_aliases(src_text)

    # Détecter le rôle du fichier
    role = None
    if is_tsx:
        if results["components"]:
            role = "react-component"
        elif "page" in filepath.stem.lower():
            role = "page"
    else:
        if "router" in filepath.stem.lower() or "route" in filepath.stem.lower():
            role = "router"
        elif "store" in filepath.stem.lower() or "slice" in filepath.stem.lower():
            role = "state"
        elif "api" in filepath.stem.lower() or "service" in filepath.stem.lower():
            role = "api-service"
        elif "hook" in filepath.stem.lower() or filepath.stem.startswith("use"):
            role = "hook"
        elif "types" in filepath.stem.lower() or "interface" in filepath.stem.lower():
            role = "types"

    # Chercher le titre JSDoc du module
    module_doc = ""
    m = re.match(r'/\*\*\s*(.*?)\s*\*/', src_text[:500], re.DOTALL)
    if m:
        module_doc = m.group(1).replace("*", "").strip().split("\n")[0].strip()

    return {
        "path": str(filepath.relative_to(root)),
        "lang": "tsx" if is_tsx else "ts",
        "role": role,
        "description": module_doc,
        "lines": src_text.count("\n") + 1,
        "imports": imports,
        "import_aliases": import_aliases,
        **results,
    }


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG / MISC PARSERS
# ══════════════════════════════════════════════════════════════════════════════

def parse_package_json(filepath: Path, root: Path) -> dict:
    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
        scripts = data.get("scripts", {})
        deps = list(data.get("dependencies", {}).keys())[:15]
        dev_deps = list(data.get("devDependencies", {}).keys())[:10]
        return {
            "path": str(filepath.relative_to(root)),
            "lang": "json-config",
            "role": "package-config",
            "description": data.get("description", ""),
            "name": data.get("name", ""),
            "version": data.get("version", ""),
            "scripts": scripts,
            "dependencies": deps,
            "dev_dependencies": dev_deps,
        }
    except Exception as e:
        return {"path": str(filepath.relative_to(root)), "lang": "json-config", "error": str(e)}


def parse_requirements_txt(filepath: Path, root: Path) -> dict:
    try:
        lines = filepath.read_text(encoding="utf-8").splitlines()
        deps = [l.strip() for l in lines if l.strip() and not l.startswith("#")]
        return {
            "path": str(filepath.relative_to(root)),
            "lang": "requirements",
            "role": "python-deps",
            "dependencies": deps[:30],
        }
    except Exception as e:
        return {"path": str(filepath.relative_to(root)), "lang": "requirements", "error": str(e)}


def parse_shell_file(filepath: Path, root: Path) -> dict:
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        # Extraire les commentaires de section (## ...)
        sections = re.findall(r'^##\s+(.+)$', source, re.MULTILINE)
        # Extraire les fonctions shell
        funcs = re.findall(r'^(\w+)\s*\(\s*\)\s*\{', source, re.MULTILINE)
        desc = ""
        m = re.match(r'^#!.*\n#\s*(.+)', source)
        if m:
            desc = m.group(1).strip()
        return {
            "path": str(filepath.relative_to(root)),
            "lang": "shell",
            "description": desc,
            "sections": sections[:10],
            "functions": [{"name": f, "signature": f"()", "description": ""} for f in funcs],
        }
    except Exception as e:
        return {"path": str(filepath.relative_to(root)), "lang": "shell", "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# SCAN DU PROJET
# ══════════════════════════════════════════════════════════════════════════════

_CACHE_FILE = ".project_mapper_cache.json"


def _load_mtime_cache(root: Path) -> dict[str, float]:
    cache_path = root / _CACHE_FILE
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_mtime_cache(root: Path, mtimes: dict[str, float]) -> None:
    (root / _CACHE_FILE).write_text(json.dumps(mtimes, indent=2), encoding="utf-8")


def _load_existing_files(json_path: Path) -> dict[str, dict]:
    """Charge project_map.json existant et retourne {path: file_data}."""
    if not json_path.exists():
        return {}
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        return {f["path"]: f for f in data.get("files", [])}
    except Exception:
        return {}


def scan_project(root: Path, incremental: bool = False, json_path: Path | None = None) -> dict:
    files = []
    skipped = 0
    reparsed = 0

    mtime_cache: dict[str, float] = _load_mtime_cache(root) if incremental else {}
    new_mtimes:  dict[str, float] = {}
    cached_files: dict[str, dict] = _load_existing_files(json_path) if (incremental and json_path) else {}

    all_files = sorted(root.rglob("*"))
    for filepath in all_files:
        if not filepath.is_file():
            continue

        parts = set(filepath.relative_to(root).parts)
        if parts & IGNORE_DIRS:
            skipped += 1
            continue

        ext  = filepath.suffix.lower()
        name = filepath.name
        rel  = str(filepath.relative_to(root))

        # Mode incrémental : réutiliser le cache si fichier inchangé
        mtime = filepath.stat().st_mtime
        new_mtimes[rel] = mtime
        if incremental and rel in mtime_cache and mtime_cache[rel] == mtime and rel in cached_files:
            files.append(cached_files[rel])
            continue

        reparsed += 1
        # Fichiers config spéciaux
        if name == "package.json":
            files.append(parse_package_json(filepath, root))
        elif name == "requirements.txt":
            files.append(parse_requirements_txt(filepath, root))
        # Python
        elif ext == ".py":
            if name == "__init__.py":
                skipped += 1
                continue
            files.append(parse_python_file(filepath, root))
        # TypeScript / React
        elif ext in (".ts", ".tsx"):
            files.append(parse_ts_file(filepath, root))
        # Shell
        elif ext == ".sh":
            files.append(parse_shell_file(filepath, root))
        else:
            skipped += 1
            reparsed -= 1
            continue

    if incremental:
        _save_mtime_cache(root, new_mtimes)
        print(f"   ↻ {reparsed} fichiers re-parsés / {len(files)} total (cache: {len(files) - reparsed})")

    # Stats
    total_fn = 0
    total_cls = 0
    total_comp = 0
    for f in files:
        total_fn  += len(f.get("functions", []))
        total_cls += len(f.get("classes", []))
        total_comp += len(f.get("components", []))

    data = {
        "project": root.name,
        "root": str(root),
        "generated_at": datetime.now().isoformat(),
        "stats": {
            "total_files": len(files),
            "skipped": skipped,
            "total_classes": total_cls,
            "total_functions": total_fn,
            "total_components": total_comp,
        },
        "files": files,
    }
    data["relations"] = build_relations(data)
    return data


# ══════════════════════════════════════════════════════════════════════════════
# RELATIONS INTER-FICHIERS
# ══════════════════════════════════════════════════════════════════════════════

_NOISY_FN_NAMES = {"then", "catch", "finally", "get", "post", "put", "patch", "delete", "request"}

def _is_noisy_call(to_fn: str) -> bool:
    """Retourne True si l'appel est du bruit (template literal, chain axios, méthode HTTP nue)."""
    if "`" in to_fn or "(" in to_fn or ")" in to_fn:
        return True
    if to_fn in _NOISY_FN_NAMES:
        return True
    # Générique TS : get<Type>(...) capturé comme "get<Type>"
    if re.match(r'^(get|post|put|patch|delete)<', to_fn):
        return True
    return False


def build_relations(data: dict) -> dict:
    """Construit les dépendances fichier→fichier et les appels inter-fichiers."""
    files = data["files"]

    # Index : stem → [chemins fichiers]
    stem_index: dict[str, list[str]] = {}
    for f in files:
        stem = Path(f["path"]).stem
        stem_index.setdefault(stem, []).append(f["path"])

    # Index : nom_fonction → [chemins fichiers]
    fn_index: dict[str, list[str]] = {}
    for f in files:
        all_fns = list(f.get("functions", []))
        for cls in f.get("classes", []):
            all_fns.extend(cls.get("methods", []))
        for fn in all_fns:
            fn_index.setdefault(fn["name"], []).append(f["path"])

    def resolve_ts(imp: str, from_file: str) -> str | None:
        if not imp.startswith("."):
            return None
        stem = Path(imp).name.split(".")[0]
        from_dir = str(Path(from_file).parent)
        matches = stem_index.get(stem, [])
        if not matches:
            return None
        # Préférer le fichier dans le même sous-dossier
        for m in matches:
            if from_dir in m:
                return m
        return matches[0]

    def resolve_py(imp: str, from_file: str) -> str | None:
        stem = imp.split(".")[-1]
        from_prefix = from_file.split("/")[0]  # ex: api-goniometrie
        matches = stem_index.get(stem, [])
        if not matches:
            return None
        for m in matches:
            if m.startswith(from_prefix):
                return m
        return matches[0]

    file_deps: dict[str, list[str]] = {}
    fn_calls: list[dict] = []
    seen_calls: set[tuple] = set()

    for f in files:
        fp = f["path"]
        lang = f.get("lang", "")
        aliases = f.get("import_aliases", {})
        is_ts = lang in ("ts", "tsx")

        # Dépendances fichier→fichier
        deps: set[str] = set()
        resolved_aliases: dict[str, str] = {}
        for alias, imp_path in aliases.items():
            target = resolve_ts(imp_path, fp) if is_ts else resolve_py(imp_path, fp)
            if target and target != fp:
                deps.add(target)
                resolved_aliases[alias] = target

        if deps:
            file_deps[fp] = sorted(deps)

        # Appels inter-fichiers (niveau fonction)
        all_fns = list(f.get("functions", []))
        for cls in f.get("classes", []):
            all_fns.extend(cls.get("methods", []))

        for fn in all_fns:
            for call in fn.get("calls", []):
                parts = call.split(".", 1)
                if len(parts) == 2:
                    prefix, method = parts
                    target_file = resolved_aliases.get(prefix)
                    if target_file:
                        key = (fp, fn["name"], target_file, method)
                        if key not in seen_calls and not _is_noisy_call(method):
                            seen_calls.add(key)
                            fn_calls.append({
                                "from_file": fp,
                                "from_fn": fn["name"],
                                "to_file": target_file,
                                "to_fn": method,
                            })
                else:
                    # Appel direct d'un symbole importé
                    target_file = resolved_aliases.get(call)
                    if target_file and not _is_noisy_call(call):
                        key = (fp, fn["name"], target_file, call)
                        if key not in seen_calls:
                            seen_calls.add(key)
                            fn_calls.append({
                                "from_file": fp,
                                "from_fn": fn["name"],
                                "to_file": target_file,
                                "to_fn": call,
                            })

    return {"file_deps": file_deps, "fn_calls": fn_calls}


def _render_relations(relations: dict, lines: list) -> None:
    """Ajoute la section relations dans le CLAUDE.md."""
    file_deps = relations.get("file_deps", {})
    fn_calls  = relations.get("fn_calls", [])

    if not file_deps and not fn_calls:
        return

    lines.append("## 🔗 Relations inter-fichiers")
    lines.append("")

    if file_deps:
        lines.append("### Dépendances fichier → fichier")
        for from_f, to_files in sorted(file_deps.items()):
            from_name = Path(from_f).name
            to_names  = [Path(t).name for t in to_files]
            lines.append(f"- `{from_name}` → {', '.join(f'`{n}`' for n in to_names)}")
        lines.append("")

    if fn_calls:
        lines.append("### Appels inter-fichiers")
        lines.append("| Appelant | Fonction | → | Cible | Fonction |")
        lines.append("|----------|----------|----|-------|----------|")
        for call in fn_calls:
            from_name = Path(call["from_file"]).name
            to_name   = Path(call["to_file"]).name
            lines.append(
                f"| `{from_name}` | `{call['from_fn']}` | → | `{to_name}` | `{call['to_fn']}` |"
            )
        lines.append("")


# ══════════════════════════════════════════════════════════════════════════════
# GÉNÉRATION CLAUDE.md
# ══════════════════════════════════════════════════════════════════════════════

ROLE_EMOJI = {
    "fastapi-app": "🚀",
    "fastapi-router": "🔌",
    "pydantic-models": "📐",
    "tests": "🧪",
    "react-component": "⚛️",
    "page": "📄",
    "router": "🔀",
    "state": "🗃️",
    "api-service": "🌐",
    "hook": "🪝",
    "types": "📝",
    "package-config": "📦",
    "python-deps": "📦",
    "shell": "⚙️",
}


def generate_claude_md_compact(data: dict, output_path: Path) -> None:
    """Génère CLAUDE.md — version ultra-compacte (chargée à chaque prompt)."""
    lines: list[str] = []
    stats = data["stats"]
    lines.append(f"# {data['project']} — Index")
    lines.append(f"> {data['generated_at'][:10]} | {stats['total_files']} fichiers | {stats['total_functions']} fonctions")
    lines.append("")

    folders: dict[str, list] = {}
    for f in data["files"]:
        folder = str(Path(f["path"]).parent)
        folders.setdefault(folder, []).append(f)

    for folder in sorted(folders.keys()):
        folder_label = folder if folder != "." else "(root)"
        lines.append(f"## `{folder_label}/`")
        for f in sorted(folders[folder], key=lambda x: x["path"]):
            if "error" in f:
                continue
            fname = Path(f["path"]).name
            role  = f.get("role", "")
            role_str = f" [{role}]" if role else ""
            lang  = f.get("lang", "")

            if lang == "json-config":
                deps = f.get("dependencies", [])
                lines.append(f"- `{fname}`{role_str} → {', '.join(deps[:8])}")
            elif lang == "requirements":
                deps = [d.split("==")[0] for d in f.get("dependencies", [])]
                lines.append(f"- `{fname}`{role_str} → {', '.join(deps[:8])}")
            elif lang == "shell":
                lines.append(f"- `{fname}`{role_str}")
            else:
                # Fonctions publiques avec numéros de ligne
                pub_fns_data = [fn for fn in f.get("functions", []) if not fn.get("is_private")]
                pub_fns = [
                    f"{fn['name']}(L{fn['line']})" if fn.get("line") else fn["name"]
                    for fn in pub_fns_data
                ]
                # Routes (priorité, sans numéros de ligne — pas pertinent)
                routes = [fn.get("route") for fn in f.get("functions", []) if fn.get("route")]
                # Classes
                cls_names = [cls["name"] for cls in f.get("classes", [])]
                symbols = routes if routes else (pub_fns + cls_names)
                sym_str = ", ".join(f"`{s}`" for s in symbols[:10])
                lines.append(f"- `{fname}`{role_str}" + (f" → {sym_str}" if sym_str else ""))
        lines.append("")

    # Relations fichier→fichier uniquement (pas les appels)
    file_deps = data.get("relations", {}).get("file_deps", {})
    if file_deps:
        lines.append("## Dépendances")
        for from_f, to_files in sorted(file_deps.items()):
            from_name = Path(from_f).name
            to_names  = [Path(t).name for t in to_files]
            lines.append(f"- `{from_name}` → {', '.join(f'`{n}`' for n in to_names)}")
        lines.append("")

    lines.append("*Détail complet : voir `CLAUDE.md.detail` — Régénérer : `python3 project_mapper.py`*")
    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ CLAUDE.md (compact) → {output_path}")


def generate_claude_md(data: dict, output_path: Path):
    lines = []

    # Préfixe statique : contenu de CLAUDE.md.bak s'il existe
    header_path = output_path.parent / "CLAUDE.md.bak"
    if header_path.exists():
        lines.append(header_path.read_text(encoding="utf-8").rstrip())
        lines.append("")
        lines.append("---")
        lines.append("")

    stats = data["stats"]

    lines.append(f"# {data['project']} — Project Map")
    lines.append(
        f"> Generated: {data['generated_at'][:10]} | "
        f"{stats['total_files']} files | "
        f"{stats['total_functions']} functions | "
        f"{stats['total_components']} components | "
        f"{stats['total_classes']} classes"
    )
    lines.append("")

    # Regrouper par dossier
    folders: dict[str, list] = {}
    for f in data["files"]:
        folder = str(Path(f["path"]).parent)
        folders.setdefault(folder, []).append(f)

    for folder in sorted(folders.keys()):
        folder_label = folder if folder != "." else "(root)"
        lines.append(f"## 📁 `{folder_label}/`")
        lines.append("")

        for f in sorted(folders[folder], key=lambda x: x["path"]):
            if "error" in f:
                lines.append(f"- ⚠️ `{Path(f['path']).name}` — parse error: {f['error']}")
                continue

            fname = Path(f["path"]).name
            role  = f.get("role", "")
            emoji = ROLE_EMOJI.get(role, "")
            desc  = f.get("description", "")
            lang  = f.get("lang", "")
            desc_str = f" — {desc}" if desc else ""
            role_str = f" `[{role}]`" if role else ""

            lines.append(f"### {emoji} `{fname}`{role_str}{desc_str}")

            # ── Package.json ────────────────────────────────────────────────
            if lang == "json-config":
                scripts = f.get("scripts", {})
                if scripts:
                    script_list = ", ".join(f"`{k}`" for k in list(scripts.keys())[:8])
                    lines.append(f"- scripts: {script_list}")
                deps = f.get("dependencies", [])
                if deps:
                    lines.append(f"- deps: {', '.join(deps[:10])}")

            # ── Requirements ────────────────────────────────────────────────
            elif lang == "requirements":
                deps = f.get("dependencies", [])
                if deps:
                    lines.append(f"- packages: {', '.join(deps[:12])}")

            # ── Shell ───────────────────────────────────────────────────────
            elif lang == "shell":
                for fn in f.get("functions", []):
                    lines.append(f"- `{fn['name']}()`")
                for sec in f.get("sections", []):
                    lines.append(f"- § {sec}")

            else:
                # ── Classes Python / TS ─────────────────────────────────────
                for cls in f.get("classes", []):
                    bases_str = f"({', '.join(cls['bases'])})" if cls.get("bases") else ""
                    cls_desc  = f" — {cls['description']}" if cls.get("description") else ""
                    lines.append(f"- **class {cls['name']}{bases_str}**{cls_desc}")
                    for m in cls.get("methods", []):
                        if m.get("is_private") and m["name"] not in ("__init__", "constructor"):
                            continue
                        m_desc = f" — {m['description']}" if m.get("description") else ""
                        lines.append(f"  - `{m['signature']}`{m_desc}")

                # ── Composants React ────────────────────────────────────────
                for comp in f.get("components", []):
                    comp_desc = f" — {comp['description']}" if comp.get("description") else ""
                    lines.append(f"- ⚛️ `{comp['signature']}`{comp_desc}")

                # ── Interfaces / Types TS ───────────────────────────────────
                for iface in f.get("interfaces", []):
                    iface_desc = f" — {iface['description']}" if iface.get("description") else ""
                    fields_str = ""
                    if iface.get("fields"):
                        fields_str = f" `{{ {'; '.join(iface['fields'][:4])} }}`"
                    lines.append(f"- **interface {iface['name']}**{fields_str}{iface_desc}")

                for typ in f.get("types", []):
                    lines.append(f"- **type {typ['name']}** = `{typ['definition'][:60]}`")

                # ── Fonctions ───────────────────────────────────────────────
                public_fns = [fn for fn in f.get("functions", []) if not fn.get("is_private")]
                for fn in public_fns:
                    fn_desc  = f" — {fn['description']}" if fn.get("description") else ""
                    route    = fn.get("route")
                    route_str = f" **`{route}`**" if route else ""
                    lines.append(f"- `{fn['signature']}`{route_str}{fn_desc}")

            lines.append("")

    _render_relations(data.get("relations", {}), lines)

    lines.append("---")
    lines.append("*Auto-generated — do not edit manually.*")
    lines.append("*Regenerate: `python project_mapper.py --md-only`*")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ CLAUDE.md → {output_path}")


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def _run_once(root: Path, out_dir: Path, incremental: bool = False) -> None:
    json_path   = out_dir / "project_map.json"
    md_compact  = out_dir / "CLAUDE.md"
    md_detail   = out_dir / "CLAUDE.md.detail"

    data = scan_project(root, incremental=incremental, json_path=json_path)

    s = data["stats"]
    print(f"   {s['total_files']} files | {s['total_functions']} fn | {s['total_classes']} cls")

    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"✅ project_map.json → {json_path}")

    generate_claude_md_compact(data, md_compact)
    generate_claude_md(data, md_detail)


def main():
    import time

    parser = argparse.ArgumentParser(
        description="Map a Python/FastAPI + TypeScript/React project",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("path",          nargs="?", default=".", help="Project root (default: .)")
    parser.add_argument("--md-only",     action="store_true",    help="Régénère CLAUDE.md depuis le JSON existant")
    parser.add_argument("--output",      default=None,           help="Répertoire de sortie (défaut: racine projet)")
    parser.add_argument("--watch",       action="store_true",    help="Surveillance continue (mode incrémental)")
    parser.add_argument("--interval",    type=int, default=10,   help="Intervalle watch en secondes (défaut: 10)")
    args = parser.parse_args()

    root = Path(args.path).resolve()
    if not root.exists():
        print(f"❌ Path not found: {root}")
        sys.exit(1)

    out_dir   = Path(args.output).resolve() if args.output else root
    json_path = out_dir / "project_map.json"
    md_path   = out_dir / "CLAUDE.md"

    if not TS_AVAILABLE:
        print("⚠️  tree-sitter not found — TypeScript parsing disabled.")
        print("   Fix: pip install tree-sitter tree-sitter-typescript\n")

    if args.md_only:
        if not json_path.exists():
            print(f"❌ project_map.json not found at {json_path}")
            sys.exit(1)
        data = json.loads(json_path.read_text(encoding="utf-8"))
        generate_claude_md_compact(data, md_path)
        generate_claude_md(data, out_dir / "CLAUDE.md.detail")
        return

    if args.watch:
        print(f"👀 Watch mode — intervalle {args.interval}s — Ctrl+C pour arrêter")
        try:
            while True:
                print(f"\n🔍 [{datetime.now().strftime('%H:%M:%S')}] Scanning {root} ...")
                _run_once(root, out_dir, incremental=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n⏹  Watch arrêté.")
        return

    print(f"🔍 Scanning {root} ...")
    _run_once(root, out_dir, incremental=False)


if __name__ == "__main__":
    main()