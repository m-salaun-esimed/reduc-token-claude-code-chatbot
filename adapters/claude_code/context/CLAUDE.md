# goniometrie — Index
> 2026-04-01 | 63 fichiers | 165 fonctions

## `api-goniometrie/`
- `requirements.txt` [python-deps] → fastapi, uvicorn[standard], sqlalchemy[asyncio], asyncpg, alembic, pydantic-settings

## `api-goniometrie/app/`
- `config.py` → `Settings`
- `database.py` → `Base`
- `dependencies.py` → `get_db(L6)`
- `main.py` [fastapi-app] → `GET /health`
- `ws_manager.py` → `ConnectionManager`

## `api-goniometrie/app/crud/`
- `bande_frequence.py` → `create_bande(L8)`, `delete_bande(L16)`, `get_bandes_actives(L26)`
- `freq_lock.py` → `delete_all_locks(L14)`, `create_freq_lock(L20)`, `get_freq_locks(L43)`, `update_lock_azimuth(L50)`, `delete_freq_lock(L71)`
- `mesure.py` → `create_mesure(L8)`, `get_mesures(L16)`, `get_mesure(L32)`, `delete_mesure(L37)`, `delete_all_mesures(L46)`
- `profil_menace.py` → `create_profil(L8)`, `count_profils(L15)`, `get_profils(L20)`, `get_profil(L31)`, `get_profil_actif(L39)`, `set_profil_active(L47)`, `delete_profil(L66)`

## `api-goniometrie/app/models/`
- `bande_frequence.py` → `BandeFrequence`
- `freq_lock.py` → `FrequenceLockee`
- `mesure.py` → `Mesure`
- `profil_menace.py` → `ProfilMenace`

## `api-goniometrie/app/routers/`
- `freq_locks.py` [fastapi-router] → `POST /`, `GET /actif`, `GET /{lock_id}`, `GET /`, `PATCH /{lock_id}/azimuth`, `DELETE /{lock_id}`
- `mesures.py` [fastapi-router] → `WEBSOCKET /ws`, `POST /`, `GET /`, `DELETE /`, `GET /{mesure_id}`, `DELETE /{mesure_id}`
- `profils.py` [fastapi-router] → `POST /`, `GET /`, `GET /actifs/bandes`, `GET /actif`, `GET /{profil_id}`, `PATCH /{profil_id}/activer`, `PATCH /{profil_id}/desactiver`, `DELETE /{profil_id}`, `POST /{profil_id}/bandes`, `DELETE /bandes/{bande_id}`

## `api-goniometrie/app/schemas/`
- `bande.py` [pydantic-models] → `BandeFrequenceCreate`, `BandeFrequenceRead`, `BandeActiveRead`
- `freq_lock.py` [pydantic-models] → `FrequenceLockeeCreate`, `AzimuthUpdate`, `FrequenceLockeeRead`
- `mesure.py` [pydantic-models] → `MesureCreate`, `MesureRead`
- `profil.py` [pydantic-models] → `ProfilMenaceCreate`, `ProfilMenaceRead`, `ProfilMenacePage`

## `front-goniometrie/`
- `package.json` [package-config] → @reduxjs/toolkit, @tailwindcss/vite, axios, react, react-dom, react-icons, react-redux, react-router-dom

## `front-goniometrie/src/`

## `front-goniometrie/src/app/`

## `front-goniometrie/src/components/`

## `front-goniometrie/src/domains/`

## `front-goniometrie/src/domains/freq_locks/`

## `front-goniometrie/src/domains/freq_locks/components/`

## `front-goniometrie/src/domains/mesures/`

## `front-goniometrie/src/domains/profils/`

## `front-goniometrie/src/domains/profils/components/`

## `front-goniometrie/src/pages/`

## `front-goniometrie/src/types/`

## `reduc-token/`
- `install.py` → `check(L66)`, `install(L80)`, `uninstall(L104)`, `main(L130)`
- `requirements.txt` [python-deps] → redis>=5.0, httpx>=0.27

## `reduc-token/adapters/chatbot/`
- `bot.py` → `main(L239)`, `Chatbot`

## `reduc-token/adapters/claude_code/`
- `hook.py` → `main(L310)`

## `reduc-token/adapters/claude_code/mcp/`
- `mcp_server.py` → `handle_project_mapper(L282)`, `handle_session_summary(L309)`, `handle_git_diff(L327)`, `handle_llm_ask(L345)`, `handle_llm_history(L368)`, `handle_smart_ask(L391)`, `handle_llm_clear_cache(L420)`, `run_stdio(L442)`, `run_http(L499)`, `main(L538)`

## `reduc-token/adapters/claude_code/tools/`
- `git_diff_summary.py` → `parse_diff(L35)`, `format_output(L104)`, `main(L123)`
- `project_mapper.py` [fastapi-app] → `parse_python_file(L194)`, `parse_ts_file(L467)`, `parse_package_json(L539)`, `parse_requirements_txt(L560)`, `parse_shell_file(L574)`, `scan_project(L628)`, `build_relations(L729)`, `generate_claude_md_compact(L887)`, `generate_claude_md(L950)`, `main(L1091)`
- `session_summary.py` → `git_diff_summary(L59)`, `recent_commits(L108)`, `generate_summary(L113)`, `main(L151)`

## `reduc-token/cache/`
- `llm_cache.py` → `get_redis(L47)`, `clear_cache(L63)`, `get_embedding(L74)`, `embed_search(L97)`, `embed_store(L136)`, `ask_ollama(L150)`, `ask_online(L180)`, `ask(L207)`, `main(L259)`

## `reduc-token/core/`
- `classifier.py` → `load_intents(L23)`, `load_config(L30)`, `classify(L64)`, `find_intent_config(L107)`
- `executor.py` → `execute(L29)`, `format_response(L56)`

## `sdr-scanner/`
- `gisement.py` → `PointScan`, `ResultatGisement`, `GisementCalculator`
- `hackrf_device.py` → `HackRFDevice`
- `requirements.txt` [python-deps] → numpy>=1.26, scipy>=1.12, httpx>=0.27
- `run_scanner.sh`
- `scanner.py` → `load_config(L53)`, `fetch_lock_actif(L63)`, `bandes_from_lock(L78)`, `fetch_bandes_actives(L93)`, `bandes_from_config(L107)`, `post_result(L120)`, `post_azimuth(L130)`, `move_servo(L150)`, `scan_at_angle(L158)`, `run_gisement_scan(L178)`
- `setup.sh`
- `signal_processor.py` → `compute_fft(L7)`, `integrate_power(L37)`, `peak_info(L61)`
- `simulateur.py` → `PointScan`, `GonioSimulator`

## Dépendances
- `database.py` → `config.py`
- `dependencies.py` → `database.py`
- `main.py` → `config.py`, `database.py`
- `bande_frequence.py` → `database.py`
- `freq_lock.py` → `database.py`
- `mesure.py` → `database.py`
- `profil_menace.py` → `database.py`
- `freq_locks.py` → `dependencies.py`
- `mesures.py` → `dependencies.py`, `ws_manager.py`
- `profils.py` → `dependencies.py`
- `profil.py` → `bande.py`
- `scanner.py` → `gisement.py`, `hackrf_device.py`, `signal_processor.py`, `simulateur.py`

*Détail complet : voir `CLAUDE.md.detail` — Régénérer : `python3 project_mapper.py`*