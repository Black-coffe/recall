# copilot — живий ко-пілот дзвінка (`app/services/copilot/`)

**Ідея:** під час запису локальна модель (Qwen-14B/Ollama, $0) веде топік-стейт-машину,
диспетчеризує RAG-пошук, ловить протиріччя/питання/факти → картки оператора. Claude
(Sonnet/Opus, «старший брат») верифікує лише позначене важливим — **local-first cascade**.
Гейт `COPILOT_ENABLED`; усе деградує (нема Ollama/ключа → whisper-only живий). Roadmap: `docs/COPILOT_ROADMAP.md`.

## Модулі
- **config.py** — чиста логіка (без I/O): `resolve_settings()` зводить UI-налаштування +
  матрицю MODE×IMPORTANCE у фінальний снапшот. MODES=light/medium/hard, IMPORTANCE=low/medium/high.
  Параметри: cadence_sec, escalate_threshold, top_k, safety_sweep_sec, verify_votes, min_unanchored_conf,
  **max_cards / min_card_gap_sec / verified_only / scope_projects** (Трек 3, 25.07.2026).
  *api_enabled=True лише якщо IMPORTANCE≥medium або форс; Opus-арбітр лише high+uncertain.*
- **service.py** — `CopilotService`: `start()`, `get_state()`, `end()`, `link_transcription()`,
  `get_timeline()`. Тонкий persist-шар. Per-session RLock. *config_json — снапшот резолвнутих налаштувань.*
- **topics.py** — `TopicTracker`: `apply(status,label,return_to,ts)`, `topic_list()`. LLM-driven
  (НЕ ембеддинги — embed_fn лишений для сумісності, не юзається). *Деген-вивід LLM → фолбек на continue (анти-галюцинація).*
- **dispatcher.py** — `Dispatcher`: `triage_and_plan()` + `analyze_with_evidence()` (2 окремі
  виклики Ollama зі structured JSON: TRIAGE_SCHEMA / ANALYSIS_SCHEMA). *Перший не бачить архів, другий бачить чанки.*
- **worker.py** — `CopilotWorker` (singleton-демон): `_tick()` — накопичення вікна → диспетч →
  топіки → ескалація. `_WS` (per-session WorkState: rolling transcript/summary, shown_chunks, topic_chunks).
  *Wake кожні ~COPILOT_TOPIC_TICK_SEC (~6s); поважає pause; rolling summary не чиститься; дедуп чанків.*
- **escalate.py** — `Escalator`: `verify(observation,chunks)` (verdict real/refuted/uncertain),
  `sweep(window,chunks)` (незалежний прохід). Tool-use enforced (report_verdict / report_findings).
  `cost_estimate()` з pricing.py. *Грейсфул: нема ключа → None, ко-пілот лишається локальним; prompt caching.
  З 28.07.2026 дефолти — Sonnet 5 (normal) / Opus 5 (gnarly); для Claude 5 thinking вимикається явно
  (on-by-default зʼїдав би малий max_tokens live-вердикту).*
- **export.py** — `collect_notes()`, `export_markdown()`, `export_json()`, `notes_digest()`.
  Чисті функції. *evidence-ланцюги через ref_event_id (insight_verified → insight_local).*

## Потік даних (cascade)
1. **Capture** — RecordingService + LiveTranscribeWorker → live-сегменти ~8s (small Whisper, без діаризації).
2. **Accumulate** (`worker._tick`) — нові сегменти у rolling-вікно (~200–1000 символів).
3. **Triage** (`dispatcher.triage_and_plan`) — вікно+топіки → Ollama → `{topic_status, needs_retrieval, observations}`.
4. **Retrieval** (умовно) — якщо needs_retrieval → `retrieval.search()` → чанки з chunk_id+провенансом.
5. **Analysis** (`dispatcher.analyze_with_evidence`) — вікно+чанки → Ollama → insights з `evidence_chunk_ids`.
6. **Topics** (`TopicTracker.apply`) → SSE `copilot_topic`.
7. **Filter** — observations відсіюються по `min_unanchored_conf` (без пруфа → тільки high-conf) → persist `insight_local`.
8. **Verify→Show** (`worker._process_insight`, Трек 3) — **вердикт ПЕРЕД показом**: persist завжди,
   `Escalator.verify()` → показуємо лише `real` і саме текст Claude (він же полірує), а не чернетку
   локалки. Гейт `_card_gate` тримає бюджет уваги (max_cards) і паузу між картками (min_card_gap_sec);
   притишене лишається в БД з `payload.shown=false` для розбору після дзвінка.
9. **Safety sweep** (періодично) — `Escalator.sweep()` незалежно шукає пропущене → sweep-findings
   (підпорядковані тому самому бюджету карток).
10. **Export** — таймлайн → md/json з інлайн-нотатками+цитатами.

## Економіка / деградація
- Ollama $0. Claude verify+sweep ~центи/год, prompt caching ~10% input.
- Бюджет: importance→budget_usd (low=$0/med=$1/high=$3), хард-стоп → local-only.
- Нема embeddings → без RAG-пруфа; нема Ollama → ко-пілот неактивний; нема ключа → лишаються локальні verdict-и;
  бюджет вичерпано → api_enabled=False.
- **verified_only деградує мʼяко:** якщо API недоступний (нема ключа / вичерпано бюджет / режим
  «лише локально») — показуємо локальне, але лише анкороване доказами. Копілот, що замовк,
  гірший за неточного.

**Привід для Треку 3 (заміри на 80 сесіях):** 5743 локальних інсайти ≈ 72 картки за дзвінок,
Claude перевіряв 5.9% (і то ПІСЛЯ показу), `operator_action` = NULL в усіх 9706 подіях при робочих
кнопках 👍/👎 — віджет не читали. Ліміти: light 3 / medium 5 / hard 8 карток.

**Офлайн-тести:** `test_copilot_dispatcher.py` (корінь), `tests/test_copilot_quiet.py` (Трек 3). **БД:** `copilot_sessions`/`topics`/`events` (міграція v19).
**SSE:** `copilot_topic`, `copilot_insight`. Сесія лінкується до transcript при transcribe → таймлайн на /transcript.
