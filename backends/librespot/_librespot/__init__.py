"""Thin adapter over the alpha ``librespot`` package (kokarare1212/librespot-python).

Every direct import of the upstream alpha library is contained HERE so a breaking
upstream change touches exactly one file. Pinned commit:
``18104622b3be02062f1f8abe8dafc396413e9784``.

All upstream imports are lazy (inside functions): importing this module is cheap and
never raises, so callers can probe :func:`is_available` and degrade gracefully. The
helpers below were written against the pinned commit's actual source, which differs
from the goal's illustrative snippets in two load-bearing ways:

* OAuth is fully self-contained. ``Session.Builder.oauth(url_callback)`` uses the
  built-in keymaster client_id + a redirect on ``127.0.0.1:5588`` and runs its OWN
  blocking callback HTTP server; we only supply a callback that opens the browser.
  It blocks until the browser login completes, so callers must run it off the UI
  thread. On success the Session itself writes ``stored_credentials_file``.
* The 167-byte (``0xA7``) Spotify header is skipped INSIDE ``content_feeder().load``
  (``CdnFeedHelper.load_track`` does ``input_stream.skip(0xA7)``), and
  ``Streamer.stream()`` returns that same already-advanced stream — so the bytes
  read from the :func:`load_loaded_stream` result already begin at the first ``OggS`` page.
"""

from __future__ import annotations

import inspect
import io
import math
import os
import queue
import struct
import threading
import time
from typing import Callable

# Cached import probe: None = not yet checked, True/False = result.
_AVAILABLE: bool | None = None
_IMPORT_ERROR: BaseException | None = None

# 8 chunks × 128 KiB = 1 MiB in flight. Measured on a real 10.9 MB / 84-chunk track:
# stock (serial) 5.04s = 2.17 MB/s, depth-8 = 0.43s = 25.4 MB/s, byte-identical;
# diminishing returns beyond 8.
PRELOAD_AHEAD_CHUNKS = 8

# CDN chunk GET timeouts (connect, read). Read must exceed a slow 128 KiB fetch yet bound
# a blackholed socket so executor threads cannot block forever after a network blip.
CDN_REQUEST_TIMEOUT_S = (10, 60)

PATCH_STATUS_APPLIED = "applied"
PATCH_STATUS_SKIPPED_INCOMPATIBLE = "skipped_incompatible"
PATCH_STATUS_SOURCE_UNAVAILABLE = "source_unavailable"

# Exact substrings from pinned ``audio/__init__.py`` ``check_availability`` (buggy preload loop).
_BROKEN_CHECK_AVAILABILITY_MARKERS = (
    "if (self.requested_chunks()[i]\n"
    "                    and self.retries[i] < self.preload_chunk_retries):",
    "self.request_chunk_from_stream(i)\n                self.requested_chunks()[chunk] = True",
)
# Re-dispatch fix depends on ``notify_chunk_error`` only recording ``chunk_exception`` when
# ``index == wait_for_chunk``.
_NOTIFY_CHUNK_ERROR_MARKER = "if index == self.wait_for_chunk"

_CHECK_AVAILABILITY_PATCHED = False
_CHECK_AVAILABILITY_PATCH_STATUS: str | None = None
_CHECK_AVAILABILITY_PATCH_LOCK = threading.Lock()

_CDN_ROBUSTNESS_PATCHED = False
_CDN_ROBUSTNESS_PATCH_STATUS: str | None = None
_CDN_ROBUSTNESS_PATCH_LOCK = threading.Lock()

# Guard markers for pinned ``CdnManager.Streamer.request_chunk`` / ``request``.
_BROKEN_CDN_REQUEST_CHUNK_MARKERS = ("response = self.request(index)",)
_BROKEN_CDN_REQUEST_CHUNK_ANTI_MARKERS = ("except",)
_BROKEN_CDN_REQUEST_MARKERS = ('"Range": "bytes={}-{}"',)
_BROKEN_CDN_REQUEST_ANTI_MARKERS = ("timeout",)

# Guard markers for pinned ``AudioKeyManager.get_audio_key`` / ``SyncCallback``.
_BROKEN_GET_AUDIO_KEY_MARKERS = (
    "return self.get_audio_key(gid, file_id, False)",
    "callback = AudioKeyManager.SyncCallback(self)",
)
_BROKEN_SYNC_CALLBACK_MARKERS = (
    "__reference = queue.Queue()",
    "__reference_lock = threading.Condition()",
)

