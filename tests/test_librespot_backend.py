"""Tests for the opt-in librespot backend (native ~320k OGG/Vorbis).

The alpha ``librespot`` library is NOT installed in the test env; everything is
exercised through the thin ``_librespot`` adapter and fakes, so these tests run
without any Spotify network access. Covers the goal's acceptance criteria:

  (a) the read-loop writes exactly the streamed OGG bytes to a ``.ogg`` and skips a
      leading proprietary header correctly;
  (b) the backend returns ``actual_ext == "ogg"`` and never invokes ffmpeg/yt-dlp;
  (c) the OGG tag writer sets Vorbis comments + METADATA_BLOCK_PICTURE;
  (d) the shared selector picks correctly via the librespot search path
      (extended / normal / already-extended / no-match);
  (e) Premium gating: a ``free`` account does not get a native OGG.
"""

from __future__ import annotations

import base64
import inspect
import json
import math
import os
import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest
import requests

import Spotify_Downloader as sd
from backends import KNOWN_DOWNLOAD_SOURCES, make_backend
from backends.chain import FallbackChainBackend
from backends.librespot import _librespot as adapter
from backends.librespot import audio, search, track_metadata, webapi
from backends.librespot.backend import LibrespotBackend
from backends.librespot.errors import (
    LibrespotCancelled,
    LibrespotNotPremium,
    NoExtendedCutError,
    OggCaptureError,
)
from backends.librespot.metadata_service import LibrespotMetadataService
from backends.librespot.session import LibrespotSession

# A real ~1s Ogg Vorbis file (440Hz sine, libsndfile) used to validate native capture
# + tagging through REAL mutagen — not a mock. The mocked tests cover the control flow;
# this proves capture_ogg + _write_metadata_ogg produce a file mutagen actually parses.
_FIXTURE_OGG = os.path.join(os.path.dirname(__file__), "fixtures", "sample_vorbis.ogg")


def _read_fixture() -> bytes:
    with open(_FIXTURE_OGG, "rb") as f:
        return f.read()


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeReader:
    """A byte reader mimicking librespot's AbsChunkedInputStream.read(n)/close()."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0
        self.closed = False

    def read(self, n: int) -> bytes:
        end = min(self._pos + n, len(self._data))
        out = self._data[self._pos : end]
        self._pos = end
        return out

    def close(self):
        self.closed = True


class FakeLoadedStream:
    """Mimics librespot LoadedStream: ``.input_stream`` (+ optional ``.track`` proto)."""

    def __init__(self, data: bytes, *, size: int | None = None, track=None):
        reader = FakeReader(data)
        self.input_stream = SimpleNamespace(
            size=size if size is not None else len(data),
            stream=lambda: reader,
        )
        self._reader = reader
        self.track = track


def _proto_track(
    *,
    name="Never Gonna Give You Up",
    album="Whenever You Need Somebody",
    artists=("Rick Astley",),
    number=1,
    disc_number=1,
    date=(1987, 11, 16),
    cover_ids=(),
):
    """A stand-in for librespot's Metadata.Track protobuf (duck-typed via getattr)."""
    album_ns = None
    if album is not None:
        date_ns = SimpleNamespace(year=date[0], month=date[1], day=date[2]) if date else None
        images = [
            SimpleNamespace(file_id=fid, size=size, width=w, height=h)
            for (fid, size, w, h) in cover_ids
        ]
        album_ns = SimpleNamespace(
            name=album,
            date=date_ns,
            cover_group=SimpleNamespace(image=images),
        )
    return SimpleNamespace(
        name=name,
        album=album_ns,
        artist=[SimpleNamespace(name=a) for a in artists],
        number=number,
        disc_number=disc_number,
    )


def _track(title="Song", artists="Artist", duration_ms=200000, tid="base62id"):
    return SimpleNamespace(title=title, artists=artists, duration_ms=duration_ms, id=tid)


def _fake_scraper(max_extended_minutes=20):
    return SimpleNamespace(
        max_track_duration_s=max_extended_minutes * 60,
        spotify_credentials_path="",
        error_signal=SimpleNamespace(emit=lambda *_a, **_k: None),
    )


def _no_cancel():
    return False


# --------------------------------------------------------------------------- #
# (a) native OGG capture: byte-exact + header skip + cancel + bad-header
# --------------------------------------------------------------------------- #


class TestCaptureOgg:
    def test_writes_exact_bytes_when_stream_starts_at_oggs(self, tmp_path):
        ogg_bytes = b"OggS" + b"\x01\x02\x03" * 5000
        dest = tmp_path / "track.ogg.part"
        written = audio.capture_ogg(
            FakeLoadedStream(ogg_bytes), str(dest), _no_cancel, chunk_size=64
        )
        assert written == len(ogg_bytes)
        assert dest.read_bytes() == ogg_bytes

    def test_skips_leading_proprietary_header(self, tmp_path):
        # Spotify prepends a 167-byte proprietary header; size includes it but only
        # the OggS payload must land on disk so mutagen can parse the file.
        header = b"\x00" * 167
        ogg_bytes = b"OggS" + b"payload-bytes" * 100
        raw = header + ogg_bytes
        dest = tmp_path / "track.ogg.part"
        written = audio.capture_ogg(
            FakeLoadedStream(raw, size=len(raw)), str(dest), _no_cancel, chunk_size=128
        )
        assert dest.read_bytes() == ogg_bytes
        assert dest.read_bytes().startswith(b"OggS")
        assert written == len(ogg_bytes)  # header bytes dropped, not size

    def test_cancel_midstream_raises(self, tmp_path):
        ogg_bytes = b"OggS" + b"x" * 100000
        dest = tmp_path / "track.ogg.part"
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 2  # allow the header read, then cancel

        with pytest.raises(LibrespotCancelled):
            audio.capture_ogg(FakeLoadedStream(ogg_bytes), str(dest), cancel, chunk_size=16)

    def test_no_oggs_in_stream_raises(self, tmp_path):
        dest = tmp_path / "track.ogg.part"
        with pytest.raises(OggCaptureError):
            audio.capture_ogg(
                FakeLoadedStream(b"\x00" * 4096), str(dest), _no_cancel, chunk_size=256
            )


class TestRealOggRoundTrip:
    """End-to-end on a REAL Ogg Vorbis file (not a mock): capture byte-exactness +
    header skip, then tag write/read through real mutagen. Validates the core
    deliverable — a native .ogg that mutagen parses with embedded tags + cover."""

    def test_capture_real_ogg_with_simulated_header(self, tmp_path):
        raw = _read_fixture()
        assert raw.startswith(b"OggS")
        # Prepend a simulated 167-byte Spotify header (the real lib already skips it,
        # but capture_ogg must trim it defensively and still produce a valid OGG).
        prefixed = b"\x00" * 167 + raw
        dest = tmp_path / "captured.ogg"
        written = audio.capture_ogg(
            FakeLoadedStream(prefixed, size=len(prefixed)), str(dest), _no_cancel, chunk_size=512
        )
        assert written == len(raw)
        assert dest.read_bytes() == raw  # byte-for-byte, header dropped

    def test_write_metadata_ogg_real_roundtrip(self, tmp_path):
        from mutagen.flac import Picture
        from mutagen.oggvorbis import OggVorbis

        dest = tmp_path / "tagged.ogg"
        dest.write_bytes(_read_fixture())
        cover = b"\xff\xd8\xff" + b"jpeg-cover-bytes" * 20
        tags = {
            "title": "My Title",
            "artists": "A1, A2",
            "album": "Alb",
            "releaseDate": "2024-01-15",
            "trackNumber": 3,
        }
        sd._write_metadata_ogg(str(dest), tags, cover)

        a = OggVorbis(str(dest))  # real mutagen parse-back
        assert a["title"] == ["My Title"]
        assert a["artist"] == ["A1, A2"]
        assert a["album"] == ["Alb"]
        assert a["date"] == ["2024-01-15"]
        assert a["tracknumber"] == ["3"]
        pic = Picture(base64.b64decode(a["metadata_block_picture"][0]))
        assert pic.data == cover
        assert pic.mime == "image/jpeg"


class TestFetchTrackOgg:
    def test_retries_transient_then_succeeds(self, tmp_path, monkeypatch):
        dest = tmp_path / "out.ogg"
        ogg_bytes = b"OggS" + b"audio" * 200
        attempts = {"n": 0}

        def fake_load(session_raw, tid, quality):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("Failed fetching audio key! gid: x")
            return FakeLoadedStream(ogg_bytes)

        monkeypatch.setattr(adapter, "track_id_from_base62", lambda b: b)
        monkeypatch.setattr(adapter, "vorbis_quality", lambda name: name)
        monkeypatch.setattr(adapter, "load_loaded_stream", fake_load)

        result = audio.fetch_track_ogg(
            object(),
            "base62id",
            dest=str(dest),
            cancel=_no_cancel,
            backoffs=(0, 0, 0),
            sleep=lambda *_: None,
        )
        assert result == str(dest)
        assert dest.read_bytes() == ogg_bytes
        assert attempts["n"] == 2  # retried once

    def test_all_attempts_fail_raises_ogg_capture_error(self, tmp_path, monkeypatch):
        dest = tmp_path / "out.ogg"
        monkeypatch.setattr(adapter, "track_id_from_base62", lambda b: b)
        monkeypatch.setattr(adapter, "vorbis_quality", lambda name: name)

        def always_unavailable(*_a, **_k):
            from backends.librespot.errors import LibrespotError

            raise LibrespotError("Cannot find suitable audio file")

        monkeypatch.setattr(adapter, "load_loaded_stream", always_unavailable)
        with pytest.raises(OggCaptureError):
            audio.fetch_track_ogg(
                object(),
                "base62id",
                dest=str(dest),
                cancel=_no_cancel,
                backoffs=(0, 0, 0),
                sleep=lambda *_: None,
            )
        assert not dest.exists()

    def test_drops_to_high_tier_and_flags_degradation(self, tmp_path, monkeypatch):
        # VERY_HIGH (320k) has no Vorbis file -> strict picker yields FeederException;
        # we drop to HIGH (160k) and MUST tell the user it's not the 320k cut.
        dest = tmp_path / "out.ogg"
        ogg = b"OggS" + b"a" * 500
        monkeypatch.setattr(adapter, "track_id_from_base62", lambda b: b)
        monkeypatch.setattr(adapter, "vorbis_quality", lambda name, **_k: name)

        def fake_load(_s, _tid, quality):
            if quality == "VERY_HIGH":
                from backends.librespot.errors import LibrespotError

                raise LibrespotError("Cannot find suitable audio file")
            return FakeLoadedStream(ogg)

        monkeypatch.setattr(adapter, "load_loaded_stream", fake_load)
        statuses = []
        audio.fetch_track_ogg(
            object(),
            "id",
            dest=str(dest),
            cancel=_no_cancel,
            on_status=statuses.append,
            backoffs=(0, 0, 0),
            sleep=lambda *_: None,
        )
        assert dest.read_bytes() == ogg
        assert any("320k" in s and "HIGH" in s for s in statuses)

    def test_eof_indexerror_at_chunk_boundary_is_not_a_failure(self, tmp_path, monkeypatch):
        # Upstream read() raises IndexError (not b"") when size is an exact 128KiB
        # multiple; capture must treat that as EOF, not discard the complete file.
        ogg = b"OggS" + b"z" * 4096

        class _EOFRaisingReader:
            def __init__(self, data):
                self.d = data
                self.p = 0

            def read(self, n):
                if self.p >= len(self.d):
                    raise IndexError("list index out of range")
                end = min(self.p + n, len(self.d))
                out = self.d[self.p : end]
                self.p = end
                return out

            def close(self):
                pass

        loaded = SimpleNamespace(
            input_stream=SimpleNamespace(size=len(ogg), stream=lambda: _EOFRaisingReader(ogg))
        )
        dest = tmp_path / "out.ogg.part"
        written = audio.capture_ogg(loaded, str(dest), _no_cancel, chunk_size=512)
        assert written == len(ogg)
        assert dest.read_bytes() == ogg


class TestTrimToLastPage:
    """Spotify's native stream can leave a few bytes past the final Ogg page. mutagen
    reads such files but raises when REWRITING tags, so the tag writer (album, cover…)
    silently fails. _trim_to_last_page trims that trailer back to a page boundary."""

    def test_trims_small_trailer_and_makes_file_taggable(self, tmp_path):
        from mutagen.oggvorbis import OggVorbis

        raw = _read_fixture()  # a real, valid Ogg that ends on a page boundary
        f = tmp_path / "t.ogg"
        f.write_bytes(raw + b"\x00\x00\x00")  # 3 trailing bytes, like a real capture

        removed = audio._trim_to_last_page(str(f))
        assert removed == 3
        assert f.read_bytes() == raw  # trailer gone, every real page intact

        a = OggVorbis(str(f))  # now rewritable
        a["album"] = "Whenever You Need Somebody"
        a.save()
        assert OggVorbis(str(f))["album"] == ["Whenever You Need Somebody"]

    def test_noop_on_clean_file(self, tmp_path):
        raw = _read_fixture()
        f = tmp_path / "t.ogg"
        f.write_bytes(raw)
        assert audio._trim_to_last_page(str(f)) == 0
        assert f.read_bytes() == raw

    def test_does_not_reshape_large_non_page_remainder(self, tmp_path):
        # A blob that desyncs after one fake page with lots left over is NOT a real Ogg
        # stream — leave it untouched rather than truncate audio away.
        data = b"OggS" + b"\x01\x02\x03" * 5000
        f = tmp_path / "t.ogg"
        f.write_bytes(data)
        assert audio._trim_to_last_page(str(f)) == 0
        assert f.read_bytes() == data

    def test_noop_when_no_complete_page(self, tmp_path):
        data = b"OggS" + b"z" * 50  # first page claims a body past EOF -> no complete page
        f = tmp_path / "t.ogg"
        f.write_bytes(data)
        assert audio._trim_to_last_page(str(f)) == 0
        assert f.read_bytes() == data

    @staticmethod
    def _ogg_page(*, eos: bool, body: bytes = b"\x01\x02\x03\x04") -> bytes:
        # Minimal well-formed Ogg page: 27-byte header + 1-segment table + body. CRC is
        # not validated by the page walker, so zeros are fine.
        header_type = 0x04 if eos else 0x00
        return (
            b"OggS"
            + bytes([0, header_type])
            + b"\x00" * 8  # granule
            + b"\x00" * 4  # serial
            + b"\x00" * 4  # seqno
            + b"\x00" * 4  # crc
            + bytes([1, len(body)])  # 1 segment of len(body) bytes
            + body
        )

    def test_does_not_trim_trailer_after_non_eos_page(self, tmp_path):
        # A complete final page WITHOUT the EOS flag means the stream may be truncated
        # mid-song; the trailing bytes could be the start of a real (partial) page, so we
        # must NOT trim and pretend the capture is complete.
        data = self._ogg_page(eos=False) + b"\x00\x00\x00"
        f = tmp_path / "t.ogg"
        f.write_bytes(data)
        assert audio._trim_to_last_page(str(f)) == 0
        assert f.read_bytes() == data

    def test_trims_trailer_after_eos_page(self, tmp_path):
        page = self._ogg_page(eos=True)
        data = page + b"\x00\x00\x00"
        f = tmp_path / "t.ogg"
        f.write_bytes(data)
        assert audio._trim_to_last_page(str(f)) == 3
        assert f.read_bytes() == page

    def test_fetch_track_ogg_trims_trailer_so_file_is_taggable(self, tmp_path, monkeypatch):
        from mutagen.oggvorbis import OggVorbis

        raw = _read_fixture()
        loaded = FakeLoadedStream(raw + b"\x00\x00\x00", track=_proto_track())
        monkeypatch.setattr(adapter, "track_id_from_base62", lambda b: b)
        monkeypatch.setattr(adapter, "vorbis_quality", lambda name, **_k: name)
        monkeypatch.setattr(adapter, "load_loaded_stream", lambda *_a, **_k: loaded)
        dest = tmp_path / "out.ogg"
        audio.fetch_track_ogg(
            object(),
            "id",
            dest=str(dest),
            cancel=_no_cancel,
            backoffs=(0, 0, 0),
            sleep=lambda *_: None,
        )
        assert dest.read_bytes() == raw  # trailer trimmed during the real fetch path
        a = OggVorbis(str(dest))
        a["album"] = "Whenever You Need Somebody"
        a.save()  # would raise on the untrimmed file
        assert OggVorbis(str(dest))["album"] == ["Whenever You Need Somebody"]


