#!/usr/bin/env python3
"""
llm_cache.py — Interface LLM avec cache Redis à deux niveaux.

Niveau 1 : hash SHA256 exact → 0ms
Niveau 2 : embedding sémantique (nomic-embed-text) → ~50ms
Niveau 3 : LLM local Ollama → ~500ms + mise en cache

Cache Redis :
  - llm:cache:{sha256}  : réponse JSON
  - llm:embed:{sha256}  : vecteur embedding (JSON list)
  - llm:embeds          : set de tous les sha256 avec embedding
  - llm:history         : historique LIFO (100 entrées)

Usage CLI :
  python3 llm_cache.py "Explique le gisement RF"
  python3 llm_cache.py "Explique le gisement RF" --no-cache
  python3 llm_cache.py "Explique le gisement RF" --online --api-key sk-...
  python3 llm_cache.py --history
  python3 llm_cache.py --clear-cache
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone

import httpx
import redis

# ── Configuration ──────────────────────────────────────────────────────────────

REDIS_URL        = "redis://localhost:6379"
OLLAMA_URL       = "http://localhost:11435"
OLLAMA_MODEL     = "qwen2.5:3b"
EMBED_MODEL      = "nomic-embed-text"
EMBEDS_SET_KEY   = "llm:embeds"
CACHE_TTL_SEC    = 60 * 60 * 24 * 7   # 7 jours
EMBED_SIMILARITY_THRESHOLD = 0.92


# ── Redis ──────────────────────────────────────────────────────────────────────

def get_redis() -> redis.Redis:
    return redis.from_url(REDIS_URL, decode_responses=True)


def _prompt_hash(model: str, prompt: str) -> str:
    return hashlib.sha256(f"{model}::{prompt}".encode()).hexdigest()


def _entry_key(h: str) -> str:
    return f"llm:entry:{h}"


def _embed_key(h: str) -> str:
    return f"llm:embed:{h}"


def clear_cache() -> int:
    r = get_redis()
    keys = r.keys("llm:entry:*") + r.keys("llm:embed:*")
    if keys:
        r.delete(*keys)
    r.delete(EMBEDS_SET_KEY)
    return len(keys)


# ── Embedding ──────────────────────────────────────────────────────────────────

def get_embedding(prompt: str) -> list[float] | None:
    """Appelle Ollama nomic-embed-text, retourne le vecteur ou None si indisponible."""
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{OLLAMA_URL}/api/embeddings",
                json={"model": EMBED_MODEL, "prompt": prompt},
            )
            resp.raise_for_status()
            return resp.json().get("embedding")
    except Exception:
        return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def embed_search(prompt: str, model: str, threshold: float = EMBED_SIMILARITY_THRESHOLD) -> dict | None:
    """
    Cherche dans Redis une réponse sémantiquement proche.
    Retourne l'entrée cache si similarité > threshold, sinon None.
    """
    vec = get_embedding(prompt)
    if vec is None:
        return None

    r = get_redis()
    hashes = r.smembers(EMBEDS_SET_KEY)
    if not hashes:
        return None

    best_score = 0.0
    best_hash = None

    for h in hashes:
        raw_embed = r.get(_embed_key(h))
        if not raw_embed:
            continue
        stored_vec = json.loads(raw_embed)
        score = _cosine_similarity(vec, stored_vec)
        if score > best_score:
            best_score = score
            best_hash = h

    if best_score >= threshold and best_hash:
        raw_entry = r.get(_entry_key(best_hash))
        if raw_entry:
            entry = json.loads(raw_entry)
            entry["cached"] = True
            entry["cache_source"] = "embedding"
            entry["similarity"] = round(best_score, 4)
            return entry

    return None


def embed_store(prompt: str, model: str, entry: dict) -> None:
    """Stocke le vecteur embedding + l'entrée réponse pour les recherches futures."""
    vec = get_embedding(prompt)
    if vec is None:
        return
    h = _prompt_hash(model, prompt)
    r = get_redis()
    r.setex(_embed_key(h), CACHE_TTL_SEC, json.dumps(vec))
    r.setex(_entry_key(h), CACHE_TTL_SEC, json.dumps(entry, ensure_ascii=False))
    r.sadd(EMBEDS_SET_KEY, h)


# ── Ollama (local) ─────────────────────────────────────────────────────────────

