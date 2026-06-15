"""Tests for Spotify_Downloader module."""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from unittest.mock import ANY, MagicMock, patch

import pytest
import requests


class TestGetFfmpegPath:
    """Tests for get_ffmpeg_path function."""

    def test_bundled_ffmpeg_macos(self, tmp_path):
        """Test bundled FFmpeg detection on macOS."""
        # Import the function
        from Spotify_Downloader import get_ffmpeg_path

        # Mock frozen attribute for PyInstaller
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", str(tmp_path), create=True),
            patch("sys.platform", "darwin"),
        ):
            # Create mock ffmpeg in bundled path
            ffmpeg_dir = tmp_path / "ffmpeg"
            ffmpeg_dir.mkdir()
            ffmpeg_path = ffmpeg_dir / "ffmpeg"
            ffmpeg_path.touch()

            result = get_ffmpeg_path()
            assert result == str(ffmpeg_dir)

    def test_bundled_ffmpeg_windows(self, tmp_path):
        """Test bundled FFmpeg detection on Windows."""
        from Spotify_Downloader import get_ffmpeg_path

        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", str(tmp_path), create=True),
            patch("sys.platform", "win32"),
        ):
            ffmpeg_dir = tmp_path / "ffmpeg"
            ffmpeg_dir.mkdir()
            ffmpeg_path = ffmpeg_dir / "ffmpeg.exe"
            ffmpeg_path.touch()

            result = get_ffmpeg_path()
            assert result == str(ffmpeg_dir)

    def test_homebrew_ffmpeg(self, tmp_path):
        """Test homebrew FFmpeg detection."""
        from Spotify_Downloader import get_ffmpeg_path

        # Mock not frozen (running from source)
        with (
            patch.object(sys, "frozen", False, create=True),
            patch("sys.platform", "darwin"),
            patch("os.path.exists") as mock_exists,
        ):
            # Return True only for homebrew path
            def exists_side_effect(path):
                return path == "/opt/homebrew/bin/ffmpeg"

            mock_exists.side_effect = exists_side_effect

            result = get_ffmpeg_path()
            assert result == "/opt/homebrew/bin"

    def test_system_ffmpeg_linux(self, tmp_path):
        """Test system FFmpeg detection on Linux."""
        from Spotify_Downloader import get_ffmpeg_path

        with (
            patch.object(sys, "frozen", False, create=True),
            patch("sys.platform", "linux"),
            patch("os.path.exists") as mock_exists,
        ):

            def exists_side_effect(path):
                return path == "/usr/bin/ffmpeg"

            mock_exists.side_effect = exists_side_effect

            result = get_ffmpeg_path()
            assert result == "/usr/bin"

    def test_ffmpeg_in_path(self):
        """Test FFmpeg detection via PATH."""
        from Spotify_Downloader import get_ffmpeg_path

        with (
            patch.object(sys, "frozen", False, create=True),
            patch("os.path.exists", return_value=False),
            patch("shutil.which", return_value="/custom/path/ffmpeg"),
        ):
            result = get_ffmpeg_path()
            assert result == "/custom/path"

    def test_ffmpeg_not_found(self):
        """Test when FFmpeg is not found anywhere."""
        from Spotify_Downloader import get_ffmpeg_path

        with (
            patch.object(sys, "frozen", False, create=True),
            patch("os.path.exists", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            result = get_ffmpeg_path()
            assert result is None


class TestMusicScraper:
    """Tests for MusicScraper class."""

    def test_sanitize_text(self):
        """Test text sanitization for filenames."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        assert scraper.sanitize_text("Hello World") == "Hello World"
        # ordinary punctuation is legal in filenames and kept verbatim
        assert scraper.sanitize_text("Test@Song#123") == "Test@Song#123"
        # accented/non-Latin titles must survive (special-char download bug)
        assert scraper.sanitize_text("MONTAGEM BAILÃO") == "MONTAGEM BAILÃO"
        # Windows-reserved characters are still stripped
        assert scraper.sanitize_text("A: B / C") == "A B C"

    def test_format_playlist_name(self):
        """Test playlist name formatting."""
        from Spotify_Downloader import MusicScraper
        from spotifydown_api import PlaylistInfo

        scraper = MusicScraper()

        # With owner
        meta = PlaylistInfo(
            name="My Playlist",
            owner="Test User",
            description=None,
            cover_url=None,
        )
        result = scraper.format_playlist_name(meta)
        assert result == "My Playlist - Test User"

        # Without owner
        meta_no_owner = PlaylistInfo(
            name="Solo Playlist",
            owner=None,
            description=None,
            cover_url=None,
        )
        result = scraper.format_playlist_name(meta_no_owner)
        assert result == "Solo Playlist - Spotify"

    def test_prepare_playlist_folder(self, tmp_path):
        """Test playlist folder creation."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        base = str(tmp_path)

        result = scraper.prepare_playlist_folder(base, "Test Playlist")
        assert os.path.exists(result)
        assert "Test Playlist" in result

    def test_prepare_playlist_folder_sanitizes_name(self, tmp_path):
        """Test that folder names are sanitized."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        base = str(tmp_path)

        result = scraper.prepare_playlist_folder(base, "Test/Playlist:Name")
        assert os.path.exists(result)
        # Special chars should be removed
        assert "/" not in os.path.basename(result)
        assert ":" not in os.path.basename(result)

    def test_returnSPOT_ID(self):
        """Test playlist ID extraction."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        url = "https://open.spotify.com/playlist/abc123xyz"
        assert scraper.returnSPOT_ID(url) == "abc123xyz"

    def test_increment_counter(self):
        """Test counter incrementing."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        assert scraper.counter == 0

        # Mock the signal emit
        scraper.count_updated = MagicMock()

        scraper.increment_counter()
        assert scraper.counter == 1
        scraper.count_updated.emit.assert_called_once_with(1)


class TestResumeManifest:
    """Tests for the resume manifest that skips already-downloaded tracks (#40)."""

    @staticmethod
    def _scraper():
        from Spotify_Downloader import MusicScraper

        return MusicScraper()

    def test_load_manifest_absent_returns_empty(self, tmp_path):
        """No manifest file means nothing to skip."""
        assert self._scraper()._load_manifest(str(tmp_path)) == set()

    def test_record_then_load_roundtrip(self, tmp_path):
        """Recorded tracks (whose files exist) come back as skip IDs."""
        folder = str(tmp_path)
        writer = self._scraper()
        writer._load_manifest(folder)  # arms _manifest_path
        (tmp_path / "a.mp3").write_bytes(b"x")
        (tmp_path / "b.mp3").write_bytes(b"x")
        writer._record_in_manifest("id_a", str(tmp_path / "a.mp3"))
        writer._record_in_manifest("id_b", str(tmp_path / "b.mp3"))

        assert self._scraper()._load_manifest(folder) == {"id_a", "id_b"}

    def test_load_prunes_missing_files(self, tmp_path):
        """A manifest entry whose file was deleted is not treated as done."""
        folder = str(tmp_path)
        writer = self._scraper()
        writer._load_manifest(folder)
        (tmp_path / "present.mp3").write_bytes(b"x")
        writer._record_in_manifest("present", str(tmp_path / "present.mp3"))
        writer._record_in_manifest("ghost", str(tmp_path / "ghost.mp3"))  # never created

        assert self._scraper()._load_manifest(folder) == {"present"}

    def test_record_without_manifest_path_is_noop(self, tmp_path):
        """Recording before a manifest is armed must not raise or write."""
        scraper = self._scraper()
        scraper._manifest_path = None
        scraper._record_in_manifest("id", str(tmp_path / "x.mp3"))
        assert self._scraper()._load_manifest(str(tmp_path)) == set()


class TestDownloadTrackAudioOpts:
    """Tests for yt-dlp performance options in download_track_audio."""

    def test_ydl_opts_include_retries(self):
        """Verify yt-dlp retries option is set."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info = MagicMock(return_value={"entries": []})
            with contextlib.suppress(Exception):
                scraper.download_track_audio("test query", "/tmp/test.mp3")
            call_args = mock_ydl.call_args
            opts = call_args[0][0] if call_args[0] else call_args[1]
            assert opts["retries"] == 5

    def test_ydl_opts_include_socket_timeout(self):
        """Verify yt-dlp socket_timeout is set."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            with contextlib.suppress(Exception):
                scraper.download_track_audio("test query", "/tmp/test.mp3")
            opts = mock_ydl.call_args[0][0]
            assert opts["socket_timeout"] == 15

    def test_ydl_opts_include_concurrent_fragments(self):
        """Verify yt-dlp concurrent_fragment_downloads is set."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            with contextlib.suppress(Exception):
                scraper.download_track_audio("test query", "/tmp/test.mp3")
            opts = mock_ydl.call_args[0][0]
            assert opts["concurrent_fragment_downloads"] == 4

    def test_per_call_audio_format_quality_override(self):
        """Per-call audio_format/audio_quality override scraper instance attrs."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper(audio_format="opus", audio_quality="192")
        url = "https://www.youtube.com/watch?v=abc"
        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
            patch("os.path.exists", return_value=True),
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            with contextlib.suppress(Exception):
                scraper.download_track_audio(
                    "test query", "/tmp/test.mp3", audio_format="mp3", audio_quality="320"
                )
            download_opts = mock_ydl.call_args[0][0]
            pp = download_opts["postprocessors"][0]
            assert pp["preferredcodec"] == "mp3"
            assert pp["preferredquality"] == "320"

            mock_ydl.reset_mock()
            with contextlib.suppress(Exception):
                scraper.download_track_audio("test query", "/tmp/test.opus")
            download_opts = mock_ydl.call_args[0][0]
            pp = download_opts["postprocessors"][0]
            assert pp["preferredcodec"] == "opus"
            assert pp["preferredquality"] == "192"

            # Lossless transcode stays available to the per-call seam (no
            # bitrate arg) even though no source offers flac for YouTube.
            mock_ydl.reset_mock()
            with contextlib.suppress(Exception):
                scraper.download_track_audio("test query", "/tmp/test.flac", audio_format="flac")
            download_opts = mock_ydl.call_args[0][0]
            pp = download_opts["postprocessors"][0]
            assert pp["preferredcodec"] == "flac"
            assert "preferredquality" not in pp


class TestOriginalPassthrough:
    """Tests for the original (no-transcode) YouTube download format."""

    @staticmethod
    def _scraper(**kw):
        from Spotify_Downloader import MusicScraper

        return MusicScraper(**kw)

    def test_ydl_opts_use_best_codec_no_quality(self):
        """With audio_format=original, PP uses preferredcodec=best and no quality."""
        scraper = self._scraper(audio_format="mp3", audio_quality="192")
        url = "https://www.youtube.com/watch?v=abc"
        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            with contextlib.suppress(Exception):
                scraper.download_track_audio(
                    "test query", "/tmp/test.mp3", audio_format="original", audio_quality="320"
                )
            pp = mock_ydl.call_args[0][0]["postprocessors"][0]
            assert pp["preferredcodec"] == "best"
            assert "preferredquality" not in pp

    def test_final_path_from_info_dict(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        dest_base = str(tmp_path / "Song")
        opus_path = tmp_path / "Song.opus"
        opus_path.write_bytes(b"opus")
        url = "https://www.youtube.com/watch?v=abc"
        info = {"requested_downloads": [{"filepath": str(opus_path)}], "format_id": "251"}
        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info = MagicMock(return_value=info)
            path, _ = scraper.download_track_audio("q", dest_base + ".mp3", audio_format="original")
        assert path == str(opus_path)

    def test_final_path_glob_when_info_none(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        dest_base = str(tmp_path / "Song")
        opus_path = tmp_path / "Song.opus"
        url = "https://www.youtube.com/watch?v=abc"

        def write_opus(*_args, **_kwargs):
            opus_path.write_bytes(b"opus")
            return None

        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info = MagicMock(side_effect=write_opus)
            path, _ = scraper.download_track_audio("q", dest_base + ".mp3", audio_format="original")
        assert path == str(opus_path)

    def test_stale_file_with_failed_attempt_raises(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        dest_base = str(tmp_path / "Song")
        stale = tmp_path / "Song.opus"
        stale.write_bytes(b"stale")
        url = "https://www.youtube.com/watch?v=abc"
        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info = MagicMock(return_value=None)
            with pytest.raises(RuntimeError, match="no playable audio source"):
                scraper.download_track_audio("q", dest_base + ".mp3", audio_format="original")
        assert stale.exists()

    def test_glob_ignores_temp_and_nonaudio(self, tmp_path):
        from Spotify_Downloader import _resolve_passthrough_download

        base = str(tmp_path / "Song")
        (tmp_path / "Song.opus.part").write_bytes(b"x")
        (tmp_path / "Song.temp.m4a").write_bytes(b"x")
        (tmp_path / "Song.jpg").write_bytes(b"x")
        m4a = tmp_path / "Song.m4a"
        m4a.write_bytes(b"audio")
        assert _resolve_passthrough_download(base, None, set()) == str(m4a)

    def test_glob_excludes_stale_lossless_files(self, tmp_path):
        from Spotify_Downloader import _resolve_passthrough_download

        base = str(tmp_path / "Song")
        (tmp_path / "Song.flac").write_bytes(b"flac")
        (tmp_path / "Song.wav").write_bytes(b"wav")
        assert _resolve_passthrough_download(base, None, set()) is None

    def test_glob_excludes_preexisting_candidates(self, tmp_path):
        from Spotify_Downloader import _resolve_passthrough_download

        base = str(tmp_path / "Song")
        m4a = tmp_path / "Song.m4a"
        m4a.write_bytes(b"audio")
        m4a_str = str(m4a)
        assert _resolve_passthrough_download(base, None, {m4a_str}) is None
        assert _resolve_passthrough_download(base, None, set()) == m4a_str

    def test_glob_handles_bracket_filenames(self, tmp_path):
        from Spotify_Downloader import _resolve_passthrough_download

        base = str(tmp_path / "Song [abc123]")
        opus = tmp_path / "Song [abc123].opus"
        opus.write_bytes(b"opus")
        assert _resolve_passthrough_download(base, None, set()) == str(opus)

    def test_size_cap_applies_to_resolved_path(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper(max_track_mb=10)
        dest_base = str(tmp_path / "Song")
        opus_path = tmp_path / "Song.opus"
        opus_path.write_bytes(b"x" * (20 * 1024 * 1024))
        url = "https://www.youtube.com/watch?v=huge"
        raised = None
        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info = MagicMock(
                return_value={"requested_downloads": [{"filepath": str(opus_path)}]}
            )
            try:
                scraper.download_track_audio("q", dest_base + ".mp3", audio_format="original")
            except RuntimeError as exc:
                raised = str(exc)
        assert raised is not None and "size limit" in raised
        assert not opus_path.exists()

    def test_no_file_raises_no_playable(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        dest_base = str(tmp_path / "Song")
        url = "https://www.youtube.com/watch?v=missing"
        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info = MagicMock(return_value=None)
            with pytest.raises(RuntimeError, match="no playable audio source"):
                scraper.download_track_audio("q", dest_base + ".mp3", audio_format="original")


class TestConfigOriginalFormat:
    def test_original_round_trips_for_youtube(self, tmp_path, monkeypatch):
        import json

        from Spotify_Downloader import load_config

        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"format": "original", "download_source": "youtube"}))
        monkeypatch.setattr("Spotify_Downloader._config_path", lambda: str(cfg))
        assert load_config()["format"] == "original"

    def test_original_coerced_for_librespot(self, tmp_path, monkeypatch):
        import json

        from Spotify_Downloader import load_config

        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"format": "original", "download_source": "librespot"}))
        monkeypatch.setattr("Spotify_Downloader._config_path", lambda: str(cfg))
        assert load_config()["format"] == "ogg"


class TestYoutubeMatchSelection:
    """Tests for duration-aware YouTube match selection.

    The top search hit is often the music video (extra intro/outro) which plays
    as a different recording than the Spotify track. Selection must steer to the
    candidate whose duration matches the Spotify track.
    """

    @staticmethod
    def _scraper():
        from Spotify_Downloader import MusicScraper

        return MusicScraper()

    def _patched(self, entries):
        candidates = {"entries": entries}
        ctx = patch("Spotify_Downloader.YoutubeDL")
        mock_ydl = ctx.start()
        mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
        mock_ydl.extract_info = MagicMock(return_value=candidates)
        return ctx

    def test_picks_duration_closest_not_top_hit(self):
        """With a known duration, the closest-length candidate wins over #1."""
        entries = [
            {"id": "musicvideo", "duration": 183, "title": "WAGWAN [MUSIC VIDEO]"},
            {"id": "audio", "duration": 127, "title": "Wagwan (Official Audio)"},
        ]
        ctx = self._patched(entries)
        try:
            url = self._scraper()._select_youtube_match("ytsearch5:wagwan", 127)
        finally:
            ctx.stop()
        assert url == "https://www.youtube.com/watch?v=audio"

    def test_keeps_top_hit_when_its_duration_is_already_close(self):
        """No regression: a top hit within tolerance is kept even if another
        candidate is a hair closer (avoids preferring a same-length wrong edit)."""
        entries = [
            {"id": "official", "duration": 204, "title": "Song (Official Audio)"},
            {"id": "spedup", "duration": 200, "title": "Song (sped up)"},
        ]
        ctx = self._patched(entries)
        try:
            # expected 200s; top is 4s off (within 7s tolerance) so it stays
            url = self._scraper()._select_youtube_match("ytsearch5:song", 200)
        finally:
            ctx.stop()
        assert url == "https://www.youtube.com/watch?v=official"

    def test_falls_back_to_first_without_expected_duration(self):
        """No known duration keeps the top available result (legacy behavior)."""
        entries = [
            {"id": "first", "duration": 183, "title": "A"},
            {"id": "second", "duration": 127, "title": "B"},
        ]
        ctx = self._patched(entries)
        try:
            url = self._scraper()._select_youtube_match("ytsearch5:x", None)
        finally:
            ctx.stop()
        assert url == "https://www.youtube.com/watch?v=first"

    def test_returns_none_on_no_results(self):
        """An empty search returns None so the caller can fall through / fail."""
        ctx = self._patched([])
        try:
            url = self._scraper()._select_youtube_match("ytsearch5:nothing", 200)
        finally:
            ctx.stop()
        assert url is None


class TestExtendedMixSelection:
    """Tests for extended-mix mode YouTube match selection."""

    @staticmethod
    def _scraper(extended_mix=False, max_extended_minutes=20):
        from Spotify_Downloader import MusicScraper

        return MusicScraper(extended_mix=extended_mix, max_extended_minutes=max_extended_minutes)

    def test_max_track_duration_from_configurable_minutes(self):
        """max_extended_minutes maps to max_track_duration_s (minutes * 60)."""
        assert (
            self._scraper(extended_mix=True, max_extended_minutes=30).max_track_duration_s == 1800
        )
        assert self._scraper(extended_mix=True).max_track_duration_s == 1200

    def test_configurable_cap_rejects_above_ceiling(self):
        """A keyworded cut above the configured minute cap is rejected."""
        entries = [
            {"id": "long", "duration": 450, "title": "Song (Extended Mix)"},
        ]
        ctx = self._patched(entries)
        try:
            url = self._scraper(extended_mix=True, max_extended_minutes=5)._select_youtube_match(
                "ytsearch10:song extended mix", 200, prefer_extended=True
            )
        finally:
            ctx.stop()
        assert url is None

    def test_configurable_cap_accepts_within_ceiling(self):
        """A longer keyworded cut within ratio and a higher cap is accepted."""
        entries = [
            {"id": "long", "duration": 450, "title": "Song (Extended Mix)"},
        ]
        ctx = self._patched(entries)
        try:
            url = self._scraper(extended_mix=True, max_extended_minutes=15)._select_youtube_match(
                "ytsearch10:song extended mix", 200, prefer_extended=True
            )
        finally:
            ctx.stop()
        assert url == "https://www.youtube.com/watch?v=long"

    def test_fallback_rejects_over_ratio_under_abs_cap(self):
        """Over-ratio keyworded cuts below the absolute cap must not win via fallback."""
        entries = [
            {"id": "over", "duration": 900, "title": "Song (Extended Mix)"},
        ]
        ctx = self._patched(entries)
        try:
            url = self._scraper()._select_youtube_match(
                "ytsearch10:song extended mix", 210, prefer_extended=True
            )
        finally:
            ctx.stop()
        assert url is None

    def test_fallback_rejects_tiny_preview(self):
        """Tiny preview clips with extended keywords must not win via fallback."""
        entries = [
            {
                "id": "preview",
                "duration": 30,
                "title": "Song (Extended Mix) preview",
            },
        ]
        ctx = self._patched(entries)
        try:
            url = self._scraper()._select_youtube_match(
                "ytsearch10:song extended mix", 210, prefer_extended=True
            )
        finally:
            ctx.stop()
        assert url is None

    def _patched(self, entries):
        candidates = {"entries": entries}
        ctx = patch("Spotify_Downloader.YoutubeDL")
        mock_ydl = ctx.start()
        mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
        mock_ydl.extract_info = MagicMock(return_value=candidates)
        return ctx

    def test_picks_extended_over_radio_edit(self):
        """Extended mode prefers a longer cut over the radio edit top hit."""
        entries = [
            {"id": "radio", "duration": 127, "title": "Song (Official Audio)"},
            {"id": "extended", "duration": 240, "title": "Song (Extended Mix)"},
        ]
        ctx = self._patched(entries)
        try:
            url = self._scraper()._select_youtube_match(
                "ytsearch10:song extended mix", 127, prefer_extended=True
            )
        finally:
            ctx.stop()
        assert url == "https://www.youtube.com/watch?v=extended"

    def test_returns_none_when_no_extended_candidate(self):
        """Strict extended mode returns None when no keyworded in-range cut exists."""
        entries = [
            {"id": "official", "duration": 204, "title": "Song (Official Audio)"},
            {"id": "spedup", "duration": 200, "title": "Song (sped up)"},
        ]
        ctx = self._patched(entries)
        try:
            url = self._scraper()._select_youtube_match(
                "ytsearch10:song extended mix", 200, prefer_extended=True
            )
        finally:
            ctx.stop()
        assert url is None

    def test_rejects_hour_long_mix_even_with_keyword(self):
        """An hour-long 'Extended Mix' exceeds the ratio cap and is rejected."""
        entries = [
            {"id": "mix", "duration": 7200, "title": "Song (Extended Mix)"},
        ]
        ctx = self._patched(entries)
        try:
            url = self._scraper()._select_youtube_match(
                "ytsearch10:song extended mix", 210, prefer_extended=True
            )
        finally:
            ctx.stop()
        assert url is None

    def test_rejects_wrong_long_upload_without_keyword(self):
        """Long DJ-set uploads without extended keywords must not be selected."""
        entries = [
            {"id": "original", "duration": 210, "title": "Song"},
            {"id": "djset", "duration": 3600, "title": "Beach House 2026 Mix"},
        ]
        ctx = self._patched(entries)
        try:
            url = self._scraper()._select_youtube_match(
                "ytsearch10:song extended mix", 210, prefer_extended=True
            )
        finally:
            ctx.stop()
        assert url is None

    def test_prefers_keyword_match_among_longer_candidates(self):
        """Among longer cuts, titles with extended keywords win over generic uploads."""
        entries = [
            {"id": "radio", "duration": 127, "title": "Song"},
            {"id": "djset", "duration": 3600, "title": "Song full set live"},
            {"id": "club", "duration": 300, "title": "Song (Club Mix)"},
        ]
        ctx = self._patched(entries)
        try:
            url = self._scraper()._select_youtube_match(
                "ytsearch10:song extended mix", 127, prefer_extended=True
            )
        finally:
            ctx.stop()
        assert url == "https://www.youtube.com/watch?v=club"

    def test_no_duration_skips_hour_long_top_hit(self):
        """Without Spotify duration, reject hour-long keyworded top hits."""
        entries = [
            {
                "id": "fullmix",
                "duration": 3124,
                "title": "Beach House 2026 (Extended Mix) 1 hour mix",
            },
            {"id": "real", "duration": 420, "title": "Weekend Infinito (Extended Mix)"},
        ]
        ctx = self._patched(entries)
        try:
            url = self._scraper()._select_youtube_match(
                "ytsearch10:song extended mix", None, prefer_extended=True
            )
        finally:
            ctx.stop()
        assert url == "https://www.youtube.com/watch?v=real"

    def test_no_duration_returns_none_when_only_over_cap_keyworded(self):
        """Without Spotify duration, over-cap keyworded results yield None."""
        entries = [
            {
                "id": "fullmix",
                "duration": 3124,
                "title": "Beach House 2026 (Extended Mix) 1 hour mix",
            },
        ]
        ctx = self._patched(entries)
        try:
            url = self._scraper()._select_youtube_match(
                "ytsearch10:song extended mix", None, prefer_extended=True
            )
        finally:
            ctx.stop()
        assert url is None

    def test_no_duration_requires_extended_keyword(self):
        """Without Spotify duration, non-keyworded candidates are rejected."""
        entries = [
            {"id": "real", "duration": 420, "title": "Weekend Infinito Official Audio"},
        ]
        ctx = self._patched(entries)
        try:
            url = self._scraper()._select_youtube_match(
                "ytsearch10:song extended mix", None, prefer_extended=True
            )
        finally:
            ctx.stop()
        assert url is None

    def test_absolute_cap_excludes_keyworded_over_ratio_and_ceiling(self):
        """Keyworded uploads beyond min(ratio*expected, abs cap) are excluded."""
        entries = [
            {"id": "djset", "duration": 4000, "title": "Song (Extended Mix)"},
        ]
        ctx = self._patched(entries)
        try:
            url = self._scraper()._select_youtube_match(
                "ytsearch10:song extended mix", 300, prefer_extended=True
            )
        finally:
            ctx.stop()
        assert url is None

    def test_already_extended_picks_closest_keyworded_within_cap(self):
        """When Spotify track is already extended, pick closest keyworded cut."""
        entries = [
            {"id": "real", "duration": 410, "title": "X (Extended Mix)"},
        ]
        ctx = self._patched(entries)
        try:
            url = self._scraper()._select_youtube_match(
                "ytsearch10:song extended mix", 420, prefer_extended=True
            )
        finally:
            ctx.stop()
        assert url == "https://www.youtube.com/watch?v=real"

    def test_already_extended_picks_slightly_longer_within_window(self):
        """Already-extended track: pick keyworded cut within ratio window."""
        entries = [
            {"id": "real", "duration": 430, "title": "Song (Extended Mix)"},
        ]
        ctx = self._patched(entries)
        try:
            url = self._scraper()._select_youtube_match(
                "ytsearch10:song extended mix", 420, prefer_extended=True
            )
        finally:
            ctx.stop()
        assert url == "https://www.youtube.com/watch?v=real"


class TestExtendedMixDownload:
    """Tests for extended-mix two-stage download fallback."""

    @staticmethod
    def _scraper(extended_mix=True):
        from Spotify_Downloader import MusicScraper

        return MusicScraper(extended_mix=extended_mix)

    def test_extended_search_query_uses_broad_extended_phrase(self):
        """Regression: the strict-extended query must append only "extended".

        Appending "extended mix audio" (the old behavior) pushed genuine
        "(Extended Version)" uploads out of YouTube's top results, so the
        extended cut was never found (e.g. AWGAZI). Guard against re-adding the
        "mix"/"audio" tokens.
        """
        from Spotify_Downloader import MusicScraper

        query = MusicScraper(extended_mix=True)._extended_search_query(
            "AWGAZI", "Palm Monkey, RUSSI, The Palm Tree Boy"
        )
        assert query.startswith("ytsearch10:")
        assert "extended" in query
        assert " mix" not in query
        assert " audio" not in query

    def test_builds_strict_extended_plan_before_fallback(self):
        scraper = self._scraper()
        extended_q = "ytsearch10:Song Artist extended mix audio"
        normal_q = "ytsearch1:Song Artist audio"
        plan = scraper._build_youtube_download_plan(extended_q, fallback_query=normal_q)
        assert plan[0] == (extended_q, True)
        assert plan[1] == ("ytsearch5:Song Artist audio", False)

    def test_falls_back_to_normal_query_when_no_extended_candidate(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper(extended_mix=True)
        extended_q = "ytsearch10:Song Artist extended mix audio"
        normal_q = "ytsearch1:Song Artist audio"
        dest = str(tmp_path / "Song.mp3")
        fallback_url = "https://www.youtube.com/watch?v=original"

        mock_select = MagicMock(
            side_effect=lambda query, *_a, **kw: (
                None
                if kw.get("prefer_extended")
                else fallback_url
                if query.startswith("ytsearch5:Song Artist audio")
                else None
            )
        )

        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", mock_select),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
            patch("os.path.exists") as mock_exists,
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            mock_exists.side_effect = lambda p: p == dest

            result, used_extended = scraper.download_track_audio(
                extended_q, dest, expected_duration_s=210, fallback_query=normal_q
            )

        assert result == dest
        assert used_extended is False
        mock_select.assert_any_call(
            extended_q, 210, prefer_extended=True, source_title=None, logger=ANY, cookiefile=None
        )
        mock_select.assert_any_call(
            "ytsearch5:Song Artist audio",
            210,
            prefer_extended=False,
            source_title=None,
            logger=ANY,
            cookiefile=None,
        )

    def test_extended_leg_reports_used_extended_true(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper(extended_mix=True)
        extended_q = "ytsearch10:Song Artist extended mix audio"
        dest = str(tmp_path / "Song.mp3")
        extended_url = "https://www.youtube.com/watch?v=extended"

        mock_select = MagicMock(
            side_effect=lambda *_a, **kw: extended_url if kw.get("prefer_extended") else None
        )

        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", mock_select),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
            patch("os.path.exists") as mock_exists,
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            mock_exists.side_effect = lambda p: p == dest

            result, used_extended = scraper.download_track_audio(
                extended_q, dest, expected_duration_s=420
            )

        assert result == dest
        assert used_extended is True


class TestResolveExtendedOutput:
    """Tests for post-download filename/title correction on extended-mix fallback."""

    @staticmethod
    def _scraper(extended_mix=True):
        from Spotify_Downloader import MusicScraper

        return MusicScraper(extended_mix=extended_mix)

    def test_extended_wins_keeps_marked_path_and_title(self, tmp_path):
        scraper = self._scraper(extended_mix=True)
        marked = tmp_path / "Song (Extended Mix) - Artist.mp3"
        marked.write_bytes(b"audio")
        display_title = "Song (Extended Mix)"
        final_path, meta_title = scraper._resolve_extended_output(
            str(marked),
            used_extended=True,
            folder=str(tmp_path),
            sanitized_artists="Artist",
            track_title="Song",
            display_title=display_title,
            track_id="abc123",
        )
        assert final_path == str(marked)
        assert meta_title == display_title
        assert marked.exists()

    def test_fallback_renames_to_unmarked_and_uses_track_title(self, tmp_path):
        scraper = self._scraper(extended_mix=True)
        marked = tmp_path / "Song (Extended Mix) - Artist.mp3"
        marked.write_bytes(b"audio")
        display_title = "Song (Extended Mix)"
        unmarked = tmp_path / "Song - Artist.mp3"
        final_path, meta_title = scraper._resolve_extended_output(
            str(marked),
            used_extended=False,
            folder=str(tmp_path),
            sanitized_artists="Artist",
            track_title="Song",
            display_title=display_title,
            track_id="abc123",
        )
        assert final_path == str(unmarked)
        assert meta_title == "Song"
        assert not marked.exists()
        assert unmarked.exists()
        assert unmarked.read_bytes() == b"audio"

    def test_fallback_no_rename_when_already_unmarked(self, tmp_path):
        scraper = self._scraper(extended_mix=True)
        unmarked = tmp_path / "Song - Artist.mp3"
        unmarked.write_bytes(b"audio")
        final_path, meta_title = scraper._resolve_extended_output(
            str(unmarked),
            used_extended=False,
            folder=str(tmp_path),
            sanitized_artists="Artist",
            track_title="Song",
            display_title="Song",
            track_id="abc123",
        )
        assert final_path == str(unmarked)
        assert meta_title == "Song"
        assert unmarked.exists()

    def test_non_extended_mode_unchanged(self, tmp_path):
        scraper = self._scraper(extended_mix=False)
        path = tmp_path / "Song - Artist.mp3"
        path.write_bytes(b"audio")
        final_path, meta_title = scraper._resolve_extended_output(
            str(path),
            used_extended=False,
            folder=str(tmp_path),
            sanitized_artists="Artist",
            track_title="Song",
            display_title="Song",
            track_id="abc123",
        )
        assert final_path == str(path)
        assert meta_title == "Song"
        assert path.exists()

    def test_fallback_collision_uses_track_id_suffix(self, tmp_path):
        scraper = self._scraper(extended_mix=True)
        marked = tmp_path / "Song (Extended Mix) - Artist.mp3"
        marked.write_bytes(b"fallback")
        (tmp_path / "Song - Artist.mp3").write_bytes(b"existing")
        display_title = "Song (Extended Mix)"
        expected = tmp_path / "Song - Artist [abc123].mp3"
        final_path, meta_title = scraper._resolve_extended_output(
            str(marked),
            used_extended=False,
            folder=str(tmp_path),
            sanitized_artists="Artist",
            track_title="Song",
            display_title=display_title,
            track_id="abc123",
        )
        assert final_path == str(expected)
        assert meta_title == "Song"
        assert not marked.exists()
        assert expected.exists()
        assert expected.read_bytes() == b"fallback"


class TestExtendedMixTitleTag:
    """Tests for the metadata title annotation in extended-mix mode."""

    @staticmethod
    def _scraper(extended_mix=False):
        from Spotify_Downloader import MusicScraper

        return MusicScraper(extended_mix=extended_mix)

    def test_default_mode_leaves_title_untouched(self):
        assert self._scraper(extended_mix=False)._meta_title("Song") == "Song"

    def test_extended_mode_appends_suffix(self):
        assert self._scraper(extended_mix=True)._meta_title("Song") == "Song (Extended Mix)"

    def test_extended_mode_does_not_double_append(self):
        """A title that already says 'extended' is left as-is."""
        title = "Song (Extended Mix)"
        assert self._scraper(extended_mix=True)._meta_title(title) == title
        assert (
            self._scraper(extended_mix=True)._meta_title("Song - Extended Version")
            == "Song - Extended Version"
        )


class TestStripRadioEdit:
    """Tests for MusicScraper._strip_radio_edit (extended-mix title cleanup)."""

    @staticmethod
    def _strip(title: str) -> str:
        from Spotify_Downloader import MusicScraper

        return MusicScraper._strip_radio_edit(title)

    def test_dash_separator(self):
        assert self._strip("Hold Me - Radio Edit") == "Hold Me"

    def test_dash_separator_maye(self):
        assert self._strip("Maye - Radio Edit") == "Maye"

    def test_paren_separator(self):
        assert self._strip("Song (Radio Edit)") == "Song"

    def test_bracket_separator(self):
        assert self._strip("Song [Radio Edit]") == "Song"

    def test_case_insensitive(self):
        assert self._strip("Track - RADIO EDIT") == "Track"

    def test_unrelated_radio_preserved(self):
        assert self._strip("Radio Ga Ga") == "Radio Ga Ga"

    def test_unchanged_when_no_descriptor(self):
        assert self._strip("Just a Song") == "Just a Song"

    def test_only_radio_edit_removed(self):
        assert self._strip("Song (Radio Edit) - Remastered") == "Song - Remastered"

    def test_radio_editorial_unchanged(self):
        assert self._strip("Song - Radio Editorial") == "Song - Radio Editorial"

    def test_radio_edit_version_unchanged(self):
        assert self._strip("Song (Radio Edit Version)") == "Song (Radio Edit Version)"

    def test_extended_mode_meta_title_integration(self):
        from Spotify_Downloader import MusicScraper

        assert (
            MusicScraper(extended_mix=True)._meta_title(
                MusicScraper._strip_radio_edit("Hold Me - Radio Edit")
            )
            == "Hold Me (Extended Mix)"
        )
        assert (
            MusicScraper(extended_mix=False)._meta_title("Hold Me - Radio Edit")
            == "Hold Me - Radio Edit"
        )


class TestExtendedMixFilenameTitle:
    """Tests for extended-mix display title used in output filenames."""

    @staticmethod
    def _scraper(extended_mix=False):
        from Spotify_Downloader import MusicScraper

        return MusicScraper(extended_mix=extended_mix)

    @staticmethod
    def _display_title(scraper, raw_title: str) -> str:
        track_title = raw_title
        if scraper.extended_mix:
            track_title = scraper._strip_radio_edit(track_title)
        return scraper._meta_title(track_title)

    def test_extended_mode_broken_pieces_display_title(self):
        scraper = self._scraper(extended_mix=True)
        display = scraper._meta_title(scraper._strip_radio_edit("Broken Pieces"))
        assert display == "Broken Pieces (Extended Mix)"
        assert scraper.sanitize_text(display) == "Broken Pieces (Extended Mix)"

    def test_extended_mode_radio_edit_display_title(self):
        scraper = self._scraper(extended_mix=True)
        display = scraper._meta_title(scraper._strip_radio_edit("Hold Me - Radio Edit"))
        assert display == "Hold Me (Extended Mix)"

    def test_normal_mode_display_title_unchanged(self):
        scraper = self._scraper(extended_mix=False)
        display = self._display_title(scraper, "Broken Pieces")
        assert display == "Broken Pieces"

    def test_output_filename_includes_extended_mix_marker(self):
        artist = "Paccu"
        extended = self._scraper(extended_mix=True)
        extended_display = self._display_title(extended, "Broken Pieces")
        extended_filename = (
            f"{extended.sanitize_text(extended_display)} - {extended.sanitize_text(artist)}.mp3"
        )
        assert extended_filename == "Broken Pieces (Extended Mix) - Paccu.mp3"

        normal = self._scraper(extended_mix=False)
        normal_display = self._display_title(normal, "Broken Pieces")
        normal_filename = (
            f"{normal.sanitize_text(normal_display)} - {normal.sanitize_text(artist)}.mp3"
        )
        assert normal_filename == "Broken Pieces - Paccu.mp3"


class TestWritingMetaTagsThread:
    """Tests for WritingMetaTagsThread synchronous cover fetch."""

    def test_meta_tags_written_to_file(self, tmp_path):
        """Verify ID3 tags are written correctly."""
        from Spotify_Downloader import WritingMetaTagsThread

        # Create a minimal valid MP3 file for mutagen
        mp3_path = str(tmp_path / "test.mp3")
        # Write a minimal MP3 frame header so mutagen can parse it
        with open(mp3_path, "wb") as f:
            # Minimal MP3 frame: sync word + valid header + padding
            f.write(b"\xff\xfb\x90\x00" + b"\x00" * 417)

        tags = {
            "title": "Test Song",
            "artists": "Test Artist",
            "album": "Test Album",
            "releaseDate": "2024-01-01",
            "cover": "",
            "file": mp3_path,
        }

        thread = WritingMetaTagsThread(tags, mp3_path)
        # Mock the signal
        thread.tags_success = MagicMock()

        with patch("Spotify_Downloader.EasyID3") as mock_easy:
            mock_audio = MagicMock()
            mock_easy.return_value = mock_audio
            thread.run()
            mock_audio.__setitem__.assert_any_call("title", "Test Song")
            mock_audio.__setitem__.assert_any_call("artist", "Test Artist")
            mock_audio.__setitem__.assert_any_call("album", "Test Album")
            mock_audio.save.assert_called()
            thread.tags_success.emit.assert_called_once_with("Tags added successfully")

    def test_cover_art_fetched_synchronously(self):
        """Verify cover art is downloaded synchronously, not via nested QThread."""
        from Spotify_Downloader import WritingMetaTagsThread

        tags = {
            "title": "Test",
            "artists": "Artist",
            "album": "Album",
            "releaseDate": "2024",
            "cover": "https://example.com/cover.jpg",
            "file": "/tmp/test.mp3",
        }

        thread = WritingMetaTagsThread(tags, "/tmp/test.mp3")
        thread.tags_success = MagicMock()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\x89PNG\r\n\x1a\n"  # Fake image data

        with (
            patch("Spotify_Downloader.EasyID3") as mock_easy,
            patch("Spotify_Downloader.ID3") as mock_id3,
            patch("Spotify_Downloader.requests.get", return_value=mock_response) as mock_get,
        ):
            mock_easy.return_value = MagicMock()
            mock_id3.return_value = MagicMock()
            thread.run()

            # Verify synchronous requests.get was called (not DownloadCover QThread)
            mock_get.assert_called_once_with("https://example.com/cover.jpg", timeout=15)
            # Verify APIC tag was set
            mock_id3.return_value.__setitem__.assert_called_once()
            thread.tags_success.emit.assert_called_once_with("Tags added successfully")

    def test_cover_art_failure_does_not_crash(self):
        """Verify cover art failure is handled gracefully."""
        from Spotify_Downloader import WritingMetaTagsThread

        tags = {
            "title": "Test",
            "artists": "Artist",
            "album": "",
            "releaseDate": "",
            "cover": "https://example.com/bad-cover.jpg",
            "file": "/tmp/test.mp3",
        }

        thread = WritingMetaTagsThread(tags, "/tmp/test.mp3")
        thread.tags_success = MagicMock()

        with (
            patch("Spotify_Downloader.EasyID3") as mock_easy,
            patch(
                "Spotify_Downloader.requests.get",
                side_effect=requests.RequestException("timeout"),
            ),
        ):
            mock_easy.return_value = MagicMock()
            thread.run()
            # Should still emit success (tags were written, just cover failed)
            thread.tags_success.emit.assert_called_once_with("Tags added successfully")


class TestStopButtonCooperative:
    """Tests for cooperative stop button behavior."""

    def test_cancel_event_stops_playlist_iteration(self):
        """Verify cancel event halts playlist download loop."""
        from Spotify_Downloader import MusicScraper

        cancel_event = threading.Event()
        scraper = MusicScraper(cancel_event=cancel_event)

        # Set cancel before scraping
        cancel_event.set()
        assert scraper.is_cancelled() is True

    def test_scraper_thread_request_cancel(self):
        """Verify ScraperThread.request_cancel sets the event."""
        from Spotify_Downloader import ScraperThread

        thread = ScraperThread("https://open.spotify.com/playlist/abc123")
        assert not thread._cancel_event.is_set()
        thread.request_cancel()
        assert thread._cancel_event.is_set()


class TestScraperThread:
    """Tests for ScraperThread class."""

    def test_init_defaults(self):
        """Test ScraperThread initialization."""
        from Spotify_Downloader import ScraperThread

        thread = ScraperThread("https://open.spotify.com/playlist/abc123")
        assert thread.spotify_link == "https://open.spotify.com/playlist/abc123"
        assert "music" in thread.music_folder

    def test_init_custom_folder(self, tmp_path):
        """Test ScraperThread with custom folder."""
        from Spotify_Downloader import ScraperThread

        folder = str(tmp_path)
        thread = ScraperThread("https://open.spotify.com/playlist/abc123", folder)
        assert thread.music_folder == folder


class TestParallelDownloads:
    """Tests for parallel track download behavior (issue #34)."""

    def _make_track(self, tid, title, artists="Artist"):
        """Build a minimal TrackInfo-like object for tests."""
        from spotifydown_api import TrackInfo

        return TrackInfo(
            id=tid,
            title=title,
            artists=artists,
            album=None,
            release_date=None,
            cover_url=None,
            duration_ms=None,
            preview_url=None,
            raw={},
        )

    def test_max_workers_default_is_four(self):
        """Default parallel worker count matches measured sweet spot."""
        from Spotify_Downloader import MusicScraper

        assert MusicScraper.MAX_WORKERS == 4

    def test_counter_increment_is_thread_safe(self):
        """Parallel increment_counter calls produce correct total with no races."""
        import concurrent.futures

        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        # Stub the signal so we don't need a QApplication for this test
        scraper.count_updated = MagicMock()

        iterations = 1000
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(lambda _: scraper.increment_counter(), range(iterations)))

        assert scraper.counter == iterations

    def test_failed_tracks_append_is_thread_safe(self):
        """Parallel appends to _failed_tracks don't lose entries."""
        import concurrent.futures

        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()

        def append_one(i):
            with scraper._failed_lock:
                scraper._failed_tracks.append(f"track_{i}")

        n = 500
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(append_one, range(n)))

        assert len(scraper._failed_tracks) == n

    def test_small_playlist_stays_sequential(self, tmp_path):
        """Playlists under the parallel threshold (3 tracks) run on one thread."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        for sig in (
            "song_meta",
            "add_song_meta",
            "dlprogress_signal",
            "Resetprogress_signal",
            "PlaylistID",
            "song_Album",
            "PlaylistCompleted",
            "error_signal",
            "count_updated",
        ):
            setattr(scraper, sig, MagicMock())

        tracks = [self._make_track(f"id{i}", f"Song {i}") for i in range(2)]
        seen_workers: set[str] = set()

        def fake_download(query, dest, **_kw):
            seen_workers.add(threading.current_thread().name)
            open(dest, "wb").close()
            return dest, False

        scraper.download_track_audio = fake_download
        mock_api = MagicMock()
        meta = MagicMock()
        meta.name = "T"
        meta.owner = "O"
        meta.cover_url = None
        mock_api.get_playlist_metadata.return_value = meta
        mock_api.iter_playlist_tracks.return_value = iter(tracks)
        scraper.ensure_spotifydown_api = MagicMock(return_value=mock_api)
        scraper.format_playlist_name = lambda _m: "T"

        scraper.scrape_playlist("https://open.spotify.com/playlist/abc123", str(tmp_path))

        # All 2 downloads happened on the main test thread — no pool spawned
        assert seen_workers == {threading.current_thread().name}
        assert scraper._parallel_mode is False

    def test_parallel_playlist_uses_max_workers_threads(self, tmp_path):
        """Playlists >= threshold actually spawn MAX_WORKERS distinct threads."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        for sig in (
            "song_meta",
            "add_song_meta",
            "dlprogress_signal",
            "Resetprogress_signal",
            "PlaylistID",
            "song_Album",
            "PlaylistCompleted",
            "error_signal",
            "count_updated",
        ):
            setattr(scraper, sig, MagicMock())

        tracks = [self._make_track(f"id{i}", f"Song {i}") for i in range(8)]
        seen_workers: set[str] = set()
        barrier = threading.Barrier(MusicScraper.MAX_WORKERS, timeout=5)

        def fake_download(query, dest, **_kw):
            seen_workers.add(threading.current_thread().name)
            # Block until MAX_WORKERS threads have arrived concurrently. Proves
            # the pool really spawned that many threads in parallel.
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait()
            open(dest, "wb").close()
            return dest, False

        scraper.download_track_audio = fake_download
        mock_api = MagicMock()
        meta = MagicMock()
        meta.name = "T"
        meta.owner = "O"
        meta.cover_url = None
        mock_api.get_playlist_metadata.return_value = meta
        mock_api.iter_playlist_tracks.return_value = iter(tracks)
        scraper.ensure_spotifydown_api = MagicMock(return_value=mock_api)
        scraper.format_playlist_name = lambda _m: "T"

        scraper.scrape_playlist("https://open.spotify.com/playlist/abc123", str(tmp_path))

        # With 8 tracks and MAX_WORKERS=4, exactly 4 unique worker threads ran
        assert len(seen_workers) == MusicScraper.MAX_WORKERS
        # Main test thread did not participate in downloads (pool ran them all)
        assert threading.current_thread().name not in seen_workers

    def test_exception_in_one_worker_does_not_abort_others(self, tmp_path):
        """A failure on one track must not prevent other tracks from finishing."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        for sig in (
            "song_meta",
            "add_song_meta",
            "dlprogress_signal",
            "Resetprogress_signal",
            "PlaylistID",
            "song_Album",
            "PlaylistCompleted",
            "error_signal",
            "count_updated",
        ):
            setattr(scraper, sig, MagicMock())

        tracks = [self._make_track(f"id{i}", f"Song {i}") for i in range(5)]
        completed = []

        def fake_download(query, dest, **_kw):
            if "Song 2" in query:
                raise RuntimeError("simulated yt-dlp failure")
            open(dest, "wb").close()
            completed.append(dest)
            return dest, False

        scraper.download_track_audio = fake_download
        mock_api = MagicMock()
        meta = MagicMock()
        meta.name = "T"
        meta.owner = "O"
        meta.cover_url = None
        mock_api.get_playlist_metadata.return_value = meta
        mock_api.iter_playlist_tracks.return_value = iter(tracks)
        scraper.ensure_spotifydown_api = MagicMock(return_value=mock_api)
        scraper.format_playlist_name = lambda _m: "T"

        scraper.scrape_playlist("https://open.spotify.com/playlist/abc123", str(tmp_path))
        # 4 tracks downloaded, 1 failed
        assert len(completed) == 4
        assert "Song 2" in scraper._failed_tracks

    def test_generator_is_materialized_before_threading(self, tmp_path):
        """Generator is fully consumed on the main thread before worker submission."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        for sig in (
            "song_meta",
            "add_song_meta",
            "dlprogress_signal",
            "Resetprogress_signal",
            "PlaylistID",
            "song_Album",
            "PlaylistCompleted",
            "error_signal",
            "count_updated",
        ):
            setattr(scraper, sig, MagicMock())

        main_thread_id = threading.get_ident()
        iter_threads = []

        def recording_generator():
            for i in range(4):
                iter_threads.append(threading.get_ident())
                yield self._make_track(f"id{i}", f"Song {i}")

        scraper.download_track_audio = lambda _q, d, **_kw: open(d, "wb").close() or (d, False)
        mock_api = MagicMock()
        meta = MagicMock()
        meta.name = "T"
        meta.owner = "O"
        meta.cover_url = None
        mock_api.get_playlist_metadata.return_value = meta
        mock_api.iter_playlist_tracks.return_value = recording_generator()
        scraper.ensure_spotifydown_api = MagicMock(return_value=mock_api)
        scraper.format_playlist_name = lambda _m: "T"

        scraper.scrape_playlist("https://open.spotify.com/playlist/abc", str(tmp_path))
        # Every yield should have come from the main thread (materialization)
        assert all(tid == main_thread_id for tid in iter_threads)
        assert len(iter_threads) == 4

    def test_cancel_before_threading_exits_early(self, tmp_path):
        """Cancel set before worker pool starts prevents any downloads."""
        from Spotify_Downloader import MusicScraper

        cancel_event = threading.Event()
        scraper = MusicScraper(cancel_event=cancel_event)
        for sig in (
            "song_meta",
            "add_song_meta",
            "dlprogress_signal",
            "Resetprogress_signal",
            "PlaylistID",
            "song_Album",
            "PlaylistCompleted",
            "error_signal",
            "count_updated",
        ):
            setattr(scraper, sig, MagicMock())

        tracks = [self._make_track(f"id{i}", f"Song {i}") for i in range(10)]

        downloads = []
        scraper.download_track_audio = lambda _q, d, **_kw: (
            downloads.append(d),
            open(d, "wb").close(),
            (d, False),
        )[2]
        mock_api = MagicMock()
        meta = MagicMock()
        meta.name = "T"
        meta.owner = "O"
        meta.cover_url = None
        mock_api.get_playlist_metadata.return_value = meta

        def gen():
            yield from tracks

        mock_api.iter_playlist_tracks.return_value = gen()
        scraper.ensure_spotifydown_api = MagicMock(return_value=mock_api)
        scraper.format_playlist_name = lambda _m: "T"

        cancel_event.set()  # Cancel before call
        scraper.scrape_playlist("https://open.spotify.com/playlist/abc", str(tmp_path))
        # No downloads should have started
        assert len(downloads) == 0

    def test_existing_files_are_skipped_in_parallel_mode(self, tmp_path):
        """Already-downloaded tracks aren't re-downloaded when parallelism is on."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        for sig in (
            "song_meta",
            "add_song_meta",
            "dlprogress_signal",
            "Resetprogress_signal",
            "PlaylistID",
            "song_Album",
            "PlaylistCompleted",
            "error_signal",
            "count_updated",
        ):
            setattr(scraper, sig, MagicMock())

        playlist_folder = tmp_path / "T"
        playlist_folder.mkdir()

        tracks = [self._make_track(f"id{i}", f"Song {i}") for i in range(4)]
        # Pre-create files for tracks 0 and 2
        for i in (0, 2):
            (playlist_folder / f"Song {i} - Artist.mp3").touch()

        downloaded = []

        def fake_dl(q, d, **_kw):
            downloaded.append(d)
            open(d, "wb").close()
            return d

        scraper.download_track_audio = fake_dl
        mock_api = MagicMock()
        meta = MagicMock()
        meta.name = "T"
        meta.owner = "O"
        meta.cover_url = None
        mock_api.get_playlist_metadata.return_value = meta
        mock_api.iter_playlist_tracks.return_value = iter(tracks)
        scraper.ensure_spotifydown_api = MagicMock(return_value=mock_api)
        scraper.format_playlist_name = lambda _m: "T"
        scraper.prepare_playlist_folder = lambda _base, _name: str(playlist_folder)

        scraper.scrape_playlist("https://open.spotify.com/playlist/abc", str(tmp_path))
        # Only tracks 1 and 3 should have triggered a download
        assert len(downloaded) == 2

    def test_state_resets_between_playlist_runs(self, tmp_path):
        """counter, _failed_tracks, and _in_flight_files clear on each scrape_playlist call.

        Audit fix H3: repeated use of the same MusicScraper must not carry
        stale counter or failure state.
        """
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        # Pre-populate with junk state as if a prior run left residue
        scraper.counter = 42
        scraper._failed_tracks = ["old_failure"]
        scraper._in_flight_files = {"/tmp/old_file.mp3"}

        for sig in (
            "song_meta",
            "add_song_meta",
            "dlprogress_signal",
            "Resetprogress_signal",
            "PlaylistID",
            "song_Album",
            "PlaylistCompleted",
            "error_signal",
            "count_updated",
        ):
            setattr(scraper, sig, MagicMock())

        tracks = [self._make_track("id1", "Song A")]
        scraper.download_track_audio = lambda _q, d, **_kw: open(d, "wb").close() or (d, False)
        mock_api = MagicMock()
        meta = MagicMock()
        meta.name = "T"
        meta.owner = "O"
        meta.cover_url = None
        mock_api.get_playlist_metadata.return_value = meta
        mock_api.iter_playlist_tracks.return_value = iter(tracks)
        scraper.ensure_spotifydown_api = MagicMock(return_value=mock_api)
        scraper.format_playlist_name = lambda _m: "T"

        scraper.scrape_playlist("https://open.spotify.com/playlist/abc", str(tmp_path))

        # Stale state must be gone; counter reflects only this run
        assert scraper.counter == 1
        assert "old_failure" not in scraper._failed_tracks
        assert "/tmp/old_file.mp3" not in scraper._in_flight_files

    def test_filename_collision_produces_unique_files(self, tmp_path):
        """Two tracks that sanitize to the same filename get unique paths.

        Audit fix M1: parallel downloads can't TOCTOU-race into the same file.
        """
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        for sig in (
            "song_meta",
            "add_song_meta",
            "dlprogress_signal",
            "Resetprogress_signal",
            "PlaylistID",
            "song_Album",
            "PlaylistCompleted",
            "error_signal",
            "count_updated",
        ):
            setattr(scraper, sig, MagicMock())

        # Simulate concurrent claim: first worker holds the filename, second
        # must get a suffixed path.
        playlist_folder = tmp_path / "T"
        playlist_folder.mkdir()

        tracks = [
            self._make_track("id_a", "Song", "Artist"),
            self._make_track("id_b", "Song", "Artist"),
        ]

        claimed_paths = []

        def slow_download(query, dest, **_kw):
            # Both workers hit this. Record which path each one claimed, then
            # create the file.
            claimed_paths.append(dest)
            open(dest, "wb").close()
            return dest, False

        scraper.download_track_audio = slow_download
        mock_api = MagicMock()
        meta = MagicMock()
        meta.name = "T"
        meta.owner = "O"
        meta.cover_url = None
        mock_api.get_playlist_metadata.return_value = meta
        mock_api.iter_playlist_tracks.return_value = iter(tracks)
        scraper.ensure_spotifydown_api = MagicMock(return_value=mock_api)
        scraper.format_playlist_name = lambda _m: "T"
        scraper.prepare_playlist_folder = lambda _base, _name: str(playlist_folder)

        # 2 tracks stays on sequential path; force parallel by calling
        # _download_one_track directly with both tracks from workers.
        scraper._parallel_mode = True
        scraper._total_tracks = 2

        # Pre-claim the filename as if worker A got there first
        base_name = "Song - Artist.mp3"
        base_path = os.path.join(str(playlist_folder), base_name)
        with scraper._filename_lock:
            scraper._in_flight_files.add(base_path)

        # Worker B arrives — should get a suffixed filename
        scraper._download_one_track(tracks[1], str(playlist_folder), None)

        assert len(claimed_paths) == 1
        claimed = claimed_paths[0]
        assert claimed != base_path
        assert "[id_b]" in claimed

    def test_parallel_mode_emits_song_meta_per_track(self, tmp_path):
        """In parallel mode, song_meta IS emitted per track.

        Previously suppressed to avoid label flicker, but an empty Song
        Information panel looked like a bug. Now the label races between
        workers and shows whichever track most recently started, which is
        fine. add_song_meta still fires for ID3 tag writing.
        """
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        for sig in (
            "song_meta",
            "add_song_meta",
            "dlprogress_signal",
            "Resetprogress_signal",
            "PlaylistID",
            "song_Album",
            "PlaylistCompleted",
            "error_signal",
            "count_updated",
        ):
            setattr(scraper, sig, MagicMock())

        tracks = [self._make_track(f"id{i}", f"Song {i}") for i in range(4)]
        scraper.download_track_audio = lambda _q, d, **_kw: open(d, "wb").close() or (d, False)

        mock_api = MagicMock()
        meta = MagicMock()
        meta.name = "T"
        meta.owner = "O"
        meta.cover_url = None
        mock_api.get_playlist_metadata.return_value = meta
        mock_api.iter_playlist_tracks.return_value = iter(tracks)
        scraper.ensure_spotifydown_api = MagicMock(return_value=mock_api)
        scraper.format_playlist_name = lambda _m: "T"

        scraper.scrape_playlist("https://open.spotify.com/playlist/abc", str(tmp_path))

        # 4 tracks >= 3 threshold, parallel mode. song_meta now emits per
        # track so the preview panel reflects active work. add_song_meta
        # still fires per track for ID3 tag writing.
        assert scraper.song_meta.emit.call_count == 4
        assert scraper.add_song_meta.emit.call_count == 4

    def test_parallel_mode_aggregate_progress_emits(self, tmp_path):
        """In parallel mode, dlprogress emits aggregate completion, not per-byte.

        Audit fix H1: progress bar can't jitter because we emit counter/total
        once per completion instead of per-worker per-byte.
        """
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        for sig in (
            "song_meta",
            "add_song_meta",
            "dlprogress_signal",
            "Resetprogress_signal",
            "PlaylistID",
            "song_Album",
            "PlaylistCompleted",
            "error_signal",
            "count_updated",
        ):
            setattr(scraper, sig, MagicMock())

        tracks = [self._make_track(f"id{i}", f"Song {i}") for i in range(4)]
        scraper.download_track_audio = lambda _q, d, **_kw: open(d, "wb").close() or (d, False)

        mock_api = MagicMock()
        meta = MagicMock()
        meta.name = "T"
        meta.owner = "O"
        meta.cover_url = None
        mock_api.get_playlist_metadata.return_value = meta
        mock_api.iter_playlist_tracks.return_value = iter(tracks)
        scraper.ensure_spotifydown_api = MagicMock(return_value=mock_api)
        scraper.format_playlist_name = lambda _m: "T"

        scraper.scrape_playlist("https://open.spotify.com/playlist/abc", str(tmp_path))

        # Each completion should emit exactly one aggregate progress value.
        # Monotonically non-decreasing. Last value should be 100.
        emitted_values = [c.args[0] for c in scraper.dlprogress_signal.emit.call_args_list]
        assert len(emitted_values) == 4  # one per track
        assert emitted_values == sorted(emitted_values)  # monotonic
        assert emitted_values[-1] == 100


class TestMainWindow:
    """Tests for MainWindow class (limited - requires QApplication)."""

    def test_get_default_download_path_returns_string(self):
        """Test default download path is a string."""
        # This test is limited since MainWindow requires QApplication
        # Just test the logic would produce a valid path format
        home = os.path.expanduser("~")
        expected_contains = os.path.join(home, "Music", "Setlist")
        # The path should be under user's home
        assert home in expected_contains


class TestCoverEnrichment:
    """Tests for per-track cover art enrichment (fixes #31).

    Playlist embed trackList does not include per-track cover urls; without
    enrichment every track would fall back to default_cover_url (the playlist
    cover) and end up with the same artwork. These tests lock in that the
    worker calls get_track when cover_url is missing, uses the enriched
    release_date too, and falls back gracefully on enrichment failure.
    """

    def _track(self, tid="id1", cover=None, release_date=None):
        from spotifydown_api import TrackInfo

        return TrackInfo(
            id=tid,
            title="Song",
            artists="Artist",
            album=None,
            release_date=release_date,
            cover_url=cover,
            duration_ms=None,
            preview_url=None,
            raw={},
        )

    def _stub_scraper_signals(self, scraper):
        for sig in (
            "song_meta",
            "add_song_meta",
            "dlprogress_signal",
            "Resetprogress_signal",
            "PlaylistID",
            "song_Album",
            "PlaylistCompleted",
            "error_signal",
            "count_updated",
        ):
            setattr(scraper, sig, MagicMock())

    def test_missing_cover_triggers_enrichment(self, tmp_path):
        """Track with cover_url=None causes a get_track call for the real cover."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        self._stub_scraper_signals(scraper)
        scraper.download_track_audio = lambda _q, d, **_kw: open(d, "wb").close() or (d, False)

        mock_api = MagicMock()
        enriched = self._track(cover="https://real/cover.jpg", release_date="2024-06-01")
        mock_api.get_track.return_value = enriched
        scraper.spotifydown_api = mock_api

        captured = []
        scraper.add_song_meta.emit.side_effect = lambda meta: captured.append(meta)

        scraper._download_one_track(
            self._track(cover=None), str(tmp_path), "fallback-playlist-cover"
        )
        assert captured[0]["cover"] == "https://real/cover.jpg"
        assert captured[0]["releaseDate"] == "2024-06-01"
        mock_api.get_track.assert_called_once_with("id1")

    def test_existing_cover_skips_enrichment(self, tmp_path):
        """Track that already has cover_url (e.g. spclient fallback path) does
        not trigger a second network call."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        self._stub_scraper_signals(scraper)
        scraper.download_track_audio = lambda _q, d, **_kw: open(d, "wb").close() or (d, False)

        mock_api = MagicMock()
        scraper.spotifydown_api = mock_api

        scraper._download_one_track(
            self._track(cover="https://already/present.jpg"),
            str(tmp_path),
            "fallback",
        )
        mock_api.get_track.assert_not_called()

    def test_enrichment_failure_falls_back_to_playlist_cover(self, tmp_path):
        """If get_track raises, the worker silently falls back to default_cover_url."""
        from Spotify_Downloader import MusicScraper
        from spotifydown_api import SpotifyDownAPIError

        scraper = MusicScraper()
        self._stub_scraper_signals(scraper)
        scraper.download_track_audio = lambda _q, d, **_kw: open(d, "wb").close() or (d, False)

        mock_api = MagicMock()
        mock_api.get_track.side_effect = SpotifyDownAPIError("offline")
        scraper.spotifydown_api = mock_api

        captured = []
        scraper.add_song_meta.emit.side_effect = lambda meta: captured.append(meta)

        scraper._download_one_track(
            self._track(cover=None), str(tmp_path), "playlist-cover-fallback"
        )
        assert captured[0]["cover"] == "playlist-cover-fallback"

    def test_no_enrichment_when_cancelled(self, tmp_path):
        """Cancel set before worker runs skips enrichment + download entirely."""
        from Spotify_Downloader import MusicScraper

        cancel = threading.Event()
        scraper = MusicScraper(cancel_event=cancel)
        self._stub_scraper_signals(scraper)
        scraper.download_track_audio = MagicMock()

        mock_api = MagicMock()
        scraper.spotifydown_api = mock_api

        cancel.set()
        result = scraper._download_one_track(self._track(cover=None), str(tmp_path), "fallback")
        assert result is None
        mock_api.get_track.assert_not_called()
        scraper.download_track_audio.assert_not_called()

    def test_enrichment_preserves_existing_release_date(self, tmp_path):
        """If the track already has release_date, enrichment shouldn't clobber it."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        self._stub_scraper_signals(scraper)
        scraper.download_track_audio = lambda _q, d, **_kw: open(d, "wb").close() or (d, False)

        mock_api = MagicMock()
        enriched = self._track(cover="https://real/cover.jpg", release_date="2020-01-01")
        mock_api.get_track.return_value = enriched
        scraper.spotifydown_api = mock_api

        captured = []
        scraper.add_song_meta.emit.side_effect = lambda meta: captured.append(meta)

        original = self._track(cover=None, release_date="2024-12-31")
        scraper._download_one_track(original, str(tmp_path), "fallback")
        assert captured[0]["releaseDate"] == "2024-12-31"


class TestTrackNumberMetadata:
    """Tests that track_num flows into song_meta and gets written to ID3."""

    def _track(self, tid="t1"):
        from spotifydown_api import TrackInfo

        return TrackInfo(
            id=tid,
            title="Title",
            artists="Artist",
            album="Album",
            release_date="2024-01-01",
            cover_url="https://x/y.jpg",
            duration_ms=None,
            preview_url=None,
            raw={},
        )

    def test_track_num_in_song_meta(self, tmp_path):
        """_download_one_track threads track_num into the emitted song_meta."""
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        for sig in (
            "song_meta",
            "add_song_meta",
            "dlprogress_signal",
            "Resetprogress_signal",
            "PlaylistID",
            "song_Album",
            "PlaylistCompleted",
            "error_signal",
            "count_updated",
        ):
            setattr(scraper, sig, MagicMock())
        scraper.download_track_audio = lambda _q, d, **_kw: open(d, "wb").close() or (d, False)

        captured = []
        scraper.add_song_meta.emit.side_effect = lambda meta: captured.append(meta)

        scraper._download_one_track(self._track(), str(tmp_path), "", track_num=7)
        assert captured[0]["trackNumber"] == 7

    def test_writingmetatagsthread_writes_tracknumber(self, tmp_path, mocker):
        """WritingMetaTagsThread writes trackNumber to ID3 when present."""
        from Spotify_Downloader import WritingMetaTagsThread

        # Mock EasyID3/ID3 so we can inspect what was written without a real mp3
        mock_easy = mocker.MagicMock()
        mocker.patch("Spotify_Downloader.EasyID3", return_value=mock_easy)
        mocker.patch(
            "Spotify_Downloader.requests.get",
            return_value=mocker.MagicMock(status_code=200, content=b""),
        )

        tags = {
            "title": "T",
            "artists": "A",
            "album": "Al",
            "releaseDate": "2024-05-01",
            "trackNumber": 5,
            "cover": "",
        }
        thread = WritingMetaTagsThread(tags, str(tmp_path / "fake.mp3"))
        thread.tags_success = MagicMock()
        thread.run()
        mock_easy.__setitem__.assert_any_call("tracknumber", "5")

    def test_writingmetatagsthread_skips_tracknumber_when_zero(self, tmp_path, mocker):
        """trackNumber=0 (unset) should not write a tag."""
        from Spotify_Downloader import WritingMetaTagsThread

        mock_easy = mocker.MagicMock()
        mocker.patch("Spotify_Downloader.EasyID3", return_value=mock_easy)
        mocker.patch(
            "Spotify_Downloader.requests.get",
            return_value=mocker.MagicMock(status_code=200, content=b""),
        )

        tags = {
            "title": "T",
            "artists": "A",
            "album": "",
            "releaseDate": "",
            "trackNumber": 0,
            "cover": "",
        }
        thread = WritingMetaTagsThread(tags, str(tmp_path / "fake.mp3"))
        thread.tags_success = MagicMock()
        thread.run()
        # Confirm tracknumber was NOT written
        calls = [c for c in mock_easy.__setitem__.call_args_list if c.args[0] == "tracknumber"]
        assert len(calls) == 0

    def test_writingmetatagsthread_writes_discnumber(self, tmp_path, mocker):
        """discNumber is written to ID3 (parity with the librespot .ogg path)."""
        from Spotify_Downloader import WritingMetaTagsThread

        mock_easy = mocker.MagicMock()
        mocker.patch("Spotify_Downloader.EasyID3", return_value=mock_easy)
        mocker.patch(
            "Spotify_Downloader.requests.get",
            return_value=mocker.MagicMock(status_code=200, content=b""),
        )
        tags = {"title": "T", "artists": "A", "album": "Al", "trackNumber": 2, "discNumber": 1}
        thread = WritingMetaTagsThread(tags, str(tmp_path / "fake.mp3"))
        thread.tags_success = MagicMock()
        thread.run()
        mock_easy.__setitem__.assert_any_call("discnumber", "1")

    def test_writingmetatagsthread_skips_empty_album(self, tmp_path, mocker):
        """An empty album must NOT be written (so a re-tag can't erase a real album)."""
        from Spotify_Downloader import WritingMetaTagsThread

        mock_easy = mocker.MagicMock()
        mocker.patch("Spotify_Downloader.EasyID3", return_value=mock_easy)
        mocker.patch(
            "Spotify_Downloader.requests.get",
            return_value=mocker.MagicMock(status_code=200, content=b""),
        )
        tags = {"title": "T", "artists": "A", "album": "", "trackNumber": 1}
        thread = WritingMetaTagsThread(tags, str(tmp_path / "fake.mp3"))
        thread.tags_success = MagicMock()
        thread.run()
        album_calls = [c for c in mock_easy.__setitem__.call_args_list if c.args[0] == "album"]
        assert album_calls == []


class TestFilenameOrder:
    """Tests for artist_first filename ordering."""

    def test_track_first_when_artist_first_false(self):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper(artist_first=False)
        assert scraper._format_track_filename("Song", "Artist") == "Song - Artist.mp3"

    def test_artist_first_when_artist_first_true(self):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper(artist_first=True)
        assert scraper._format_track_filename("Song", "Artist") == "Artist - Song.mp3"

    def test_default_is_track_first(self):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        assert scraper._format_track_filename("Song", "Artist") == "Song - Artist.mp3"

    def test_collision_suffix_artist_first(self):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper(artist_first=True)
        assert (
            scraper._format_track_filename("Song", "Artist", suffix=" [abc123]")
            == "Artist - Song [abc123].mp3"
        )

    def test_collision_suffix_track_first(self):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper(artist_first=False)
        assert (
            scraper._format_track_filename("Song", "Artist", suffix=" [abc123]")
            == "Song - Artist [abc123].mp3"
        )


class _ConfigStub:
    """Minimal stand-in for MainWindow so apply_* can be unit-tested without
    building the whole Qt UI (the methods only touch _config / download_path)."""

    def __init__(self, config=None):
        self._config = dict(config or {})
        self.download_path = None
        self._download_path_set = False


class TestApplySettings:
    """The Settings pane persists each change immediately via apply_setting /
    apply_download_path — every key is written (incl. future keys), not a
    hand-maintained allowlist (regression: artist_first / max_extended_minutes
    were once silently dropped on save)."""

    def test_apply_setting_writes_and_saves(self, mocker):
        from Spotify_Downloader import MainWindow

        save = mocker.patch("Spotify_Downloader.save_config")
        stub = _ConfigStub({"format": "mp3", "artist_first": False})
        MainWindow.apply_setting(stub, "artist_first", True)
        MainWindow.apply_setting(stub, "format", "flac")
        assert stub._config["artist_first"] is True
        assert stub._config["format"] == "flac"
        assert save.call_count == 2

    def test_apply_setting_persists_unknown_future_key(self, mocker):
        from Spotify_Downloader import MainWindow

        save = mocker.patch("Spotify_Downloader.save_config")
        stub = _ConfigStub({"a": 1})
        MainWindow.apply_setting(stub, "some_future_setting", "x")
        assert stub._config["some_future_setting"] == "x"
        save.assert_called_once_with(stub._config)

    def test_apply_download_path(self, mocker):
        from Spotify_Downloader import MainWindow

        save = mocker.patch("Spotify_Downloader.save_config")
        stub = _ConfigStub()
        MainWindow.apply_download_path(stub, "/music/Setlist")
        assert stub.download_path == "/music/Setlist"
        assert stub._download_path_set is True
        assert stub._config["download_path"] == "/music/Setlist"
        save.assert_called_once_with(stub._config)


class TestMaxTrackSize:
    """Tests for the optional max file-size cap (avoids grabbing huge uploads)."""

    @staticmethod
    def _scraper(max_track_mb=0):
        from Spotify_Downloader import MusicScraper

        return MusicScraper(max_track_mb=max_track_mb)

    def test_max_track_bytes_from_config(self):
        """max_track_mb maps to max_track_bytes (MB -> bytes); 0 = no limit."""
        assert self._scraper(max_track_mb=50).max_track_bytes == 50 * 1024 * 1024
        assert self._scraper().max_track_bytes == 0

    def test_oversized_download_is_discarded(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper(max_track_mb=10)  # 10 MB cap
        dest = str(tmp_path / "Song.mp3")
        url = "https://www.youtube.com/watch?v=huge"
        raised = None
        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
            patch("os.path.exists", side_effect=lambda p: p == dest),
            patch("os.path.getsize", return_value=20 * 1024 * 1024),  # 20 MB > cap
            patch("os.remove") as mock_remove,
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            try:
                scraper.download_track_audio("ytsearch1:Song Artist audio", dest)
            except RuntimeError as exc:
                raised = str(exc)
        assert raised is not None and "size limit" in raised
        assert mock_remove.called  # the oversized file was deleted

    def test_within_limit_download_succeeds(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper(max_track_mb=10)
        dest = str(tmp_path / "Song.mp3")
        url = "https://www.youtube.com/watch?v=ok"
        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
            patch("os.path.exists", side_effect=lambda p: p == dest),
            patch("os.path.getsize", return_value=5 * 1024 * 1024),  # 5 MB < cap
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            result, _ = scraper.download_track_audio("ytsearch1:Song Artist audio", dest)
        assert result == dest

    def test_no_limit_keeps_large_file(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper(max_track_mb=0)  # no limit
        dest = str(tmp_path / "Song.mp3")
        url = "https://www.youtube.com/watch?v=big"
        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
            patch("os.path.exists", side_effect=lambda p: p == dest),
            patch("os.path.getsize", return_value=500 * 1024 * 1024),  # 500 MB
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            result, _ = scraper.download_track_audio("ytsearch1:Song Artist audio", dest)
        assert result == dest  # no cap -> kept regardless of size


def _valid_cookie_file(tmp_path):
    p = tmp_path / "cookies.txt"
    p.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n")
    return str(p)


def _download_track_with_stub_match(scraper, dest, mock_ydl, exists=True):
    url = "https://www.youtube.com/watch?v=abc"
    patches = [
        patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
        patch.object(scraper, "_select_youtube_match", return_value=url),
        patch("Spotify_Downloader.YoutubeDL", mock_ydl),
    ]
    if exists:
        patches.append(patch("os.path.exists", return_value=True))
    ctx = contextlib.ExitStack()
    for p in patches:
        ctx.enter_context(p)
    mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
    mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
    with ctx, contextlib.suppress(Exception):
        scraper.download_track_audio("test query", dest)
    return mock_ydl.call_args[0][0] if mock_ydl.call_args else {}


class TestYoutubePremiumCookies:
    """Tests for optional YouTube Premium cookies on download-only yt-dlp calls."""

    def test_no_cookie_keys_when_unset(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        dest = str(tmp_path / "track.mp3")
        mock_ydl = MagicMock()
        opts = _download_track_with_stub_match(scraper, dest, mock_ydl)
        assert "cookiefile" not in opts
        assert "extractor_args" not in opts

    def test_cookiefile_and_web_music_client_when_set(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        cookie_path = _valid_cookie_file(tmp_path)
        with open(cookie_path, encoding="utf-8") as src:
            source_content = src.read()
        scraper = MusicScraper(youtube_cookies_file=cookie_path)
        dest = str(tmp_path / "track.mp3")
        all_opts = []
        snap_content = []

        def ydl_factory(opts):
            all_opts.append(opts)
            if opts.get("cookiefile"):
                with open(opts["cookiefile"], encoding="utf-8") as snap:
                    snap_content.append(snap.read())
            mock = MagicMock()
            mock.__enter__ = MagicMock(return_value=mock)
            mock.__exit__ = MagicMock(return_value=False)
            return mock

        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value="https://youtu.be/x"),
            patch("Spotify_Downloader.YoutubeDL", side_effect=ydl_factory),
            patch("os.path.exists", return_value=True),
        ):
            scraper.download_track_audio("test query", dest)
        download_opts = [o for o in all_opts if not o.get("extract_flat")]
        opts = download_opts[0]
        assert opts["extractor_args"] == {"youtube": {"player_client": ["web_music", "default"]}}
        assert opts["cookiefile"] != cookie_path
        assert snap_content[0] == source_content

    def test_temp_cookie_snapshot_cleaned_up(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        cookie_path = _valid_cookie_file(tmp_path)
        scraper = MusicScraper(youtube_cookies_file=cookie_path)
        dest = str(tmp_path / "track.mp3")
        mock_ydl = MagicMock()
        opts = _download_track_with_stub_match(scraper, dest, mock_ydl)
        assert not os.path.exists(opts["cookiefile"])

    def test_snapshot_failure_falls_back_anonymous(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        cookie_path = _valid_cookie_file(tmp_path)
        scraper = MusicScraper(youtube_cookies_file=cookie_path)
        messages = []
        scraper.error_signal.connect(messages.append)
        dest = str(tmp_path / "track.mp3")
        url = "https://www.youtube.com/watch?v=abc"
        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader._snapshot_cookiefile", side_effect=OSError("gone")),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
            patch("os.path.exists", return_value=True),
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            result, _ = scraper.download_track_audio("test query", dest)
        opts = mock_ydl.call_args[0][0]
        assert result == dest
        assert "cookiefile" not in opts
        assert "extractor_args" not in opts
        assert len([m for m in messages if "ignored" in m]) == 1

    def test_invalid_cookie_file_downloads_anonymously(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        bad = tmp_path / "bad.txt"
        bad.write_text('{"json": "garbage"}')
        scraper = MusicScraper(youtube_cookies_file=str(bad))
        messages = []
        scraper.error_signal.connect(messages.append)
        dest = str(tmp_path / "track.mp3")
        mock_ydl = MagicMock()
        opts = _download_track_with_stub_match(scraper, dest, mock_ydl)
        assert "cookiefile" not in opts
        assert "extractor_args" not in opts
        assert len([m for m in messages if "ignored" in m]) == 1

    def test_missing_cookie_file_downloads_anonymously(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper(youtube_cookies_file=str(tmp_path / "missing.txt"))
        dest = str(tmp_path / "track.mp3")
        mock_ydl = MagicMock()
        opts = _download_track_with_stub_match(scraper, dest, mock_ydl)
        assert "cookiefile" not in opts
        assert "extractor_args" not in opts

    def test_search_opts_get_cookiefile_when_set(self, tmp_path):
        # Regression guard for the bot-wall-on-search bug: when a cookie file is
        # configured, the SEARCH (_select_youtube_match) request must carry it, using
        # the SAME snapshot as the download. player_client extractor_args stay
        # download-only (inert under extract_flat; web_music could reroute the search).
        from Spotify_Downloader import MusicScraper

        cookie_path = _valid_cookie_file(tmp_path)
        scraper = MusicScraper(youtube_cookies_file=cookie_path)
        dest = str(tmp_path / "track.mp3")
        all_opts = []

        def ydl_factory(opts):
            all_opts.append(opts)
            mock = MagicMock()
            mock.__enter__ = MagicMock(return_value=mock)
            mock.__exit__ = MagicMock(return_value=False)
            if opts.get("extract_flat"):
                mock.extract_info = MagicMock(
                    return_value={"entries": [{"id": "abc", "title": "Song", "duration": 180}]}
                )
            else:
                mock.extract_info = MagicMock(return_value={"format_id": "251"})
            return mock

        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch("Spotify_Downloader.YoutubeDL", side_effect=ydl_factory),
            patch("os.path.exists", return_value=True),
        ):
            scraper.download_track_audio("Song Artist", dest, expected_duration_s=180)

        search_opts = [o for o in all_opts if o.get("extract_flat")]
        download_opts = [o for o in all_opts if not o.get("extract_flat")]
        assert search_opts
        assert download_opts
        for opts in search_opts:
            assert opts["cookiefile"] != cookie_path  # private snapshot, not the user's file
            assert "extractor_args" not in opts  # player_client is download-only
        for opts in download_opts:
            assert opts["cookiefile"] != cookie_path
            assert opts["extractor_args"] == {
                "youtube": {"player_client": ["web_music", "default"]}
            }
        # Same snapshot path shared by search and download.
        assert search_opts[0]["cookiefile"] == download_opts[0]["cookiefile"]

    def test_search_and_download_share_one_snapshot(self, tmp_path):
        # The cookie file is snapshotted exactly ONCE per download_track_audio call —
        # not once for the search plus once for the download.
        from Spotify_Downloader import MusicScraper
        from Spotify_Downloader import _snapshot_cookiefile as real_snapshot

        cookie_path = _valid_cookie_file(tmp_path)
        scraper = MusicScraper(youtube_cookies_file=cookie_path)
        dest = str(tmp_path / "track.mp3")
        snapshots = []

        def counting_snapshot(src):
            path = real_snapshot(src)
            snapshots.append(path)
            return path

        def ydl_factory(opts):
            mock = MagicMock()
            mock.__enter__ = MagicMock(return_value=mock)
            mock.__exit__ = MagicMock(return_value=False)
            if opts.get("extract_flat"):
                mock.extract_info = MagicMock(
                    return_value={"entries": [{"id": "abc", "title": "Song", "duration": 180}]}
                )
            else:
                mock.extract_info = MagicMock(return_value={"format_id": "251"})
            return mock

        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch("Spotify_Downloader._snapshot_cookiefile", side_effect=counting_snapshot),
            patch("Spotify_Downloader.YoutubeDL", side_effect=ydl_factory),
            patch("os.path.exists", return_value=True),
        ):
            scraper.download_track_audio("Song Artist", dest, expected_duration_s=180)

        assert len(snapshots) == 1

    def test_snapshot_taken_once_across_retries(self, tmp_path):
        # A gate retry (two outer attempts) must NOT re-snapshot: the snapshot is lifted
        # out of the retry loop and reused by every attempt's search + download.
        from Spotify_Downloader import MusicScraper
        from Spotify_Downloader import _snapshot_cookiefile as real_snapshot

        cookie_path = _valid_cookie_file(tmp_path)
        scraper = MusicScraper(youtube_cookies_file=cookie_path)
        scraper._yt_rate_gate.COOLDOWN_S = 0.05
        scraper._yt_rate_gate.SLICE_S = 0.01
        dest = str(tmp_path / "Song.mp3")
        snapshots = []
        search_attempts = 0
        download_attempts = 0

        def counting_snapshot(src):
            path = real_snapshot(src)
            snapshots.append(path)
            return path

        class FakeYdl:
            def __init__(self, opts):
                self._opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, _url, download=False):
                nonlocal search_attempts, download_attempts
                if not download:
                    search_attempts += 1
                    logger = self._opts["logger"]  # strict: must be threaded in
                    if search_attempts == 1:
                        logger.error(
                            "This content isn't available, try again later — rate-limited by YouTube"
                        )
                        return {"entries": []}
                    return {"entries": [{"id": "abc", "title": "Song Artist", "duration": 200}]}
                download_attempts += 1
                return None

        def exists(path):
            return path == dest and download_attempts >= 1

        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch("Spotify_Downloader._snapshot_cookiefile", side_effect=counting_snapshot),
            patch.object(scraper, "_build_youtube_download_plan", return_value=[("q", False)]),
            patch("Spotify_Downloader.YoutubeDL", FakeYdl),
            patch("os.path.exists", side_effect=exists),
        ):
            result, _ = scraper.download_track_audio("ytsearch1:Song Artist audio", dest)
        assert result == dest
        assert search_attempts == 2  # the retry loop ran
        assert len(snapshots) == 1  # but the cookie file was snapshotted only once

    def test_search_opts_anonymous_when_cookies_unset(self, tmp_path):
        # No cookies configured -> the search opts must be byte-for-byte anonymous,
        # exactly as before this fix (no behavior change for the default path).
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        dest = str(tmp_path / "track.mp3")
        all_opts = []

        def ydl_factory(opts):
            all_opts.append(opts)
            mock = MagicMock()
            mock.__enter__ = MagicMock(return_value=mock)
            mock.__exit__ = MagicMock(return_value=False)
            if opts.get("extract_flat"):
                mock.extract_info = MagicMock(
                    return_value={"entries": [{"id": "abc", "title": "Song", "duration": 180}]}
                )
            else:
                mock.extract_info = MagicMock(return_value={"format_id": "251"})
            return mock

        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch("Spotify_Downloader.YoutubeDL", side_effect=ydl_factory),
            patch("os.path.exists", return_value=True),
        ):
            scraper.download_track_audio("Song Artist", dest, expected_duration_s=180)

        search_opts = [o for o in all_opts if o.get("extract_flat")]
        assert search_opts
        for opts in search_opts:
            assert "cookiefile" not in opts
            assert "extractor_args" not in opts

    def test_invalid_cookie_search_and_download_anonymous(self, tmp_path):
        # An unrecognized cookie file warns once and proceeds anonymously on BOTH legs.
        from Spotify_Downloader import MusicScraper

        bad = tmp_path / "bad.txt"
        bad.write_text('{"json": "garbage"}')
        scraper = MusicScraper(youtube_cookies_file=str(bad))
        messages = []
        scraper.error_signal.connect(messages.append)
        dest = str(tmp_path / "track.mp3")
        all_opts = []

        def ydl_factory(opts):
            all_opts.append(opts)
            mock = MagicMock()
            mock.__enter__ = MagicMock(return_value=mock)
            mock.__exit__ = MagicMock(return_value=False)
            if opts.get("extract_flat"):
                mock.extract_info = MagicMock(
                    return_value={"entries": [{"id": "abc", "title": "Song", "duration": 180}]}
                )
            else:
                mock.extract_info = MagicMock(return_value={"format_id": "251"})
            return mock

        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch("Spotify_Downloader.YoutubeDL", side_effect=ydl_factory),
            patch("os.path.exists", return_value=True),
        ):
            scraper.download_track_audio("Song Artist", dest, expected_duration_s=180)

        search_opts = [o for o in all_opts if o.get("extract_flat")]
        download_opts = [o for o in all_opts if not o.get("extract_flat")]
        assert search_opts
        assert download_opts
        for opts in search_opts + download_opts:
            assert "cookiefile" not in opts
            assert "extractor_args" not in opts
        assert len([m for m in messages if "ignored" in m]) == 1

    def test_premium_format_reported_once(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        cookie_path = _valid_cookie_file(tmp_path)
        scraper = MusicScraper(youtube_cookies_file=cookie_path)
        messages = []
        scraper.error_signal.connect(messages.append)
        dest = str(tmp_path / "track.mp3")
        url = "https://www.youtube.com/watch?v=abc"
        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
            patch("os.path.exists", return_value=True),
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info = MagicMock(return_value={"format_id": "774"})
            scraper.download_track_audio("test query", dest)
            scraper.download_track_audio("test query", dest)
        premium_msgs = [m for m in messages if "Premium quality active" in m]
        assert len(premium_msgs) == 1

    def test_non_premium_format_warns_once(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        cookie_path = _valid_cookie_file(tmp_path)
        scraper = MusicScraper(youtube_cookies_file=cookie_path)
        messages = []
        scraper.error_signal.connect(messages.append)
        dest = str(tmp_path / "track.mp3")
        url = "https://www.youtube.com/watch?v=abc"
        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
            patch("os.path.exists", return_value=True),
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info = MagicMock(return_value={"format_id": "251"})
            scraper.download_track_audio("test query", dest)
            scraper.download_track_audio("test query", dest)
        warn_msgs = [m for m in messages if "Premium formats not served" in m]
        assert len(warn_msgs) == 1


class TestValidateCookiesFile:
    def test_accepts_netscape_magic(self, tmp_path):
        from Spotify_Downloader import validate_cookies_file

        p = tmp_path / "c.txt"
        p.write_text("# Netscape HTTP Cookie File\n")
        assert validate_cookies_file(str(p)) is None

    def test_accepts_headerless_tab_lines(self, tmp_path):
        from Spotify_Downloader import validate_cookies_file

        p = tmp_path / "c.txt"
        p.write_text(".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n")
        assert validate_cookies_file(str(p)) is None

    def test_rejects_garbage_file(self, tmp_path):
        from Spotify_Downloader import validate_cookies_file

        p = tmp_path / "c.txt"
        p.write_text("hello world\nnothing here")
        reason = validate_cookies_file(str(p))
        assert reason is not None
        assert "not a recognized" in reason

    def test_rejects_missing_path(self, tmp_path):
        from Spotify_Downloader import validate_cookies_file

        assert validate_cookies_file(str(tmp_path / "nope.txt")) == "file not found"

    def test_rejects_empty_file(self, tmp_path):
        from Spotify_Downloader import validate_cookies_file

        p = tmp_path / "c.txt"
        p.write_text("")
        assert validate_cookies_file(str(p)) == "file is empty"


class TestCookieFormatConversion:
    def test_netscape_passthrough_is_byte_identical(self, tmp_path):
        from Spotify_Downloader import _snapshot_cookiefile

        src = tmp_path / "cookies.txt"
        content = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n"
        src.write_text(content)
        snap = _snapshot_cookiefile(str(src))
        try:
            with open(snap, encoding="utf-8") as f:
                assert f.read() == content
        finally:
            os.remove(snap)

    def test_netscape_crlf_snapshot_byte_identical(self, tmp_path):
        from Spotify_Downloader import _snapshot_cookiefile

        src = tmp_path / "cookies.txt"
        content = "# Netscape HTTP Cookie File\r\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tv"
        with open(src, "w", newline="") as f:
            f.write(content)
        snap = _snapshot_cookiefile(str(src))
        try:
            with open(src, "rb") as f:
                source_bytes = f.read()
            with open(snap, "rb") as f:
                snap_bytes = f.read()
            assert snap_bytes == source_bytes
        finally:
            os.remove(snap)

    def test_json_export_converts(self, tmp_path):
        import json
        import time

        from Spotify_Downloader import _snapshot_cookiefile

        cookies = [
            {
                "name": "SID",
                "value": "abc123",
                "domain": ".youtube.com",
                "path": "/",
                "secure": True,
                "expirationDate": 2000000000.5,
            },
            {
                "name": "HSID",
                "value": "sessval",
                "session": True,
            },
        ]
        src = tmp_path / "cookies.json"
        src.write_text(json.dumps(cookies))
        snap = _snapshot_cookiefile(str(src))
        try:
            with open(snap, encoding="utf-8") as f:
                lines = f.read().splitlines()
            assert lines[0] == "# Netscape HTTP Cookie File"
            assert len(lines) == 3
            sid_parts = lines[1].split("\t")
            assert sid_parts[0] == ".youtube.com"
            assert sid_parts[1] == "TRUE"
            assert sid_parts[2] == "/"
            assert sid_parts[3] == "TRUE"
            assert sid_parts[4] == "2000000000"
            assert sid_parts[5] == "SID"
            assert sid_parts[6] == "abc123"
            hsid_parts = lines[2].split("\t")
            assert hsid_parts[0] == ".youtube.com"
            assert hsid_parts[5] == "HSID"
            assert hsid_parts[6] == "sessval"
            assert int(hsid_parts[4]) > int(time.time())
        finally:
            os.remove(snap)

    def test_json_cookies_wrapper_accepted(self, tmp_path):
        import json

        from Spotify_Downloader import validate_cookies_file

        p = tmp_path / "wrapped.json"
        p.write_text(json.dumps({"cookies": [{"name": "SID", "value": "x"}]}))
        assert validate_cookies_file(str(p)) is None

    def test_json_without_name_value_rejected(self, tmp_path):
        from Spotify_Downloader import validate_cookies_file

        p = tmp_path / "bad.json"
        p.write_text('[{"foo": 1}]')
        reason = validate_cookies_file(str(p))
        assert reason is not None
        assert "not a recognized" in reason

    def test_header_string_converts(self, tmp_path):
        from Spotify_Downloader import _snapshot_cookiefile

        src = tmp_path / "header.txt"
        src.write_text("SID=abc; __Secure-1PSID=def")
        snap = _snapshot_cookiefile(str(src))
        try:
            with open(snap, encoding="utf-8") as f:
                lines = f.read().splitlines()
            assert lines[0] == "# Netscape HTTP Cookie File"
            assert len(lines) == 3
            sid_parts = lines[1].split("\t")
            assert sid_parts[0] == ".youtube.com"
            assert sid_parts[1] == "TRUE"
            assert sid_parts[2] == "/"
            assert sid_parts[3] == "TRUE"
            assert sid_parts[5] == "SID"
            assert sid_parts[6] == "abc"
            psid_parts = lines[2].split("\t")
            assert psid_parts[5] == "__Secure-1PSID"
            assert psid_parts[6] == "def"
        finally:
            os.remove(snap)

    def test_header_value_with_equals_preserved(self, tmp_path):
        from Spotify_Downloader import _snapshot_cookiefile

        src = tmp_path / "header.txt"
        src.write_text("LOGIN_INFO=a=b=c")
        snap = _snapshot_cookiefile(str(src))
        try:
            with open(snap, encoding="utf-8") as f:
                parts = f.read().splitlines()[1].split("\t")
            assert parts[5] == "LOGIN_INFO"
            assert parts[6] == "a=b=c"
        finally:
            os.remove(snap)

    def test_validate_accepts_all_three_formats(self, tmp_path):
        import json

        from Spotify_Downloader import validate_cookies_file

        netscape = tmp_path / "n.txt"
        netscape.write_text("# Netscape HTTP Cookie File\n")
        json_f = tmp_path / "j.json"
        json_f.write_text(json.dumps([{"name": "SID", "value": "v"}]))
        header = tmp_path / "h.txt"
        header.write_text("SID=abc")
        assert validate_cookies_file(str(netscape)) is None
        assert validate_cookies_file(str(json_f)) is None
        assert validate_cookies_file(str(header)) is None


class TestConfigYoutubeCookies:
    def test_default_is_empty(self, tmp_path, monkeypatch):
        from Spotify_Downloader import load_config

        monkeypatch.setattr(
            "Spotify_Downloader._config_path", lambda: str(tmp_path / "config.json")
        )
        assert load_config()["youtube_cookies_file"] == ""

    def test_round_trip_persists_path(self, tmp_path, monkeypatch):
        from Spotify_Downloader import load_config, save_config

        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr("Spotify_Downloader._config_path", lambda: str(cfg_path))
        path = str(tmp_path / "cookies.txt")
        config = load_config()
        config["youtube_cookies_file"] = path
        save_config(config)
        assert load_config()["youtube_cookies_file"] == path

    def test_non_string_coerced_to_empty(self, tmp_path, monkeypatch):
        import json

        from Spotify_Downloader import load_config

        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"youtube_cookies_file": 123}))
        monkeypatch.setattr("Spotify_Downloader._config_path", lambda: str(cfg))
        assert load_config()["youtube_cookies_file"] == ""


class TestFallbackOrderConfig:
    def test_fresh_defaults_per_source(self, tmp_path, monkeypatch):
        from Spotify_Downloader import load_config

        monkeypatch.setattr(
            "Spotify_Downloader._config_path", lambda: str(tmp_path / "missing.json")
        )
        assert load_config()["fallback_order"] == []

    def test_old_keys_absent_after_save(self, tmp_path, monkeypatch):
        import json

        from Spotify_Downloader import load_config, save_config

        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr("Spotify_Downloader._config_path", lambda: str(cfg_path))
        cfg = load_config()
        save_config(cfg)
        saved = json.loads(cfg_path.read_text())
        assert "lossless_youtube_fallback" not in saved
        assert "librespot_extended_yt_fallback" not in saved
        assert "fallback_order" in saved


class TestFallbackChainIntegration:
    def test_lossless_chain_sets_served_by_and_via_youtube(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        from backends.librespot.errors import LibrespotNotPremium
        from lossless.errors import NotFoundOnServiceError
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper(
            download_source="lossless",
            fallback_order=["librespot", "youtube"],
        )
        lossless = scraper._backend._steps[0][1]
        lib = scraper._backend._steps[1][1]
        yt = scraper._backend._steps[2][1]

        class Q:
            def search_by_isrc(self, *a, **k):
                raise NotFoundOnServiceError("nope")

        lossless._isrc = SimpleNamespace(resolve=lambda _id: None)
        lossless._qobuz = Q()
        monkeypatch.setattr(
            lib, "_ensure_session", lambda: (_ for _ in ()).throw(LibrespotNotPremium("x"))
        )

        def fake_yt(**kw):
            dest = kw["destination"]
            with open(dest, "wb") as f:
                f.write(b"id3")
            return dest, "mp3", False

        monkeypatch.setattr(yt, "fetch", fake_yt)

        emitted = []
        scraper.add_song_meta.connect(lambda m: emitted.append(dict(m)))

        track = SimpleNamespace(
            id="tid",
            title="Song",
            artists="Artist",
            album="Alb",
            duration_ms=200000,
            cover_url=None,
            release_date="",
        )
        monkeypatch.setattr(scraper, "_enrich_song_meta", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            scraper, "_resolve_extended_output", lambda path, *_a, **_kw: (path, "Song")
        )
        monkeypatch.setattr(scraper, "_record_in_manifest", lambda *_a, **_kw: None)
        monkeypatch.setattr(scraper, "_finish_track_ui", lambda *_a, **_kw: None)

        scraper._download_one_track(track, str(tmp_path), None)
        assert emitted
        meta = emitted[0]
        assert meta["served_by"] == "youtube"
        assert meta["primary_source"] == "lossless"
        assert meta["via_youtube_fallback"] is True


class TestFallbackChainMetadataSeam:
    def _song_meta(self):
        return {
            "title": "Song",
            "artists": "Artist,\xa0Feat",
            "album": "",
            "releaseDate": "",
            "trackNumber": 7,
        }

    def test_chain_lossless_spotify_pref_uses_forwarded_isrc(self):
        from types import SimpleNamespace

        from backends.chain import FallbackChainBackend
        from Spotify_Downloader import MusicScraper

        lossless = SimpleNamespace(
            name="lossless",
            max_concurrency=4,
            _isrc=MagicMock(),
        )
        lossless._isrc.resolve_metadata.return_value = {
            "album": "Spotify Album",
            "artists": "Artist",
            "trackNumber": 1,
        }
        lossless.provider_metadata_for = lambda _tid: {
            "source": "qobuz",
            "meta": {"album": "Qobuz Album", "artists": "Artist", "trackNumber": 2},
        }
        scraper_ns = SimpleNamespace(error_signal=SimpleNamespace(emit=lambda *_a: None))
        chain = FallbackChainBackend("lossless", [("lossless", lossless)], scraper=scraper_ns)
        scraper = MusicScraper(download_source="lossless", flac_metadata_source="spotify")
        scraper._backend = chain
        meta = self._song_meta()
        scraper._enrich_song_meta(meta, "tid")
        assert meta["album"] == "Spotify Album"
        assert meta["trackNumber"] == 1

    def test_chain_librespot_served_uses_last_track_metadata(self):
        from types import SimpleNamespace

        from backends.chain import FallbackChainBackend
        from Spotify_Downloader import MusicScraper

        lib = SimpleNamespace(
            name="librespot",
            max_concurrency=1,
            last_track_metadata={"album": "From Librespot", "artists": "Artist"},
            provider_metadata_for=lambda _tid: None,
        )
        scraper_ns = SimpleNamespace(error_signal=SimpleNamespace(emit=lambda *_a: None))
        chain = FallbackChainBackend("librespot", [("librespot", lib)], scraper=scraper_ns)
        chain._tls.last_track_metadata = lib.last_track_metadata
        scraper = MusicScraper(download_source="librespot")
        scraper._backend = chain
        scraper._metadata_service = SimpleNamespace(
            get=lambda _id: (_ for _ in ()).throw(AssertionError("service must not be called"))
        )
        meta = self._song_meta()
        scraper._enrich_song_meta(meta, "tid")
        assert meta["album"] == "From Librespot"

    def test_chain_youtube_served_uses_metadata_service(self):
        from types import SimpleNamespace

        from backends.chain import FallbackChainBackend
        from Spotify_Downloader import MusicScraper

        yt = SimpleNamespace(
            name="youtube",
            max_concurrency=4,
            last_track_metadata=None,
            provider_metadata_for=lambda _tid: None,
        )
        scraper_ns = SimpleNamespace(error_signal=SimpleNamespace(emit=lambda *_a: None))
        chain = FallbackChainBackend("lossless", [("youtube", yt)], scraper=scraper_ns)
        chain._tls.served_by = "youtube"
        scraper = MusicScraper(download_source="lossless", fallback_order=["youtube"])
        scraper._backend = chain
        scraper._metadata_service = SimpleNamespace(
            get=lambda tid: (
                {"album": "YT Album", "artists": "YT Artist", "trackNumber": 1}
                if tid == "tid"
                else None
            )
        )
        meta = self._song_meta()
        scraper._enrich_song_meta(meta, "tid")
        assert meta["album"] == "YT Album"
        assert meta["artists"] == "YT Artist"

    def test_lossless_leaf_no_provider_record_spotify_pref_uses_resolver(self):
        from types import SimpleNamespace

        from Spotify_Downloader import MusicScraper

        lossless = SimpleNamespace(
            provider_metadata_for=lambda _tid: None,
            _isrc=MagicMock(),
        )
        lossless._isrc.resolve_metadata.return_value = {
            "album": "Spotify Album",
            "artists": "Artist",
            "trackNumber": 1,
        }
        scraper = MusicScraper(download_source="lossless", flac_metadata_source="spotify")
        scraper._backend = lossless
        scraper._metadata_service = SimpleNamespace(
            get=lambda _id: (_ for _ in ()).throw(AssertionError("service must not be called"))
        )
        meta = self._song_meta()
        scraper._enrich_song_meta(meta, "tid")
        assert meta["album"] == "Spotify Album"
        lossless._isrc.resolve_metadata.assert_called_once_with("tid")

    def test_chain_lossless_served_no_provider_record_spotify_pref_uses_resolver(self):
        from types import SimpleNamespace

        from backends.chain import FallbackChainBackend
        from Spotify_Downloader import MusicScraper

        lossless = SimpleNamespace(
            name="lossless",
            max_concurrency=4,
            provider_metadata_for=lambda _tid: None,
            _isrc=MagicMock(),
        )
        lossless._isrc.resolve_metadata.return_value = {
            "album": "Spotify Album",
            "artists": "Artist",
            "trackNumber": 1,
        }
        scraper_ns = SimpleNamespace(error_signal=SimpleNamespace(emit=lambda *_a: None))
        chain = FallbackChainBackend("lossless", [("lossless", lossless)], scraper=scraper_ns)
        chain._tls.served_by = "lossless"
        scraper = MusicScraper(download_source="lossless", flac_metadata_source="spotify")
        scraper._backend = chain
        scraper._metadata_service = SimpleNamespace(
            get=lambda _id: (_ for _ in ()).throw(AssertionError("service must not be called"))
        )
        meta = self._song_meta()
        scraper._enrich_song_meta(meta, "tid")
        assert meta["album"] == "Spotify Album"
        lossless._isrc.resolve_metadata.assert_called_once_with("tid")

    def test_skip_path_ignores_stale_chain_served_by(self):
        from types import SimpleNamespace

        from backends.chain import FallbackChainBackend
        from Spotify_Downloader import MusicScraper

        lossless = SimpleNamespace(
            name="lossless",
            max_concurrency=4,
            provider_metadata_for=lambda _tid: None,
            _isrc=MagicMock(),
        )
        lossless._isrc.resolve_metadata.return_value = {
            "album": "Spotify Album",
            "artists": "Artist",
            "trackNumber": 1,
        }
        scraper_ns = SimpleNamespace(error_signal=SimpleNamespace(emit=lambda *_a: None))
        chain = FallbackChainBackend("lossless", [("lossless", lossless)], scraper=scraper_ns)
        chain._tls.served_by = "youtube"  # stale from a prior track
        scraper = MusicScraper(download_source="lossless", flac_metadata_source="spotify")
        scraper._backend = chain
        scraper._metadata_service = SimpleNamespace(
            get=lambda _id: (_ for _ in ()).throw(AssertionError("service must not be called"))
        )
        meta = self._song_meta()
        scraper._enrich_song_meta(meta, "tid", fresh_fetch=False)
        assert meta["album"] == "Spotify Album"
        lossless._isrc.resolve_metadata.assert_called_once_with("tid")


class TestSpotifydownApiIntegration:
    def test_ensure_spotifydown_api_injects_metadata_resolver(self):
        from Spotify_Downloader import MusicScraper

        class _FakeMetadataService:
            def get(self, _id):
                return {"title": "T", "artists": "A"}

        from spotifydown_api import PlaylistClient

        scraper = MusicScraper()
        scraper._metadata_service = _FakeMetadataService()
        client = scraper.ensure_spotifydown_api()
        assert isinstance(client, PlaylistClient)
        assert client._embed_api._metadata_resolver.__self__ is scraper._metadata_service

    def test_scrape_playlist_passes_on_notice(self, tmp_path):
        from unittest.mock import MagicMock

        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper()
        for sig in (
            "song_meta",
            "add_song_meta",
            "dlprogress_signal",
            "Resetprogress_signal",
            "PlaylistID",
            "song_Album",
            "PlaylistCompleted",
            "error_signal",
        ):
            setattr(scraper, sig, MagicMock())

        mock_api = MagicMock()
        meta = MagicMock()
        meta.track_count = 1
        mock_api.get_playlist_metadata.return_value = meta
        mock_api.iter_playlist_tracks.return_value = iter([])
        scraper.ensure_spotifydown_api = MagicMock(return_value=mock_api)
        scraper.format_playlist_name = lambda _m: "T"

        scraper.scrape_playlist("https://open.spotify.com/playlist/abc", str(tmp_path))

        _, kwargs = mock_api.iter_playlist_tracks.call_args
        assert "on_notice" in kwargs
        assert callable(kwargs["on_notice"])
        assert kwargs["on_notice"] is scraper.error_signal.emit


class TestYoutubeRateLimitSurvival:
    """Tests for YouTube rate-limit detection, gate retry, and user knobs."""

    @staticmethod
    def _scraper(**kw):
        from Spotify_Downloader import MusicScraper

        return MusicScraper(**kw)

    def test_random_sleep_opts_when_enabled(self, tmp_path):
        scraper = self._scraper(youtube_random_sleep=True)
        dest = str(tmp_path / "track.mp3")
        url = "https://www.youtube.com/watch?v=abc"
        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
            patch("os.path.exists", return_value=True),
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            scraper.download_track_audio("test query", dest)
            opts = mock_ydl.call_args[0][0]
        assert opts["sleep_interval"] == 10
        assert opts["max_sleep_interval"] == 20
        assert opts["sleep_interval_requests"] == 0.75

    def test_random_sleep_opts_absent_when_disabled(self, tmp_path):
        scraper = self._scraper(youtube_random_sleep=False)
        dest = str(tmp_path / "track.mp3")
        url = "https://www.youtube.com/watch?v=abc"
        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader.YoutubeDL") as mock_ydl,
            patch("os.path.exists", return_value=True),
        ):
            mock_ydl.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.return_value.__exit__ = MagicMock(return_value=False)
            scraper.download_track_audio("test query", dest)
            opts = mock_ydl.call_args[0][0]
        for key in ("sleep_interval", "max_sleep_interval", "sleep_interval_requests"):
            assert key not in opts

    def test_youtube_max_concurrency_reaches_backend(self):
        scraper = self._scraper(youtube_max_concurrency=2)
        assert scraper._backend.max_concurrency == 2

    def test_youtube_max_concurrency_clamps_worker_pool(self, tmp_path):
        from Spotify_Downloader import MusicScraper

        scraper = MusicScraper(youtube_max_concurrency=2)
        for sig in (
            "song_meta",
            "add_song_meta",
            "dlprogress_signal",
            "Resetprogress_signal",
            "PlaylistID",
            "song_Album",
            "PlaylistCompleted",
            "error_signal",
            "count_updated",
        ):
            setattr(scraper, sig, MagicMock())

        tracks = [TestParallelDownloads()._make_track(f"id{i}", f"Song {i}") for i in range(8)]
        seen_workers: set[str] = set()
        barrier = threading.Barrier(2, timeout=5)

        def fake_download(query, dest, **_kw):
            seen_workers.add(threading.current_thread().name)
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait()
            open(dest, "wb").close()
            return dest, False

        scraper.download_track_audio = fake_download
        mock_api = MagicMock()
        meta = MagicMock()
        meta.name = "T"
        meta.owner = "O"
        meta.cover_url = None
        mock_api.get_playlist_metadata.return_value = meta
        mock_api.iter_playlist_tracks.return_value = iter(tracks)
        scraper.ensure_spotifydown_api = MagicMock(return_value=mock_api)
        scraper.format_playlist_name = lambda _m: "T"

        scraper.scrape_playlist("https://open.spotify.com/playlist/abc123", str(tmp_path))
        assert len(seen_workers) == 2

    def test_rate_limit_retries_once_then_succeeds(self, tmp_path):
        scraper = self._scraper()
        scraper._yt_rate_gate.COOLDOWN_S = 0.05
        scraper._yt_rate_gate.SLICE_S = 0.01
        dest = str(tmp_path / "Song.mp3")
        url = "https://www.youtube.com/watch?v=abc"
        download_attempts = 0

        class FakeYdl:
            def __init__(self, opts):
                self._opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, _url, download=False):
                nonlocal download_attempts
                if not download:
                    return {"entries": []}
                download_attempts += 1
                logger = self._opts.get("logger")
                if download_attempts == 1:
                    logger.error(
                        "This content isn't available, try again later — rate-limited by YouTube"
                    )
                    return None
                return None

        def exists(path):
            return path == dest and download_attempts >= 2

        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader.YoutubeDL", FakeYdl),
            patch("os.path.exists", side_effect=exists),
        ):
            result, _ = scraper.download_track_audio("ytsearch1:Song Artist audio", dest)
        assert result == dest
        assert download_attempts == 2

    def test_search_leg_rate_limit_retries(self, tmp_path):
        # Single-query plan, so the ONLY way to reach a second search is the gate
        # retry loop (not a second query within one outer attempt). The search leg
        # uses self._opts["logger"] (strict): if production failed to thread the
        # per-attempt logger into the SEARCH opts, the KeyError is swallowed by
        # _select_youtube_match, the trip is never recorded, and this test fails
        # with "no playable audio source" instead of returning the dest.
        scraper = self._scraper()
        scraper._yt_rate_gate.COOLDOWN_S = 0.05
        scraper._yt_rate_gate.SLICE_S = 0.01
        dest = str(tmp_path / "Song.mp3")
        search_attempts = 0
        download_attempts = 0

        class FakeYdl:
            def __init__(self, opts):
                self._opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, _url, download=False):
                nonlocal search_attempts, download_attempts
                if not download:
                    search_attempts += 1
                    logger = self._opts["logger"]  # strict: must be threaded in
                    if search_attempts == 1:
                        # Rate-limit surfaces on the SEARCH leg; no candidate.
                        logger.error(
                            "This content isn't available, try again later — rate-limited by YouTube"
                        )
                        return {"entries": []}
                    # Second outer attempt (post-cooldown): candidate appears.
                    return {"entries": [{"id": "abc", "title": "Song Artist", "duration": 200}]}
                download_attempts += 1
                return None

        def exists(path):
            return path == dest and download_attempts >= 1

        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_build_youtube_download_plan", return_value=[("q", False)]),
            patch("Spotify_Downloader.YoutubeDL", FakeYdl),
            patch("os.path.exists", side_effect=exists),
        ):
            result, _ = scraper.download_track_audio("ytsearch1:Song Artist audio", dest)
        assert result == dest
        # 2 searches = one per outer attempt (proves the retry loop ran); 1 download
        # (only the second attempt reached the download leg).
        assert search_attempts == 2
        assert download_attempts == 1

    def test_max_hold_cap_terminates_download_loop(self, tmp_path):
        import time

        scraper = self._scraper()
        scraper._yt_rate_gate._engaged_at = time.monotonic() - scraper._yt_rate_gate.MAX_HOLD_S - 1
        dest = str(tmp_path / "Song.mp3")
        url = "https://www.youtube.com/watch?v=abc"
        download_attempts = 0

        class FakeYdl:
            def __init__(self, opts):
                self._opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, _url, download=False):
                nonlocal download_attempts
                if not download:
                    return None
                download_attempts += 1
                logger = self._opts.get("logger")
                logger.error(
                    "This content isn't available, try again later — rate-limited by YouTube"
                )
                return None

        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader.YoutubeDL", FakeYdl),
            patch("os.path.exists", return_value=False),
            pytest.raises(RuntimeError, match="rate limit persisted"),
        ):
            scraper.download_track_audio("ytsearch1:Song Artist audio", dest)
        assert download_attempts <= 2

    def test_genuine_not_found_raises_without_retry(self, tmp_path):
        scraper = self._scraper()
        dest = str(tmp_path / "Song.mp3")
        url = "https://www.youtube.com/watch?v=missing"
        download_attempts = 0

        class FakeYdl:
            def __init__(self, opts):
                self._opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, _url, download=False):
                nonlocal download_attempts
                if download:
                    download_attempts += 1
                return None

        with (
            patch("Spotify_Downloader.get_ffmpeg_path", return_value="/usr/bin"),
            patch.object(scraper, "_build_youtube_download_plan", return_value=[("q", False)]),
            patch.object(scraper, "_select_youtube_match", return_value=url),
            patch("Spotify_Downloader.YoutubeDL", FakeYdl),
            patch("os.path.exists", return_value=False),
            pytest.raises(RuntimeError, match="no playable audio source found"),
        ):
            scraper.download_track_audio("ytsearch1:Song Artist audio", dest)
        assert download_attempts == 1
