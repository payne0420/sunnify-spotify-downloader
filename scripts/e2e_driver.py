#!/usr/bin/env python
"""Headless E2E driver for Setlist's audio backends.

Drives the real ``MusicScraper`` (the same object the GUI's ``ScraperThread``
wraps) against a live Spotify playlist/track URL — no GUI, no mocks — and
replicates the GUI-side tag write (``WritingMetaTagsThread``) after every
finished track, so the output files match exactly what a user would get.

Run from the repo root with the project venv:

    QT_QPA_PLATFORM=offscreen ./.venv/bin/python scripts/e2e_driver.py \
        --source librespot --format ogg \
        --url https://open.spotify.com/playlist/<id> --out /tmp/e2e/librespot

Every scraper signal is printed as an ``[event]`` line and the full event
stream is dumped to ``<out>/_e2e_events.json`` for assertions (per-track
``actual_ext``, ``via_youtube_fallback``, status messages, failures).

Implementation notes (hard-won, do not "simplify" away):
- All signal connections use ``Qt.DirectConnection``: the scraper emits from
  worker threads and this driver runs no Qt event loop, so default (queued)
  cross-thread connections would silently never deliver.
- Tag writing is replicated from ``MainWindow.add_song_META`` by running
  ``WritingMetaTagsThread.run()`` synchronously — without it, output files
  have no tags and metadata verification is meaningless.
- Never run two driver processes that BOTH open a Spotify session
  (librespot source, or youtube source whose Mercury metadata service has
  stored credentials) at the same time: concurrent logins on one account
  cause transient BadCredentials failures.
"""

import argparse
import json
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.getcwd())

from PyQt5.QtCore import Qt  # noqa: E402

from backends import DEFAULT_FALLBACK_ORDER  # noqa: E402
from Spotify_Downloader import MusicScraper, detect_spotify_url_type  # noqa: E402

_EV_LOCK = threading.Lock()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True, choices=["youtube", "lossless", "librespot"])
    p.add_argument("--format", default="mp3")
    p.add_argument("--quality", default="320")
    p.add_argument("--url", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--extended", action="store_true")
    p.add_argument("--max-extended-minutes", type=int, default=20)
    p.add_argument("--no-strict-title", action="store_true")
    # Default matches the app's config default (artist_first=False). Pass
    # --artist-first to get "Artist - Title.ext" naming like a configured app.
    p.add_argument("--artist-first", action="store_true")
    # librespot knobs
    p.add_argument("--credentials-path", default="")
    p.add_argument("--client-id", default="")
    p.add_argument("--client-secret", default="")
    p.add_argument(
        "--cookies-file",
        default="",
        help="path to a YouTube cookies.txt; authenticates both search and download",
    )
    p.add_argument(
        "--fallback-order",
        default=None,
        help='comma-separated fallback chain (e.g. "youtube" or "librespot,youtube"); '
        '"" = no fallback; unset = app default for source',
    )
    # lossless knobs
    p.add_argument("--tidal-api-url", default="")
    p.add_argument("--lossless-quality", default="27")
    p.add_argument("--lossless-service-order", default="qobuz,amazon")
    p.add_argument("--flac-metadata-source", default="provider")
    args = p.parse_args()

    if args.fallback_order is None:
        fallback_order = DEFAULT_FALLBACK_ORDER[args.source]
    elif args.fallback_order == "":
        fallback_order = ()
    else:
        fallback_order = tuple(s.strip() for s in args.fallback_order.split(",") if s.strip())

    os.makedirs(args.out, exist_ok=True)
    events = []

    def ev(kind, payload):
        rec = {"t": round(time.time(), 2), "kind": kind, "payload": payload}
        with _EV_LOCK:
            events.append(rec)
        print(f"[{kind}] {payload}", flush=True)

    scraper = MusicScraper(
        audio_format=args.format,
        audio_quality=args.quality,
        extended_mix=args.extended,
        max_extended_minutes=args.max_extended_minutes,
        max_track_mb=0,
        artist_first=args.artist_first,
        download_source=args.source,
        extended_strict_match=not args.no_strict_title,
        tidal_api_url=args.tidal_api_url,
        lossless_quality=args.lossless_quality,
        lossless_service_order=args.lossless_service_order,
        fallback_order=fallback_order,
        flac_metadata_source=args.flac_metadata_source,
        spotify_credentials_path=args.credentials_path,
        spotify_client_id=args.client_id,
        spotify_client_secret=args.client_secret,
        youtube_cookies_file=args.cookies_file,
    )

    scraper.error_signal.connect(lambda m: ev("status", m), Qt.DirectConnection)
    scraper.song_meta.connect(
        lambda m: ev(
            "song_meta", {k: m.get(k) for k in ("artists", "title", "album", "track_num")}
        ),
        Qt.DirectConnection,
    )

    def on_done(m):
        ev(
            "done",
            {
                k: m.get(k)
                for k in (
                    "artists",
                    "title",
                    "album",
                    "track_num",
                    "disc_num",
                    "releaseDate",
                    "file",
                    "actual_ext",
                    "via_youtube_fallback",
                    "served_by",
                    "primary_source",
                    "source",
                )
            },
        )
        # Replicate MainWindow.add_song_META's tag write (AddMetaDataCheck on):
        # same WritingMetaTagsThread, run synchronously (no Qt event loop here).
        try:
            from Spotify_Downloader import WritingMetaTagsThread

            t = WritingMetaTagsThread(m, m["file"])
            t.run()
            ev("tagged", m.get("file"))
        except Exception as exc:  # noqa: BLE001
            ev("tag_error", f"{m.get('file')}: {exc}")

    scraper.add_song_meta.connect(on_done, Qt.DirectConnection)
    scraper.PlaylistCompleted.connect(lambda m: ev("completed", m), Qt.DirectConnection)
    scraper.count_updated.connect(lambda c: ev("count", c), Qt.DirectConnection)
    scraper.song_Album.connect(lambda a: ev("album", a), Qt.DirectConnection)

    kind, _ = detect_spotify_url_type(args.url)
    t0 = time.time()
    try:
        if kind in ("playlist", "album"):
            scraper.scrape_playlist(args.url, args.out)
        else:
            scraper.scrape_track(args.url, args.out)
    finally:
        for attr in ("_backend", "_metadata_service"):
            obj = getattr(scraper, attr, None)
            close = getattr(obj, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:  # noqa: BLE001
                    ev("teardown_error", f"{attr}: {exc}")
        ev("elapsed_s", round(time.time() - t0, 1))
        log_path = os.path.join(args.out, "_e2e_events.json")
        with open(log_path, "w") as fh:
            json.dump(events, fh, indent=1, default=str)
        print(f"[log] {log_path}", flush=True)


if __name__ == "__main__":
    main()
