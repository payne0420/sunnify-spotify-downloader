# Librespot 320k OGG backend — design (verified against pinned commit)

Pinned: `kokarare1212/librespot-python@18104622b3be02062f1f8abe8dafc396413e9784`.
All claims below were read from that commit's source, not the goal's illustrative snippets.

## Verified library facts (these override the goal's example code where they differ)

1. **OAuth is fully self-contained.** `Session.Builder.oauth(url_callback, success_page=None)`
   hardcodes `MercuryRequests.keymaster_client_id` + redirect `http://127.0.0.1:5588/login`,
   spins up its OWN blocking `http.server` on 5588, and calls `url_callback(auth_url)` so the
   caller can open a browser. It is BLOCKING (`run_callback_server()` loops until the code
   arrives). => We do NOT run our own loopback server and we do NOT use the Rust ref's
   client_id/port 8898. We pass a callback that does `QDesktopServices.openUrl`. The whole
   `...oauth(cb).create()` call runs on a background QThread (it blocks).
2. **`oauth()` auto-reuses a cached file**: if `conf.stored_credentials_file` exists it calls
   `stored_file(None)` first. On a successful connect the Session itself writes
   `stored_credentials_file` — the **reusable AP auth credentials** (`reusable_auth_credentials`),
   NOT a PKCE token and never the password (still sensitive, hence chmod 0600). => point that path
   at our config dir and login persists across launches with no re-login.
3. **Header skip is internal.** `content_feeder().load(TrackId, VorbisOnlyAudioQuality(VERY_HIGH),
   False, None)` -> `load_track` -> `load_stream` -> `CdnFeedHelper.load_track`, which does
   `input_stream.skip(0xA7)` (167 bytes) and returns a `LoadedStream`. `LoadedStream.input_stream`
   is the `Streamer`; `Streamer.stream()` returns the SAME `InternalStream` instance already
   advanced to byte 167 = the first real `OggS` page. So the bytes we read already begin at `OggS`.
   `.input_stream.size` is the FULL file size incl. the 167-byte header, so we read `size-167`
   bytes and must terminate on empty read, not on `written < size`.
4. `session.get_user_attribute("type")` -> "premium" | "free" (premium gate).
5. `session.tokens().get("user-read-email")` -> Web-API bearer (Login5). Used by search.py.
6. `TrackId.from_uri("spotify:track:<base62>")` / `TrackId.from_base62(<base62>)` both exist.
7. Deps (from requirements.txt): defusedxml, protobuf==3.20.1, pycryptodomex (`Cryptodome`),
   pyogg, requests, websocket-client, **zeroconf**. pyogg + zeroconf are NOT imported on our
   capture path (pyogg = decode-only; zeroconf = discovery), so they're auto-installed by pip but
   need not be PyInstaller hiddenimports. (Verified: the whole adapter + a real OGG round-trip work
   under Python 3.13 with protobuf 3.20.1, despite the old pin.)

## Module layout (all under backends/librespot/)

