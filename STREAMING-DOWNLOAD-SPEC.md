# Spec — stream downloads while metadata is still fetching

Branch: `feat/streaming-download-while-metadata-fetches` (scope `desktop`).
Worktree off `main` (3cb270d1). Phase 0 of FEATURE-WORKFLOW.

## Goal

Today `scrape_playlist` fully drains the metadata generator into a `tracks` list
(emitting "Fetching track metadata (N of total)…") *before* submitting any
download. Make the multi-worker path submit each track to the download pool the
moment its metadata is **yielded**, so the first download starts after the
*first* track resolves and the rest of the metadata keeps resolving in the
background, overlapping fetch with download.

This is a throughput/UX win, not a behavior change: the set of files produced,
their tags, resume/skip, cancellation, and the per-track progress UI all stay
correct.

## Architecture (verified against live code in this worktree)

- **Producer** — the active provider is `PlaylistClient.iter_playlist_tracks`
  (`spotifydown_api.py:811`), a generator delegating (`yield from`) to
  `SpotifyEmbedAPI.iter_playlist_tracks` (`:409`). It accepts
  `content_type`, `skip_ids`, `on_notice`. Embed tracks (first ≤100) yield in
  **playlist order**; the spclient overflow (>100) yields in **completion
  order** via `as_completed` on a 4-worker pool. On `GeneratorExit`
  (caller `break`/`close`) its `finally` does
  `pool.shutdown(wait=False, cancel_futures=True)` (`:552-554`).
- **Consumer** — `MusicScraper._download_one_track` (`Spotify_Downloader.py:1360`)
  is a self-contained worker; calls `self._backend.fetch(...)`. Every code path
  calls `_finish_track_ui` exactly once **except** the top-of-function
  `if self.is_cancelled(): return None` (`:1375`) which is the cancel fast-path.
- **Barrier (removed)** — `scrape_playlist` (`:1580`) drains the generator into
  `tracks` (`:1636-1650`), then sizes the pool from `len(tracks)` and submits all
  (`:1660-1716`).
- **Progress denominator** — `self._total_tracks` drives both the aggregate
  progress percent in `_finish_track_ui` (`:1519-1524`, parallel mode only) and
  the "Songs downloaded X of N" label in `MainWindow.update_counter`
  (`:3635`). Today it is `len(tracks)` (the resume-filtered, downloadable count).
- **Backends** — `youtube.max_concurrency = min(4, youtube_max_concurrency)`
  (configurable 1–4); `librespot.max_concurrency = 1` (single Spotify session,
  must serialize); `real_flac = 4`. `MusicScraper.MAX_WORKERS = 4`.
- `metadata.track_count` from `get_playlist_metadata` is **always a populated
  int ≥ 0** in production (`spotifydown_api.py:384,397,406`).

## Design

Replace `:1635-1716` with pool sizing from `metadata.track_count` and two paths.

**Sizing (up front, no drain):**
```
expected_total = track_count if isinstance(int) and > 0 else 0   # 0 == unknown
remaining      = max(0, expected_total - len(already_done)) if expected_total else 0
worker_count   = (1 if remaining < 3 else min(MAX_WORKERS, remaining)) if expected_total
                 else MAX_WORKERS            # unknown count → assume large → parallel
worker_count   = max(1, min(worker_count, backend.max_concurrency))
_parallel_mode = worker_count > 1
```

**Single-worker path (`worker_count == 1`)** — tiny playlists *and* single-session
backends (librespot). Preserve today's behavior **exactly**: fully drain the
generator first, then download sequentially with per-track UI. This keeps a
single Spotify session from interleaving metadata and download work, and keeps
the tiny-playlist single-track feel. `_total_tracks = len(tracks)` (exact).

**Multi-worker streaming path (`worker_count > 1`)** — create the executor once;
iterate the generator and `executor.submit(_download_one_track, …, track_num)`
per yielded track (cancel checked at the top of the loop, before submit);
exhaust the generator; then drain in-flight futures with `as_completed` exactly
as today. `_total_tracks` starts at the resume-adjusted `remaining` estimate
(smooth early %) and is corrected to the exact `submitted` count once the
generator is exhausted (non-cancel).

When that correction actually changes the denominator (i.e. `track_count`
mis-estimated the real work), some workers may already have finished and emitted
their percent/count against the stale estimate — leaving the bar stuck below 100
and the label at e.g. "3 of 100". So after the `as_completed` drain completes
normally (the `for…else`), if the denominator was corrected and any track ran,
emit ONE corrective `dlprogress_signal.emit(100)` + `count_updated.emit(counter)`
against the now-exact totals. Skipped when the estimate was right (the common
case), so steady-state emits stay one-per-track. *(Found by Phase-3 adversarial
review; covered by a dedicated test that holds the producer open until all
workers have emitted, so it deterministically reproduces the
worker-finishes-before-correction ordering.)*