_AUDIO_KEY_PATCHED = False
_AUDIO_KEY_PATCH_STATUS: str | None = None
_AUDIO_KEY_PATCH_LOCK = threading.Lock()

# Reconnect resilience: pinned ``Session.reconnect`` tries ONE random access point and
# raises out of the receiver thread if it refuses. Retry a FRESH AP a few times.
_RECONNECT_AP_ATTEMPTS = 5
_RECONNECT_BACKOFF_BASE_S = 0.5
_RECONNECT_BACKOFF_CAP_S = 8.0

_RECONNECT_RESILIENCE_PATCHED = False
_RECONNECT_RESILIENCE_PATCH_STATUS: str | None = None
_RECONNECT_RESILIENCE_PATCH_LOCK = threading.Lock()

# Guard markers from pinned ``Session.reconnect`` (core.py:1241).
_BROKEN_RECONNECT_MARKERS = (
    "self.connection = Session.ConnectionHolder.create(",
    "ApResolver.get_random_accesspoint(), self.__inner.conf)",
    "reusable_auth_credentials_type",
)

# Receiver-thread excepthook (defense-in-depth for a TOTAL reconnect failure).
_RECEIVER_THREAD_NAME = "session-packet-receiver"
_EXCEPTHOOK_INSTALLED = False
_EXCEPTHOOK_LOCK = threading.Lock()
_PREV_EXCEPTHOOK = None


class AudioKeyError(RuntimeError):
    """Raised by the audio-key patch on throttle (``.code`` set) or timeout (``None``)."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class _OneShotKeyCallback:
    """Per-request audio-key waiter with an instance-level queue (no shared state)."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()

    def key(self, key: bytes) -> None:
        self._queue.put(("key", key))

    def error(self, code: int) -> None:
        self._queue.put(("error", code))

    def wait(self, timeout: float):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


def check_availability_patch_status() -> str | None:
    """Patch outcome: ``applied`` | ``skipped_incompatible`` | ``source_unavailable`` | None."""
    return _CHECK_AVAILABILITY_PATCH_STATUS


def cdn_robustness_patch_status() -> str | None:
    """Patch outcome: ``applied`` | ``skipped_incompatible`` | ``source_unavailable`` | None."""
    return _CDN_ROBUSTNESS_PATCH_STATUS


def audio_key_patch_status() -> str | None:
    """Patch outcome: ``applied`` | ``skipped_incompatible`` | ``source_unavailable`` | None."""
    return _AUDIO_KEY_PATCH_STATUS


def reconnect_resilience_patch_status() -> str | None:
    """Patch outcome: ``applied`` | ``skipped_incompatible`` | ``source_unavailable`` | None."""
    return _RECONNECT_RESILIENCE_PATCH_STATUS


def _fixed_check_availability(self, chunk: int, wait: bool, halted: bool) -> None:
    """Upstream ``AbsChunkedInputStream.check_availability`` with five fixes.

    Transcribed from the pinned librespot commit (``audio/__init__.py`` lines 116-149)
    except: (1) ``not self.requested_chunks()[i]`` in the preload guard, and
    (2) ``self.requested_chunks()[i] = True`` instead of ``[chunk]``, and
    (3) the wait predicate also wakes on ``chunk_exception`` or ``closed``, and
    (4) mark-before-submit: set ``requested_chunks()[…] = True`` before
    ``request_chunk_from_stream`` so an instant failure (which resets ``requested`` in
    ``notify_chunk_error``) is never overwritten back to True, and
    (5) armed re-dispatch: after arming ``wait_for_chunk``, re-submit the chunk if a
    pre-arm failure already consumed the request so the outcome cannot be lost.
    """
    if halted and not wait:
        raise TypeError()
    if not self.requested_chunks()[chunk]:
        self.requested_chunks()[chunk] = True
        self.request_chunk_from_stream(chunk)
    for i in range(
        chunk + 1,
        min(self.chunks() - 1, chunk + self.preload_ahead) + 1,
    ):
        if not self.requested_chunks()[i] and self.retries[i] < self.preload_chunk_retries:
            self.requested_chunks()[i] = True
            self.request_chunk_from_stream(i)
    if wait:
        if self.available_chunks()[chunk]:
            return
        retry = False
        with self.wait_lock:
            if not halted:
                self.stream_read_halted(chunk, int(time.time() * 1000))
            self.chunk_exception = None
            self.wait_for_chunk = chunk
            if not self.requested_chunks()[chunk] and not self.available_chunks()[chunk]:
                # a pre-arm failure consumed the request; re-dispatch now that the waiter is
                # armed so the outcome cannot be lost (failure -> chunk_exception -> wake)
                self.requested_chunks()[chunk] = True
                self.request_chunk_from_stream(chunk)
            self.wait_lock.wait_for(
                lambda: (
                    self.available_chunks()[chunk]
                    or self.chunk_exception is not None
                    or self.closed
                )
            )
            if self.closed:
                return
            if self.chunk_exception is not None:
                if self.should_retry(chunk):
                    retry = True
                else:
                    from librespot.audio import AbsChunkedInputStream

                    raise AbsChunkedInputStream.ChunkException
            if not retry:
                self.stream_read_halted(chunk, int(time.time() * 1000))
        if retry:
            time.sleep(math.log10(self.retries[chunk]))
            self.check_availability(chunk, True, True)


