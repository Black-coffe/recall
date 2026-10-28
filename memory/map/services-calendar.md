# services — calendar (ICS sync + recording binding, k6-calendar-ics 4.34.0, full-sync CLI 4.34.1)

last-verified: 2026-10-28 (4.37.0, schema v60: meeting_brief); 2026-10-27 (4.34.1)

Part of `app/services/*`; hub — [services.md](services.md). In-process daemon thread like `MailPoller` (`services-mail.md`).
User doc: `docs/calendar.md`. Read-only toward the calendar; the secret ICS address is typed only on `/connect/calendar/<token>`.

## calendar_ics.py (sources, keyring, fetch/parse, sync, poller)
- `KEYRING_SERVICE="Recall Calendar"`, keyring user = `normalize_label(label)` (whitespace-collapsed, <=60 chars, case kept: «Google» and
  «google» are two sources). DB keeps label, host, last sync, counters; never the address. `CalendarError.code` (message never holds the URL):
  `CALENDAR_BAD_URL|HOST_NOT_ALLOWED|FETCH_FAILED|PARSE_FAILED`.
- `validate_url` -> host: https (or `webcal://`) and host in `ALLOWED_HOSTS` (calendar.google.com, outlook.office365.com, outlook.office.com,
  outlook.live.com). `fetch_feed`: `_AllowlistRedirect` (a redirect to a non-allowed host raises), 30 s, `MAX_FEED_BYTES`=20 MB.
- `parse_occurrences(data, start, end)`: `recurring_ical_events` expansion (`skip_bad_series`); occurrence key = RECURRENCE-ID else DTSTART;
  date-only = all-day (midnight UTC); floating time = local; STATUS -> confirmed|tentative|cancelled (the library does not filter CANCELLED).
- `connect_source(db, label, url, fetch=)`: trial fetch+parse FIRST, then keyring, then DB (`INSERT OR IGNORE` + enable; a DB failure restores the previous
  keyring value), then first sync. `disconnect_source`: NULLs `calendar_event_id` on `audio_downloads`/`transcriptions`, deletes events+source, drops keyring.
- `sync_source(db, id, window_days=30|None, fetch=, now=)`: upsert on UNIQUE(source_id, uid, occurrence_key); an occurrence that vanished from the
  window is marked `cancelled`, never deleted (a bound recording keeps its link); failure writes `last_error` code and re-raises. `sync_all` isolates sources.
- `list_sources(db, check_keyring=True)` (`configured` + `events`; MCP passes False), `get_source`, `has_source`, `events_between(db, start, end, include_cancelled=)`.
- `EARLIEST_SQL` (:496): `MIN(start_utc)` over non-cancelled, timed (`all_day = 0`) events only, so an all-day series cannot push the history back.
  `earliest_event(db, source_id=None)` (:500) = that, optionally per source, ISO UTC or None.
- CLI `python -m app.services.calendar_ics sync [--full] [--source ID] [--db]` (`main`, :510): `--full` = `window_days=None` (whole feed; run before
  `calendar_bind backfill`, the poller keeps only +-30 days). Prints JSON `{full, sources{id: result|{error: code|NOT_FOUND}}, calendar_earliest}`; exit 1 if any source errored.
- `CalendarPoller(db)`: `start/stop/is_alive/trigger(source_id=None)/run_once`; interval = `CALENDAR_SYNC_MINUTES` (15) each loop;
  `CALENDAR_SYNC_ENABLED=0` -> `run_once` serves queued triggers only. Logs "Calendar poller started" (proof of a restart).

## calendar_bind.py (no network)
- `best_event`: non-cancelled, non-all-day events overlapping the recording widened by `TOLERANCE`=10 min each side; largest overlap wins and
  must cover >= `MIN_SHARE`=1/3 of the recording. `recording_window(started_at, duration_sec)`.
- `bind_download(conn, download_id, start, end)` sets `audio_downloads.calendar_event_id`; an existing binding is kept (first decision wins).
- `link_attendees`: only EXISTING persons via `enrichment._find_entity(conn, "person", _normalize(name))`; never creates an entity, never stores an
  email as alias; `INSERT OR IGNORE INTO meeting_entities ... source='calendar'`.
