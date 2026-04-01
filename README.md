# reduc-token — Réduction de tokens Claude via LLM local + cache Redis

Middleware générique d'optimisation LLM. Intercepte chaque prompt via un hook `UserPromptSubmit`, route les questions générales vers un LLM local (Ollama), détecte et exécute les actions app via embedding sémantique, et injecte un contexte compact pour les questions projet.

**Adaptable à n'importe quelle app** : seuls `config/intents.json` et `config/routing.json` sont à personnaliser.

---

## Gains estimés

| Optimisation | Tokens économisés | Mécanisme |
|---|---|---|
| **Routing → LLM local** | ~1 100 tokens/question générale | Context non injecté (session_context.md + CLAUDE.md) |
| **Lecture ciblée** (line numbers) | ~300–2 300 tokens/read | `offset/limit` au lieu de lire un fichier entier |
| **Cache embedding** | 100% tokens LLM | Similarité cosinus ≥ 0.92 → cache hit (~50ms) |

**Exemple de session (50 messages)** :
- 15 questions générales → ~16 500 tokens épargnés sur context injection
- 4 lectures ciblées (ex: `MesuresPage.tsx` 500 lignes → 50 lignes) → ~9 200 tokens épargnés
- **Total estimé : ~25 000 tokens/session** (~$0.075 au tarif Sonnet)

---

## Architecture & Flow

```
Prompt utilisateur
        │
        ▼
┌──────────────────────────────────────────────────────────┐
│  adapters/claude_code/hook.py  (UserPromptSubmit hook)   │
│                                                          │
│  0. ACTION ? (pré-filtre embedding sémantique)           │
│     └─ embedding(prompt) vs descriptions intents.json   │
│         ├─ similarité ≥ threshold (config/routing.json)  │
│         │   → core/classifier.py → qwen2.5:3b JSON mode  │
│         │   → core/executor.py → HTTP call direct        │
│         │   → "— via action directe"  (0 token Claude)   │
│         └─ pas d'action → continuer                      │
│                                                          │
│  1. ROUTING (config/routing.json + project_map.json)     │
│     ├─ chemin/extension + fichier projet → Claude        │
│     ├─ pattern question générale (configurable)  → LLM   │
│     ├─ identifiant projet reconnu       → Claude         │
│     └─ défaut                           → Claude         │
│                                                          │
│  2a. Route = LLM local                                   │
│      └─ cache/llm_cache.ask()                            │
│          ├─ Niveau 1 : embedding     → Redis (~50ms)     │
│          └─ Niveau 2 : Ollama local  → ~500ms            │
│             → print(réponse) + "— via IA locale"         │
│                                                          │
│  2b. Route = Claude                                      │
│      ├─ context périmé ? → session_summary.py            │
│      │   + code changé ? → project_mapper.py             │
│      ├─ injecter session_context.md (~300 tok)           │
│      │            + CLAUDE.md        (~800 tok)          │
│      └─ "— via Claude"                                   │
└──────────────────────────────────────────────────────────┘
        │
        ▼
   Claude Code traite avec contexte frais
```

### Pré-filtre action par embedding

Au lieu d'une regex sur les verbes (fragile, langue-dépendante), `hook.py` compare l'embedding du prompt aux **descriptions** de chaque intent dans `intents.json` :

```
embedding(prompt)  ──cosine──▶  embedding("Verrouiller une fréquence radio")  → sim=0.81 ≥ 0.72 → ACTION
                                embedding("Lister les profils de menace")      → sim=0.41
```

- Embeddings des descriptions mis en cache Redis 30 jours (`routing:action_embed:<hash>`)
- Threshold configurable dans `config/routing.json → action_embed_threshold`
- Language-agnostique — fonctionne en FR, EN, ou mélangé sans modification

### CLAUDE.md avec numéros de ligne

`project_mapper.py` enrichit `CLAUDE.md` avec les numéros de ligne des fonctions :

```
- `freq_lock.py` → `update_lock_azimuth(L50)`, `get_freq_locks(L43)`
- `signal_processor.py` → `compute_fft(L7)`, `peak_info(L61)`
```

Claude peut alors lire **uniquement les lignes nécessaires** :
```python
# Au lieu de : Read("freq_lock.py")  → 91 lignes = ~400 tokens
# Claude fait : Read("freq_lock.py", offset=50, limit=20) → ~90 tokens
```

### Cache Redis à 2 niveaux