def _apply_check_availability_patch() -> None:
    """Monkeypatch upstream preload bug; idempotent, locked, and source-guarded."""
    global _CHECK_AVAILABILITY_PATCHED, _CHECK_AVAILABILITY_PATCH_STATUS
    if _CHECK_AVAILABILITY_PATCHED:
        return
    with _CHECK_AVAILABILITY_PATCH_LOCK:
        if _CHECK_AVAILABILITY_PATCHED:
            return
        from librespot.audio import AbsChunkedInputStream

        try:
            source = inspect.getsource(AbsChunkedInputStream.check_availability)
            notify_source = inspect.getsource(AbsChunkedInputStream.notify_chunk_error)
        except (OSError, TypeError):
            AbsChunkedInputStream.check_availability = _fixed_check_availability
            AbsChunkedInputStream.preload_ahead = PRELOAD_AHEAD_CHUNKS
            _CHECK_AVAILABILITY_PATCH_STATUS = PATCH_STATUS_SOURCE_UNAVAILABLE
            _CHECK_AVAILABILITY_PATCHED = True
            return
        if not all(marker in source for marker in _BROKEN_CHECK_AVAILABILITY_MARKERS):
            _CHECK_AVAILABILITY_PATCH_STATUS = PATCH_STATUS_SKIPPED_INCOMPATIBLE
            _CHECK_AVAILABILITY_PATCHED = True
            return
        if _NOTIFY_CHUNK_ERROR_MARKER not in notify_source:
            _CHECK_AVAILABILITY_PATCH_STATUS = PATCH_STATUS_SKIPPED_INCOMPATIBLE
            _CHECK_AVAILABILITY_PATCHED = True
            return
        AbsChunkedInputStream.check_availability = _fixed_check_availability
        AbsChunkedInputStream.preload_ahead = PRELOAD_AHEAD_CHUNKS
        _CHECK_AVAILABILITY_PATCH_STATUS = PATCH_STATUS_APPLIED
        _CHECK_AVAILABILITY_PATCHED = True


def _fixed_cdn_request_chunk(self, index: int) -> None:
    """Upstream ``CdnManager.Streamer.request_chunk`` with executor-safe error notify."""
    try:
        response = self.request(index)
        self.write_chunk(response.buffer, index, False)
    except Exception as exc:  # noqa: BLE001 - route all CDN failures through notify_chunk_error
        self._Streamer__internal_stream.notify_chunk_error(index, exc)


def _fixed_cdn_request(self, chunk: int = None, range_start: int = None, range_end: int = None):
    """Upstream ``CdnManager.Streamer.request`` with a bounded ``client().get`` timeout."""
    from librespot.audio import CdnManager
    from librespot.audio.storage import ChannelManager
    from requests.structures import CaseInsensitiveDict

    if chunk is None and range_start is None and range_end is None:
        raise TypeError()
    if chunk is not None:
        range_start = ChannelManager.chunk_size * chunk
        range_end = (chunk + 1) * ChannelManager.chunk_size - 1
    response = self._Streamer__session.client().get(
        self._Streamer__cdn_url.url,
        headers=CaseInsensitiveDict(
            {
                "Range": "bytes={}-{}".format(range_start, range_end)  # noqa: UP032
            }
        ),
        timeout=CDN_REQUEST_TIMEOUT_S,
    )
    if response.status_code != 206:
        raise IOError(response.status_code)  # noqa: UP024
    body = response.content
    if body is None:
        raise IOError("Response body is empty!")  # noqa: UP024
    return CdnManager.InternalResponse(body, response.headers)