# --------------------------------------------------------------------------- #
# (b) backend returns ogg, no ffmpeg; (e) premium gating
# --------------------------------------------------------------------------- #


class _FakeSession:
    def __init__(self, product="premium"):
        self._product = product
        self.raw = object()
        self.closed = False

    def is_premium(self):
        return self._product == "premium"

    def is_known_free(self):
        return self._product is not None and self._product != "premium"

    def is_connected(self):
        return True

    def close(self):
        self.closed = True


class TestBackendFetch:
    def _backend(self, monkeypatch, *, product="premium", scraper=None, **kw):
        be = LibrespotBackend(scraper or _fake_scraper(), **kw)
        monkeypatch.setattr(be, "_ensure_session", lambda: _FakeSession(product=product))
        return be

    def test_fetch_returns_native_ogg_without_ffmpeg(self, tmp_path, monkeypatch):
        be = self._backend(monkeypatch, product="premium")
        captured = {}

        def fake_fetch_ogg(
            session_raw, base62, *, dest, cancel, on_status=None, on_metadata=None, on_throttle=None
        ):
            with open(dest, "wb") as f:
                f.write(b"OggS-data")
            captured["dest"] = dest
            captured["base62"] = base62
            return dest

        monkeypatch.setattr(audio, "fetch_track_ogg", fake_fetch_ogg)
        # Guard: ffmpeg/yt-dlp must never be touched on the native path.
        monkeypatch.setattr(sd, "get_ffmpeg_path", lambda: pytest.fail("ffmpeg invoked"))

        dest = tmp_path / "Song - Artist.mp3"  # seam hands us an .mp3 destination
        path, ext, used = be.fetch(
            track=_track(),
            destination=str(dest),
            extended=False,
            audio_format="mp3",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert ext == "ogg"
        assert path.endswith(".ogg")
        assert used is False
        assert captured["base62"] == "base62id"  # streams the pasted id (no search)

    def test_existing_ogg_short_circuits(self, tmp_path, monkeypatch):
        be = self._backend(monkeypatch, product="premium")
        dest_mp3 = tmp_path / "Song - Artist.mp3"
        (tmp_path / "Song - Artist.ogg").write_bytes(b"OggS-existing")
        monkeypatch.setattr(
            audio, "fetch_track_ogg", lambda *_a, **_k: pytest.fail("should not download")
        )
        path, ext, used = be.fetch(
            track=_track(),
            destination=str(dest_mp3),
            extended=False,
            audio_format="mp3",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert path.endswith(".ogg")
        assert ext == "ogg"

    def test_known_free_account_raises_not_premium(self, tmp_path, monkeypatch):
        be = self._backend(monkeypatch, product="free")
        with pytest.raises(LibrespotNotPremium):
            be.fetch(
                track=_track(),
                destination=str(tmp_path / "s.mp3"),
                extended=False,
                audio_format="mp3",
                audio_quality="192",
                cancel=_no_cancel,
            )

    def test_unknown_product_attempts_native_not_fallback(self, tmp_path, monkeypatch):
        # Inconclusive premium detection (e.g. a 429 on the profile probe) must NOT
        # downgrade to YouTube — the backend attempts the native stream.
        be = self._backend(monkeypatch, product=None)
        captured = {}

        def fake_fetch_ogg(
            session_raw, base62, *, dest, cancel, on_status=None, on_metadata=None, on_throttle=None
        ):
            with open(dest, "wb") as f:
                f.write(b"OggS")
            captured["called"] = True
            return dest

        monkeypatch.setattr(audio, "fetch_track_ogg", fake_fetch_ogg)
        path, ext, used = be.fetch(
            track=_track(),
            destination=str(tmp_path / "s.mp3"),
            extended=False,
            audio_format="mp3",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert ext == "ogg" and captured.get("called")

    def test_extended_streams_searched_id(self, tmp_path, monkeypatch):
        be = self._backend(monkeypatch, product="premium")
        monkeypatch.setattr(search, "find_extended_id", lambda *_a, **_k: "EXTENDED_ID")
        captured = {}

        def fake_fetch_ogg(
            session_raw, base62, *, dest, cancel, on_status=None, on_metadata=None, on_throttle=None
        ):
            captured["base62"] = base62
            with open(dest, "wb") as f:
                f.write(b"OggS")
            return dest

        monkeypatch.setattr(audio, "fetch_track_ogg", fake_fetch_ogg)
        path, ext, used = be.fetch(
            track=_track(),
            destination=str(tmp_path / "s.mp3"),
            extended=True,
            audio_format="mp3",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert captured["base62"] == "EXTENDED_ID"
        assert used is True

    def test_extended_no_match_streams_original_unmarked(self, tmp_path, monkeypatch):
        be = self._backend(monkeypatch, product="premium")
        monkeypatch.setattr(search, "find_extended_id", lambda *_a, **_k: None)
        captured = {}

        def fake_fetch_ogg(
            session_raw, base62, *, dest, cancel, on_status=None, on_metadata=None, on_throttle=None
        ):
            captured["base62"] = base62
            with open(dest, "wb") as f:
                f.write(b"OggS")
            return dest

        monkeypatch.setattr(audio, "fetch_track_ogg", fake_fetch_ogg)
        path, ext, used = be.fetch(
            track=_track(tid="orig"),
            destination=str(tmp_path / "s.mp3"),
            extended=True,
            audio_format="mp3",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert captured["base62"] == "orig"  # fell back to the original id
        assert used is False  # NOT mislabeled as extended

    def test_extended_no_match_with_has_fallback_raises(self, tmp_path, monkeypatch):
        be = self._backend(monkeypatch, product="premium")
        monkeypatch.setattr(search, "find_extended_id", lambda *_a, **_k: None)
        monkeypatch.setattr(
            audio,
            "fetch_track_ogg",
            lambda *_a, **_k: pytest.fail("must not capture native when chain will advance"),
        )
        with pytest.raises(NoExtendedCutError):
            be.fetch(
                track=_track(),
                destination=str(tmp_path / "s.mp3"),
                extended=True,
                audio_format="mp3",
                audio_quality="192",
                cancel=_no_cancel,
                has_fallback=True,
            )

    def test_extended_no_match_without_has_fallback_streams_original(self, tmp_path, monkeypatch):
        be = self._backend(monkeypatch, product="premium")
        monkeypatch.setattr(search, "find_extended_id", lambda *_a, **_k: None)
        captured = {}

        def fake_fetch_ogg(
            session_raw, base62, *, dest, cancel, on_status=None, on_metadata=None, on_throttle=None
        ):
            captured["base62"] = base62
            with open(dest, "wb") as f:
                f.write(b"OggS")
            return dest

        monkeypatch.setattr(audio, "fetch_track_ogg", fake_fetch_ogg)
        path, ext, used = be.fetch(
            track=_track(tid="orig"),
            destination=str(tmp_path / "s.mp3"),
            extended=True,
            audio_format="mp3",
            audio_quality="192",
            cancel=_no_cancel,
            has_fallback=False,
        )
        assert captured["base62"] == "orig"
        assert used is False

    def test_extended_search_failure_streams_original_not_youtube(self, tmp_path, monkeypatch):
        # Search FAILS (e.g. 429) — stream the original native track, NOT NoExtendedCutError.
        be = self._backend(monkeypatch, product="premium")

        def boom(*_a, **_k):
            raise RuntimeError("API rate limit exceeded")

        monkeypatch.setattr(search, "find_extended_id", boom)
        captured = {}

        def fake_fetch_ogg(
            session_raw, base62, *, dest, cancel, on_status=None, on_metadata=None, on_throttle=None
        ):
            captured["base62"] = base62
            with open(dest, "wb") as f:
                f.write(b"OggS")
            return dest

        monkeypatch.setattr(audio, "fetch_track_ogg", fake_fetch_ogg)
        path, ext, used = be.fetch(
            track=_track(tid="orig"),
            destination=str(tmp_path / "s.mp3"),
            extended=True,
            audio_format="mp3",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert ext == "ogg"
        assert used is False
        assert captured["base62"] == "orig"  # streamed the original, native

    def test_max_concurrency_is_one(self):
        assert LibrespotBackend.max_concurrency == 1


# --------------------------------------------------------------------------- #
# (c) OGG tag writer
# --------------------------------------------------------------------------- #


class TestWriteMetadataOgg:
    def test_sets_vorbis_comments_and_picture(self, tmp_path, monkeypatch):
        import mutagen.oggvorbis

        instances = []

        class FakeOgg(dict):
            def __init__(self, filename):
                super().__init__()
                self.filename = filename
                self.saved = False
                instances.append(self)

            def save(self):
                self.saved = True

        monkeypatch.setattr(mutagen.oggvorbis, "OggVorbis", FakeOgg)

        tags = {
            "title": "My Title",
            "artists": "A1, A2",
            "album": "My Album",
            "releaseDate": "2024-01-15",
            "trackNumber": 3,
        }
        sd._write_metadata_ogg("/tmp/x.ogg", tags, cover_bytes=b"\xff\xd8jpegbytes")
        a = instances[-1]
        assert a.saved is True
        assert a["title"] == "My Title"
        assert a["artist"] == "A1, A2"
        assert a["album"] == "My Album"
        assert a["date"] == "2024-01-15"
        assert a["tracknumber"] == "3"
        assert "metadata_block_picture" in a
        assert isinstance(a["metadata_block_picture"], list)

    def test_no_cover_omits_picture(self, tmp_path, monkeypatch):
        import mutagen.oggvorbis

        instances = []

        class FakeOgg(dict):
            def __init__(self, filename):
                super().__init__()
                instances.append(self)

            def save(self):
                pass

        monkeypatch.setattr(mutagen.oggvorbis, "OggVorbis", FakeOgg)
        sd._write_metadata_ogg("/tmp/x.ogg", {"title": "T", "artists": "A", "album": ""}, None)
        assert "metadata_block_picture" not in instances[-1]

    def test_ogg_registered_in_metadata_writers(self):
        assert ".ogg" in sd._METADATA_WRITERS
        assert sd._METADATA_WRITERS[".ogg"] is sd._write_metadata_ogg


# --------------------------------------------------------------------------- #
# (d) extended search selection
# --------------------------------------------------------------------------- #


def _spotify_item(tid, name, dur_s, artists):
    return {
        "id": tid,
        "name": name,
        "duration_ms": int(dur_s * 1000),
        "artists": [{"name": a} for a in artists],
    }


class TestExtendedSearch:
    def _patch_candidates(self, monkeypatch, items):
        monkeypatch.setattr(
            search,
            "_fetch_candidates",
            lambda _session_raw, _query, **_kw: [
                {
                    "id": it["id"],
                    "title": it["name"],
                    "duration_s": (it["duration_ms"] / 1000) or None,
                    "artists": [a["name"] for a in it["artists"]],
                }
                for it in items
            ],
        )

    def test_picks_longest_keyworded_extended_cut(self, monkeypatch):
        self._patch_candidates(
            monkeypatch,
            [
                _spotify_item("radio", "Song", 200, ["Artist"]),
                _spotify_item("ext1", "Song (Extended Mix)", 360, ["Artist"]),
                _spotify_item("ext2", "Song - Extended", 300, ["Artist"]),
            ],
        )
        chosen = search.find_extended_id(
            object(), title="Song", artists="Artist", expected_s=200, max_track_duration_s=1200
        )
        assert chosen == "ext1"

    def test_rejects_cover_by_other_artist(self, monkeypatch):
        self._patch_candidates(
            monkeypatch,
            [
                _spotify_item("cover", "Song (Extended Mix)", 360, ["Tribute Band"]),
            ],
        )
        chosen = search.find_extended_id(
            object(),
            title="Song",
            artists="Real Artist",
            expected_s=200,
            max_track_duration_s=1200,
        )
        assert chosen is None

    def test_rejects_sped_up(self, monkeypatch):
        self._patch_candidates(
            monkeypatch,
            [
                _spotify_item("sped", "Song (Sped Up Extended)", 360, ["Artist"]),
            ],
        )
        chosen = search.find_extended_id(
            object(), title="Song", artists="Artist", expected_s=200, max_track_duration_s=1200
        )
        assert chosen is None

    def test_no_extended_candidate_returns_none(self, monkeypatch):
        self._patch_candidates(
            monkeypatch,
            [
                _spotify_item("a", "Song", 360, ["Artist"]),  # no extended keyword
            ],
        )
        chosen = search.find_extended_id(
            object(), title="Song", artists="Artist", expected_s=200, max_track_duration_s=1200
        )
        assert chosen is None

    def test_already_extended_fallback_window(self, monkeypatch):
        # 205s isn't > expected+7=207, so the already-extended near window catches it.
        self._patch_candidates(
            monkeypatch,
            [
                _spotify_item("a", "Song (Extended Mix)", 205, ["Artist"]),
            ],
        )
        chosen = search.find_extended_id(
            object(), title="Song", artists="Artist", expected_s=200, max_track_duration_s=1200
        )
        assert chosen == "a"

    def test_search_failure_propagates(self, monkeypatch):
        # A search/network/token failure must PROPAGATE (not collapse to None), so the
        # backend can distinguish it from a genuine "no extended cut" and stream the
        # original native track instead of diverting to YouTube.
        def boom(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr(search, "_fetch_candidates", boom)
        with pytest.raises(RuntimeError):
            search.find_extended_id(
                object(), title="Song", artists="Artist", expected_s=200, max_track_duration_s=1200
            )


class TestClientCredentialsToken:
    """The user's own app token (Client-Credentials) is fetched once and cached until
    ~1min before expiry, so /v1/search rides a proper per-app rate-limit bucket."""

    class _Resp:
        def __init__(self, status=200, body=None):
            self.status_code = status
            self._body = body or {}

        def json(self):
            return self._body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(str(self.status_code))

    def test_fetches_caches_and_reuses(self, monkeypatch):
        clock = {"t": 1000.0}
        posts = {"n": 0}

        def fake_post(_url, data=None, timeout=None):
            posts["n"] += 1
            assert data["grant_type"] == "client_credentials"
            assert data["client_id"] == "cid" and data["client_secret"] == "sec"
            return self._Resp(200, {"access_token": "TOK1", "expires_in": 3600})

        monkeypatch.setattr("backends.librespot.webapi.requests.post", fake_post)
        tk = webapi.ClientCredentialsToken("cid", "sec", _clock=lambda: clock["t"])
        assert tk.get() == "TOK1"
        clock["t"] += 100  # still inside the (3600 - 60) validity window
        assert tk.get() == "TOK1"
        assert posts["n"] == 1  # served from cache, not re-fetched

    def test_refreshes_after_expiry(self, monkeypatch):
        clock = {"t": 0.0}
        tokens = iter(["TOK1", "TOK2"])

        def fake_post(_url, data=None, timeout=None):
            return self._Resp(200, {"access_token": next(tokens), "expires_in": 3600})

        monkeypatch.setattr("backends.librespot.webapi.requests.post", fake_post)
        tk = webapi.ClientCredentialsToken("cid", "sec", _clock=lambda: clock["t"])
        assert tk.get() == "TOK1"
        clock["t"] = 3600  # past the refresh point (3600 - 60 = 3540)
        assert tk.get() == "TOK2"

    def test_raises_on_auth_failure(self, monkeypatch):
        monkeypatch.setattr(
            "backends.librespot.webapi.requests.post",
            lambda *_a, **_k: self._Resp(400, {"error": "invalid_client"}),
        )
        tk = webapi.ClientCredentialsToken("bad", "bad")
        with pytest.raises(requests.HTTPError):
            tk.get()


class TestSearchTokenSource:
    """`_fetch_candidates` uses a supplied app token verbatim and only falls back to the
    keymaster token when none is given; `find_extended_id` forwards `web_token`."""

    class _Resp:
        status_code = 200

        def json(self):
            return {"tracks": {"items": []}}

        def raise_for_status(self):
            pass

    def _capture_get(self, seen):
        def fake_get(_url, **kwargs):
            seen["auth"] = kwargs["headers"]["Authorization"]
            return self._Resp()

        return fake_get

    def test_uses_provided_token_not_keymaster(self, monkeypatch):
        seen = {}
        monkeypatch.setattr("backends.librespot.search.requests.get", self._capture_get(seen))
        monkeypatch.setattr(
            adapter,
            "web_bearer_token",
            lambda *_a, **_k: pytest.fail("keymaster token must not be used when web_token given"),
        )
        search._fetch_candidates(object(), "q", token="APPTOKEN")
        assert seen["auth"] == "Bearer APPTOKEN"

    def test_falls_back_to_keymaster_when_no_token(self, monkeypatch):
        seen = {}
        monkeypatch.setattr("backends.librespot.search.requests.get", self._capture_get(seen))
        monkeypatch.setattr(adapter, "web_bearer_token", lambda *_a, **_k: "KEYMASTER")
        search._fetch_candidates(object(), "q")
        assert seen["auth"] == "Bearer KEYMASTER"

    def test_find_extended_id_forwards_web_token(self, monkeypatch):
        seen = {}

        def fake_fetch(_session_raw, _query, *, limit=20, token=None):
            seen["token"] = token
            return []

        monkeypatch.setattr(search, "_fetch_candidates", fake_fetch)
        search.find_extended_id(
            object(),
            title="Song",
            artists="Artist",
            expected_s=200,
            max_track_duration_s=1200,
            web_token="APPTOK",
        )
        assert seen["token"] == "APPTOK"


# --------------------------------------------------------------------------- #
# (e) premium detection: librespot 'type' attribute FIRST, /v1/me as 429-aware
#     fallback; an inconclusive result is NOT treated as "free"
# --------------------------------------------------------------------------- #

_NO_SLEEP = {"_sleep": lambda *_: None, "_attr_polls": 1}


class TestPremiumDetection:
    def _session(self):
        sess = LibrespotSession("/tmp/creds.json")
        sess._session = object()
        return sess

    def test_premium_via_attribute(self, monkeypatch):
        sess = self._session()
        monkeypatch.setattr(adapter, "product_type", lambda _s: "premium")
        assert sess._detect_product(**_NO_SLEEP) == "premium"

    def test_free_via_attribute_is_known_free(self, monkeypatch):
        sess = self._session()
        monkeypatch.setattr(adapter, "product_type", lambda _s: "free")
        sess._product = sess._detect_product(**_NO_SLEEP)
        assert sess._product == "free"
        assert sess.is_premium() is False
        assert sess.is_known_free() is True

    def test_attribute_preferred_over_profile(self, monkeypatch):
        # When the librespot attribute is present, /v1/me must NOT be called at all.
        sess = self._session()
        monkeypatch.setattr(adapter, "product_type", lambda _s: "premium")
        monkeypatch.setattr(
            "backends.librespot.session.requests.get",
            lambda *_a, **_k: pytest.fail("/v1/me must not be called when attribute present"),
        )
        assert sess._detect_product(**_NO_SLEEP) == "premium"

    def test_falls_back_to_profile_when_attribute_empty(self, monkeypatch):
        sess = self._session()
        monkeypatch.setattr(adapter, "product_type", lambda _s: None)
        seen = {}

        def fake_token(_session, scope="user-read-email"):
            seen["scope"] = scope
            return "tok"

        monkeypatch.setattr(adapter, "web_bearer_token", fake_token)

        class FakeResp:
            status_code = 200

            def json(self):
                return {"product": "premium"}

        monkeypatch.setattr("backends.librespot.session.requests.get", lambda *_a, **_k: FakeResp())
        assert sess._detect_product(**_NO_SLEEP) == "premium"
        # /v1/me only returns `product` with the user-read-private scope.
        assert seen["scope"] == "user-read-private"

    def test_429_profile_is_inconclusive_not_free(self, monkeypatch):
        # A rate-limited profile probe must yield None (unknown) — NOT "free" — so the
        # backend won't downgrade a Premium user to YouTube on a transient 429.
        sess = self._session()
        monkeypatch.setattr(adapter, "product_type", lambda _s: None)
        monkeypatch.setattr(adapter, "web_bearer_token", lambda *_a, **_k: "tok")

        class Resp429:
            status_code = 429
            headers = {"Retry-After": "0"}

            def json(self):
                return {}

        monkeypatch.setattr("backends.librespot.session.requests.get", lambda *_a, **_k: Resp429())
        sess._product = sess._detect_product(**_NO_SLEEP)
        assert sess._product is None
        assert sess.is_known_free() is False  # inconclusive != free
        assert sess.is_premium() is False


class _FakeChunkedStream:
    """Minimal ``AbsChunkedInputStream`` stand-in for preload-loop tests."""

    preload_ahead = adapter.PRELOAD_AHEAD_CHUNKS
    preload_chunk_retries = 2
    closed = False
    chunk_exception = None
    wait_for_chunk = -1
    wait_lock = threading.Condition()

    def __init__(self, *, n_chunks: int = 10):
        self._requested = [False] * n_chunks
        self._available = [False] * n_chunks
        self.retries = [0] * n_chunks
        self.requested: list[int] = []

    def requested_chunks(self):
        return self._requested

    def available_chunks(self):
        return self._available

    def chunks(self):
        return len(self._requested)

    def request_chunk_from_stream(self, index: int) -> None:
        self.requested.append(index)

    def stream_read_halted(self, chunk: int, _time: int) -> None:
        pass

    def should_retry(self, chunk: int) -> bool:
        return False

    def check_availability(self, chunk: int, wait: bool, halted: bool) -> None:
        adapter._fixed_check_availability(self, chunk, wait, halted)


class _SyncFailingFakeChunkedStream(_FakeChunkedStream):
    """``request_chunk_from_stream`` fails synchronously (pre-arm race reproducer)."""

    def request_chunk_from_stream(self, index: int) -> None:
        self.requested.append(index)
        self.notify_chunk_error(index, RuntimeError("instant chunk fail"))

    def notify_chunk_error(self, index: int, exc: BaseException) -> None:
        self._available[index] = False
        self._requested[index] = False
        self.retries[index] += 1
        with self.wait_lock:
            if index == self.wait_for_chunk:
                self.chunk_exception = exc
                self.wait_for_chunk = -1
                self.wait_lock.notify_all()


# Pinned upstream ``check_availability`` body (buggy preload loop) for guard tests.
_BROKEN_CHECK_AVAILABILITY_SOURCE = (
    "    def check_availability(self, chunk: int, wait: bool, halted: bool) -> None:\n"
    "        if halted and not wait:\n"
    "            raise TypeError()\n"
    "        if not self.requested_chunks()[chunk]:\n"
    "            self.request_chunk_from_stream(chunk)\n"
    "            self.requested_chunks()[chunk] = True\n"
    "        for i in range(chunk + 1,\n"
    "                       min(self.chunks() - 1, chunk + self.preload_ahead) + 1):\n"
    "            if (self.requested_chunks()[i]\n"
    "                    and self.retries[i] < self.preload_chunk_retries):\n"
    "                self.request_chunk_from_stream(i)\n"
    "                self.requested_chunks()[chunk] = True\n"
)
_BROKEN_NOTIFY_CHUNK_ERROR_SOURCE = (
    "    def notify_chunk_error(self, index: int, exception: Exception) -> None:\n"
    "        self.available_chunks()[index] = False\n"
    "        self.requested_chunks()[index] = False\n"
    "        self.retries[index] += 1\n"
    "        with self.wait_lock:\n"
    "            if index == self.wait_for_chunk:\n"
    "                self.chunk_exception = exception\n"
    "                self.wait_for_chunk = -1\n"
    "                self.wait_lock.notify_all()\n"
)


def _reset_patch_state(monkeypatch) -> None:
    monkeypatch.setattr(adapter, "_CHECK_AVAILABILITY_PATCHED", False, raising=False)
    monkeypatch.setattr(adapter, "_CHECK_AVAILABILITY_PATCH_STATUS", None, raising=False)


def _install_dummy_audio_module(monkeypatch, cls) -> None:
    mod = types.ModuleType("librespot.audio")
    mod.AbsChunkedInputStream = cls
    monkeypatch.setitem(sys.modules, "librespot.audio", mod)


class TestCheckAvailabilityPatch:
    """Guards the upstream preload-loop bugfix shim in ``_librespot``."""

    def test_fixed_preloads_ahead_chunks(self):
        stream = _FakeChunkedStream(n_chunks=10)
        adapter._fixed_check_availability(stream, 0, False, False)
        assert stream.requested == list(range(9))  # chunks 0..8 (depth 8 ahead of 0)
        assert stream._requested[0:9] == [True] * 9

    def test_fixed_preload_clamps_at_eof(self):
        stream = _FakeChunkedStream(n_chunks=3)
        adapter._fixed_check_availability(stream, 1, False, False)
        assert stream.requested == [1, 2]

    def test_fixed_marks_preloaded_index_not_parent(self):
        stream = _FakeChunkedStream(n_chunks=10)
        adapter._fixed_check_availability(stream, 2, False, False)
        assert stream._requested[3] is True
        assert stream._requested[2] is True

    def test_buggy_upstream_logic_would_skip_preload(self):
        stream = _FakeChunkedStream(n_chunks=10)

        def buggy(self, chunk, wait, halted):
            if not self.requested_chunks()[chunk]:
                self.request_chunk_from_stream(chunk)
                self.requested_chunks()[chunk] = True
            for i in range(
                chunk + 1,
                min(self.chunks() - 1, chunk + self.preload_ahead) + 1,
            ):
                if self.requested_chunks()[i] and self.retries[i] < self.preload_chunk_retries:
                    self.request_chunk_from_stream(i)
                    self.requested_chunks()[chunk] = True

        buggy(stream, 0, False, False)
        assert stream.requested == [0]

    def test_halted_without_wait_raises_type_error(self):
        stream = _FakeChunkedStream()
        with pytest.raises(TypeError):
            adapter._fixed_check_availability(stream, 0, False, True)

    def test_wait_path_unblocks_when_chunk_becomes_available(self):
        stream = _FakeChunkedStream(n_chunks=5)

        def wake():
            time.sleep(0.05)
            with stream.wait_lock:
                stream._available[2] = True
                stream.wait_lock.notify_all()

        threading.Thread(target=wake, daemon=True).start()
        worker = threading.Thread(
            target=adapter._fixed_check_availability,
            args=(stream, 2, True, False),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert stream._available[2]

    def test_wait_path_calls_stream_read_halted_not_when_halted(self):
        stream = _FakeChunkedStream(n_chunks=5)
        halted_calls: list[tuple[int, int]] = []
        stream.stream_read_halted = lambda chunk, ts: halted_calls.append((chunk, ts))

        def wake():
            time.sleep(0.05)
            with stream.wait_lock:
                stream._available[1] = True
                stream.wait_lock.notify_all()

        threading.Thread(target=wake, daemon=True).start()
        worker = threading.Thread(
            target=adapter._fixed_check_availability,
            args=(stream, 1, True, False),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert halted_calls and halted_calls[0][0] == 1

        halted_calls.clear()
        stream._available[1] = False
        worker2 = threading.Thread(
            target=adapter._fixed_check_availability,
            args=(stream, 1, True, True),
            daemon=True,
        )

        def wake2():
            time.sleep(0.05)
            with stream.wait_lock:
                stream._available[1] = True
                stream.wait_lock.notify_all()

        threading.Thread(target=wake2, daemon=True).start()
        worker2.start()
        worker2.join(timeout=2)
        assert not worker2.is_alive()
        # Upstream quirk: only the pre-wait call is gated by ``not halted``; the
        # post-wait success path still invokes ``stream_read_halted``.
        assert len(halted_calls) == 1
        assert halted_calls[0][0] == 1

    def test_wait_path_retries_with_log10_sleep_and_recursion(self, monkeypatch):
        stream = _FakeChunkedStream(n_chunks=5)
        stream.retries[2] = 100
        stream.should_retry = lambda _chunk: True
        sleeps: list[float] = []
        recursed: list[bool] = []
        entered_wait = threading.Event()
        stream.stream_read_halted = lambda *_a, **_k: entered_wait.set()
        stream.check_availability = lambda *_a, **_k: recursed.append(True)
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

        def wake_with_error():
            assert entered_wait.wait(timeout=2)
            with stream.wait_lock:
                stream.chunk_exception = RuntimeError("chunk fail")
                stream._available[2] = True
                stream.wait_lock.notify_all()

        threading.Thread(target=wake_with_error, daemon=True).start()
        worker = threading.Thread(
            target=adapter._fixed_check_availability,
            args=(stream, 2, True, False),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert sleeps == [math.log10(100)]
        assert recursed == [True]

    def test_wait_path_raises_chunk_exception_when_not_retrying(self, monkeypatch):
        class DummyAbsChunkedInputStream:
            class ChunkException(Exception):
                pass

        _install_dummy_audio_module(monkeypatch, DummyAbsChunkedInputStream)
        stream = _FakeChunkedStream(n_chunks=5)
        stream.should_retry = lambda _chunk: False

        def wake_with_error():
            time.sleep(0.05)
            with stream.wait_lock:
                stream.chunk_exception = RuntimeError("chunk fail")
                stream._available[3] = True
                stream.wait_lock.notify_all()

        threading.Thread(target=wake_with_error, daemon=True).start()
        caught: list[BaseException] = []

        def run():
            try:
                adapter._fixed_check_availability(stream, 3, True, False)
            except BaseException as exc:
                caught.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(caught) == 1
        assert isinstance(caught[0], DummyAbsChunkedInputStream.ChunkException)

    def test_patch_applies_when_broken_markers_present(self, monkeypatch):
        _reset_patch_state(monkeypatch)

        class DummyAbsChunkedInputStream:
            preload_ahead = 0

            @staticmethod
            def check_availability(self, chunk, wait, halted):  # noqa: ARG004
                pass

            @staticmethod
            def notify_chunk_error(self, index, exception):  # noqa: ARG004
                pass

        _install_dummy_audio_module(monkeypatch, DummyAbsChunkedInputStream)
        monkeypatch.setattr(
            inspect,
            "getsource",
            lambda fn: (
                _BROKEN_CHECK_AVAILABILITY_SOURCE
                if fn.__name__ == "check_availability"
                else _BROKEN_NOTIFY_CHUNK_ERROR_SOURCE
            ),
        )
        adapter._apply_check_availability_patch()
        assert DummyAbsChunkedInputStream.check_availability is adapter._fixed_check_availability
        assert DummyAbsChunkedInputStream.preload_ahead == adapter.PRELOAD_AHEAD_CHUNKS
        assert adapter.check_availability_patch_status() == adapter.PATCH_STATUS_APPLIED

    def test_patch_skips_when_markers_absent(self, monkeypatch):
        _reset_patch_state(monkeypatch)

        def original(self, _c, _w, _h):
            pass

        class DummyAbsChunkedInputStream:
            check_availability = original
            preload_ahead = 99

            @staticmethod
            def notify_chunk_error(self, index, exception):  # noqa: ARG004
                pass

        _install_dummy_audio_module(monkeypatch, DummyAbsChunkedInputStream)
        monkeypatch.setattr(
            inspect,
            "getsource",
            lambda fn: (
                "def check_availability(self, chunk, wait, halted): pass"
                if fn.__name__ == "check_availability"
                else _BROKEN_NOTIFY_CHUNK_ERROR_SOURCE
            ),
        )
        adapter._apply_check_availability_patch()
        assert DummyAbsChunkedInputStream.check_availability is original
        assert DummyAbsChunkedInputStream.preload_ahead == 99
        assert (
            adapter.check_availability_patch_status() == adapter.PATCH_STATUS_SKIPPED_INCOMPATIBLE
        )

    def test_patch_skips_when_notify_chunk_error_marker_absent(self, monkeypatch):
        _reset_patch_state(monkeypatch)

        def original(self, _c, _w, _h):
            pass

        class DummyAbsChunkedInputStream:
            check_availability = original
            preload_ahead = 99

            @staticmethod
            def notify_chunk_error(self, index, exception):  # noqa: ARG004
                pass

        _install_dummy_audio_module(monkeypatch, DummyAbsChunkedInputStream)
        monkeypatch.setattr(
            inspect,
            "getsource",
            lambda fn: (
                _BROKEN_CHECK_AVAILABILITY_SOURCE
                if fn.__name__ == "check_availability"
                else "def notify_chunk_error(self, index, exception): pass"
            ),
        )
        adapter._apply_check_availability_patch()
        assert DummyAbsChunkedInputStream.check_availability is original
        assert DummyAbsChunkedInputStream.preload_ahead == 99
        assert (
            adapter.check_availability_patch_status() == adapter.PATCH_STATUS_SKIPPED_INCOMPATIBLE
        )

    @pytest.mark.parametrize(
        "getsource_exc",
        [OSError("source code not available"), TypeError("source code not available")],
    )
    def test_patch_source_unavailable_on_frozen_build(self, monkeypatch, getsource_exc):
        _reset_patch_state(monkeypatch)

        class DummyAbsChunkedInputStream:
            preload_ahead = 0

            @staticmethod
            def check_availability(self, chunk, wait, halted):  # noqa: ARG004
                pass

            @staticmethod
            def notify_chunk_error(self, index, exception):  # noqa: ARG004
                pass

        _install_dummy_audio_module(monkeypatch, DummyAbsChunkedInputStream)

        def raise_getsource(_fn):
            raise getsource_exc

        monkeypatch.setattr(inspect, "getsource", raise_getsource)
        adapter._apply_check_availability_patch()
        assert DummyAbsChunkedInputStream.check_availability is adapter._fixed_check_availability
        assert DummyAbsChunkedInputStream.preload_ahead == adapter.PRELOAD_AHEAD_CHUNKS
        assert adapter.check_availability_patch_status() == adapter.PATCH_STATUS_SOURCE_UNAVAILABLE

    def test_instant_failure_of_waited_chunk_raises_not_hangs(self, monkeypatch):
        class DummyAbsChunkedInputStream:
            class ChunkException(Exception):
                pass

        _install_dummy_audio_module(monkeypatch, DummyAbsChunkedInputStream)
        stream = _SyncFailingFakeChunkedStream(n_chunks=10)
        stream.should_retry = lambda _chunk: False
        caught: list[BaseException] = []

        def run():
            try:
                adapter._fixed_check_availability(stream, 0, True, False)
            except BaseException as exc:
                caught.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=3)
        assert not worker.is_alive()
        assert len(caught) == 1
        assert isinstance(caught[0], DummyAbsChunkedInputStream.ChunkException)

    def test_instant_failure_of_preload_chunk_not_stuck_requested(self):
        stream = _SyncFailingFakeChunkedStream(n_chunks=10)
        adapter._fixed_check_availability(stream, 0, False, False)
        for i in range(9):  # chunks 0..8 preloaded
            assert stream._requested[i] is False
            assert stream.retries[i] == 1

    def test_wait_wakes_on_chunk_error_and_raises(self, monkeypatch):
        class DummyAbsChunkedInputStream:
            class ChunkException(Exception):
                pass

        _install_dummy_audio_module(monkeypatch, DummyAbsChunkedInputStream)
        stream = _FakeChunkedStream(n_chunks=5)
        stream.should_retry = lambda _chunk: False
        caught: list[BaseException] = []

        def run():
            try:
                adapter._fixed_check_availability(stream, 2, True, False)
            except BaseException as exc:
                caught.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        time.sleep(0.05)
        with stream.wait_lock:
            stream.chunk_exception = RuntimeError("chunk fail")
            stream.wait_lock.notify_all()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(caught) == 1
        assert isinstance(caught[0], DummyAbsChunkedInputStream.ChunkException)

    def test_wait_wakes_on_close(self):
        stream = _FakeChunkedStream(n_chunks=5)

        def run():
            adapter._fixed_check_availability(stream, 2, True, False)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        time.sleep(0.05)
        with stream.wait_lock:
            stream.closed = True
            stream.wait_lock.notify_all()
        worker.join(timeout=2)
        assert not worker.is_alive()


# Pinned upstream ``CdnManager.Streamer`` bodies for CDN guard tests.
_BROKEN_CDN_REQUEST_CHUNK_SOURCE = (
    "        def request_chunk(self, index: int) -> None:\n"
    "            response = self.request(index)\n"
    "            self.write_chunk(response.buffer, index, False)\n"
)
_BROKEN_CDN_REQUEST_SOURCE = (
    "        def request(self, chunk: int = None, range_start: int = None, range_end: int = None)\n"
    "                -> CdnManager.InternalResponse:\n"
    "            if chunk is None and range_start is None and range_end is None:\n"
    "                raise TypeError()\n"
    "            if chunk is not None:\n"
    "                range_start = ChannelManager.chunk_size * chunk\n"
    "                range_end = (chunk + 1) * ChannelManager.chunk_size - 1\n"
    "            response = self.__session.client().get(\n"
    "                self.__cdn_url.url,\n"
    "                headers=CaseInsensitiveDict({\n"
    '                    "Range": "bytes={}-{}".format(range_start, range_end)\n'
    "                }),\n"
    "            )\n"
    "            if response.status_code != 206:\n"
    "                raise IOError(response.status_code)\n"
    "            body = response.content\n"
    "            if body is None:\n"
    '                raise IOError("Response body is empty!")\n'
    "            return CdnManager.InternalResponse(body, response.headers)\n"
)


def _reset_cdn_patch_state(monkeypatch) -> None:
    monkeypatch.setattr(adapter, "_CDN_ROBUSTNESS_PATCHED", False, raising=False)
    monkeypatch.setattr(adapter, "_CDN_ROBUSTNESS_PATCH_STATUS", None, raising=False)


def _install_dummy_cdn_streamer_module(monkeypatch, streamer_cls) -> None:
    mod = types.ModuleType("librespot.audio")
    cdn = types.SimpleNamespace(Streamer=streamer_cls)
    mod.CdnManager = cdn
    monkeypatch.setitem(sys.modules, "librespot.audio", mod)


class TestCdnRobustnessPatch:
    """Guards the CDN timeout + executor-error notify shim in ``_librespot``."""

    def test_request_chunk_failure_notifies_internal_stream(self):
        calls: list[tuple[int, BaseException]] = []
        internal = SimpleNamespace(notify_chunk_error=lambda index, exc: calls.append((index, exc)))
        err = RuntimeError("cdn down")

        class DummyStreamer:
            def request(self, index: int):
                raise err

            def write_chunk(self, *_a, **_k):
                pytest.fail("write_chunk must not run on failure")

        streamer = DummyStreamer()
        streamer._Streamer__internal_stream = internal
        adapter._fixed_cdn_request_chunk(streamer, 3)
        assert calls == [(3, err)]

    def test_request_chunk_success_writes_chunk(self):
        calls: list[tuple[int, BaseException]] = []
        written: list[tuple[bytes, int, bool]] = []
        internal = SimpleNamespace(notify_chunk_error=lambda index, exc: calls.append((index, exc)))
        payload = b"chunk-bytes"

        class DummyStreamer:
            def request(self, index: int):
                return SimpleNamespace(buffer=payload)

            def write_chunk(self, buffer, index, cached):
                written.append((buffer, index, cached))

        streamer = DummyStreamer()
        streamer._Streamer__internal_stream = internal
        adapter._fixed_cdn_request_chunk(streamer, 5)
        assert written == [(payload, 5, False)]
        assert calls == []

    def test_request_adds_timeout_and_preserves_behavior(self, monkeypatch):
        from librespot.audio.storage import ChannelManager

        recorded: dict = {}

        class FakeResponse:
            def __init__(self, status_code=206, content=b"body", headers=None):
                self.status_code = status_code
                self.content = content
                self.headers = headers or {}

        class DummyStreamer:
            _Streamer__cdn_url = SimpleNamespace(url="https://cdn.example/track")

            def _Streamer__session(self):
                pass

        streamer = DummyStreamer()
        streamer._Streamer__session = SimpleNamespace(
            client=lambda: SimpleNamespace(
                get=lambda url, headers=None, timeout=None: (
                    recorded.update({"url": url, "headers": headers, "timeout": timeout})
                    or FakeResponse(headers={"Content-Range": "bytes 0-131071/999999"})
                )
            )
        )

        chunk = 2
        result = adapter._fixed_cdn_request(streamer, chunk=chunk)
        assert recorded["timeout"] == adapter.CDN_REQUEST_TIMEOUT_S
        assert recorded["url"] == "https://cdn.example/track"
        expected_start = ChannelManager.chunk_size * chunk
        expected_end = (chunk + 1) * ChannelManager.chunk_size - 1
        assert recorded["headers"]["Range"] == f"bytes={expected_start}-{expected_end}"
        assert result.buffer == b"body"

        streamer._Streamer__session.client = lambda: SimpleNamespace(
            get=lambda *_a, **_k: FakeResponse(status_code=404)
        )
        with pytest.raises(IOError):
            adapter._fixed_cdn_request(streamer, chunk=chunk)

        streamer._Streamer__session.client = lambda: SimpleNamespace(
            get=lambda *_a, **_k: FakeResponse(status_code=206, content=None)
        )
        with pytest.raises(IOError, match="Response body is empty"):
            adapter._fixed_cdn_request(streamer, chunk=chunk)

    def test_patch_applies_when_broken_markers_present(self, monkeypatch):
        _reset_cdn_patch_state(monkeypatch)

        class DummyStreamer:
            @staticmethod
            def request_chunk(self, index):  # noqa: ARG004
                pass

            @staticmethod
            def request(self, chunk=None, range_start=None, range_end=None):  # noqa: ARG004
                pass

        _install_dummy_cdn_streamer_module(monkeypatch, DummyStreamer)

        def fake_getsource(fn):
            if fn.__name__ == "request_chunk":
                return _BROKEN_CDN_REQUEST_CHUNK_SOURCE
            if fn.__name__ == "request":
                return _BROKEN_CDN_REQUEST_SOURCE
            raise AssertionError(f"unexpected getsource target: {fn}")

        monkeypatch.setattr(inspect, "getsource", fake_getsource)
        adapter._apply_cdn_robustness_patch()
        assert DummyStreamer.request_chunk is adapter._fixed_cdn_request_chunk
        assert DummyStreamer.request is adapter._fixed_cdn_request
        assert adapter.cdn_robustness_patch_status() == adapter.PATCH_STATUS_APPLIED

    def test_patch_skips_when_markers_absent(self, monkeypatch):
        _reset_cdn_patch_state(monkeypatch)

        def original_chunk(self, index):  # noqa: ARG004
            pass

        def original_request(self, chunk=None, range_start=None, range_end=None):  # noqa: ARG004
            pass

        class DummyStreamer:
            request_chunk = original_chunk
            request = original_request

        _install_dummy_cdn_streamer_module(monkeypatch, DummyStreamer)
        monkeypatch.setattr(
            inspect,
            "getsource",
            lambda fn: (
                "def request_chunk(self, index):\n    try:\n        response = self.request(index)\n"
                if fn.__name__ == "request_chunk"
                else 'def request(self):\n    timeout=1\n    "Range": "bytes={}-{}"\n'
            ),
        )
        adapter._apply_cdn_robustness_patch()
        assert DummyStreamer.request_chunk is original_chunk
        assert DummyStreamer.request is original_request
        assert adapter.cdn_robustness_patch_status() == adapter.PATCH_STATUS_SKIPPED_INCOMPATIBLE

    @pytest.mark.parametrize(
        "getsource_exc",
        [OSError("source code not available"), TypeError("source code not available")],
    )
    def test_patch_source_unavailable_on_frozen_build(self, monkeypatch, getsource_exc):
        _reset_cdn_patch_state(monkeypatch)

        class DummyStreamer:
            @staticmethod
            def request_chunk(self, index):  # noqa: ARG004
                pass

            @staticmethod
            def request(self, chunk=None, range_start=None, range_end=None):  # noqa: ARG004
                pass

        _install_dummy_cdn_streamer_module(monkeypatch, DummyStreamer)

        def raise_getsource(_fn):
            raise getsource_exc

        monkeypatch.setattr(inspect, "getsource", raise_getsource)
        adapter._apply_cdn_robustness_patch()
        assert DummyStreamer.request_chunk is adapter._fixed_cdn_request_chunk
        assert DummyStreamer.request is adapter._fixed_cdn_request
        assert adapter.cdn_robustness_patch_status() == adapter.PATCH_STATUS_SOURCE_UNAVAILABLE

    def test_patch_idempotent(self, monkeypatch):
        _reset_cdn_patch_state(monkeypatch)

        class DummyStreamer:
            @staticmethod
            def request_chunk(self, index):  # noqa: ARG004
                pass

            @staticmethod
            def request(self, chunk=None, range_start=None, range_end=None):  # noqa: ARG004
                pass

        _install_dummy_cdn_streamer_module(monkeypatch, DummyStreamer)
        monkeypatch.setattr(
            inspect,
            "getsource",
            lambda fn: (
                _BROKEN_CDN_REQUEST_CHUNK_SOURCE
                if fn.__name__ == "request_chunk"
                else _BROKEN_CDN_REQUEST_SOURCE
            ),
        )
        adapter._apply_cdn_robustness_patch()
        chunk_fn = DummyStreamer.request_chunk
        request_fn = DummyStreamer.request
        adapter._apply_cdn_robustness_patch()
        assert DummyStreamer.request_chunk is chunk_fn
        assert DummyStreamer.request is request_fn


class TestStrictVorbisPicker:
    """Exercises the REAL strict picker against real librespot protos when the alpha
    lib is installed (skipped otherwise). Guards against a regression that reintroduces
    upstream's any-Vorbis fallback (which would silently downgrade 320k -> 160/96k)."""

    def test_strict_picker_selects_exact_tier_else_none(self):
        pytest.importorskip("librespot")
        from librespot.proto import Metadata_pb2 as Metadata

        f320 = Metadata.AudioFile(file_id=b"x" * 20, format=Metadata.AudioFile.OGG_VORBIS_320)
        f160 = Metadata.AudioFile(file_id=b"y" * 20, format=Metadata.AudioFile.OGG_VORBIS_160)
        fmp3 = Metadata.AudioFile(file_id=b"z" * 20, format=Metadata.AudioFile.MP3_320)
        # VERY_HIGH picks ONLY the 320 Vorbis (not MP3_320, not 160).
        assert adapter.vorbis_quality("VERY_HIGH").get_file([fmp3, f160, f320]) is f320
        # HIGH picks ONLY the 160 Vorbis.
        assert adapter.vorbis_quality("HIGH").get_file([f320, f160]) is f160
        # No exact-tier Vorbis -> None (so load raises FeederException -> next tier),
        # NOT a silent any-Vorbis downgrade.
        assert adapter.vorbis_quality("VERY_HIGH").get_file([f160, fmp3]) is None
        assert adapter.vorbis_quality("VERY_HIGH").get_file([]) is None


# --------------------------------------------------------------------------- #
# registration / config / graceful-unavailable
# --------------------------------------------------------------------------- #


class TestRegistrationAndConfig:
    def test_librespot_is_known_source(self):
        assert "librespot" in KNOWN_DOWNLOAD_SOURCES

    def test_make_backend_librespot(self):
        be = make_backend("librespot", scraper=_fake_scraper())
        assert isinstance(be, LibrespotBackend)
        assert be.max_concurrency == 1

    def test_config_accepts_librespot_and_new_keys(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "download_source": "librespot",
                    "spotify_credentials_path": "/tmp/creds.json",
                    "librespot_consented": True,
                    "fallback_order": ["youtube"],
                }
            )
        )
        monkeypatch.setattr(sd, "_config_path", lambda: str(cfg))
        loaded = sd.load_config()
        assert loaded["download_source"] == "librespot"
        assert loaded["spotify_credentials_path"] == "/tmp/creds.json"
        assert loaded["librespot_consented"] is True
        assert loaded["fallback_order"] == ["youtube"]

    def test_migrates_librespot_extended_toggle_to_chain(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "download_source": "librespot",
                    "librespot_extended_yt_fallback": False,
                }
            )
        )
        monkeypatch.setattr(sd, "_config_path", lambda: str(cfg))
        assert sd.load_config()["fallback_order"] == ["youtube"]

    def test_migrates_lossless_youtube_fallback_off(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "download_source": "lossless",
                    "lossless_youtube_fallback": False,
                }
            )
        )
        monkeypatch.setattr(sd, "_config_path", lambda: str(cfg))
        assert sd.load_config()["fallback_order"] == []

    def test_migrates_lossless_youtube_fallback_on(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"download_source": "lossless"}))
        monkeypatch.setattr(sd, "_config_path", lambda: str(cfg))
        assert sd.load_config()["fallback_order"] == ["youtube"]

    def test_ogg_in_supported_formats_not_lossless(self):
        assert "ogg" in sd.SUPPORTED_FORMATS
        assert sd.SUPPORTED_FORMATS["ogg"]["ext"] == "ogg"
        assert sd.SUPPORTED_FORMATS["ogg"].get("codec") == "vorbis"

    def test_adapter_availability_is_consistent(self):
        # Whether or not the alpha lib is installed (it may be, via req.txt),
        # is_available() must never raise and import_error() must be consistent.
        available = adapter.is_available()
        assert isinstance(available, bool)
        if available:
            assert adapter.import_error() is None
        else:
            assert adapter.import_error() is not None

    def test_unavailable_adapter_raises_unavailable_not_crash(self, monkeypatch):
        # Simulate the alpha lib being broken/missing regardless of the real env:
        # connecting must raise LibrespotUnavailable (which the backend catches and
        # degrades to YouTube), never an uncaught import error.
        from backends.librespot.errors import LibrespotUnavailable

        monkeypatch.setattr(adapter, "is_available", lambda: False)
        monkeypatch.setattr(adapter, "import_error", lambda: ImportError("boom"))
        with pytest.raises(LibrespotUnavailable):
            LibrespotSession("/tmp/none.json").connect_stored()

    def test_backend_raises_when_adapter_unavailable(self, monkeypatch):
        from backends.librespot.errors import LibrespotUnavailable

        be = LibrespotBackend(_fake_scraper())

        def boom():
            raise LibrespotUnavailable("librespot missing")

        monkeypatch.setattr(be, "_ensure_session", boom)
        with pytest.raises(LibrespotUnavailable):
            be.fetch(
                track=_track(),
                destination="/tmp/nonexistent_dir_xyz/s.mp3",
                extended=False,
                audio_format="mp3",
                audio_quality="192",
                cancel=_no_cancel,
            )

    def test_make_backend_librespot_chain_falls_back_when_unavailable(self, monkeypatch):
        from backends.librespot.errors import LibrespotUnavailable

        be = make_backend("librespot", scraper=_fake_scraper(), fallback_order=["youtube"])
        assert isinstance(be, FallbackChainBackend)
        lib = be._steps[0][1]

        def boom():
            raise LibrespotUnavailable("librespot missing")

        monkeypatch.setattr(lib, "_ensure_session", boom)
        yt = be._steps[1][1]
        calls = {}

        def fake_yt_fetch(**kw):
            calls["fetched"] = True
            return ("/tmp/fallback.mp3", "mp3", kw["extended"])

        monkeypatch.setattr(yt, "fetch", fake_yt_fetch)
        path, ext, used = be.fetch(
            track=_track(),
            destination="/tmp/s.mp3",
            extended=False,
            audio_format="ogg",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert ext == "mp3" and calls.get("fetched")


# --------------------------------------------------------------------------- #
# filename-honesty: extended-fallback rename preserves the real .ogg extension
# --------------------------------------------------------------------------- #


class TestResolveExtendedOutputOgg:
    def test_ogg_fallback_rename_keeps_ogg_extension(self, tmp_path):
        scraper = sd.MusicScraper(extended_mix=True)
        marked = tmp_path / "Song (Extended Mix) - Artist.ogg"
        marked.write_bytes(b"OggS-audio")
        final_path, meta_title = scraper._resolve_extended_output(
            str(marked),
            used_extended=False,
            folder=str(tmp_path),
            sanitized_artists="Artist",
            track_title="Song",
            display_title="Song (Extended Mix)",
            track_id="abc123",
        )
        assert final_path == str(tmp_path / "Song - Artist.ogg")  # .ogg preserved, not .mp3
        assert meta_title == "Song"
        assert (tmp_path / "Song - Artist.ogg").exists()
        assert not marked.exists()


# --------------------------------------------------------------------------- #
# (f) rich metadata from the LoadedStream.track protobuf — the real album gap
# --------------------------------------------------------------------------- #


class TestExtractTrackMetadata:
    """track_metadata.extract_track_metadata turns the Spotify Metadata.Track proto
    into the seam's tag dict — supplying the album the spotifydown embed lacks and
    clean (no-nbsp) artist names."""

    def test_full_protobuf_yields_all_fields(self):
        meta = track_metadata.extract_track_metadata(
            FakeLoadedStream(b"OggS", track=_proto_track())
        )
        assert meta["title"] == "Never Gonna Give You Up"
        assert meta["album"] == "Whenever You Need Somebody"  # the previously-empty field
        assert meta["artists"] == "Rick Astley"
        assert meta["trackNumber"] == 1
        assert meta["discNumber"] == 1
        assert meta["releaseDate"] == "1987-11-16"

    def test_multiple_artists_joined_clean_no_nbsp(self):
        # The bug: spotifydown joins artists with a non-breaking space. The protobuf
        # carries them as separate clean strings; we join with a plain ", ".
        track = _proto_track(artists=("Jan Blomqvist", "Ben B\xf6hmer"))
        meta = track_metadata.extract_track_metadata(FakeLoadedStream(b"OggS", track=track))
        assert meta["artists"] == "Jan Blomqvist, Ben B\xf6hmer"
        assert "\xa0" not in meta["artists"]

    def test_strips_stray_nbsp_defensively(self):
        track = _proto_track(artists=("A\xa0Name",), album="Al\xa0bum")
        meta = track_metadata.extract_track_metadata(FakeLoadedStream(b"OggS", track=track))
        assert meta["artists"] == "A Name"
        assert meta["album"] == "Al bum"

    def test_no_track_returns_none(self):
        # An episode/podcast load (or any stream without .track) yields no metadata.
        assert track_metadata.extract_track_metadata(FakeLoadedStream(b"OggS")) is None
        assert track_metadata.extract_track_metadata(SimpleNamespace(track=None)) is None

    def test_empty_album_and_zero_numbers_omitted(self):
        track = _proto_track(album=None, number=0, disc_number=0)
        meta = track_metadata.extract_track_metadata(FakeLoadedStream(b"OggS", track=track))
        assert "album" not in meta
        assert "trackNumber" not in meta
        assert "discNumber" not in meta
        assert meta["title"] == "Never Gonna Give You Up"  # still surfaces what's present

    def test_date_year_only_and_year_month(self):
        y = track_metadata.extract_track_metadata(
            FakeLoadedStream(b"x", track=_proto_track(date=(1999, 0, 0)))
        )
        assert y["releaseDate"] == "1999"
        ym = track_metadata.extract_track_metadata(
            FakeLoadedStream(b"x", track=_proto_track(date=(1999, 5, 0)))
        )
        assert ym["releaseDate"] == "1999-05"

    def test_cover_url_picks_largest_image(self):
        # cover_group has 3 sizes; we pick the largest by known pixel area and emit a
        # canonical i.scdn.co URL from the hex file_id.
        track = _proto_track(
            cover_ids=[
                (b"\x11" * 20, 1, 64, 64),  # SMALL
                (b"\x22" * 20, 0, 300, 300),  # DEFAULT
                (b"\x33" * 20, 2, 640, 640),  # LARGE  <- largest area
            ]
        )
        meta = track_metadata.extract_track_metadata(FakeLoadedStream(b"x", track=track))
        assert meta["cover"] == "https://i.scdn.co/image/" + ("33" * 20)

    def test_cover_falls_back_to_size_rank_when_dimensions_unset(self):
        # width/height are commonly 0 in metadata responses -> rank by the size enum
        # (SMALL < DEFAULT < LARGE < XLARGE).
        track = _proto_track(
            cover_ids=[
                (b"\xaa" * 20, 1, 0, 0),  # SMALL
                (b"\xbb" * 20, 3, 0, 0),  # XLARGE <- highest rank
                (b"\xcc" * 20, 2, 0, 0),  # LARGE
            ]
        )
        meta = track_metadata.extract_track_metadata(FakeLoadedStream(b"x", track=track))
        assert meta["cover"] == "https://i.scdn.co/image/" + ("bb" * 20)

    def test_no_cover_images_omits_cover(self):
        meta = track_metadata.extract_track_metadata(
            FakeLoadedStream(b"x", track=_proto_track(cover_ids=[]))
        )
        assert "cover" not in meta


class TestBackendSurfacesMetadata:
    """The backend exposes the captured protobuf metadata on ``last_track_metadata``
    (single-slot attr; the chain snapshots it per fetch under concurrency)."""

    def _backend(self, monkeypatch, *, product="premium"):
        be = LibrespotBackend(_fake_scraper())
        monkeypatch.setattr(be, "_ensure_session", lambda: _FakeSession(product=product))
        return be

    def test_native_fetch_stashes_metadata(self, tmp_path, monkeypatch):
        be = self._backend(monkeypatch)

        def fake_fetch_ogg(
            session_raw, base62, *, dest, cancel, on_status=None, on_metadata=None, on_throttle=None
        ):
            with open(dest, "wb") as f:
                f.write(b"OggS")
            on_metadata({"album": "Whenever You Need Somebody", "artists": "Rick Astley"})
            return dest

        monkeypatch.setattr(audio, "fetch_track_ogg", fake_fetch_ogg)
        be.fetch(
            track=_track(),
            destination=str(tmp_path / "s.mp3"),
            extended=False,
            audio_format="mp3",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert be.last_track_metadata == {
            "album": "Whenever You Need Somebody",
            "artists": "Rick Astley",
        }

    def test_metadata_reset_each_fetch(self, tmp_path, monkeypatch):
        be = self._backend(monkeypatch, product="premium")

        def fake_fetch_ogg(
            session_raw, base62, *, dest, cancel, on_status=None, on_metadata=None, on_throttle=None
        ):
            with open(dest, "wb") as f:
                f.write(b"OggS")
            on_metadata({"album": "A"})
            return dest

        monkeypatch.setattr(audio, "fetch_track_ogg", fake_fetch_ogg)
        be.fetch(
            track=_track(),
            destination=str(tmp_path / "a.mp3"),
            extended=False,
            audio_format="mp3",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert be.last_track_metadata == {"album": "A"}

        be2 = self._backend(monkeypatch, product="free")
        be2.last_track_metadata = {"album": "STALE"}
        with pytest.raises(LibrespotNotPremium):
            be2.fetch(
                track=_track(),
                destination=str(tmp_path / "b.mp3"),
                extended=False,
                audio_format="mp3",
                audio_quality="192",
                cancel=_no_cancel,
            )
        assert be2.last_track_metadata is None

    def test_fetch_track_ogg_invokes_on_metadata_with_extracted_dict(self, tmp_path, monkeypatch):
        # End-to-end through the REAL audio.fetch_track_ogg: a LoadedStream carrying a
        # .track proto is extracted and handed to on_metadata after a successful write.
        ogg = b"OggS" + b"a" * 200
        loaded = FakeLoadedStream(ogg, track=_proto_track())
        monkeypatch.setattr(adapter, "track_id_from_base62", lambda b: b)
        monkeypatch.setattr(adapter, "vorbis_quality", lambda name, **_k: name)
        monkeypatch.setattr(adapter, "load_loaded_stream", lambda *_a, **_k: loaded)

        seen = []
        dest = tmp_path / "out.ogg"
        audio.fetch_track_ogg(
            object(),
            "id",
            dest=str(dest),
            cancel=_no_cancel,
            on_metadata=seen.append,
            backoffs=(0, 0, 0),
            sleep=lambda *_: None,
        )
        assert seen and seen[0]["album"] == "Whenever You Need Somebody"
        assert seen[0]["artists"] == "Rick Astley"

    def test_extraction_error_never_fails_a_written_download(self, tmp_path, monkeypatch):
        # If metadata extraction throws, the already-written .ogg must still succeed.
        ogg = b"OggS" + b"a" * 50
        loaded = FakeLoadedStream(ogg, track=_proto_track())
        monkeypatch.setattr(adapter, "track_id_from_base62", lambda b: b)
        monkeypatch.setattr(adapter, "vorbis_quality", lambda name, **_k: name)
        monkeypatch.setattr(adapter, "load_loaded_stream", lambda *_a, **_k: loaded)
        monkeypatch.setattr(
            track_metadata,
            "extract_track_metadata",
            lambda _l: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        dest = tmp_path / "out.ogg"
        result = audio.fetch_track_ogg(
            object(),
            "id",
            dest=str(dest),
            cancel=_no_cancel,
            on_metadata=lambda _m: pytest.fail("should not be called with a value"),
            backoffs=(0, 0, 0),
            sleep=lambda *_: None,
        )
        assert result == str(dest)
        assert dest.read_bytes() == ogg


class TestEnrichSongMeta:
    """The seam merges rich metadata into song_meta before tagging — from the librespot
    protobuf (streaming path) or the metadata service (YouTube path)."""

    def _scraper_with_backend_meta(self, meta, *, service=None):
        scraper = sd.MusicScraper()
        scraper._backend = SimpleNamespace(last_track_metadata=meta)
        scraper._metadata_service = service  # protobuf path: service must NOT be consulted
        return scraper

    def _song_meta(self, **over):
        base = {
            "title": "More - Zerb Remix",
            "artists": "Jan Blomqvist,\xa0Zerb",  # spotifydown nbsp join
            "album": "",  # the empty album bug
            "releaseDate": "2026-06-05",
            "cover": "https://embed-cover",
            "trackNumber": 1,
        }
        base.update(over)
        return base

    # -- librespot streaming path (protobuf) -------------------------------

    def test_fills_album_and_cleans_artists(self):
        scraper = self._scraper_with_backend_meta(
            {"album": "More", "artists": "Jan Blomqvist, Zerb", "trackNumber": 1, "discNumber": 1}
        )
        song_meta = self._song_meta()
        scraper._enrich_song_meta(song_meta, "tid")
        assert song_meta["album"] == "More"  # no longer empty
        assert song_meta["artists"] == "Jan Blomqvist, Zerb"  # nbsp gone
        assert song_meta["discNumber"] == 1

    def test_replaces_cover_when_backend_provides_one(self):
        scraper = self._scraper_with_backend_meta({"cover": "https://i.scdn.co/image/abc"})
        song_meta = self._song_meta()
        scraper._enrich_song_meta(song_meta, "tid")
        assert song_meta["cover"] == "https://i.scdn.co/image/abc"

    def test_does_not_override_title(self):
        # Title is owned by the seam (extended-mix marker / _meta_title); the rich
        # metadata title must not clobber it.
        scraper = self._scraper_with_backend_meta({"title": "More", "album": "More"})
        song_meta = self._song_meta(title="More (Extended Mix)")
        scraper._enrich_song_meta(song_meta, "tid")
        assert song_meta["title"] == "More (Extended Mix)"

    def test_release_date_fill_only_when_empty(self):
        scraper = self._scraper_with_backend_meta({"releaseDate": "1987-11-16"})
        keep = self._song_meta(releaseDate="2026-06-05")
        scraper._enrich_song_meta(keep, "tid")
        assert keep["releaseDate"] == "2026-06-05"  # existing date preserved

        fill = self._song_meta(releaseDate="")
        scraper._enrich_song_meta(fill, "tid")
        assert fill["releaseDate"] == "1987-11-16"  # filled when absent

    def test_protobuf_present_skips_metadata_service(self):
        # When the librespot stream already captured metadata, the service must NOT be
        # consulted (no redundant Mercury fetch).
        service = SimpleNamespace(get=lambda _id: pytest.fail("service must not be called"))
        scraper = self._scraper_with_backend_meta({"album": "From Protobuf"}, service=service)
        song_meta = self._song_meta()
        scraper._enrich_song_meta(song_meta, "tid")
        assert song_meta["album"] == "From Protobuf"

    # -- YouTube path (metadata service) -----------------------------------

    def test_youtube_path_enriches_from_metadata_service(self):
        # No protobuf (YouTube backend) -> pull rich metadata from the service by id.
        scraper = sd.MusicScraper()
        scraper._backend = SimpleNamespace()  # YouTube: no last_track_metadata
        scraper._metadata_service = SimpleNamespace(
            get=lambda tid: (
                {
                    "album": "Whenever You Need Somebody",
                    "artists": "Rick Astley",
                    "trackNumber": 1,
                    "discNumber": 1,
                }
                if tid == "rick"
                else None
            )
        )
        song_meta = self._song_meta()
        scraper._enrich_song_meta(song_meta, "rick")
        assert song_meta["album"] == "Whenever You Need Somebody"  # album on a YouTube DL
        assert song_meta["artists"] == "Rick Astley"
        assert song_meta["trackNumber"] == 1

    def test_youtube_path_service_unavailable_still_cleans_nbsp(self):
        # Service returns None (not logged in) -> keep embed data, but the nbsp in the
        # artist tag is still cleaned (the universal win that needs no Spotify session).
        scraper = sd.MusicScraper()
        scraper._backend = SimpleNamespace()
        scraper._metadata_service = SimpleNamespace(get=lambda _id: None)
        song_meta = self._song_meta()  # artists carry the nbsp, album empty
        scraper._enrich_song_meta(song_meta, "tid")
        assert song_meta["album"] == ""  # no rich source -> unchanged
        assert song_meta["artists"] == "Jan Blomqvist, Zerb"  # nbsp cleaned regardless
        assert "\xa0" not in song_meta["artists"]

    def test_no_track_id_skips_service(self):
        scraper = sd.MusicScraper()
        scraper._backend = SimpleNamespace()
        scraper._metadata_service = SimpleNamespace(
            get=lambda _id: pytest.fail("service must not be called without a track id")
        )
        song_meta = self._song_meta()
        scraper._enrich_song_meta(song_meta, "")  # no id
        assert song_meta["album"] == ""

    def test_real_scraper_and_service_wiring_end_to_end(self, monkeypatch):
        # Integration: a REAL MusicScraper (YouTube backend) with its REAL
        # LibrespotMetadataService (constructed in __init__), driven through the REAL
        # _enrich_song_meta. Only the Mercury fetch is mocked. Proves the production
        # wiring actually enriches a YouTube download (not just the isolated units).
        scraper = sd.MusicScraper(download_source="youtube")
        assert isinstance(scraper._metadata_service, LibrespotMetadataService)
        # Hand the real service a session without a live login, and mock only the fetch.
        monkeypatch.setattr(scraper._metadata_service, "_session_provider", lambda: object())
        monkeypatch.setattr(adapter, "track_metadata", lambda _raw, _b62: _proto_track())
        song_meta = self._song_meta()  # album empty, nbsp artists, trackNumber 1
        scraper._enrich_song_meta(song_meta, "rick")
        assert song_meta["album"] == "Whenever You Need Somebody"
        assert song_meta["artists"] == "Rick Astley"  # nbsp cleaned
        assert song_meta["trackNumber"] == 1
        assert song_meta["discNumber"] == 1


class TestExtractFromProto:
    def test_extracts_from_bare_track_proto(self):
        meta = track_metadata.extract_from_proto(_proto_track())
        assert meta["album"] == "Whenever You Need Somebody"
        assert meta["artists"] == "Rick Astley"
        assert meta["trackNumber"] == 1

    def test_none_proto_returns_none(self):
        assert track_metadata.extract_from_proto(None) is None

    def test_duration_ms_extracted_when_positive(self):
        track = _proto_track()
        track.duration = 213000
        meta = track_metadata.extract_from_proto(track)
        assert meta["durationMs"] == 213000

    def test_duration_ms_omitted_when_absent_or_zero(self):
        track = _proto_track()
        meta = track_metadata.extract_from_proto(track)
        assert "durationMs" not in meta

        track_zero = _proto_track()
        track_zero.duration = 0
        meta_zero = track_metadata.extract_from_proto(track_zero)
        assert "durationMs" not in meta_zero


class TestLibrespotMetadataService:
    """The YouTube-path metadata resolver: a cached, best-effort, metadata-only fetch."""

    def test_get_via_reused_session(self, monkeypatch):
        # adapter.track_metadata returns a Metadata.Track proto; the service wraps it via
        # extract_from_proto, so feed a proto stand-in.
        monkeypatch.setattr(adapter, "track_metadata", lambda _raw, _b62: _proto_track())
        svc = LibrespotMetadataService(session_provider=lambda: object())  # external session
        meta = svc.get("rick")
        assert meta["album"] == "Whenever You Need Somebody"
        assert meta["artists"] == "Rick Astley"

    def test_caches_results(self, monkeypatch):
        calls = {"n": 0}

        def fake(_raw, _b62):
            calls["n"] += 1
            return _proto_track()

        monkeypatch.setattr(adapter, "track_metadata", fake)
        svc = LibrespotMetadataService(session_provider=lambda: object())
        assert svc.get("x")["album"]
        assert svc.get("x")["album"]
        assert calls["n"] == 1  # second lookup served from cache

    def test_blank_id_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            adapter, "track_metadata", lambda *_a: pytest.fail("must not fetch for blank id")
        )
        assert LibrespotMetadataService(session_provider=lambda: object()).get("") is None

    def test_per_track_error_returns_none_but_stays_enabled(self, monkeypatch):
        seq = iter([RuntimeError("boom"), _proto_track()])

        def fake(_raw, _b62):
            v = next(seq)
            if isinstance(v, Exception):
                raise v
            return v

        monkeypatch.setattr(adapter, "track_metadata", fake)
        svc = LibrespotMetadataService(session_provider=lambda: object())
        assert svc.get("a") is None  # one track's fetch blew up
        assert svc.get("b")["album"]  # service is NOT disabled by a single failure

    def test_no_credentials_disables(self, monkeypatch):
        # autouse conftest fixture forces has_credentials False -> self-connect disabled.
        monkeypatch.setattr(
            adapter, "track_metadata", lambda *_a: pytest.fail("must not fetch without a session")
        )
        svc = LibrespotMetadataService("/tmp/none.json")  # no session_provider -> self-connect
        assert svc.get("x") is None

    def test_connect_failure_disables(self, monkeypatch):
        from backends.librespot.session import LibrespotSession

        monkeypatch.setattr(LibrespotSession, "has_credentials", lambda _self: True)
        connects = {"n": 0}

        def boom(self):
            connects["n"] += 1
            raise RuntimeError("login failed")

        monkeypatch.setattr(LibrespotSession, "connect_stored", boom)
        svc = LibrespotMetadataService("/tmp/creds.json")
        assert svc.get("a") is None
        assert svc.get("b") is None
        assert connects["n"] == 1  # disabled after the first failure; no reconnect storm

    def test_self_connect_then_fetch_and_close(self, monkeypatch):
        from backends.librespot import session as session_mod

        fake_raw = object()
        connected = {"closed": False}

        class FakeSession:
            def __init__(self, path):
                pass

            def has_credentials(self):
                return True

            def connect_stored(self):
                return self

            @property
            def raw(self):
                return fake_raw

            def close(self):
                connected["closed"] = True

        monkeypatch.setattr(session_mod, "LibrespotSession", FakeSession)
        # the service imports LibrespotSession into its own module namespace:
        import backends.librespot.metadata_service as ms

        monkeypatch.setattr(ms, "LibrespotSession", FakeSession)
        got = {}

        def fake_md(raw, b62):
            got["raw"] = raw
            return _proto_track()

        monkeypatch.setattr(adapter, "track_metadata", fake_md)
        svc = LibrespotMetadataService("/tmp/creds.json")  # no provider -> self-connect
        meta = svc.get("rick")
        assert meta["album"] == "Whenever You Need Somebody"
        assert got["raw"] is fake_raw  # used the self-connected session
        svc.close()
        assert connected["closed"] is True  # owns + closes its own session

    def test_close_does_not_close_reused_external_session(self, monkeypatch):
        monkeypatch.setattr(adapter, "track_metadata", lambda _r, _b: _proto_track())
        ext_closed = {"n": 0}
        ext = SimpleNamespace(close=lambda: ext_closed.__setitem__("n", ext_closed["n"] + 1))
        svc = LibrespotMetadataService(session_provider=lambda: ext)
        assert svc.get("x")["album"]
        svc.close()
        assert ext_closed["n"] == 0  # never closes a session it doesn't own

    def test_reuses_backend_session_no_self_connect(self, monkeypatch):
        # A librespot run's YouTube-fallback track: the service must reuse the backend's
        # live session (via raw_session) rather than open a second login.
        backend = LibrespotBackend(_fake_scraper())
        backend._session = _FakeSession(product="free")  # has .raw + is_connected()
        used = {}
        monkeypatch.setattr(
            adapter,
            "track_metadata",
            lambda raw, _b62: used.__setitem__("raw", raw) or _proto_track(),
        )
        # If the service tried to self-connect, has_credentials (autouse False) would
        # disable it; reuse must make that irrelevant.
        svc = LibrespotMetadataService(
            "/tmp/creds.json", session_provider=lambda: backend.raw_session
        )
        meta = svc.get("x")
        assert meta["album"] == "Whenever You Need Somebody"
        assert used["raw"] is backend._session.raw  # used the backend's session

    def test_provider_none_then_self_connect_disabled(self, monkeypatch):
        # Pure-YouTube path with no login: provider returns None -> self-connect ->
        # autouse has_credentials False -> disabled -> None, and no fetch attempted.
        monkeypatch.setattr(
            adapter, "track_metadata", lambda *_a: pytest.fail("no session -> must not fetch")
        )
        svc = LibrespotMetadataService("/tmp/none.json", session_provider=lambda: None)
        assert svc.get("a") is None

    def test_malformed_proto_yields_none_not_crash(self, monkeypatch):
        # A proto missing every usable field extracts to None; get() returns None
        # gracefully and does NOT cache it (stays retryable).
        monkeypatch.setattr(adapter, "track_metadata", lambda _r, _b: SimpleNamespace())
        svc = LibrespotMetadataService(session_provider=lambda: object())
        assert svc.get("x") is None
        assert "x" not in svc._cache  # None result not pinned

    def test_concurrent_get_is_thread_safe(self, monkeypatch):
        # The 4-worker YouTube path hits get() concurrently; lock guards the cache and
        # the fetch runs outside it. Drive many threads at distinct + shared ids.
        import threading

        def fake(_raw, b62):
            return _proto_track(album=f"Album {b62}")

        monkeypatch.setattr(adapter, "track_metadata", fake)
        svc = LibrespotMetadataService(session_provider=lambda: object())
        results = {}
        errors = []

        def worker(i):
            try:
                tid = f"t{i % 5}"  # 20 calls across 5 ids -> exercises cache + parallelism
                results[i] = svc.get(tid)["album"]
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(results) == 20
        assert all(v.startswith("Album t") for v in results.values())


class TestWriteMetadataOggDisc:
    def test_disc_number_written_and_album_non_empty(self, tmp_path):
        from mutagen.oggvorbis import OggVorbis

        dest = tmp_path / "tagged.ogg"
        dest.write_bytes(_read_fixture())
        tags = {
            "title": "More - Zerb Remix",
            "artists": "Jan Blomqvist, Zerb",
            "album": "More",
            "releaseDate": "2026-06-05",
            "trackNumber": 1,
            "discNumber": 2,
        }
        sd._write_metadata_ogg(str(dest), tags, None)
        a = OggVorbis(str(dest))
        assert a["album"] == ["More"]  # the fix: a real, non-empty album
        assert a["discnumber"] == ["2"]
        assert "\xa0" not in a["artist"][0]

    def test_disc_omitted_when_absent(self, tmp_path):
        from mutagen.oggvorbis import OggVorbis

        dest = tmp_path / "tagged.ogg"
        dest.write_bytes(_read_fixture())
        sd._write_metadata_ogg(str(dest), {"title": "T", "artists": "A", "album": "Al"}, None)
        a = OggVorbis(str(dest))
        assert "discnumber" not in a

    def test_empty_album_does_not_erase_existing(self, tmp_path):
        # Re-tagging an already-tagged .ogg with an empty album (e.g. the backend
        # short-circuited an existing file, so there's no fresh protobuf) must NOT
        # wipe the album written on the first download.
        from mutagen.oggvorbis import OggVorbis

        dest = tmp_path / "tagged.ogg"
        dest.write_bytes(_read_fixture())
        sd._write_metadata_ogg(
            str(dest), {"title": "T", "artists": "A", "album": "Real Album"}, None
        )
        sd._write_metadata_ogg(str(dest), {"title": "T", "artists": "A", "album": ""}, None)
        assert OggVorbis(str(dest))["album"] == ["Real Album"]


# Pinned upstream ``AudioKeyManager`` bodies for audio-key guard tests.
_BROKEN_GET_AUDIO_KEY_SOURCE = (
    "    def get_audio_key(self,\n"
    "                      gid: bytes,\n"
    "                      file_id: bytes,\n"
    "                      retry: bool = True) -> bytes:\n"
    "        seq: int\n"
    "        with self.__seq_holder_lock:\n"
    "            seq = self.__seq_holder\n"
    "            self.__seq_holder += 1\n"
    "        out = io.BytesIO()\n"
    "        out.write(file_id)\n"
    "        out.write(gid)\n"
    '        out.write(struct.pack(">i", seq))\n'
    "        out.write(self.__zero_short)\n"
    "        out.seek(0)\n"
    "        self.__session.send(Packet.Type.request_key, out.read())\n"
    "        callback = AudioKeyManager.SyncCallback(self)\n"
    "        self.__callbacks[seq] = callback\n"
    "        key = callback.wait_response()\n"
    "        if key is None:\n"
    "            if retry:\n"
    "                return self.get_audio_key(gid, file_id, False)\n"
    "            raise RuntimeError(\n"
    '                "Failed fetching audio key! gid: {}, fileId: {}".format(\n'
    "                    util.bytes_to_hex(gid), util.bytes_to_hex(file_id)))\n"
    "        return key\n"
)
_BROKEN_SYNC_CALLBACK_SOURCE = (
    "    class SyncCallback(Callback):\n"
    "        __audio_key_manager: AudioKeyManager\n"
    "        __reference = queue.Queue()\n"
    "        __reference_lock = threading.Condition()\n"
    "\n"
    "        def __init__(self, audio_key_manager: AudioKeyManager):\n"
    "            self.__audio_key_manager = audio_key_manager\n"
    "\n"
    "        def key(self, key: bytes) -> None:\n"
    "            with self.__reference_lock:\n"
    "                self.__reference.put(key)\n"
    "                self.__reference_lock.notify_all()\n"
    "\n"
    "        def error(self, code: int) -> None:\n"
    "            self.__audio_key_manager.logger.fatal(\n"
    '                "Audio key error, code: {}".format(code))\n'
    "            with self.__reference_lock:\n"
    "                self.__reference.put(None)\n"
    "                self.__reference_lock.notify_all()\n"
    "\n"
    "        def wait_response(self) -> bytes:\n"
    "            with self.__reference_lock:\n"
    "                self.__reference_lock.wait(\n"
    "                    AudioKeyManager.audio_key_request_timeout)\n"
    "                return self.__reference.get(block=False)\n"
)


def _reset_audio_key_patch_state(monkeypatch) -> None:
    monkeypatch.setattr(adapter, "_AUDIO_KEY_PATCHED", False, raising=False)
    monkeypatch.setattr(adapter, "_AUDIO_KEY_PATCH_STATUS", None, raising=False)


def _install_dummy_audio_key_module(monkeypatch, manager_cls) -> None:
    mod = types.ModuleType("librespot.audio")
    mod.AudioKeyManager = manager_cls
    monkeypatch.setitem(sys.modules, "librespot.audio", mod)


def _make_audio_key_manager_self(*, timeout=0.05, on_send=None):
    """Dummy ``AudioKeyManager`` ``self`` for :func:`adapter._fixed_get_audio_key`."""
    callbacks: dict = {}
    seq_lock = threading.Lock()

    class Session:
        def send(self, cmd, payload):
            if on_send is not None:
                on_send(cmd, payload, callbacks)

    mgr = SimpleNamespace(
        _AudioKeyManager__seq_holder_lock=seq_lock,
        _AudioKeyManager__seq_holder=0,
        _AudioKeyManager__zero_short=b"\x00\x00",
        _AudioKeyManager__callbacks=callbacks,
        _AudioKeyManager__session=Session(),
        audio_key_request_timeout=timeout,
    )
    mgr._callbacks = callbacks
    return mgr


class TestAudioKeyPatch:
    """Guards the audio-key retry/callback shim in ``_librespot``."""

    def test_error_code_2_single_send_callbacks_empty(self):
        sends: list = []

        def on_send(cmd, payload, callbacks):
            sends.append((cmd, payload))
            assert len(callbacks) == 1
            next(iter(callbacks.values())).error(2)

        mgr = _make_audio_key_manager_self(on_send=on_send)
        gid, file_id = b"g" * 16, b"f" * 20
        with pytest.raises(adapter.AudioKeyError, match="audio key") as exc_info:
            adapter._fixed_get_audio_key(mgr, gid, file_id)
        assert exc_info.value.code == 2
        assert len(sends) == 1
        assert mgr._callbacks == {}

    def test_success_returns_key_and_payload_layout(self):
        key = b"\xaa" * 16
        recorded: dict = {}

        def on_send(cmd, payload, callbacks):
            recorded["cmd"] = cmd
            recorded["payload"] = payload
            next(iter(callbacks.values())).key(key)

        mgr = _make_audio_key_manager_self(on_send=on_send)
        gid, file_id = b"g" * 16, b"f" * 20
        result = adapter._fixed_get_audio_key(mgr, gid, file_id)
        assert result == key
        assert recorded["cmd"] == b"\x0c"
        assert recorded["payload"] == file_id + gid + (0).to_bytes(4, "big") + b"\x00\x00"
        assert mgr._callbacks == {}

    def test_timeout_code_none_callbacks_empty(self):
        mgr = _make_audio_key_manager_self(timeout=0.01)
        gid, file_id = b"g" * 16, b"f" * 20
        with pytest.raises(adapter.AudioKeyError, match="audio key") as exc_info:
            adapter._fixed_get_audio_key(mgr, gid, file_id)
        assert exc_info.value.code is None
        assert mgr._callbacks == {}

    def test_sequential_requests_no_crosstalk(self):
        key2 = b"\xbb" * 16
        stale_key = b"\xdd" * 16
        calls = {"n": 0}
        captured: dict = {}

        def on_send(_cmd, _payload, callbacks):
            calls["n"] += 1
            cb = next(iter(callbacks.values()))
            if calls["n"] == 1:
                captured["stale_cb"] = cb
                return  # deliver nothing → request 1 times out
            cb.key(key2)

        mgr = _make_audio_key_manager_self(timeout=0.05, on_send=on_send)
        gid, file_id = b"g" * 16, b"f" * 20
        with pytest.raises(adapter.AudioKeyError):
            adapter._fixed_get_audio_key(mgr, gid, file_id)
        assert mgr._callbacks == {}
        captured["stale_cb"].key(stale_key)
        assert adapter._fixed_get_audio_key(mgr, gid, file_id) == key2
        assert mgr._callbacks == {}

    def test_patch_applies_when_broken_markers_present(self, monkeypatch):
        _reset_audio_key_patch_state(monkeypatch)

        class DummyAudioKeyManager:
            @staticmethod
            def get_audio_key(self, gid, file_id, retry=True):  # noqa: ARG004
                pass

            class SyncCallback:
                pass

        _install_dummy_audio_key_module(monkeypatch, DummyAudioKeyManager)

        def fake_getsource(fn):
            if fn.__name__ == "get_audio_key":
                return _BROKEN_GET_AUDIO_KEY_SOURCE
            if fn.__name__ == "SyncCallback":
                return _BROKEN_SYNC_CALLBACK_SOURCE
            raise AssertionError(f"unexpected getsource target: {fn}")

        monkeypatch.setattr(inspect, "getsource", fake_getsource)
        adapter._apply_audio_key_patch()
        assert DummyAudioKeyManager.get_audio_key is adapter._fixed_get_audio_key
        assert adapter.audio_key_patch_status() == adapter.PATCH_STATUS_APPLIED

    def test_patch_skips_when_get_audio_key_markers_absent(self, monkeypatch):
        _reset_audio_key_patch_state(monkeypatch)

        def original_get(self, gid, file_id, retry=True):  # noqa: ARG004
            pass

        class DummyAudioKeyManager:
            get_audio_key = original_get

            class SyncCallback:
                pass

        _install_dummy_audio_key_module(monkeypatch, DummyAudioKeyManager)
        monkeypatch.setattr(
            inspect,
            "getsource",
            lambda fn: (
                "def get_audio_key(self):\n    pass\n"
                if fn.__name__ == "get_audio_key"
                else _BROKEN_SYNC_CALLBACK_SOURCE
            ),
        )
        adapter._apply_audio_key_patch()
        assert DummyAudioKeyManager.get_audio_key is original_get
        assert adapter.audio_key_patch_status() == adapter.PATCH_STATUS_SKIPPED_INCOMPATIBLE

    def test_patch_skips_when_sync_callback_markers_absent(self, monkeypatch):
        _reset_audio_key_patch_state(monkeypatch)

        def original_get(self, gid, file_id, retry=True):  # noqa: ARG004
            pass

        class DummyAudioKeyManager:
            get_audio_key = original_get

            class SyncCallback:
                pass

        _install_dummy_audio_key_module(monkeypatch, DummyAudioKeyManager)
        monkeypatch.setattr(
            inspect,
            "getsource",
            lambda fn: (
                _BROKEN_GET_AUDIO_KEY_SOURCE
                if fn.__name__ == "get_audio_key"
                else "class SyncCallback:\n    pass\n"
            ),
        )
        adapter._apply_audio_key_patch()
        assert DummyAudioKeyManager.get_audio_key is original_get
        assert adapter.audio_key_patch_status() == adapter.PATCH_STATUS_SKIPPED_INCOMPATIBLE

    @pytest.mark.parametrize(
        "getsource_exc",
        [OSError("source code not available"), TypeError("source code not available")],
    )
    def test_patch_source_unavailable_on_frozen_build(self, monkeypatch, getsource_exc):
        _reset_audio_key_patch_state(monkeypatch)

        class DummyAudioKeyManager:
            @staticmethod
            def get_audio_key(self, gid, file_id, retry=True):  # noqa: ARG004
                pass

            class SyncCallback:
                pass

        _install_dummy_audio_key_module(monkeypatch, DummyAudioKeyManager)

        def raise_getsource(_fn):
            raise getsource_exc

        monkeypatch.setattr(inspect, "getsource", raise_getsource)
        adapter._apply_audio_key_patch()
        assert DummyAudioKeyManager.get_audio_key is adapter._fixed_get_audio_key
        assert adapter.audio_key_patch_status() == adapter.PATCH_STATUS_SOURCE_UNAVAILABLE

    def test_patch_idempotent(self, monkeypatch):
        _reset_audio_key_patch_state(monkeypatch)

        class DummyAudioKeyManager:
            @staticmethod
            def get_audio_key(self, gid, file_id, retry=True):  # noqa: ARG004
                pass

            class SyncCallback:
                pass

        _install_dummy_audio_key_module(monkeypatch, DummyAudioKeyManager)
        monkeypatch.setattr(
            inspect,
            "getsource",
            lambda fn: (
                _BROKEN_GET_AUDIO_KEY_SOURCE
                if fn.__name__ == "get_audio_key"
                else _BROKEN_SYNC_CALLBACK_SOURCE
            ),
        )
        adapter._apply_audio_key_patch()
        get_fn = DummyAudioKeyManager.get_audio_key
        adapter._apply_audio_key_patch()
        assert DummyAudioKeyManager.get_audio_key is get_fn


class TestFetchTrackOggOnThrottle:
    def test_on_throttle_fires_on_code_2_then_succeeds(self, tmp_path, monkeypatch):
        dest = tmp_path / "out.ogg"
        ogg_bytes = b"OggS" + b"audio" * 200
        attempts = {"n": 0}
        seen: list = []

        def fake_load(session_raw, tid, quality):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise adapter.AudioKeyError("audio key error, code: 2", code=2)
            return FakeLoadedStream(ogg_bytes)

        monkeypatch.setattr(adapter, "track_id_from_base62", lambda b: b)
        monkeypatch.setattr(adapter, "vorbis_quality", lambda name: name)
        monkeypatch.setattr(adapter, "load_loaded_stream", fake_load)

        def on_throttle():
            seen.append(None)

        audio.fetch_track_ogg(
            object(),
            "base62id",
            dest=str(dest),
            cancel=_no_cancel,
            on_throttle=on_throttle,
            backoffs=(0, 0, 0),
            sleep=lambda *_: None,
        )
        assert attempts["n"] == 2, f"expected retry, got {attempts['n']} loads"
        assert seen == [None]
        assert dest.exists()

    def test_on_throttle_not_fired_for_timeout(self, tmp_path, monkeypatch):
        dest = tmp_path / "out.ogg"
        seen: list = []

        def on_throttle():
            seen.append(None)

        def fake_load(*_a, **_k):
            raise adapter.AudioKeyError("audio key request timed out", code=None)

        monkeypatch.setattr(adapter, "track_id_from_base62", lambda b: b)
        monkeypatch.setattr(adapter, "vorbis_quality", lambda name: name)
        monkeypatch.setattr(adapter, "load_loaded_stream", fake_load)

        with pytest.raises(OggCaptureError):
            audio.fetch_track_ogg(
                object(),
                "base62id",
                dest=str(dest),
                cancel=_no_cancel,
                on_throttle=on_throttle,
                backoffs=(0, 0, 0),
                sleep=lambda *_: None,
            )
        assert seen == []

    def test_on_throttle_not_fired_for_ioerror(self, tmp_path, monkeypatch):
        dest = tmp_path / "out.ogg"
        seen: list = []

        def on_throttle():
            seen.append(None)

        def fake_load(*_a, **_k):
            raise OSError("connection reset")

        monkeypatch.setattr(adapter, "track_id_from_base62", lambda b: b)
        monkeypatch.setattr(adapter, "vorbis_quality", lambda name: name)
        monkeypatch.setattr(adapter, "load_loaded_stream", fake_load)

        with pytest.raises(OggCaptureError):
            audio.fetch_track_ogg(
                object(),
                "base62id",
                dest=str(dest),
                cancel=_no_cancel,
                on_throttle=on_throttle,
                backoffs=(0, 0, 0),
                sleep=lambda *_: None,
            )
        assert seen == []

    def test_on_throttle_callback_exception_suppressed(self, tmp_path, monkeypatch):
        dest = tmp_path / "out.ogg"
        ogg_bytes = b"OggS" + b"audio" * 200
        attempts = {"n": 0}

        def fake_load(*_a, **_k):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise adapter.AudioKeyError("audio key error, code: 2", code=2)
            return FakeLoadedStream(ogg_bytes)

        def boom():
            raise RuntimeError("callback blew up")

        monkeypatch.setattr(adapter, "track_id_from_base62", lambda b: b)
        monkeypatch.setattr(adapter, "vorbis_quality", lambda name: name)
        monkeypatch.setattr(adapter, "load_loaded_stream", fake_load)

        audio.fetch_track_ogg(
            object(),
            "base62id",
            dest=str(dest),
            cancel=_no_cancel,
            on_throttle=boom,
            backoffs=(0, 0, 0),
            sleep=lambda *_: None,
        )
        assert dest.exists()

    def test_on_throttle_fires_when_attempts_exhausted(self, tmp_path, monkeypatch):
        dest = tmp_path / "out.ogg"
        seen: list = []

        def on_throttle():
            seen.append(None)

        def always_throttle(*_a, **_k):
            raise adapter.AudioKeyError("audio key error, code: 2", code=2)

        monkeypatch.setattr(adapter, "track_id_from_base62", lambda b: b)
        monkeypatch.setattr(adapter, "vorbis_quality", lambda name: name)
        monkeypatch.setattr(adapter, "load_loaded_stream", always_throttle)

        with pytest.raises(OggCaptureError):
            audio.fetch_track_ogg(
                object(),
                "base62id",
                dest=str(dest),
                cancel=_no_cancel,
                on_throttle=on_throttle,
                backoffs=(0, 0, 0),
                sleep=lambda *_: None,
            )
        assert len(seen) >= 1


class TestKeyThrottlePacer:
    def test_baseline_bounds(self):
        from backends.librespot.pacing import KeyThrottlePacer

        pacer = KeyThrottlePacer(base_jitter_s=(0.4, 1.3))
        for _ in range(20):
            delay = pacer.next_delay()
            assert 0.4 <= delay <= 1.3

    def test_throttle_sets_10_floor(self):
        from backends.librespot.pacing import THROTTLE_FLOOR_S, KeyThrottlePacer

        pacer = KeyThrottlePacer()
        assert pacer.note_throttle() == THROTTLE_FLOOR_S
        assert pacer.is_elevated

    def test_escalation_to_30_cap(self):
        from backends.librespot.pacing import FLOOR_CAP_S, KeyThrottlePacer

        pacer = KeyThrottlePacer()
        assert pacer.note_throttle() == 10.0
        assert pacer.note_throttle() == 15.0
        assert pacer.note_throttle() == 22.5
        assert pacer.note_throttle() == FLOOR_CAP_S
        assert pacer.note_throttle() == FLOOR_CAP_S

    def test_decay_to_zero(self):
        from backends.librespot.pacing import KeyThrottlePacer

        pacer = KeyThrottlePacer()
        pacer.note_throttle()
        assert pacer.note_success() == 5.0
        assert pacer.note_success() == 2.5
        assert pacer.note_success() == 1.25
        assert pacer.note_success() == 0.0
        assert not pacer.is_elevated

    def test_is_elevated(self):
        from backends.librespot.pacing import KeyThrottlePacer

        pacer = KeyThrottlePacer()
        assert not pacer.is_elevated
        pacer.note_throttle()
        assert pacer.is_elevated
        pacer.note_success()
        pacer.note_success()
        pacer.note_success()
        pacer.note_success()
        assert not pacer.is_elevated


class TestBackendPacing:
    def _backend(self, monkeypatch, *, product="premium", sleep=None):
        be = LibrespotBackend(_fake_scraper(), sleep=sleep or (lambda _s: None))
        monkeypatch.setattr(be, "_ensure_session", lambda: _FakeSession(product=product))
        return be

    def test_pacing_floor_applied_after_throttle(self, tmp_path, monkeypatch):
        delays: list[float] = []
        be = self._backend(monkeypatch, sleep=lambda s: delays.append(s))
        call = {"n": 0}

        def fake_fetch(session_raw, base62, *, dest, cancel, **kwargs):
            call["n"] += 1
            if call["n"] == 1:
                on_throttle = kwargs.get("on_throttle")
                if on_throttle is not None:
                    on_throttle()
            with open(dest, "wb") as f:
                f.write(b"OggS")
            return dest

        monkeypatch.setattr(audio, "fetch_track_ogg", fake_fetch)
        be.fetch(
            track=_track(),
            destination=str(tmp_path / "a.mp3"),
            extended=False,
            audio_format="mp3",
            audio_quality="192",
            cancel=_no_cancel,
        )
        delays.clear()
        be.fetch(
            track=_track(),
            destination=str(tmp_path / "b.mp3"),
            extended=False,
            audio_format="mp3",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert sum(delays) >= 10.0

    def test_clean_fetch_decays_floor(self, tmp_path, monkeypatch):
        delays: list[float] = []
        be = self._backend(monkeypatch, sleep=lambda s: delays.append(s))
        throttled = {"done": False}

        def fake_fetch(session_raw, base62, *, dest, cancel, **kwargs):
            if not throttled["done"]:
                on_throttle = kwargs.get("on_throttle")
                if on_throttle is not None:
                    on_throttle()
                throttled["done"] = True
            with open(dest, "wb") as f:
                f.write(b"OggS")
            return dest

        monkeypatch.setattr(audio, "fetch_track_ogg", fake_fetch)
        be.fetch(
            track=_track(),
            destination=str(tmp_path / "a.mp3"),
            extended=False,
            audio_format="mp3",
            audio_quality="192",
            cancel=_no_cancel,
        )
        delays.clear()
        be.fetch(
            track=_track(),
            destination=str(tmp_path / "b.mp3"),
            extended=False,
            audio_format="mp3",
            audio_quality="192",
            cancel=_no_cancel,
        )
        elevated = sum(delays)
        delays.clear()
        be.fetch(
            track=_track(),
            destination=str(tmp_path / "c.mp3"),
            extended=False,
            audio_format="mp3",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert sum(delays) < elevated

    def test_status_line_emitted_once(self, tmp_path, monkeypatch, capsys):
        be = self._backend(monkeypatch)
        messages: list[str] = []
        monkeypatch.setattr(be, "_emit", messages.append)

        def fake_fetch(session_raw, base62, *, dest, cancel, **kwargs):
            on_throttle = kwargs.get("on_throttle")
            if on_throttle is not None:
                on_throttle()
                on_throttle()
            with open(dest, "wb") as f:
                f.write(b"OggS")
            return dest

        monkeypatch.setattr(audio, "fetch_track_ogg", fake_fetch)
        be.fetch(
            track=_track(),
            destination=str(tmp_path / "a.mp3"),
            extended=False,
            audio_format="mp3",
            audio_quality="192",
            cancel=_no_cancel,
        )
        pacing_msgs = [m for m in messages if "rate-limiting audio keys" in m]
        assert len(pacing_msgs) == 1

    def test_cancel_during_elevated_pacing_returns_promptly(self, tmp_path, monkeypatch):
        delays: list[float] = []
        be = self._backend(monkeypatch, sleep=lambda s: delays.append(s))
        be._served_any = True
        be._pacer.note_throttle()
        steps = {"n": 0}

        def cancel():
            steps["n"] += 1
            return steps["n"] >= 3

        monkeypatch.setattr(
            audio,
            "fetch_track_ogg",
            lambda *_a, **_k: pytest.fail("must not fetch after cancel during pacing"),
        )
        with pytest.raises(LibrespotCancelled):
            be.fetch(
                track=_track(),
                destination=str(tmp_path / "a.mp3"),
                extended=False,
                audio_format="mp3",
                audio_quality="192",
                cancel=cancel,
            )
        assert sum(delays) < 10.0


# --------------------------------------------------------------------------- #
# Reconnect resilience + receiver excepthook
# --------------------------------------------------------------------------- #


# Captured at import time, while ``Session.reconnect`` is still the pristine pinned function
# (before any test that calls the real ``is_available()`` / ``load_loaded_stream()`` can apply
# the monkeypatch process-wide). The fixture restores THIS so the source-guarded "applies on
# pinned source" test always sees the unpatched upstream source regardless of test order.
try:  # pragma: no cover - exercised only when the alpha librespot lib is installed
    import librespot.core as _core_for_pristine

    _PRISTINE_RECONNECT = _core_for_pristine.Session.reconnect
except Exception:  # noqa: BLE001 - lib absent: the reconnect tests importorskip anyway
    _PRISTINE_RECONNECT = None


@pytest.fixture
def _reconnect_state(monkeypatch):
    """Snapshot/restore the module globals + ``Session.reconnect`` + ``threading.excepthook``.

    The reconnect patch and the receiver excepthook both mutate process-wide state
    (``librespot.core.Session.reconnect`` and ``threading.excepthook``); without an explicit
    restore the mutation would leak into the rest of the suite (including pytest's own threads).
    The test BODY is seeded with the pristine pinned function captured at import time (so the
    source-guarded "applies on pinned source" test sees unpatched upstream markers regardless of
    order), but ``finally`` restores the ACTUAL pre-test ``Session.reconnect`` so it stays
    consistent with the restored ``_RECONNECT_RESILIENCE_PATCHED`` flag and nothing leaks.
    """
    core = pytest.importorskip("librespot.core")

    saved = {
        "patched": adapter._RECONNECT_RESILIENCE_PATCHED,
        "status": adapter._RECONNECT_RESILIENCE_PATCH_STATUS,
        "installed": adapter._EXCEPTHOOK_INSTALLED,
        "prev": adapter._PREV_EXCEPTHOOK,
    }
    # Restore the ACTUAL pre-test reconnect (which may already be the applied
    # ``_fixed_reconnect`` if an earlier test ran the real ``is_available()``), so the
    # restored ``Session.reconnect`` stays consistent with the restored ``_RECONNECT_
    # RESILIENCE_PATCHED`` flag and nothing leaks. The test BODY still starts from the
    # pristine pinned source so the source-guarded "applies on pinned source" test sees
    # the unpatched upstream markers regardless of order.
    saved_reconnect = core.Session.reconnect
    pristine_reconnect = _PRISTINE_RECONNECT or saved_reconnect
    saved_excepthook = threading.excepthook
    core.Session.reconnect = pristine_reconnect
    try:
        yield core
    finally:
        adapter._RECONNECT_RESILIENCE_PATCHED = saved["patched"]
        adapter._RECONNECT_RESILIENCE_PATCH_STATUS = saved["status"]
        adapter._EXCEPTHOOK_INSTALLED = saved["installed"]
        adapter._PREV_EXCEPTHOOK = saved["prev"]
        core.Session.reconnect = saved_reconnect
        threading.excepthook = saved_excepthook


class _FakeReconnectSession:
    """Minimal stand-in for a librespot ``Session`` exercised by ``_fixed_reconnect``.

    Exposes the public ``connection`` plus the name-mangled privates the patch reaches via
    the explicit ``_Session__*`` spellings, and records ``connect`` / authenticate calls.
    """

    def __init__(self):
        self.connection = SimpleNamespace(close=lambda: self.closed.append(True))
        self.closed: list[bool] = []
        self._Session__receiver = SimpleNamespace(stop=lambda: self.stopped.append(True))
        self.stopped: list[bool] = []
        self._Session__inner = SimpleNamespace(conf=object())
        # ``typ`` flows into a real ``Authentication.LoginCredentials`` protobuf, so it must be a
        # valid ``AuthenticationType`` enum value (1 == AUTHENTICATION_STORED_SPOTIFY_CREDENTIALS);
        # ``auth_data`` must be bytes.
        self._Session__ap_welcome = SimpleNamespace(
            reusable_auth_credentials_type=1,
            canonical_username="alice",
            reusable_auth_credentials=b"creds",
        )
        self.connect_calls = 0
        self.authenticate_calls: list[tuple] = []
        self.logger = SimpleNamespace(info=lambda *_a, **_k: None)

    def connect(self):
        self.connect_calls += 1

    def _Session__authenticate_partial(self, credentials, ap_welcome):
        self.authenticate_calls.append((credentials, ap_welcome))


class TestReconnectResilience:
    """Guards the multi-AP reconnect shim + the receiver-thread excepthook in ``_librespot``."""

    def test_fixed_reconnect_retries_fresh_ap_after_refused(self, _reconnect_state, monkeypatch):
        core = _reconnect_state
        aps = iter(["ap-1", "ap-2", "ap-3"])
        used_aps: list[str] = []
        stub_connection = object()
        create_calls: list[str] = []

        def fake_create(ap, _conf):
            create_calls.append(ap)
            if len(create_calls) == 1:
                raise ConnectionRefusedError(61, "Connection refused")
            return stub_connection

        def fake_random_ap():
            ap = next(aps)
            used_aps.append(ap)
            return ap

        sleeps: list[float] = []
        monkeypatch.setattr(core.Session.ConnectionHolder, "create", staticmethod(fake_create))
        monkeypatch.setattr(core.ApResolver, "get_random_accesspoint", fake_random_ap)
        monkeypatch.setattr(adapter.time, "sleep", lambda s: sleeps.append(s))

        session = _FakeReconnectSession()
        adapter._fixed_reconnect(session)

        # create called twice, against the two distinct fresh APs
        assert create_calls == ["ap-1", "ap-2"]
        assert create_calls[0] != create_calls[1]
        assert session.connect_calls == 1
        assert len(session.authenticate_calls) == 1
        assert session.connection is stub_connection
        assert sleeps == [adapter._RECONNECT_BACKOFF_BASE_S]

    def test_fixed_reconnect_raises_after_all_aps_refused(self, _reconnect_state, monkeypatch):
        core = _reconnect_state
        create_calls: list[object] = []

        def fake_create(ap, _conf):  # noqa: ARG001
            create_calls.append(ap)
            raise ConnectionRefusedError(61, "Connection refused")

        sleeps: list[float] = []
        monkeypatch.setattr(core.Session.ConnectionHolder, "create", staticmethod(fake_create))
        monkeypatch.setattr(core.ApResolver, "get_random_accesspoint", lambda: "ap", raising=True)
        monkeypatch.setattr(adapter.time, "sleep", lambda s: sleeps.append(s))

        session = _FakeReconnectSession()
        with pytest.raises(ConnectionRefusedError):
            adapter._fixed_reconnect(session)

        assert len(create_calls) == adapter._RECONNECT_AP_ATTEMPTS
        assert session.connect_calls == 0
        assert len(sleeps) == adapter._RECONNECT_AP_ATTEMPTS - 1

    def test_reconnect_patch_applies_on_pinned_source(self, _reconnect_state):
        core = _reconnect_state
        adapter._RECONNECT_RESILIENCE_PATCHED = False
        adapter._RECONNECT_RESILIENCE_PATCH_STATUS = None

        adapter._apply_reconnect_resilience_patch()

        assert adapter.reconnect_resilience_patch_status() == adapter.PATCH_STATUS_APPLIED
        assert core.Session.reconnect is adapter._fixed_reconnect

    def test_reconnect_patch_skips_incompatible_source(self, _reconnect_state, monkeypatch):
        core = _reconnect_state
        original_reconnect = core.Session.reconnect
        adapter._RECONNECT_RESILIENCE_PATCHED = False
        adapter._RECONNECT_RESILIENCE_PATCH_STATUS = None
        monkeypatch.setattr(adapter.inspect, "getsource", lambda _fn: "def reconnect(self): pass")

        adapter._apply_reconnect_resilience_patch()

        assert (
            adapter.reconnect_resilience_patch_status() == adapter.PATCH_STATUS_SKIPPED_INCOMPATIBLE
        )
        assert core.Session.reconnect is original_reconnect

    def test_reconnect_patch_source_unavailable(self, _reconnect_state, monkeypatch):
        core = _reconnect_state
        adapter._RECONNECT_RESILIENCE_PATCHED = False
        adapter._RECONNECT_RESILIENCE_PATCH_STATUS = None

        def raise_getsource(_fn):
            raise OSError("source code not available")

        monkeypatch.setattr(adapter.inspect, "getsource", raise_getsource)

        adapter._apply_reconnect_resilience_patch()

        assert (
            adapter.reconnect_resilience_patch_status() == adapter.PATCH_STATUS_SOURCE_UNAVAILABLE
        )
        assert core.Session.reconnect is adapter._fixed_reconnect

    def test_receiver_excepthook_quiets_only_receiver_thread(self, _reconnect_state, capsys):
        delegated: list = []
        adapter._PREV_EXCEPTHOOK = lambda args: delegated.append(args)

        receiver_args = SimpleNamespace(
            thread=SimpleNamespace(name="session-packet-receiver"),
            exc_value=ConnectionRefusedError(61, "Connection refused"),
        )
        adapter._receiver_quiet_excepthook(receiver_args)
        out = capsys.readouterr().out
        assert "Spotify session dropped" in out
        assert delegated == []

        other_args = SimpleNamespace(
            thread=SimpleNamespace(name="other"),
            exc_value=RuntimeError("boom"),
        )
        adapter._receiver_quiet_excepthook(other_args)
        assert delegated == [other_args]

    def test_install_receiver_excepthook_idempotent_and_chains(self, _reconnect_state):
        adapter._EXCEPTHOOK_INSTALLED = False
        adapter._PREV_EXCEPTHOOK = None

        def sentinel(_args):
            pass

        threading.excepthook = sentinel

        adapter._install_receiver_excepthook()
        assert threading.excepthook is adapter._receiver_quiet_excepthook
        assert adapter._PREV_EXCEPTHOOK is sentinel

        # second install is a no-op: still our hook, prev unchanged
        adapter._install_receiver_excepthook()
        assert threading.excepthook is adapter._receiver_quiet_excepthook
        assert adapter._PREV_EXCEPTHOOK is sentinel