**Generator lifetime / cancel** — bind the generator to a variable and
`close()` it (guarded) in a `finally`, in **both** paths, so the provider's
`GeneratorExit`-driven `shutdown(cancel_futures=True)` fires promptly on cancel
instead of relying on GC. The `with ThreadPoolExecutor` exit still waits on
in-flight downloads; cancelled futures `f.cancel()` exactly as today.

**Track numbering** — number by *arrival* (`submitted`/`enumerate`), identical to
today. The real ID3 TRCK comes from Mercury via `_enrich_song_meta` (`:1351`);
`track_num` is only a fallback. Carrying a true playlist index would require
adding a field to `TrackInfo` and touching every construction site — out of
scope and "do not let it block the first download". **Intentional
non-divergence: numbering stays exactly as correct as today.**

**Metadata notice** — keep emitting "Fetching track metadata (N of total)…"
every 10 yields in both paths (reassurance on large playlists); `on_notice`
still wired to `error_signal.emit`.

## Invariants / do-not-break

- Lossless-honesty (`real_flac.py`/`chain.py` byte logic) — untouched.
- Resume/skip: `already_done` pre-scan + `skip_ids=already_done` into the
  generator — unchanged; skipped tracks never downloaded.
- `_parallel_mode` true only in multi-worker; reset only after the executor
  fully drains (`finally`).
- Never exceed `backend.max_concurrency`; single-session backends serialize.
- 3.9-compatible; ruff (line 100, double quotes, py39). `Template.py` untouched.
- No real network in unit tests.

## Intentional divergences (must NOT be "fixed" back)

1. **Multi-worker no longer drains before submitting.** This is the feature.
   `test_generator_is_materialized_before_threading` asserted the old contract;
   its *assertions* (every yield on the main/consumer thread; all yields
   consumed) **remain true** because the generator is still driven solely from
   the consumer thread — but the name/docstring claimed "before threading", now
   false. Repurpose it to assert the still-valid invariant (generator consumed
   on a single thread — generators aren't thread-safe) and add explicit
   streaming-overlap tests.
2. **Pool sizing now depends on `metadata.track_count`.** Existing scrape_playlist
   tests build `meta = MagicMock()` without `track_count`. Real
   `get_playlist_metadata` always returns an int, so this is a **fixture gap**:
   set `meta.track_count = len(tracks)` in the affected tests. Assertions are
   unchanged — this is not weakening a test, it is supplying the input real code
   always has. Tests needing it: small-playlist-sequential (2), and
   aggregate-progress (4, for deterministic `_total_tracks`). Others get it for
   clarity/robustness.

## Test plan (additions + the one repurpose)

New tests (multi-worker streaming path):
- `test_downloads_start_before_metadata_fully_fetched` — generator blocks after
  yielding track 0 until its download starts; assert the first download is logged
  *before* tracks 1..N are yielded (proves overlap, deterministically).
- `test_streaming_denominator_matches_track_count` — parallel denominator comes
  from `track_count`; `_total_tracks` ends == submitted; aggregate % reaches 100.
- `test_streaming_progress_corrects_when_track_count_overestimates` — the
  Phase-3 regression guard: producer held open until all workers emit against the
  stale denominator, so only the corrective post-drain emit lands the bar on 100
  and the label on "3 of 3".
- `test_streaming_cancel_mid_stream_closes_producer_and_pending` — cancel fired
  from the first download; assert generator receives `GeneratorExit`
  (producer cancelled) and tracks after the cancel point are never downloaded.
- `test_streaming_resume_skips_already_done` — manifest pre-seeded; assert
  `skip_ids` carries the done IDs and the resume notice surfaces.
- `test_single_session_backend_serializes` — backend `max_concurrency = 1` with a
  multi-track playlist → single-worker drain path, all downloads on one thread,
  `_parallel_mode is False`.

Repurpose: `test_generator_is_materialized_before_threading` →
`test_generator_consumed_on_single_thread` (assertions kept).

Fixture updates (`meta.track_count = len(tracks)`): small-playlist-sequential,
parallel-uses-max-workers, exception-in-one-worker, existing-files-skipped,
state-resets, song-meta-per-track, aggregate-progress,
youtube-max-concurrency-clamps, and the repurposed single-thread test.

## Test-count arithmetic

- Baseline (this worktree, measured): **642** collected.
- Additions: **+6** new tests (1 repurpose is a rename, net 0). The 6th
  (`…overestimates`) was added in Phase 3 to cover the review-found correction bug.
- Expected after: **648** collected, 0 failures.

## Acceptance criteria

- First download begins after the first metadata resolves, not the last
  (proven by the overlap test + live E2E).
- "X of N", numbering, tags, resume/skip, cancellation all correct.
- Worker concurrency never exceeds `backend.max_concurrency`; tiny-playlist UX
  preserved.
- `ruff check . && ruff format --check . && pytest tests/ -q` green, count == 647,
  codex APPROVE, live E2E (youtube + librespot) passes.