def _apply_cdn_robustness_patch() -> None:
    """Monkeypatch CDN chunk fetch robustness; idempotent, locked, and source-guarded."""
    global _CDN_ROBUSTNESS_PATCHED, _CDN_ROBUSTNESS_PATCH_STATUS
    if _CDN_ROBUSTNESS_PATCHED:
        return
    with _CDN_ROBUSTNESS_PATCH_LOCK:
        if _CDN_ROBUSTNESS_PATCHED:
            return
        from librespot.audio import CdnManager

        try:
            request_chunk_source = inspect.getsource(CdnManager.Streamer.request_chunk)
            request_source = inspect.getsource(CdnManager.Streamer.request)
        except (OSError, TypeError):
            CdnManager.Streamer.request_chunk = _fixed_cdn_request_chunk
            CdnManager.Streamer.request = _fixed_cdn_request
            _CDN_ROBUSTNESS_PATCH_STATUS = PATCH_STATUS_SOURCE_UNAVAILABLE
            _CDN_ROBUSTNESS_PATCHED = True
            return
        chunk_ok = all(m in request_chunk_source for m in _BROKEN_CDN_REQUEST_CHUNK_MARKERS)
        chunk_ok = chunk_ok and not any(
            m in request_chunk_source for m in _BROKEN_CDN_REQUEST_CHUNK_ANTI_MARKERS
        )
        request_ok = all(m in request_source for m in _BROKEN_CDN_REQUEST_MARKERS)
        request_ok = request_ok and not any(
            m in request_source for m in _BROKEN_CDN_REQUEST_ANTI_MARKERS
        )
        if not (chunk_ok and request_ok):
            _CDN_ROBUSTNESS_PATCH_STATUS = PATCH_STATUS_SKIPPED_INCOMPATIBLE
            _CDN_ROBUSTNESS_PATCHED = True
            return
        CdnManager.Streamer.request_chunk = _fixed_cdn_request_chunk
        CdnManager.Streamer.request = _fixed_cdn_request
        _CDN_ROBUSTNESS_PATCH_STATUS = PATCH_STATUS_APPLIED
        _CDN_ROBUSTNESS_PATCHED = True


def _fixed_get_audio_key(self, gid: bytes, file_id: bytes, retry: bool = True) -> bytes:
    """Upstream ``AudioKeyManager.get_audio_key`` with five fixes.

    Transcribed from the pinned librespot commit (``audio/__init__.py`` lines 258-282)
    except: (1) no internal retry — ``audio.py`` owns all retry/backoff; (2) per-call
    instance-level queue via :class:`_OneShotKeyCallback` (not the shared class-level
    ``SyncCallback.__reference``); (3) register the callback **before** ``send``; (4)
    ``finally: callbacks.pop(seq, None)`` so late responses cannot deliver stale keys;
    (5) raise :class:`AudioKeyError` on error and timeout (``.code`` is the server code
    when received, ``None`` on timeout). The class-level ``__callbacks`` dict is left in
    place — dispatch reads it; the metadata service never requests keys.
    """
    from librespot import util
    from librespot.crypto import Packet

    seq: int
    with self._AudioKeyManager__seq_holder_lock:
        seq = self._AudioKeyManager__seq_holder
        self._AudioKeyManager__seq_holder += 1
    out = io.BytesIO()
    out.write(file_id)
    out.write(gid)
    out.write(struct.pack(">i", seq))
    out.write(self._AudioKeyManager__zero_short)
    out.seek(0)
    payload = out.read()
    callback = _OneShotKeyCallback()
    self._AudioKeyManager__callbacks[seq] = callback
    try:
        self._AudioKeyManager__session.send(Packet.Type.request_key, payload)
        result = callback.wait(self.audio_key_request_timeout)
        if result is None:
            raise AudioKeyError(
                "audio key request timed out (gid: {}, fileId: {})".format(  # noqa: UP032
                    util.bytes_to_hex(gid), util.bytes_to_hex(file_id)
                ),
                code=None,
            )
        kind, value = result
        if kind == "error":
            raise AudioKeyError("audio key error, code: {}".format(value), code=value)  # noqa: UP032
        return value
    finally:
        self._AudioKeyManager__callbacks.pop(seq, None)


