# recording — серверний запис (`app/services/recording/`)

WASAPI-запис mic + system loopback з live-метром, live-сегментами (SSE), пауза/резюм,
finalize у фоні, recovery осиротілих сесій. Windows-only (`pyaudiowpatch`).

## Модулі
- **recorder.py** — `WasapiRecorder`: `start()`, `stop()`, `drain()`, `level_meter`, `get_callback_error()`.
  `list_input_devices()` (mic + loopback-копії виходів), `resolve_device_params()`, `LevelMeter` (RMS+peak, 100ms).
  *Loopback-пристрої синтетичні (WASAPI, по одному на вихід); кадри в `queue.Queue` з PortAudio-callback-потоку.*
- **service.py** — `RecordingService`: оркестрація recorder+writer+store, single-session FSM.
  `start()`, `pause()`, `resume()`, `stop()`, `get_state()`, `drain()`, `recover_orphaned()`. Per-session RLock;
  flush-потік @10Hz (рівень+чанк+маніфест); finalize → фоновий Job. *Одна активна сесія (SessionConflictError);
  pause без тиші в PCM (зсув маркерів).*
- **finalize.py** — `pcm_to_wav()` (стрім-запис, без RAM-буфера), `finalize_session()` (mic.pcm→wav +
  system.pcm→wav → pydub overlay → final.mp3), `_compute_levels()`. *pydub stereo-mix; ffmpeg для MP3; стрім для GB-файлів.*
- **pcm_writer.py** — `ChunkedPcmWriter`: `append()`, `flush()`, `close()`, `total_bytes`. *O_APPEND;
  fsync ~5s (краш-резист); lazy-open; recovery з кінця файлу.*
- **session_store.py** — `SessionStore`: маніфест JSON з атомік-rename. `create()`, `modify()` (tmp+rename),
  `read()`. Статуси recording/paused/stopping/finalized/crashed/discarded. *Завжди валідний JSON на диску;
  .tmp — orphan (ігнор на recovery); RLock per session_id; нема in-memory кешу.*

## FSM
```
[recording] ──pause()──> [paused] ──resume()─┐
     │                        │              │
     └──── stop() ──> [stopping] ──finalize──> [finalized]
                          │
                          └──crash──> [crashed]  (recover_orphaned на старті)
```

## Реєстрація в Аудіотеці (`library.py`)
`register_recording()` — ідемпотентний upsert у `audio_downloads`, ключ `youtube_id='recording_'+sid`.
Авто-нейм: manifest.name → auto_name → «Запис <sid[:8]>». Категорія успадковується з
`copilot_sessions.category_id`, якщо запис лінкнутий до ко-пілота.
*Ідемпотентно по youtube_id; повертає None якщо нема final.mp3.*

> **Урок (memory):** finalize ≠ збереження в Аудіотеці. Раніше insert у `audio_downloads`
> робив тільки фронтовий `/save` → записи-сироти. Фікс — серверний upsert у finalize-callback
> (`recording_orphan_finalize_bug`, `recording_save_transcribe_ux`).

**Тести:** `test_recording_devices.py`. **SSE:** level (10Hz), status, chunk_saved, error.