```
Question normalisée
        │
        ├─ embedding similaire ?──── hit → réponse (~50ms)
        │   (nomic-embed-text,             (similarité cosinus ≥ 0.92)
        │    cosine similarity)             questions identiques : sim=1.0 → hit aussi
        │
        └─ miss → Ollama (qwen2.5:3b)
                   └─ store embedding → prochaines questions similaires
```

---

## Installation

### 1. Placer le dossier

Clone ou copie `reduc-token/` n'importe où sur ta machine. Deux conventions courantes :

```
# Option A — dans ton projet (monorepo)
mon-projet/
├── reduc-token/      ← ici
├── src/
└── ...

# Option B — dossier partagé entre plusieurs projets
~/tools/reduc-token/
```

L'emplacement n'a pas d'importance : tous les chemins sont résolus automatiquement depuis `Path(__file__).resolve()`.

### 2. Lancer Redis + Ollama

```bash
cd reduc-token
docker compose up -d

# Télécharger les modèles (première fois seulement)
docker exec -it reduc-token-ollama ollama pull qwen2.5:3b
docker exec -it reduc-token-ollama ollama pull nomic-embed-text
```

### 3. Installer les dépendances

```bash
pip install -r requirements.txt
```

### 4. Configurer les deux fichiers

**`config/intents.json`** — les actions de ton app (verbes, entités, routes HTTP) :

```json
{
  "base_url": "http://localhost:8000",
  "intents": [
    {
      "name": "lock_frequency",
      "type": "ACTION",
      "description": "Verrouiller une fréquence radio",
      "entities": [{"name": "frequency_mhz", "type": "float", "required": true}],
      "action": {"method": "POST", "url": "/freq-locks", "body": {"frequency_mhz": "{frequency_mhz}"}},
      "response_template": "Fréquence {frequency_mhz} MHz verrouillée."
    }
  ]
}
```

**`config/routing.json`** — patterns qui basculent vers le LLM local :

```json
{
  "local_llm_patterns": ["explique", "c'est quoi", "comment", "what is", "why"],
  "action_embed_threshold": 0.72
}
```

### 5. Choisir ton cas d'usage

---

## Cas 1 — Dev assistant Claude Code

Pour utiliser reduc-token dans Claude Code (hook `UserPromptSubmit`) :

```bash
python3 install.py        # configure ~/.claude/settings.json automatiquement
python3 install.py --check      # vérifie l'installation (exit 0/1)
python3 install.py --uninstall  # retire les hooks
```

`install.py` détecte les chemins absolus et merge les hooks dans `~/.claude/settings.json` sans écraser la config existante. Aucune édition manuelle.

---

## Cas 2 — Middleware FastAPI (chatbot existant)

Pour brancher reduc-token devant un chatbot FastAPI qui appelle déjà un LLM :

```python
# main.py de ton app FastAPI
import sys
from pathlib import Path
sys.path.insert(0, "/chemin/vers/reduc-token")

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
import json
from adapters.chatbot.bot import Chatbot

class ReducTokenMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, config_dir: Path, claude_api_key: str = "",
                 intercept_paths: list = None, message_field: str = "message"):
        super().__init__(app)
        self.bot = Chatbot(config_dir=config_dir, claude_api_key=claude_api_key)
        self.intercept_paths = intercept_paths or ["/chat"]
        self.message_field   = message_field

    async def dispatch(self, request: Request, call_next):
        if request.method != "POST" or request.url.path not in self.intercept_paths:
            return await call_next(request)

        raw = await request.body()
        try:
            message = json.loads(raw).get(self.message_field, "")
        except Exception:
            return await call_next(request)

        result = self.bot.ask(message)

        if result["source"] in ("action", "local"):
            # Court-circuit — le LLM réel n'est PAS appelé
            return JSONResponse({"response": result["response"], "source": result["source"]})

        # Laisser passer vers ton LLM existant — body intact
        async def receive():
            return {"type": "http.request", "body": raw}
        request._receive = receive
        return await call_next(request)


app = FastAPI()
app.add_middleware(
    ReducTokenMiddleware,
    config_dir=Path("reduc-token/config"),
    claude_api_key="sk-ant-...",       # optionnel si ton LLM gère le fallback
    intercept_paths=["/chat"],
    message_field="message",           # adapter au format de ton body JSON
)

@app.post("/chat")
async def chat(body: dict):
    # Atteint uniquement si reduc-token n'a pas court-circuité
    # → question complexe/projet → ton LLM habituel
    response = your_existing_llm_client.ask(body["message"])
    return {"response": response}
```