def _apply_audio_key_patch() -> None:
    """Monkeypatch audio-key fetch robustness; idempotent, locked, and source-guarded."""
    global _AUDIO_KEY_PATCHED, _AUDIO_KEY_PATCH_STATUS
    if _AUDIO_KEY_PATCHED:
        return
    with _AUDIO_KEY_PATCH_LOCK:
        if _AUDIO_KEY_PATCHED:
            return
        from librespot.audio import AudioKeyManager

        try:
            get_key_source = inspect.getsource(AudioKeyManager.get_audio_key)
            sync_source = inspect.getsource(AudioKeyManager.SyncCallback)
        except (OSError, TypeError):
            AudioKeyManager.get_audio_key = _fixed_get_audio_key
            _AUDIO_KEY_PATCH_STATUS = PATCH_STATUS_SOURCE_UNAVAILABLE
            _AUDIO_KEY_PATCHED = True
            return
        if not all(marker in get_key_source for marker in _BROKEN_GET_AUDIO_KEY_MARKERS):
            _AUDIO_KEY_PATCH_STATUS = PATCH_STATUS_SKIPPED_INCOMPATIBLE
            _AUDIO_KEY_PATCHED = True
            return
        if not all(marker in sync_source for marker in _BROKEN_SYNC_CALLBACK_MARKERS):
            _AUDIO_KEY_PATCH_STATUS = PATCH_STATUS_SKIPPED_INCOMPATIBLE
            _AUDIO_KEY_PATCHED = True
            return
        AudioKeyManager.get_audio_key = _fixed_get_audio_key
        _AUDIO_KEY_PATCH_STATUS = PATCH_STATUS_APPLIED
        _AUDIO_KEY_PATCHED = True


def _fixed_reconnect(self) -> None:
    """Upstream ``Session.reconnect`` with a multi-AP retry around the socket connect.

    Pinned ``reconnect`` resolves ONE random access point and calls
    ``ConnectionHolder.create`` exactly once; if that AP refuses (``ConnectionRefusedError``)
    or is blackholed, the error propagates out of the ``Session.Receiver.run`` except-handler
    that called us, kills the receiver thread with an uncaught traceback, and leaves the
    session with a closed socket and no packet pump. We retry ``create`` against a FRESH
    random AP up to ``_RECONNECT_AP_ATTEMPTS`` times with bounded exponential backoff so a
    single transient refused/blackholed AP no longer kills the self-heal. Everything else
    (close old connection, stop old receiver, connect, re-authenticate) is transcribed
    verbatim from the pinned source.
    """
    from librespot.core import ApResolver, Authentication, Session

    if self.connection is not None:
        self.connection.close()
        self._Session__receiver.stop()
    for attempt in range(_RECONNECT_AP_ATTEMPTS):
        try:
            self.connection = Session.ConnectionHolder.create(
                ApResolver.get_random_accesspoint(), self._Session__inner.conf
            )
            break
        except OSError:  # refused / reset / blackholed / DNS — all OSError subclasses
            if attempt + 1 >= _RECONNECT_AP_ATTEMPTS:
                raise
            delay = min(_RECONNECT_BACKOFF_BASE_S * (2**attempt), _RECONNECT_BACKOFF_CAP_S)
            time.sleep(delay)
    self.connect()
    self._Session__authenticate_partial(
        Authentication.LoginCredentials(
            typ=self._Session__ap_welcome.reusable_auth_credentials_type,
            username=self._Session__ap_welcome.canonical_username,
            auth_data=self._Session__ap_welcome.reusable_auth_credentials,
        ),
        True,
    )
    self.logger.info(
        "Re-authenticated as {}!".format(self._Session__ap_welcome.canonical_username)  # noqa: UP032
    )