- `apply_to_transcription(conn, tid, download_id)` copies the binding onto `transcriptions.calendar_event_id` + links attendees.
- CLI `python -m app.services.calendar_bind backfill [--dry-run] [--db] [--sessions]`: titles untouched; start = manifest `started_at` else
  `created_at - duration` (drifts when saved late); stats incl. `persons_before/after` (must be equal), `per_source`,
  and (4.34.1) `calendar_earliest` (from `EARLIEST_SQL`), `before_calendar` (unbound and ended before earliest - `TOLERANCE`, or no events at all),
  `coverable` = `recordings - before_calendar` (`backfill`, :157; stats at :170-213).

## Wiring
- Title order owner > event > Claude: `recording/library._bind_calendar` (inside `register_recording`, both insert and existing-row paths) replaces
  only an automatic name (`manifest.auto_name` or `Запис <sid8>`); `autosave._event_titled(row)` makes the autosave skip `suggest-meta`.
- `transcription_run` calls `calendar_bind.apply_to_transcription(conn, tid, meta['library_audio_id'])`; failure only warns.
- `enrichment` re-enrich deletes `meeting_entities` rows except `source IN ('calendar','owner')` and upserts (not OR REPLACE), so calendar and owner links survive.
- 4.36.0 consumer: `recording/participants.suggest` reads `calendar_events.attendees` overlapping now +- `calendar_bind.TOLERANCE` (uses `calendar_bind._iso`) to prefill the recorder's participants.
- v58: `calendar_sources(label UNIQUE, host, enabled, last_sync_at, last_error, events_seen, events_with_attendees)`, `calendar_events` (CASCADE),
  `calendar_event_id` on `audio_downloads` and `transcriptions`. HTTP — `blueprints.md` § settings_api/connect; MCP `list_calendar_events` — `root-processes.md`.

## meeting_brief.py (4.37.0, wave4-client-calls; migration v60 `meeting_brief_sent`)
- Pre-meeting card: `brief_for_event(conn, event)` / `brief_for_person(conn, name)` (-> `{event|None, people[], hidden, generated_at}`; each person `name, entity_id, matched, open_tasks (<=5), open_more, last_calls (<=5)`).
  Attendees matched like `calendar_bind.link_attendees` (`enrichment._find_entity` on `_normalize`); unmatched = name only, empty lists, nothing created, no email kept.
  Open tasks = `owner_entity_id` match, status `open|stale`, `dup_of IS NULL`; last calls = `meeting_entities` x `transcriptions` with `source_type IN recording|file|library` (`CALL_SOURCES`).
- Chain: `filter_brief` (`pipes_privacy.filter_data`, private directions + stop words -> `hidden`) -> `render_card` (`pipes_cards._Guard` per field, `link_base()`, returns `(html, leaked)`) -> `_leaked_after_render` -> `deliver`. `PrivacyError` = failed, nothing sent.
- `MeetingBriefTicker(db, deliver, tick_sec=60, lead_minutes=None, now_fn=)`: `run_once` -> `{sent, failed, skipped}`; events starting within `MEETING_BRIEF_LEAD_MINUTES` (15, clamp 1..180), not all-day/cancelled. Marker `meeting_brief_sent(event_id, start_utc)` PK;
  `sent|skipped` close it (`skipped` + `last_error` `private` | `no_people`), `failed` is retried each tick until start. Started in `app.py:1399` `_maybe_start_meeting_brief` (only under `__main__`, needs `MEETING_BRIEF_ENABLED` + `tg_bot.is_configured()`), `app.extensions['meeting_brief_ticker']`, logs "Meeting brief ticker started".
- HTTP: `GET /api/meeting-brief?event_id=|person=` (`automation.py:226`, after `filter_brief`; `PrivacyError` -> 503 `UNAVAILABLE`); MCP `get_meeting_brief` proxies it (`root-processes.md`).

## Gotchas
- `meeting_entities.source` values now: NULL (Claude), `thread_match`, `thread_morph`, `calendar`, `owner` (4.36.0) (`services-entities.md`).
- Outlook published feeds may lack attendees; `events_with_attendees` per source shows it.
- Disconnect is a gated D action `calendar_disconnect` (`settings_api.disconnect_calendar`), hard delete, not recoverable.
