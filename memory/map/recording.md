# recording — серверний запис (`app/services/recording/`)

Mic + системний звук з live-метром, live-сегментами (SSE), пауза/резюм, finalize у фоні,
recovery осиротілих сесій. **До `RECORDING_MAX_ACTIVE` (дефолт 2) паралельних сесій**; системний
звук — увесь endpoint або ізольовано по застосунку. Windows-only (`pyaudiowpatch` для mic/`all`,
comtypes+pycaw для process loopback). ADR-026 (+ поправки A1-A4). Wiki: `docs/wiki/recording-sources-and-sessions.md`.

## Модулі
- **service.py** — `RecordingService(..., max_active=2, app_source_factory=None)`: реєстр
  `_sessions: dict[sid,_ActiveSession]` під `_registry_lock` (лише dict), `_start_lock` серіалізує
  ліміт, per-session `RLock` на pause/resume/stop/retarget/тік. `start(..., system_source=SystemSpec)`,
  `pause()`, `resume()`, `stop()`, `discard()`, `retarget_system(sid, spec, reason)`, `get_state()`,
  `active_session_ids()`, `active_session_id` (лише коли рівно одна, інакше None), `is_active(sid)`,
  `recover_orphaned()`, `recover_unfinalized()`. `_build_source()` — єдиний dispatch spec→джерело.
  Помилки (`code`): `SessionConflictError` `SESSION_LIMIT` (несе `.active`, `.limit`),
  `SessionNotFoundError` `NOT_FOUND`, `NoSystemStreamError` `NO_SYSTEM_STREAM`, `RecordingError`
  (`NO_SYSTEM_DEVICE` для `all` без індексу).
- **audio_source.py** — межа захоплення. `AudioSource` Protocol (дак-тайп `WasapiRecorder` +
  `drain_events()`), `MicSpec`, `SystemSpec(mode all|include|exclude, apps, device_index)` з
  `from_dict/to_dict/validate`, `UnsupportedSourceSpecError` (`UNSUPPORTED_SOURCE_SPEC`),
  `MAX_INCLUDE_CLIENTS=4`, формат `48000/2 s16le`. `ProcessLoopbackSource` (один клієнт pid×mode,
  bounded queue drop-oldest), `MixedSource` (int16 насичена сума, частина, що відстає >500 мс,
  добивається нулями), `AppAudioSource` (тримає клієнти, watcher-потік `app-audio-watcher` кожні 2 с
  → `refresh_pids()` → подія `pid_change`; добиває тишу до стінного часу при дефіциті >50 мс),
  `resolve_root_pids()` (psutil-корені exe), `make_app_source()`.
- **process_loopback.py** — `ProcessLoopbackClient` (ActivateAudioInterfaceAsync
  `VAD\Process_Loopback`, INCLUDE/EXCLUDE tree, опитування на MTA-потоці), `LoopbackClient`
  Protocol, `ClientFactory`, `default_client_factory`. Нативне вантажиться ліниво (`_native()`).
- **app_audio.py** — `list_sounding_apps(live_specs)` (сесії Active+Inactive на ВСІХ активних
  render-endpoint, дедуп по exe, `recorded_by`; ніколи не кидає → `available:false`),
  `session_pids()`, `session_roots(exe, fallback=)` (сесійні PID → корінь дерева exe, першим —
  дерево з Active-сесією). Незалежний від `audio_source.py`.
  `list_call_sessions(enumerate_sessions=, own_pid=)` (K2) → `{exe: {mic|out: {active, peak, endpoint}}}`
  по Active/Inactive-сесіях обох напрямків (власний PID пропущено); `None`, коли саме перелічення впало.
- **call_watch.py** (K2, 4.9.0) — `CallWatcher` (потік `call-watch`, тік 1 с, усі залежності ін'єктовані →
  `tick()` тестований). Дзвінок = exe зі списку, у якого mic І out Active `prompt_seconds` поспіль і кожна сторона
  > `SPEECH_PEAK=0.02` ≥3 тіків → один тост «Записати?» (не, якщо живий запис уже захоплює exe: `captures()`).
  Кінець дзвінка (сесії неактивні 20 с або тиша 40 с) відкликає непрочитану підказку й питає «Зупинити?» про
  кожен живий непаузований запис, що захоплює exe; далі `autostop_minutes` тиші на обох доріжках → `stop_fn`
  (шлях кнопки Stop, запис цілий). `pending()/consume(token,action)/dismiss(id)`: id підказки = одноразовий токен,
  TTL 3 год. JSONL-лог рішень (без PID/мови, ротація 5 МБ → `.1`). `enumerate → None` тримає стан. `ToastNotifier`.