- `_librespot/__init__.py` — the ONLY place that imports the alpha `librespot` package. Lazy:
  `load()` imports on first use; `is_available()` returns False (caching the ImportError) if the
  alpha lib/its deps are missing. Exposes thin helpers: `build_session(creds_path, on_auth_url)`,
  `track_id(base62)`, `vorbis_quality(name)`, `load_stream(session, tid, quality)`,
  `product_type(session)`, `web_token(session)`. This contains an upstream break to ONE file.
  **Upstream bugfix:** pinned `AbsChunkedInputStream.check_availability` preloads the wrong
  chunks — the loop tests `requested_chunks()[i]` (already requested) instead of
  `not requested_chunks()[i]`, and marks `requested_chunks()[chunk]` instead of `[i]`. We
  monkeypatch `_fixed_check_availability` (verbatim transcription of upstream lines 116–149
  with the enumerated deltas below) onto the class from `is_available()` / `load_loaded_stream()`,
  guarded by `inspect.getsource` broken-source markers on `check_availability` and
  `notify_chunk_error` (skip on incompatible upstream; on frozen builds where source is
  unavailable, still apply the patch and record `source_unavailable`).
  Also sets `preload_ahead = PRELOAD_AHEAD_CHUNKS` (8).
  Resume pacing, `stream_read_halted` (not `_resumed`), `math.log10` retry sleep, and recursive
  `self.check_availability(chunk, True, True)` are preserved verbatim. Benchmark on a pinned
  track: stock ~2.17 MB/s vs fixed ~25.4 MB/s; output bytes are byte-identical to stock.
  **Third fix (wait predicate):** upstream `wait_lock.wait_for` only tests
  `available_chunks()[chunk]`, so a `notify_all()` from `notify_chunk_error` or `close()` wakes
  the waiter but the predicate stays false and the thread sleeps forever — the single worker
  hangs at a `.part` file. Our predicate also wakes on `chunk_exception is not None` or
  `closed`, so errors and shutdown propagate out of `reader.read()`.
  **Fourth fix (mark-before-submit):** upstream sets `requested_chunks()[…] = True` *after*
  `request_chunk_from_stream`. An instant (synchronous) chunk failure resets `requested[i]` to
  False inside `notify_chunk_error` but the post-dispatch assignment overwrites it back to True,
  leaving a chunk marked in-flight that nobody is fetching.
  **Fifth fix (armed re-dispatch):** upstream dispatches the waited chunk *before* arming
  `wait_for_chunk`. `notify_chunk_error` only records `chunk_exception` when
  `index == wait_for_chunk`, so a pre-arm failure is lost and the waiter hangs forever even
  with the fixed predicate. After arming the waiter we re-dispatch if the request was consumed
  without success, so the failure is recorded and the waiter wakes.
  **CDN robustness patch:** pinned `CdnManager.Streamer.request_chunk` has no try/except, so
  executor-submitted chunk fetches swallow exceptions and never call `notify_chunk_error`.
  `request()` calls `client().get(...)` with no timeout, so a blackholed socket blocks the
  executor thread forever. We monkeypatch both onto `CdnManager.Streamer` from the same call
  sites, guarded by `inspect.getsource` markers (all-or-nothing; skip on incompatible upstream;
  on frozen builds where source is unavailable, still apply and record `source_unavailable`).
  `request_chunk` wraps the body in try/except and calls
  `notify_chunk_error(index, exc)` on failure. `request` is a verbatim transcription with
  `timeout=CDN_REQUEST_TIMEOUT_S` `(10, 60)` on the GET.
  **Audio-key robustness patch:** pinned `AudioKeyManager.get_audio_key` retries once with
  zero delay on failure (doubling request pressure when throttled), registers the callback
  after `send` (fast-response race), uses a shared class-level `SyncCallback.__reference`
  queue (stale keys can leak to the next track), and never removes `__callbacks` entries.
  We monkeypatch `get_audio_key` onto `AudioKeyManager` from the same call sites, guarded by
  `inspect.getsource` markers (all-or-nothing; skip on incompatible upstream; on frozen builds
  where source is unavailable, still apply and record `source_unavailable`). Five deltas:
  (1) no internal retry — `audio.py` owns all retry/backoff; (2) per-call instance-level
  queue via `_OneShotKeyCallback`; (3) register callback before send; (4)
  `finally: callbacks.pop(seq, None)`; (5) raise `AudioKeyError` on error and timeout
  (`.code` is the server code when received, `None` on timeout). `SyncCallback` and the
  class-level `__callbacks` dict are left in place — dispatch reads it; the metadata service
  never requests keys.
  **Recovery semantics:** a failing or timing-out chunk now flows: exception in executor →
  `notify_chunk_error` (increments `retries[i]` first, sets `requested[i]=False`, and only when
  `index == wait_for_chunk` sets `chunk_exception` and wakes the waiter) → the fixed wait
  predicate wakes → `should_retry` is consulted. Pinned librespot increments `retries[index]`
  inside `notify_chunk_error` *before* `should_retry` runs, so the `retries[chunk] < 1` branch
  is already false on the first error reaching an armed waiter; CDN `InternalStream` is
  constructed with `retry_on_chunk_error=False`, so `should_retry` returns false immediately —
  the first armed error raises `ChunkException` straight to `audio.py`'s outer retry ladder.
  With the armed re-dispatch fix, a pre-arm failure gets exactly one in-flight re-attempt while
  the waiter is armed before that same path applies → `ChunkException` propagates out of
  `reader.read()` →
  `capture_ogg` raises → `audio.py`'s existing transient-retry ladder (`ChunkException` matches
  `"chunk"` in `_is_transient` by class name and message) does cleanup + 10/20/30s backoff +
  a fresh `load_loaded_stream` (new storage resolve, new CDN URL, new audio key on the
  self-reconnected session) → after 3 attempts drops to the next quality tier → after both tiers,
  `OggCaptureError` → the per-track fallback chain takes over and the run continues. No permanent
  hangs; Stop stays responsive because `read()` always terminates in bounded time.
  **Note on AP session logs:** the scary `Failed reading packet! [Errno 54] Connection reset by
  peer` line is the access-point Mercury session self-healing upstream (`Receiver` catches it,
  `session.reconnect()` picks a new AP, re-auths, and spawns a fresh `Receiver`). The permanent
  hang was the CDN byte stream, not the AP session — now fixed by the CDN + wait-predicate shims.
  The single-line `Failed reading packet!` logger line is benign and intentionally left alone.
  **Reconnect-resilience patch:** pinned `Session.reconnect` (`core.py:1241`) resolves exactly ONE
  random access point and calls `ConnectionHolder.create` → `sock.connect()` once. It runs inside
  `Session.Receiver.run`'s except-handler when the long-lived Mercury socket drops (`Errno 54`,
  normal on long runs). If that single AP refuses (`ConnectionRefusedError`, `Errno 61`) or is
  blackholed, the error escapes the except-handler and out of `run()`, so (a) Python dumps a
  ~25-line `Exception in thread session-packet-receiver` traceback to the console, and (b) the
  replacement `Receiver` is never spawned, leaving the session half-dead (closed socket, no packet
  pump). We monkeypatch `reconnect` onto `Session` from the same guarded call sites (idempotent +
  locked + `inspect.getsource` markers, all-or-nothing; skip on incompatible upstream; apply and
  record `source_unavailable` on frozen builds). `_fixed_reconnect` is the pinned source transcribed
  verbatim except the `ConnectionHolder.create` call is wrapped in a retry loop that resolves a
  FRESH random AP up to `_RECONNECT_AP_ATTEMPTS` (5) times with bounded exponential backoff
  (`_RECONNECT_BACKOFF_BASE_S` 0.5s, doubling, capped at `_RECONNECT_BACKOFF_CAP_S` 8s). Only
  `OSError` (refused/reset/blackholed/DNS) retries; a non-network error propagates immediately, and
  all attempts exhausted re-raises after the last. Name-mangled privates are reached via the
  explicit `self._Session__inner` / `_Session__ap_welcome` / `_Session__authenticate_partial` /
  `_Session__receiver` spellings (a replacement defined outside the class gets no `__name`
  mangling), exactly like the audio-key patch. `self.connection` is public.
  **Receiver excepthook (defense-in-depth):** for the rare TOTAL reconnect failure (every AP
  attempt exhausted), `_receiver_quiet_excepthook` replaces the escaping multi-line traceback with
  one concise line for the `session-packet-receiver` thread only and delegates every other thread
  to the previously-installed `threading.excepthook` (so unrelated thread crashes — including
  pytest's own — are never masked). Install is idempotent and chains the prior hook. Because the
  hook mutates `threading.excepthook` process-wide, it is installed ONLY at actual-use chokepoints
  (`login_stored` / `login_oauth` / `load_loaded_stream`), never in the `is_available()` probe.
- `session.py` — `LibrespotSession`: resolve creds path (QStandardPaths AppConfigLocation,
  chmod 0600), OAuth-or-stored login (delegates to adapter), `is_premium()`, `close()`.
- `audio.py` — `capture_ogg(stream, dest_tmp, cancel, *, chunk_size=64*1024)`: pure byte pump,
  defensive OggS-trim, returns bytes written. `fetch_track_ogg(session, base62, *, dest, cancel,
  on_status, on_throttle)`: load at VERY_HIGH, fall back to HIGH, audio-key retry w/ exponential
  backoff; `on_throttle` signals key-server rate limits to the backend pacer; writes to
  `dest.tmp` then atomic rename to `dest`. NEVER re-encodes (OGG-only).
- `search.py` — `find_extended_id(session, *, title, artists, expected_s, max_track_duration_s)`
  and `find_normal_id(...)`: Web-API search, build `{"id","title","duration_s"}` candidates,
  require primary-artist match (reject covers), reject sped-up/remix unless the title itself has
  it, then defer to `track_selectors.select_extended/select_normal`. Returns a base62 id or None.
- `backend.py` — `LibrespotBackend(scraper)`: `max_concurrency = 1`. `fetch(...)` orchestrates:
  consent/premium already gated in UI; resolve the id (extended search or the pasted id),
  capture OGG, return `(final_path, "ogg", used_extended)`. AIMD inter-track pacing via
  `KeyThrottlePacer` (floor + jitter; escalates on audio-key throttle, decays on clean fetches)
  + cancel-aware sleep.

## fetch() control flow (backend.py)

```
dest_ogg = splitext(destination)[0] + ".ogg"          # destination arrives as ...mp3 from the seam
if exists(dest_ogg): return (dest_ogg, "ogg", extended)
session = scraper-cached LibrespotSession (built once, reused; serialized by max_concurrency=1)
if not session.is_premium(): raise LibrespotError("Spotify Premium required for 320k OGG")
base62 = track.id
used_extended = False
if extended:
    cand = search.find_extended_id(session, title=strip_radio_edit(track.title), artists=track.artists,
                                   expected_s=track.duration_ms/1000, max_track_duration_s=scraper.max_track_duration_s)
    if cand: base62, used_extended = cand, True      # else fall through to original id (normal)
audio.fetch_track_ogg(session, base62, dest=dest_ogg, cancel=cancel, on_status=scraper.error_signal.emit)
return (dest_ogg, "ogg", used_extended)
```

`used_extended` drives `_resolve_extended_output` / `_meta_title` exactly like YouTube.

## Header-skip strategy (audio.capture_ogg)

The library already positions at `OggS`. We still trim defensively so a future upstream change
can't corrupt output: read the first chunk; if it does NOT start with `OggS`, find the first
`OggS` and drop everything before it; otherwise write as-is. In the pinned commit byte0 == `OggS`
so the scan is a no-op and never reaches into audio data. Loop terminates on empty read; poll
`cancel()` each iteration; assert `>=1` byte and a leading `OggS` after trim.

## Error taxonomy + graceful degradation

- `LibrespotUnavailable` (adapter import failed) — raised by session build and re-raised by
  backend.fetch as a classified "advance" error: the user's `fallback_order` chain
  (`backends/chain.py`) decides whether the track diverts to another source, so the alpha lib
  breaking never bricks Setlist (YouTube stays default source).
- `LibrespotAuthError` (not logged in) — always aborts the track, even with a fallback chain
  configured (a silent source swap would be misleading); friendly per-track error, UI also gates.
- `LibrespotNotPremium` (positively-known free account) — classified "advance" error for the chain.
- Transient `Failed fetching audio key` / load errors — retry 3x exponential backoff (10s base,
  30s cap, mirrors Rust ref), then a friendly error via `_get_user_friendly_error`. Audio-key
  throttle (code 2) also raises the AIMD inter-track floor via `on_throttle` → `KeyThrottlePacer`.
- VERY_HIGH vorbis unavailable for a track -> retry HIGH (still native .ogg). If both unavailable
  -> `OggCaptureError`, another "advance" error for the chain (goal §10.5's graceful degradation,
  now user-ordered instead of hardwired to YouTube).
- `NoExtendedCutError` — extended-mix mode only: the extended search succeeded but found no
  extended cut AND a fallback step exists (`has_fallback=True`); the chain advances with
  `extended=True` so the next source runs its own extended search. With no fallback configured
  the backend streams the original Spotify track natively instead (file left unmarked).

## Threading / cancel

`max_concurrency = 1` (foundation clamps the worker pool). The session is created once and reused
across tracks in a run (a single account can't safely run parallel native streams). AIMD pacing
sleep between tracks (cancel-aware; floor escalates on audio-key throttle, decays on clean
fetches). The read loop polls `cancel` so Stop is responsive (<=3s).

## Metadata + formats (Spotify_Downloader.py)

- `SUPPORTED_FORMATS["ogg"] = {"ext":"ogg","lossy":True}` — but the librespot OGG is the FINAL
  file; it never goes through the yt-dlp transcode/quality path (that path is YouTube-only).
- `_write_metadata_ogg` via `mutagen.oggvorbis.OggVorbis` + `METADATA_BLOCK_PICTURE`
  (base64 FLAC Picture). Register `".ogg"` in `_METADATA_WRITERS`. Reuses the existing tags dict.

## Filename-honesty fix (small, in-scope)

`_resolve_extended_output` builds the unmarked name via `_format_track_filename`, which hardcodes
`.mp3`. For a real `.ogg` (or `.flac`) that rename would mislabel the container. Fix: preserve the
real extension of `final_path` when building the unmarked path. No-op for the YouTube+mp3 case
(keeps existing tests green); corrects ogg + the latent flac case.

## Config / Settings / packaging

- Append `("Spotify (librespot 320k OGG)","librespot")` to the source dropdown + add "librespot"
  to `KNOWN_DOWNLOAD_SOURCES`. New config keys `spotify_credentials_path`, `librespot_consented`.
- "SPOTIFY ACCOUNT" settings card: Log in/out (runs OAuth on a QThread), shows account + premium,
  one-time consent dialog persisting `librespot_consented`, disclaimer. Greyed unless source==librespot.
- Thread new keys _start_scraper -> ScraperThread -> MusicScraper -> make_backend.
- req.txt + Setlist.spec hiddenimports: librespot (pinned) + Cryptodome, google.protobuf,
  websocket, defusedxml. ffmpeg NOT required for OGG path.
```