def ask_ollama(prompt: str, model: str = OLLAMA_MODEL, stream: bool = True) -> tuple[str, int]:
    """Envoie un prompt à Ollama et retourne (réponse, nb_tokens)."""
    url = f"{OLLAMA_URL}/api/generate"
    payload = {"model": model, "prompt": prompt, "stream": stream}

    response_text = ""
    total_tokens = 0

    with httpx.Client(timeout=120.0) as client:
        with client.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                response_text += chunk.get("response", "")
                if chunk.get("done"):
                    total_tokens = chunk.get("eval_count", 0) + chunk.get("prompt_eval_count", 0)
                    break
                if stream:
                    print(chunk.get("response", ""), end="", flush=True)

    if stream:
        print()

    return response_text, total_tokens


# ── API en ligne (compatible OpenAI) ──────────────────────────────────────────

def ask_online(
    prompt: str,
    model: str = "mistral-small-latest",
    api_key: str = "",
    api_url: str = "https://api.mistral.ai/v1",
) -> tuple[str, int]:
    """Envoie un prompt à l'API Mistral cloud (ou compatible OpenAI)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(f"{api_url}/chat/completions", json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    text = data["choices"][0]["message"]["content"]
    tokens = data.get("usage", {}).get("total_tokens", 0)
    return text, tokens


# ── Interface principale ───────────────────────────────────────────────────────

def ask(
    prompt: str,
    model: str = OLLAMA_MODEL,
    use_cache: bool = True,
    online: bool = False,
    api_key: str = "",
    api_url: str = "https://api.mistral.ai/v1",
    online_model: str = "mistral-small-latest",
    embed_threshold: float = EMBED_SIMILARITY_THRESHOLD,
) -> dict:
    """
    Pose une question au LLM avec cache Redis à deux niveaux.

    Niveau 1 : embedding sémantique → ~50ms (couvre aussi les questions identiques, sim=1.0)
    Niveau 2 : LLM local → ~500ms + mise en cache embedding

    Retourne : { prompt, response, model, source, cached, cache_source, timestamp, tokens }
    """
    effective_model = online_model if online else model

    if use_cache:
        embed_hit = embed_search(prompt, effective_model, threshold=embed_threshold)
        if embed_hit:
            return embed_hit

    # Niveau 2 : appel LLM
    if online:
        response, tokens = ask_online(prompt, model=online_model, api_key=api_key, api_url=api_url)
        source = "online"
    else:
        response, tokens = ask_ollama(prompt, model=model)
        source = "ollama"

    entry = {
        "prompt": prompt,
        "response": response,
        "model": effective_model,
        "source": source,
        "cached": False,
        "cache_source": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tokens": tokens,
    }

    if use_cache:
        embed_store(prompt, effective_model, entry)

    return entry


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prompt", nargs="?", help="Question à poser au LLM")
    parser.add_argument("--model",            default=OLLAMA_MODEL,               help="Modèle Ollama")
    parser.add_argument("--no-cache",         action="store_true",                help="Ignorer le cache Redis")
    parser.add_argument("--online",           action="store_true",                help="Utiliser l'API cloud")
    parser.add_argument("--api-key",          default="",                         help="Clé API cloud")
    parser.add_argument("--api-url",          default="https://api.mistral.ai/v1",help="URL API cloud")
    parser.add_argument("--online-model",     default="mistral-small-latest",     help="Modèle cloud")
    parser.add_argument("--embed-threshold",  type=float, default=EMBED_SIMILARITY_THRESHOLD, help="Seuil similarité embedding (défaut: 0.92)")
    parser.add_argument("--clear-cache",      action="store_true",                help="Vider le cache embedding")
    args = parser.parse_args()

    if args.clear_cache:
        n = clear_cache()
        print(f"Cache vidé ({n} entrées supprimées).")
        return

    if not args.prompt:
        parser.print_help()
        sys.exit(1)

    result = ask(
        prompt=args.prompt,
        model=args.model,
        use_cache=not args.no_cache,
        online=args.online,
        api_key=args.api_key,
        api_url=args.api_url,
        online_model=args.online_model,
        embed_threshold=args.embed_threshold,
    )

    if result["cached"]:
        sim = result.get("similarity", "?")
        print(f"\n[cache embedding sim={sim} — {result['model']}]\n")
        print(result["response"])
    else:
        if args.online:
            print(result["response"])

    print(f"\n— {result['tokens']} tokens | {result['source']} | {'cache ✓' if result['cached'] else 'nouveau'}")


if __name__ == "__main__":
    main()