- **toast.py** (K2) — тости Windows без pip-залежності: `powershell.exe -EncodedCommand` (WinRT
  `ToastNotificationManager`, `NO_WINDOW`), AUMID `Recall.CallPrompt` у HKCU; `build_xml`, `show`, `remove`,
  `ensure_registered`. Кнопки — protocol activation (URL у браузері). Повертає `bool`, не кидає.
- **recorder.py** — `WasapiRecorder` (+ `drain_events()` → `[]`), `list_input_devices()`,
  `resolve_device_params()`, `LevelMeter`, `AudioFrame`.
- **finalize.py** — `pcm_to_wav()`, `finalize_session()` (mic+system → overlay → final.mp3). Читає лише байти.
- **pcm_writer.py** — `ChunkedPcmWriter`: O_APPEND, fsync ~5s, lazy-open.
- **session_store.py** — `SessionStore`: маніфест JSON з атомік-rename, RLock per sid. Статуси
  recording/paused/stopping/finalized/crashed/discarded. `create(..., system_source=)`,
  `set_stream_source(sid, stream, source, event)`, `stream_source(manifest, stream)` (нема `source` → `all`).

## Джерело системного звуку
- `all` на старті → `recorder_factory` (`WasapiRecorder`, endpoint `device_index`) — байт-у-байт як до 4.7.0.
- `include:[≤4 exe]` / `exclude:[рівно 1 exe]` → `app_source_factory`; у `app.py:835-841` резолвер
  PID = `session_roots(exe, fallback=resolve_root_pids)`. Exclude без запущеного застосунку →
  EXCLUDE власного PID (= увесь звук). Exclude з кількома деревами → лише перше (звучне).
- Формат `.pcm` стріму незмінний: `all` на стрімі, що стартував include/exclude, = EXCLUDE-own-PID.
- Retarget: нове джерело відкривається першим, залишок старого дописується, кадри нового з
  `ts < swap_ts` відкидаються, дірка >20 мс → тиша. На паузі міняється лише spec (`applied_on_resume`).
- Кожна зміна → запис у `streams.system.source_events` (`reason` user|pid_change|auto_restore;
  PID у маніфест не пишуться) + SSE `source_changed`.

## FSM (per session)
```
[recording] ──pause()──> [paused] ──resume()─┐
     │                        │              │
     └──── stop() ──> [stopping] ──finalize──> [finalized]
                          └──crash──> [crashed]  (recover_orphaned / recover_unfinalized)
```

## Реєстрація в Аудіотеці (`library.py`)
`register_recording()` — ідемпотентний upsert у `audio_downloads` (`youtube_id='recording_'+sid`),
категорія з `copilot_sessions.category_id`. *finalize ≠ збереження: upsert робить finalize-callback.*

**Gotchas:** клієнт на мертвий PID стрімить тишу без помилки — watcher єдиний детектор смерті;
watcher при include >4 процесів обрізає до 4 (open() у тій самій ситуації кидає); копілот — один на
процес (`CopilotBusyError`, `memory/map/copilot.md`); reembed відкладається, поки живий хоч один запис.
**K2:** нічого не стартує без кліку; автостоп діє лише після «Зупинити?» і не на паузі; watcher живе тільки
під `__main__` (`app.extensions['call_watcher']`, `app.py:1189`); налаштування `RECORDING_AUTOPROMPT[_APPS|_SECONDS]`,
`RECORDING_AUTOSTOP_SILENCE_MIN` (`app/core/settings.py:187-204`, `.env.example:120-129`); HTTP — `memory/map/blueprints.md`.
**SSE** (`recording:{sid}`): level (10Hz), status, chunk_saved, error, source_changed.
**Тести:** `test_recording_devices.py`, `tests/test_{audio_source,app_audio,call_watch,parallel_recordings,
recording_service,recording_blueprint,session_store}.py`. Спайк-кіт: `scripts/spikes/process_loopback/`.

last-verified: 2026-10-08