def _apply_reconnect_resilience_patch() -> None:
    """Monkeypatch Session.reconnect for multi-AP resilience; idempotent, locked, source-guarded."""
    global _RECONNECT_RESILIENCE_PATCHED, _RECONNECT_RESILIENCE_PATCH_STATUS
    if _RECONNECT_RESILIENCE_PATCHED:
        return
    with _RECONNECT_RESILIENCE_PATCH_LOCK:
        if _RECONNECT_RESILIENCE_PATCHED:
            return
        from librespot.core import Session

        try:
            reconnect_source = inspect.getsource(Session.reconnect)
        except (OSError, TypeError):
            Session.reconnect = _fixed_reconnect
            _RECONNECT_RESILIENCE_PATCH_STATUS = PATCH_STATUS_SOURCE_UNAVAILABLE
            _RECONNECT_RESILIENCE_PATCHED = True
            return
        if not all(m in reconnect_source for m in _BROKEN_RECONNECT_MARKERS):
            _RECONNECT_RESILIENCE_PATCH_STATUS = PATCH_STATUS_SKIPPED_INCOMPATIBLE
            _RECONNECT_RESILIENCE_PATCHED = True
            return
        Session.reconnect = _fixed_reconnect
        _RECONNECT_RESILIENCE_PATCH_STATUS = PATCH_STATUS_APPLIED
        _RECONNECT_RESILIENCE_PATCHED = True


def _receiver_quiet_excepthook(args) -> None:
    """Quiet ONLY the librespot ``session-packet-receiver`` thread; delegate all others.

    A total reconnect failure (every AP attempt exhausted) re-raises out of
    ``Session.Receiver.run`` and would otherwise dump a multi-line traceback to the user's
    console. Replace it with one concise line; the per-track ``audio.py`` ladder + the
    fallback chain still handle the affected track. Every other thread is delegated to the
    previously-installed hook so unrelated crashes are never masked.
    """
    thread = getattr(args, "thread", None)
    if thread is not None and getattr(thread, "name", None) == _RECEIVER_THREAD_NAME:
        reason = getattr(args, "exc_value", None) or "connection lost"
        print(f"[*] librespot: Spotify session dropped ({reason}); reconnecting on next track.")
        return
    if _PREV_EXCEPTHOOK is not None:
        _PREV_EXCEPTHOOK(args)


def _install_receiver_excepthook() -> None:
    """Install :func:`_receiver_quiet_excepthook` once, chaining the prior hook."""
    global _EXCEPTHOOK_INSTALLED, _PREV_EXCEPTHOOK
    if _EXCEPTHOOK_INSTALLED:
        return
    with _EXCEPTHOOK_LOCK:
        if _EXCEPTHOOK_INSTALLED:
            return
        _PREV_EXCEPTHOOK = threading.excepthook
        threading.excepthook = _receiver_quiet_excepthook
        _EXCEPTHOOK_INSTALLED = True


def is_available() -> bool:
    """True iff the alpha ``librespot`` package and its deps import cleanly.

    Result is cached. On failure the captured exception is available via
    :func:`import_error` so callers can surface a specific reason.
    """
    global _AVAILABLE, _IMPORT_ERROR
    if _AVAILABLE is None:
        try:
            import librespot.audio.decoders  # noqa: F401
            import librespot.core  # noqa: F401
            import librespot.metadata  # noqa: F401

            _apply_check_availability_patch()
            _apply_cdn_robustness_patch()
            _apply_audio_key_patch()
            _apply_reconnect_resilience_patch()
            _AVAILABLE = True
        except BaseException as exc:  # noqa: BLE001 - any import-time failure disables it
            _AVAILABLE = False
            _IMPORT_ERROR = exc
    return _AVAILABLE


def import_error() -> BaseException | None:
    """The exception that made :func:`is_available` return False (or None)."""
    return _IMPORT_ERROR


def _configuration(credentials_path: str):
    """Build a Session.Configuration that persists credentials to *credentials_path*.

    The chunk cache is a no-op stub upstream, so only credential storage matters; we
    point it at the caller's path (default would be ``cwd/credentials.json``).
    """
    from librespot.core import Session

    return (
        Session.Configuration.Builder()
        .set_store_credentials(True)
        .set_stored_credential_file(credentials_path)
        .build()
    )