**Ce qui se passe selon le message :**

```
POST /chat {"message": "Verrouille 433.92 MHz"}
  → action détectée  → 200 {"response": "Fréquence 433.92 verrouillée", "source": "action"}
  (endpoint /chat jamais appelé, 0 token LLM)

POST /chat {"message": "C'est quoi le RSSI ?"}
  → LLM local + Redis → 200 {"response": "...", "source": "local"}
  (endpoint /chat jamais appelé)

POST /chat {"message": "Optimise ma fonction process()"}
  → rien détecté → call_next → endpoint /chat → ton LLM
```

---

## Deux adapters — un moteur commun

```
                    config/intents.json   config/routing.json
                              │                   │
                    ┌─────────▼───────────────────▼─────────┐
                    │         core/ + cache/                 │
                    │  classifier · executor · llm_cache     │
                    └──────────┬──────────────┬─────────────┘
                               │              │
              ┌────────────────▼──┐      ┌────▼─────────────────┐
              │  adapters/        │      │  adapters/            │
              │  claude_code/     │      │  chatbot/             │
              │                   │      │                       │
              │  Hook Claude Code │      │  bot.py               │
              │  (dev assistant)  │      │  (chatbot autonome)   │
              └───────────────────┘      └───────────────────────┘
```

| | **claude_code** | **chatbot** |
|---|---|---|
| Rôle | Dev assistant dans Claude Code | Chatbot autonome (API/Discord/Slack…) |
| LLM principal | **Claude** (hook injecte le contexte) | **Ollama local** (Claude = fallback) |
| Actions | Exécutées, Claude reformule | Exécutées, réponse directe |
| Questions générales | Ollama répond à la place de Claude | Ollama répond directement |
| Questions complexes | Claude avec contexte projet injecté | Claude API (fallback) |
| Installation | `python3 install.py` | import Python ou CLI |

---

## Structure des fichiers

```
reduc-token/
├── core/                            ← générique, 0 dépendance Claude Code
│   ├── classifier.py                # Intent + entity extraction (qwen2.5:3b JSON mode)
│   └── executor.py                  # Exécution d'actions HTTP depuis l'intent registry
├── cache/
│   └── llm_cache.py                 # Cache Redis embedding + interface Ollama
├── config/                          ← personnalisation utilisateur (à adapter)
│   ├── intents.json                 # Actions de l'app (verbes, entités, routes HTTP)
│   └── routing.json                 # Patterns LLM local + threshold embedding action
├── adapters/
│   ├── claude_code/                 ← Hook Claude Code (dev assistant)
│   │   ├── hook.py                  # Hook UserPromptSubmit (action → routing → context)
│   │   ├── tools/
│   │   │   ├── project_mapper.py    # Génère CLAUDE.md + project_map.json
│   │   │   ├── session_summary.py   # Génère session_context.md (git diff + commits)
│   │   │   └── git_diff_summary.py  # Résumé human-readable du git diff
│   │   ├── mcp/
│   │   │   └── mcp_server.py        # Serveur MCP (smart_ask, git_diff, session_summary…)
│   │   └── context/
│   │       ├── CLAUDE.md            # Index projet compact (régénéré auto)
│   │       ├── project_map.json     # Structure JSON du projet
│   │       ├── session_context.md   # État git actuel (régénéré si périmé)
│   │       └── routing_log.jsonl    # Log des décisions de routing
│   └── chatbot/                     ← Chatbot autonome (API/Discord/Slack…)
│       └── bot.py                   # Classe Chatbot (action → LLM local → Claude API)
├── docker-compose.yml
└── requirements.txt
```

---

## Config — référence complète

### `config/intents.json`

- `base_url` : URL de base de ton API
- `intents[].description` : phrase naturelle décrivant l'action — **c'est ce texte qui est comparé par embedding** au message utilisateur, pas les verbes
- `intents[].entities` : paramètres extraits par le classifier LLM
- `intents[].action` : appel HTTP à exécuter
- `intents[].response_template` : réponse formatée avec les entités extraites

Le pré-filtre embedding se calibre **automatiquement** à partir des descriptions — pas besoin de lister des verbes, fonctionne en toutes langues.

**Exemples inclus** (goniométrie) : `lock_frequency`, `get_active_lock`, `delete_lock`, `get_mesures`, `clear_mesures`, `get_active_profil`, `activate_profil`, `deactivate_profil`, `get_profils`.