def login_oauth(
    credentials_path: str,
    on_auth_url: Callable[[str], None],
    *,
    device_name: str = "Setlist",
    success_page: str | None = None,
):
    """Interactive OAuth login (BLOCKING — run off the UI thread).

    ``on_auth_url(url)`` is invoked with the Spotify authorize URL so the caller can
    open a browser; the upstream lib then waits on its own loopback server for the
    redirect. If a credentials file already exists, upstream ``oauth`` short-circuits
    to ``stored_file`` and no server/browser is needed. Returns a connected Session.
    """
    from librespot.core import Session

    conf = _configuration(credentials_path)
    builder = Session.Builder(conf).set_device_name(device_name)
    session = builder.oauth(on_auth_url, success_page).create()
    _install_receiver_excepthook()
    return session


def login_stored(credentials_path: str, *, device_name: str = "Setlist"):
    """Resume a session from a cached ``credentials.json`` (no browser). Returns a Session."""
    from librespot.core import Session

    conf = _configuration(credentials_path)
    builder = Session.Builder(conf).set_device_name(device_name)
    session = builder.stored_file(credentials_path).create()
    _install_receiver_excepthook()
    return session


def has_stored_credentials(credentials_path: str) -> bool:
    """True iff a cached credentials file exists at *credentials_path*."""
    return bool(credentials_path) and os.path.isfile(credentials_path)


def product_type(session) -> str | None:
    """Account product type: ``"premium"`` | ``"free"`` | None (from user attributes)."""
    return session.get_user_attribute("type")


def account_username(session) -> str | None:
    """Canonical username of the authenticated account, best-effort."""
    try:
        return session.username()
    except Exception:  # noqa: BLE001 - purely informational
        return None


def web_bearer_token(session, scope: str = "user-read-email") -> str:
    """A Spotify Web-API bearer token for *scope* (used by the extended search)."""
    return session.tokens().get(scope)


def vorbis_quality(name: str, *, strict: bool = True):
    """An AudioQualityPicker selecting an OGG/Vorbis file for AudioQuality *name*
    ("VERY_HIGH" -> ~320k, "HIGH" -> ~160k, ...).

    Default ``strict=True`` returns a picker that matches ONLY the requested quality
    tier (no silent downgrade), so the caller's VERY_HIGH->HIGH ladder is real and the
    obtained bitrate is known. Upstream's ``VorbisOnlyAudioQuality`` (``strict=False``)
    instead falls back to any available Vorbis file, which can quietly yield 160/96k
    while we promise 320k.
    """
    from librespot.audio import SuperAudioFormat
    from librespot.audio.decoders import AudioQuality, VorbisOnlyAudioQuality

    preferred = AudioQuality[name]
    if not strict:
        return VorbisOnlyAudioQuality(preferred)

    from librespot.structure import AudioQualityPicker

    class _StrictVorbisQuality(AudioQualityPicker):
        def get_file(self, files):
            for f in preferred.get_matches(files):
                if (
                    f.HasField("format")
                    and SuperAudioFormat.get(f.format) == SuperAudioFormat.VORBIS
                ):
                    return f
            return None  # strict: no any-Vorbis fallback -> caller drops to next tier

    return _StrictVorbisQuality()


def track_id_from_base62(base62: str):
    """A ``TrackId`` from a base62 Spotify track id."""
    from librespot.metadata import TrackId

    return TrackId.from_base62(base62)


def load_loaded_stream(session, track_id, quality):
    """``content_feeder().load(...)`` -> LoadedStream (header already skipped to OggS).

    ``preload=False``, ``halt_listener=None`` per the pinned API.
    """
    _apply_check_availability_patch()
    _apply_cdn_robustness_patch()
    _apply_audio_key_patch()
    _apply_reconnect_resilience_patch()
    _install_receiver_excepthook()
    return session.content_feeder().load(track_id, quality, False, None)


def track_metadata(session, base62: str):
    """Fetch a track's ``Metadata.Track`` protobuf WITHOUT streaming any audio.

    ``session.api().get_metadata_4_track(...)`` is a metadata-only Mercury
    (extended-metadata) request — no audio key, no CDN stream, no Premium gate. It
    returns the SAME ``Metadata.Track`` proto carried by a ``LoadedStream`` (album,
    artists, track/disc number, cover ids), and it goes over the authenticated
    session, so it is NOT subject to the anonymous Spotify Web-API rate limits. This
    is how the YouTube download path obtains the rich metadata it can't get from the
    no-auth embed (which has no album/track number).
    """
    from librespot.metadata import TrackId

    return session.api().get_metadata_4_track(TrackId.from_base62(base62))