### `config/routing.json`

- `local_llm_patterns` : mots/phrases qui basculent vers Ollama local (pas de LLM cloud)
- `action_embed_threshold` : seuil cosine pour la détection d'action (0.72 = permissif, 0.85 = strict)

---

## Redis — structure des données

| Clé | Type | Contenu |
|-----|------|---------|
| `llm:entry:<sha256>` | string JSON | `{ prompt, response, model, source, cached, timestamp, tokens }` |
| `llm:embed:<sha256>` | string JSON | vecteur embedding (liste de floats) |
| `llm:embeds` | set | tous les sha256 ayant un embedding stocké |
| `routing:action_embed:<hash>` | string JSON | embedding d'une description d'intent (TTL 30j) |

TTL réponses : 7 jours. Le sha256 est calculé sur `model::prompt`.

---

## CLI llm_cache.py

```bash
# Question simple (LLM local)
python3 cache/llm_cache.py "Explique le gisement radiogoniométrique"

# Même question → réponse Redis instantanée
python3 cache/llm_cache.py "Explique le gisement RF"

# Seuil de similarité embedding personnalisé (défaut: 0.92)
python3 cache/llm_cache.py "Question" --embed-threshold 0.85

# Sans cache
python3 cache/llm_cache.py "Question" --no-cache

# API Mistral cloud
python3 cache/llm_cache.py "Question" --online --api-key sk-xxx

# Vider le cache et les embeddings
python3 cache/llm_cache.py --clear-cache
```

---

## Routing — règles de décision

Appliqué uniquement si aucune action n'a été détectée à l'étape 0.

| Priorité | Condition | Route |
|---|---|---|
| 1 | Chemin explicite (`/api/xxx`) dans le prompt | Claude |
| 2 | Fichier `.ext` mentionné ET stem dans project_map.json | Claude |
| 3 | Pattern question générale (`config/routing.json`) | LLM local |
| 4 | Identifiant projet reconnu (nom de fonction/classe/fichier) | Claude |
| 5 | Défaut | Claude |

Les patterns sont testés après normalisation Unicode (accents supprimés).
Le log des décisions est dans `context/routing_log.jsonl`.

---

## MCP — outils disponibles

Le serveur MCP expose ces outils à Claude :

| Outil | Description |
|---|---|
| `smart_ask` | Route et répond (routing + cache complet) |
| `llm_ask` | Appel direct au LLM local avec cache |
| `session_summary` | Régénère session_context.md |
| `project_mapper` | Régénère CLAUDE.md + project_map.json |
| `git_diff` | Résumé des changements git |
| `llm_clear_cache` | Vide le cache Redis |

---

## Adapter chatbot — utilisation

### Python

```python
from pathlib import Path
from adapters.chatbot.bot import Chatbot

bot = Chatbot(
    config_dir=Path("reduc-token/config"),
    claude_api_key="sk-ant-...",          # optionnel, uniquement pour le fallback
    system_prompt="Tu es un assistant radio.",
)

result = bot.ask("Verrouille 433.92 MHz")
print(result["response"])
# source : "action" | "local" | "claude"
print(f"— via {result['source']}")
```

### CLI

```bash
python3 adapters/chatbot/bot.py "Verrouille 433.92 MHz"
python3 adapters/chatbot/bot.py "Explique le gisement RF"          # → LLM local
python3 adapters/chatbot/bot.py "Optimise ma DB" --api-key sk-ant-...  # → Claude
```

### Flow de décision (chatbot)

```
Message utilisateur
      │
      ▼
[0] ACTION ?  embedding(message) vs descriptions intents.json
      ├─ sim ≥ threshold → classifier LLM → execute HTTP → réponse directe
      └─ non → continuer

[1] QUESTION GÉNÉRALE ?  pattern routing.json
      ├─ oui → llm_cache.ask() → Ollama local + Redis cache → réponse
      └─ non → continuer

[2] FALLBACK → Claude API (httpx)
```

---

## Utilisation depuis Python (cache seul)

```python
import sys
sys.path.insert(0, "reduc-token/cache")
from llm_cache import ask

result = ask("Qu'est-ce que le SNR en RF ?")
print(result["response"])
print(f"{result['tokens']} tokens | cached={result['cached']} | source={result['cache_source']}")

# Résultat typique :
# 42 tokens | cached=True | source=embedding
```
