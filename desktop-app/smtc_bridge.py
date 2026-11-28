"""Cross-platform system media bridge for IDE Lyrics Studio.

Reads now-playing metadata, timeline position, artwork, and dispatches
transport commands via WinRT SMTC on Windows, media-control / nowplaying-cli /
AppleScript on macOS, and playerctl / gdbus MPRIS2 on Linux.
"""

from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse
import urllib.request

_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS = sys.platform == "darwin"
_IS_LINUX = sys.platform.startswith("linux")

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _SessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as _PlaybackStatus,
    )
    from winrt.windows.storage.streams import (
        Buffer as _WinRTBuffer,
        DataReader as _DataReader,
        InputStreamOptions as _InputStreamOptions,
    )

    _WINRT_AVAILABLE = True
except Exception:
    _SessionManager = None
    _PlaybackStatus = None
    _WinRTBuffer = None
    _DataReader = None
    _InputStreamOptions = None
    _WINRT_AVAILABLE = False

_WIN32_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
_MAX_ARTWORK_BYTES = 10 * 1024 * 1024
_ARTWORK_CACHE_LIMIT = 32
_VALID_MEDIA_COMMANDS = ("play_pause", "next", "previous")

_KNOWN_APP_NAMES: tuple[tuple[str, str], ...] = (
    ("spotify", "Spotify"),
    ("cider", "Cider"),
    ("applemusic", "Apple Music"),
    ("apple.music", "Apple Music"),
    ("appleinc.applemusic", "Apple Music"),
    ("itunes", "iTunes"),
    ("youtubemusic", "YouTube Music"),
    ("msedge", "Edge"),
    ("chrome", "Chrome"),
    ("firefox", "Firefox"),
    ("brave", "Brave"),
    ("opera", "Opera"),
    ("vivaldi", "Vivaldi"),
    ("safari", "Safari"),
    ("arc", "Arc"),
    ("tidal", "TIDAL"),
    ("deezer", "Deezer"),
    ("amazonmusic", "Amazon Music"),
    ("soundcloud", "SoundCloud"),
    ("qobuz", "Qobuz"),
    ("foobar", "foobar2000"),
    ("musicbee", "MusicBee"),
    ("vlc", "VLC"),
    ("netflix", "Netflix"),
    ("twitch", "Twitch"),
    ("mpv", "mpv"),
    ("mpc-hc", "MPC-HC"),
    ("mpc-be", "MPC-BE"),
    ("potplayer", "PotPlayer"),
    ("zunevideo", "Films & TV"),
    ("plex", "Plex"),
    ("jellyfin", "Jellyfin"),
    ("kodi", "Kodi"),
    ("iina", "IINA"),
    ("quicktime", "QuickTime"),
    ("zunemusic", "Media Player"),
    ("windowsmedia", "Media Player"),
)

_MUSIC_ONLY_APPS: frozenset[str] = frozenset({
    "Spotify",
    "Apple Music",
    "Cider",
    "iTunes",
    "YouTube Music",
    "TIDAL",
    "Deezer",
    "Amazon Music",
    "SoundCloud",
    "foobar2000",
    "MusicBee",
    "Qobuz",
})
_VIDEO_ONLY_APPS: frozenset[str] = frozenset({
    "Netflix",
    "Twitch",
    "mpv",
    "MPC-HC",
    "MPC-BE",
    "PotPlayer",
    "Films & TV",
    "Plex",
    "Jellyfin",
    "Kodi",
    "IINA",
    "QuickTime",
})
_BROWSER_APPS: frozenset[str] = frozenset({
    "Chrome",
    "Edge",
    "Firefox",
    "Brave",
    "Opera",
    "Vivaldi",
    "Safari",
    "Arc",
})
_GENERIC_SITE_NAMES: frozenset[str] = frozenset({
    "youtube",
    "youtube.com",
    "twitch",
    "netflix",
    "vimeo",
    "tiktok",
    "instagram",
    "twitter",
    "x",
    "reddit",
    "facebook",
    "kick",
    "dailymotion",
    "bilibili",
    "chrome",
    "edge",
    "firefox",
    "brave",
    "opera",
    "safari",
})

_AUDIO_EXT_RE = re.compile(r"\.(?:mp3|flac|wav|m4a|ogg|opus|aac|alac|wma)(?:\b|$)", re.I)
_VIDEO_EXT_RE = re.compile(r"\.(?:mp4|mkv|avi|mov|webm|wmv|m4v|flv|ts)(?:\b|$)", re.I)
_MUSIC_URL_TOKENS: tuple[str, ...] = (
    "music.youtube.com",
    "open.spotify.com",
    "music.apple.com",
    "soundcloud.com",
    "tidal.com",
)
_TOPIC_VEVO_RE = re.compile(r"(\s+-\s+topic$|\bvevo$)", re.I)
_MUSIC_TITLE_RE = re.compile(
    r"(\bofficial\s+(music\s+)?video\b|\bofficial\s+(lyric\s+)?video\b|\bofficial\s+audio\b"
    r"|\blyrics?\s+video\b|\(\s*lyrics?\s*\)|\[\s*lyrics?\s*\]|\(\s*audio\s*\)|\[\s*audio\s*\]"
    r"|\bmusic\s+video\b|\bvisualizer\b|\b(feat\.?|ft\.?|featuring)\b|\bprod\.?\s+by\b"
    r"|\(\s*(remix|acoustic|sped\s+up|slowed(\s*\+\s*reverb)?|live\s+session|instrumental|extended\s+mix|radio\s+edit)\s*\)"
    r"|\[\s*(remix|acoustic|sped\s+up|slowed(\s*\+\s*reverb)?|mv)\s*\])",
    re.I,
)
_ARTIST_DASH_SONG_RE = re.compile(r"^.{2,45}\s+[-\u2013\u2014]\s+.{2,80}$")
_VIDEO_TITLE_RE = re.compile(
    r"(\b(ep\.?\s*\d+|episode\s*\d+|s\d{1,2}e\d{1,2}|part\s*\d+|pt\.?\s*\d+)\b"
    r"|\b(tutorial|walkthrough|playthrough|gameplay|let'?s\s+play|podcast|interview|review"
    r"|unboxing|vlog|reaction|reacts\s+to|trailer|teaser|highlights|compilation|documentary"
    r"|explained|breakdown|tier\s+list|speedrun|livestream|live\s+stream|vod|full\s+movie"
    r"|full\s+match|recap|video\s+essay|news|lecture|course|lesson|diy|q&a|ama|shorts?)\b"
    r"|^\s*(i\s+(tried|built|bought|survived|spent|tested|made|played|lived|went|got)|why\s+"
    r"|how\s+(to|i|we|they)\s+|top\s+\d+\b|\d+\s+(things|ways|tips|tricks|facts|reasons|mistakes)\b"
    r"|what\s+happens\s+if\b|testing\s+|building\s+))",
    re.I,
)


def _map_playback_type(val: Any) -> str:
    """Normalize OS media playback type into 'music', 'video', 'image', or 'unknown'."""
    if val is None:
        return "unknown"
    if isinstance(val, bool):
        return "unknown"
    if isinstance(val, str):
        low = val.strip().lower()
        if not low:
            return "unknown"
        if low in ("1", "music", "audio") or "music" in low or "audio" in low:
            return "music"
        if low in ("2", "video") or "video" in low:
            return "video"
        if low in ("3", "image") or "image" in low:
            return "image"
        return "unknown"
    with suppress(Exception):
        num = int(val)
        if num == 1:
            return "music"
        if num == 2:
            return "video"
        if num == 3:
            return "image"
    return "unknown"


def classify_media_kind(
    title: str,
    artist: str = "",
    album: str = "",
    source_app: str = "",
    duration_sec: float = 0.0,
    playback_type: Any = "unknown",
    genres: tuple[str, ...] | list[str] = (),
    media_url: str = "",
) -> tuple[str, str]:
    """Classify active playback as ('song', reason) or ('video', reason)."""
    t = str(title or "").strip()
    a = str(artist or "").strip()
    alb = str(album or "").strip()
    app = str(source_app or "").strip()
    dur = max(0.0, _safe_float(duration_sec, 0.0))
    ptype = _map_playback_type(playback_type)
    url_low = str(media_url or "").strip().lower()

    if _AUDIO_EXT_RE.search(t) or (url_low and _AUDIO_EXT_RE.search(url_low)):
        return "song", "audio_file_ext"
    if _VIDEO_EXT_RE.search(t) or (url_low and _VIDEO_EXT_RE.search(url_low)):
        return "video", "video_file_ext"

    if url_low and any(tok in url_low for tok in _MUSIC_URL_TOKENS):
        return "song", "music_url"

    if app in _MUSIC_ONLY_APPS:
        return "song", "music_app"

    has_real_album = bool(alb and alb.lower() not in _GENERIC_SITE_NAMES and alb.lower() != app.lower())
    is_topic_or_vevo = bool(_TOPIC_VEVO_RE.search(a))
    has_music_title_marker = bool(_MUSIC_TITLE_RE.search(t))
    has_artist_dash_song = bool(_ARTIST_DASH_SONG_RE.match(t))
    has_music_genre = bool([g for g in (genres or ()) if str(g).strip()])
    has_video_title_marker = bool(_VIDEO_TITLE_RE.search(t))

    if app in _VIDEO_ONLY_APPS:
        if ptype == "music" and has_real_album:
            return "song", "album_metadata"
        return "video", "video_app"

    if app in ("VLC", "Media Player"):
        if ptype == "video" or has_video_title_marker:
            return "video", "media_player_video"
        if ptype == "music" or has_real_album or has_music_genre:
            return "song", "media_player_music"
        if dur > 720:
            return "video", "long_media"
        return ("video", "media_player_default") if not a else ("song", "media_player_audio")

    if has_video_title_marker and not (is_topic_or_vevo or has_music_title_marker):
        return "video", "video_title_pattern"
    if is_topic_or_vevo:
        return "song", "topic_or_vevo_channel"
    if has_real_album:
        return "song", "album_metadata"
    if has_music_title_marker:
        return "song", "music_title_marker"

    if app in _BROWSER_APPS:
        if dur > 720:
            return "video", "long_browser_media"
        if dur == 0.0 or 0.0 < dur < 30.0:
            return "video", "browser_short_or_live"
        if has_artist_dash_song and 45.0 <= dur <= 600.0 and not has_video_title_marker:
            return "song", "artist_title_dash"
        if ptype == "music" and 30.0 <= dur <= 720.0:
            return "song", "browser_audio_playback"
        if ptype == "video":
            return "video", "browser_video_playback"
        if not a or a.lower() in _GENERIC_SITE_NAMES:
            return "video", "browser_generic_video"
        return "video", "browser_unclassified_video"

    if ptype == "video":
        return "video", "os_video_type"
    if has_video_title_marker:
        return "video", "video_title_pattern"
    if dur > 900 and not has_real_album:
        return "video", "long_non_music"
    return "song", "default_song"

_MACOS_POLL_APPLESCRIPT = r'''
on run
  set delim to tab
  if application "Spotify" is running then
    tell application "Spotify"
      set st to player state as string
      if st is "playing" or st is "paused" then
        set tName to name of current track as string
        set tArtist to artist of current track as string
        set tAlbum to album of current track as string
        set tPos to player position as string
        set tDur to duration of current track as string
        set tArt to ""
        try
          set tArt to artwork url of current track as string
        end try
        return "Spotify" & delim & st & delim & tName & delim & tArtist & delim & tAlbum & delim & tPos & delim & tDur & delim & tArt
      end if
    end tell
  end if
  if application "Music" is running then
    tell application "Music"
      set st to player state as string
      if st is "playing" or st is "paused" then
        set tName to name of current track as string
        set tArtist to artist of current track as string
        set tAlbum to album of current track as string
        set tPos to player position as string
        set tDur to duration of current track as string
        return "Apple Music" & delim & st & delim & tName & delim & tArtist & delim & tAlbum & delim & tPos & delim & tDur & delim & ""
      end if
    end tell
  end if
  return ""
end run
'''


def _default_snapshot() -> dict[str, Any]:
    return {
        "title": "",
        "artist": "",
        "album": "",
        "status": "stopped",
        "positionSec": 0.0,
        "durationSec": 0.0,
        "artworkDataUrl": "",
        "sourceApp": "",
        "playbackType": "unknown",
        "mediaKind": "song",
        "mediaKindReason": "",
    }


def _build_snapshot(
    *,
    title: str = "",
    artist: str = "",
    album: str = "",
    status: str = "stopped",
    position_sec: float = 0.0,
    duration_sec: float = 0.0,
    artwork_data_url: str = "",
    source_app: str = "",
    playback_type: Any = "unknown",
    genres: tuple[str, ...] | list[str] = (),
    media_url: str = "",
    art_url: Optional[str] = None,
) -> dict[str, Any]:
    dur = max(0.0, duration_sec)
    pos = max(0.0, position_sec)
    if dur > 0.0:
        pos = min(pos, dur)
    norm_ptype = _map_playback_type(playback_type)
    if title or artist:
        media_kind, media_reason = classify_media_kind(
            title=title,
            artist=artist,
            album=album,
            source_app=source_app,
            duration_sec=dur,
            playback_type=norm_ptype,
            genres=genres,
            media_url=media_url,
        )
    else:
        media_kind, media_reason = "song", ""
    snap: dict[str, Any] = {
        "title": title,
        "artist": artist,
        "album": album,
        "status": status,
        "positionSec": round(pos, 3),
        "durationSec": round(dur, 3),
        "artworkDataUrl": artwork_data_url,
        "sourceApp": source_app,
        "playbackType": norm_ptype,
        "mediaKind": media_kind,
        "mediaKindReason": media_reason,
    }
    if art_url is not None:
        snap["_artUrl"] = art_url
    return snap


_UNIX_EXTRA_BIN_DIRS: tuple[str, ...] = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
)


def _which_unix(name: str) -> Optional[str]:
    """Resolve a CLI binary even when launched from Finder/GUI with minimal PATH."""
    found = shutil.which(name)
    if found:
        return found
    if _IS_MACOS or _IS_LINUX:
        for directory in _UNIX_EXTRA_BIN_DIRS:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def _run_cmd(args: list[str], timeout: float = 1.5) -> Optional[subprocess.CompletedProcess[str]]:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=max(0.2, float(timeout)),
            check=False,
        )
    except Exception:
        return None


def _format_source_app(aumid: Optional[str]) -> str:
    if not aumid:
        return ""
    raw = str(aumid).strip()
    lower = raw.lower()
    for needle, label in _KNOWN_APP_NAMES:
        if needle in lower:
            return label

    cleaned = raw.split("!")[0].split("_")[0]
    cleaned = cleaned.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    if cleaned.lower().endswith(".exe"):
        cleaned = cleaned[:-4]
    if "." in cleaned:
        parts = [p for p in cleaned.split(".") if p]
        if parts:
            cleaned = parts[-1]
    cleaned = cleaned.strip()
    if not cleaned:
        return ""
    return cleaned[0].upper() + cleaned[1:] if cleaned.islower() else cleaned


def _timespan_to_seconds(ts: Any) -> float:
    if ts is None:
        return 0.0
    if isinstance(ts, timedelta):
        return ts.total_seconds()
    if isinstance(ts, (int, float)):
        return float(ts)
    duration = getattr(ts, "duration", None)
    return float(duration) / 10_000_000.0 if isinstance(duration, (int, float)) else 0.0


def _to_utc_datetime(dt: Any) -> Optional[datetime]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    universal_time = getattr(dt, "universal_time", None)
    if isinstance(universal_time, int) and universal_time > 0:
        with suppress(Exception):
            return _WIN32_EPOCH + timedelta(microseconds=universal_time / 10.0)
    return None


def _detect_image_mime(raw: bytes, fallback: str = "") -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if fallback and fallback.lower().startswith("image/"):
        return fallback.lower()
    return "image/jpeg"


def _encode_image_data_url(raw: bytes, fallback_mime: str = "") -> str:
    if not raw or len(raw) > _MAX_ARTWORK_BYTES:
        return ""
    mime = _detect_image_mime(raw, fallback=fallback_mime)
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _map_playback_status(status: Any) -> str:
    if _PlaybackStatus is not None:
        if status == _PlaybackStatus.PLAYING:
            return "playing"
        if status == _PlaybackStatus.PAUSED:
            return "paused"
    if isinstance(status, str):
        low = status.strip().lower()
        return low if low in ("playing", "paused") else "stopped"
    with suppress(Exception):
        val = int(status)
        if val == 4:
            return "playing"
        if val == 5:
            return "paused"
    return "stopped"


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        num = float(str(val).strip())
        return default if (num != num or num in (float("inf"), float("-inf"))) else num
    except Exception:
        return default


def _load_art_url_to_data_url(art_url: str, timeout: float = 2.5) -> str:
    url = str(art_url or "").strip()
    if not url:
        return ""
    if url.startswith("data:image/"):
        return url

    raw = b""
    content_type = ""
    try:
        parsed = urlparse(url)
        if parsed.scheme == "file":
            local_path = Path(unquote(parsed.path))
            if local_path.is_file() and local_path.stat().st_size <= _MAX_ARTWORK_BYTES:
                raw = local_path.read_bytes()
        elif parsed.scheme in ("http", "https"):
            req = urllib.request.Request(url, headers={"User-Agent": "IDE-Lyrics-Studio/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content_type = str(resp.headers.get("Content-Type") or "").split(";")[0].strip()
                data = resp.read(_MAX_ARTWORK_BYTES + 1)
                if len(data) <= _MAX_ARTWORK_BYTES:
                    raw = data
        else:
            local_path = Path(url)
            if local_path.is_file() and local_path.stat().st_size <= _MAX_ARTWORK_BYTES:
                raw = local_path.read_bytes()
    except Exception:
        return ""

    return _encode_image_data_url(raw, fallback_mime=content_type)


def _split_padded(text: str, sep: str, count: int) -> list[str]:
    parts = text.split(sep)
    if len(parts) < count:
        parts.extend([""] * (count - len(parts)))
    return parts


def _parse_playerctl_output(raw_output: str) -> dict[str, Any]:
    text = str(raw_output or "").strip()
    if not text:
        return _default_snapshot()

    # Fields: status, player, title, artist, album, position_us, length_us, art_url, [url, genre]
    parts = _split_padded(text, "\t", 10)
    status = _map_playback_status(parts[0])
    title = parts[2].strip()
    artist = parts[3].strip()
    raw_genre = parts[9].strip()
    genres = tuple(g.strip() for g in raw_genre.split(",") if g.strip()) if raw_genre else ()
    return _build_snapshot(
        title=title,
        artist=artist,
        album=parts[4].strip(),
        status=status if (title or artist) else "stopped",
        position_sec=_safe_float(parts[5]) / 1_000_000.0,
        duration_sec=_safe_float(parts[6]) / 1_000_000.0,
        source_app=_format_source_app(parts[1]),
        genres=genres,
        media_url=parts[8].strip(),
        art_url=parts[7].strip(),
    )


def _parse_media_control_output(raw_output: str) -> dict[str, Any]:
    """Parse JSON emitted by `media-control get` (macOS 15.4+ MediaRemote adapter)."""
    text = str(raw_output or "").strip()
    if not text or text == "null":
        return _default_snapshot()

    try:
        data = json.loads(text)
    except Exception:
        return _default_snapshot()

    if not isinstance(data, dict):
        return _default_snapshot()

    title = str(data.get("title") or "").strip()
    artist = str(data.get("artist") or "").strip()
    if not title and not artist:
        return _default_snapshot()

    album = str(data.get("album") or "").strip()
    playing_flag = data.get("playing")
    if isinstance(playing_flag, bool):
        status = "playing" if playing_flag else "paused"
    else:
        status = "playing" if _safe_float(data.get("playbackRate")) > 0.0 else "paused"

    elapsed_raw = data.get("elapsedTimeNow")
    if elapsed_raw is None:
        elapsed_raw = data.get("elapsedTime")
    elapsed = _safe_float(elapsed_raw)
    duration = _safe_float(data.get("duration"))

    app_raw = (
        data.get("bundleIdentifier")
        or data.get("clientProcessName")
        or data.get("appName")
        or ""
    )
    source_app = _format_source_app(str(app_raw)) or "macOS Media"

    artwork_data_url = ""
    art_b64 = str(data.get("artworkData") or "").strip()
    if art_b64:
        if art_b64.startswith("data:image/"):
            artwork_data_url = art_b64
        else:
            art_mime = str(data.get("artworkMimeType") or "").strip()
            with suppress(Exception):
                artwork_data_url = _encode_image_data_url(
                    base64.b64decode(art_b64, validate=False),
                    fallback_mime=art_mime,
                )

    return _build_snapshot(
        title=title,
        artist=artist,
        album=album,
        status=status,
        position_sec=elapsed,
        duration_sec=duration,
        artwork_data_url=artwork_data_url,
        source_app=source_app,
        playback_type=data.get("mediaType") if data.get("mediaType") is not None else ("music" if data.get("isMusicApp") is True else "unknown"),
    )


def _parse_nowplaying_output(raw_output: str) -> dict[str, Any]:
    lines = [line.strip() for line in str(raw_output or "").splitlines()]
    if len(lines) < 6:
        return _default_snapshot()

    def _clean(val: str) -> str:
        return "" if val in ("null", "(null)", "nil", "None") else val

    title = _clean(lines[0])
    artist = _clean(lines[1])
    if not title and not artist:
        return _default_snapshot()

    album = _clean(lines[2])
    rate = _safe_float(_clean(lines[3]))
    elapsed = _safe_float(_clean(lines[4]))
    duration = _safe_float(_clean(lines[5]))
    app_name = _format_source_app(_clean(lines[6])) if len(lines) > 6 else ""
    art_b64 = _clean(lines[7]) if len(lines) > 7 else ""

    artwork_data_url = ""
    if art_b64:
        with suppress(Exception):
            artwork_data_url = _encode_image_data_url(base64.b64decode(art_b64, validate=False))

    return _build_snapshot(
        title=title,
        artist=artist,
        album=album,
        status="playing" if rate > 0.0 else "paused",
        position_sec=elapsed,
        duration_sec=duration,
        artwork_data_url=artwork_data_url,
        source_app=app_name or "macOS Media",
    )


def _parse_applescript_output(raw_output: str) -> dict[str, Any]:
    text = str(raw_output or "").strip()
    if not text:
        return _default_snapshot()

    parts = _split_padded(text, "\t", 8)
    title = parts[2].strip()
    artist = parts[3].strip()
    if not title and not artist:
        return _default_snapshot()

    duration_sec = max(0.0, _safe_float(parts[6]))
    # Spotify AppleScript reports track duration in milliseconds.
    if duration_sec > 10_000.0:
        duration_sec /= 1000.0

    return _build_snapshot(
        title=title,
        artist=artist,
        album=parts[4].strip(),
        status=_map_playback_status(parts[1]),
        position_sec=_safe_float(parts[5]),
        duration_sec=duration_sec,
        source_app=_format_source_app(parts[0]),
        art_url=parts[7].strip(),
    )


def _extrapolate_position_sec(
    raw_pos: float,
    status: str,
    last_updated: Optional[datetime],
    now_utc: datetime,
    anchor: Optional[tuple[tuple[str, str, str], float, float]],
    track_key: tuple[str, str, str],
    mono_now: float,
) -> tuple[float, Optional[tuple[tuple[str, str, str], float, float]]]:
    """Advance WinRT SMTC playback timestamps smoothly when players throttle updates."""
    base_pos = max(0.0, float(raw_pos))
    if status != "playing":
        return base_pos, None

    rounded_raw = round(base_pos, 3)
    same_anchor = bool(
        anchor is not None
        and anchor[0] == track_key
        and abs(anchor[1] - rounded_raw) < 0.002
    )
    anchor_pos = base_pos + max(0.0, mono_now - anchor[2]) if (same_anchor and anchor is not None) else base_pos

    if last_updated is not None and last_updated.year >= 2000:
        elapsed = (now_utc - last_updated).total_seconds()
        if -1.0 <= elapsed < 86400.0:
            extrapolated = base_pos + max(0.0, elapsed)
            if elapsed <= 0.0:
                if same_anchor and anchor is not None:
                    return max(extrapolated, anchor_pos), anchor
                return extrapolated, (track_key, rounded_raw, mono_now)
            return extrapolated, (track_key, rounded_raw, mono_now - max(0.0, elapsed))

    if same_anchor and anchor is not None:
        return anchor_pos, anchor
    return base_pos, (track_key, rounded_raw, mono_now)


class SMTCBridge:
    """Polls the OS media session for live track metadata, timeline, and artwork."""

    def __init__(
        self,
        on_update: Optional[Callable[[dict[str, Any]], None]] = None,
        interval: float = 0.15,
    ) -> None:
        self._callback = on_update
        self._interval = max(0.08, float(interval))
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._manager: Any = None
        self._snapshot: dict[str, Any] = _default_snapshot()
        self._artwork_cache: OrderedDict[tuple[str, str, str, str], str] = OrderedDict()
        self._last_emitted_key: Optional[tuple[Any, ...]] = None
        self._winrt_clock_anchor: Optional[tuple[tuple[str, str, str], float, float]] = None

    @property
    def available(self) -> bool:
        if _IS_WINDOWS:
            return _WINRT_AVAILABLE
        if _IS_MACOS:
            return bool(
                _which_unix("media-control")
                or _which_unix("nowplaying-cli")
                or _which_unix("osascript")
            )
        if _IS_LINUX:
            return bool(_which_unix("playerctl") or _which_unix("gdbus"))
        return False

    def get_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)

    def set_poll_interval(self, interval: float) -> float:
        """Thread-safely update the polling interval (clamped to >= 0.08s)."""
        if isinstance(interval, bool):
            with self._lock:
                return self._interval
        try:
            val = float(interval)
        except Exception:
            with self._lock:
                return self._interval
        if not math.isfinite(val):
            with self._lock:
                return self._interval
        clamped = max(0.08, val)
        with self._lock:
            self._interval = clamped
        return clamped

    def _get_poll_interval(self) -> float:
        with self._lock:
            return self._interval

    def start(
        self,
        callback: Optional[Callable[[dict[str, Any]], None]] = None,
        interval: Optional[float] = None,
    ) -> None:
        if callback is not None:
            self._callback = callback
        if interval is not None:
            self.set_poll_interval(interval)

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_thread,
                name="SMTCBridgeThread",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _cache_get(self, key: tuple[str, str, str, str]) -> Optional[str]:
        if key not in self._artwork_cache:
            return None
        self._artwork_cache.move_to_end(key)
        return self._artwork_cache[key]

    def _cache_put(self, key: tuple[str, str, str, str], data_url: str) -> None:
        if not data_url:
            return
        self._artwork_cache[key] = data_url
        self._artwork_cache.move_to_end(key)
        while len(self._artwork_cache) > _ARTWORK_CACHE_LIMIT:
            self._artwork_cache.popitem(last=False)

    def _run_thread(self) -> None:
        if _IS_WINDOWS:
            if not _WINRT_AVAILABLE:
                return
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            with self._lock:
                self._loop = loop
            try:
                loop.run_until_complete(self._poll_loop())
            except Exception:
                pass
            finally:
                with self._lock:
                    self._loop = None
                    self._manager = None
                with suppress(Exception):
                    loop.close()
            return

        if _IS_MACOS or _IS_LINUX:
            self._poll_loop_unix()

    def _poll_loop_unix(self) -> None:
        while not self._stop_event.is_set():
            try:
                snap = self._read_snapshot_macos() if _IS_MACOS else self._read_snapshot_linux()
            except Exception:
                snap = _default_snapshot()

            art_url = str(snap.pop("_artUrl", "") or "").strip()
            if not snap.get("artworkDataUrl") and art_url and (snap.get("title") or snap.get("artist")):
                track_key = (
                    str(snap.get("sourceApp", "")),
                    str(snap.get("title", "")),
                    str(snap.get("artist", "")),
                    str(snap.get("album", "")),
                )
                cached = self._cache_get(track_key)
                if cached is not None:
                    snap["artworkDataUrl"] = cached
                else:
                    loaded = _load_art_url_to_data_url(art_url)
                    if loaded:
                        self._cache_put(track_key, loaded)
                        snap["artworkDataUrl"] = loaded

            with self._lock:
                self._snapshot = snap
            self._maybe_emit(snap)

            elapsed = 0.0
            while not self._stop_event.is_set():
                target = self._get_poll_interval()
                if elapsed >= target:
                    break
                step = min(0.1, max(0.01, target - elapsed))
                if self._stop_event.wait(step):
                    break
                elapsed += step

    def _read_snapshot_macos(self) -> dict[str, Any]:
        mc_bin = _which_unix("media-control")
        if mc_bin:
            proc = _run_cmd([mc_bin, "get"], timeout=1.5)
            if proc and proc.returncode == 0 and proc.stdout.strip():
                snap = _parse_media_control_output(proc.stdout)
                if snap.get("title") or snap.get("artist"):
                    return snap

        np_bin = _which_unix("nowplaying-cli")
        if np_bin:
            proc = _run_cmd(
                [
                    np_bin,
                    "get",
                    "title",
                    "artist",
                    "album",
                    "playbackRate",
                    "elapsedTime",
                    "duration",
                    "clientProcessName",
                    "artworkData",
                ],
                timeout=1.5,
            )
            if proc and proc.returncode == 0 and proc.stdout.strip():
                snap = _parse_nowplaying_output(proc.stdout)
                if snap.get("title") or snap.get("artist"):
                    return snap

        osa_bin = _which_unix("osascript")
        if osa_bin:
            proc = _run_cmd([osa_bin, "-e", _MACOS_POLL_APPLESCRIPT], timeout=1.5)
            if proc and proc.returncode == 0 and proc.stdout.strip():
                return _parse_applescript_output(proc.stdout)

        return _default_snapshot()

    def _read_snapshot_linux(self) -> dict[str, Any]:
        pctl = _which_unix("playerctl")
        if pctl:
            fmt = (
                "{{status}}\t{{playerName}}\t{{title}}\t{{artist}}\t{{album}}\t"
                "{{position}}\t{{mpris:length}}\t{{mpris:artUrl}}\t{{xesam:url}}\t{{xesam:genre}}"
            )
            proc = _run_cmd([pctl, "metadata", "--format", fmt], timeout=1.5)
            if proc and proc.returncode == 0 and proc.stdout.strip():
                return _parse_playerctl_output(proc.stdout)

        gdbus = _which_unix("gdbus")
        return self._read_snapshot_gdbus(gdbus) if gdbus else _default_snapshot()

    @staticmethod
    def _gdbus_call(
        gdbus: str,
        dest: str,
        obj_path: str,
        method: str,
        *extra_args: str,
        timeout: float = 1.5,
    ) -> Optional[subprocess.CompletedProcess[str]]:
        return _run_cmd(
            [
                gdbus,
                "call",
                "--session",
                "--dest",
                dest,
                "--object-path",
                obj_path,
                "--method",
                method,
                *extra_args,
            ],
            timeout=timeout,
        )

    def _find_gdbus_mpris_bus(self, gdbus: str) -> str:
        proc = self._gdbus_call(
            gdbus,
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus.ListNames",
        )
        if not proc or proc.returncode != 0:
            return ""
        buses = re.findall(r"'(org\.mpris\.MediaPlayer2\.[^']+)'", proc.stdout)
        if not buses:
            return ""
        for bus in buses:
            st_proc = self._gdbus_call(
                gdbus,
                bus,
                "/org/mpris/MediaPlayer2",
                "org.freedesktop.DBus.Properties.Get",
                "org.mpris.MediaPlayer2.Player",
                "PlaybackStatus",
                timeout=1.0,
            )
            if st_proc and "Playing" in st_proc.stdout:
                return bus
        return buses[0]

    def _read_snapshot_gdbus(self, gdbus: str) -> dict[str, Any]:
        bus = self._find_gdbus_mpris_bus(gdbus)
        if not bus:
            return _default_snapshot()

        proc = self._gdbus_call(
            gdbus,
            bus,
            "/org/mpris/MediaPlayer2",
            "org.freedesktop.DBus.Properties.GetAll",
            "org.mpris.MediaPlayer2.Player",
        )
        if not proc or proc.returncode != 0 or not proc.stdout:
            return _default_snapshot()
        out = proc.stdout

        def _extract_str(key: str) -> str:
            m = re.search(
                rf"'{re.escape(key)}':\s*<(?:@as\s*)?\[?\s*(['\"])((?:\\.|(?!\1)[^\\])*)\1",
                out,
            )
            if not m:
                return ""
            escapes = {
                "n": "\n",
                "r": "\r",
                "t": "\t",
                "b": "\b",
                "f": "\f",
                "\\": "\\",
                "'": "'",
                '"': '"',
            }
            val = re.sub(r"\\(.)", lambda em: escapes.get(em.group(1), em.group(1)), m.group(2))
            return val.strip()

        def _extract_int(key: str) -> float:
            m = re.search(rf"'{re.escape(key)}':\s*<(?:int64|uint64)?\s*(-?\d+)>", out)
            return _safe_float(m.group(1)) if m else 0.0

        title = _extract_str("xesam:title")
        artist = _extract_str("xesam:artist")
        if not title and not artist:
            return _default_snapshot()

        player_name = bus.split("org.mpris.MediaPlayer2.", 1)[-1].split(".")[0]
        raw_genre = _extract_str("xesam:genre")
        genres = tuple(g.strip() for g in raw_genre.split(",") if g.strip()) if raw_genre else ()
        return _build_snapshot(
            title=title,
            artist=artist,
            album=_extract_str("xesam:album"),
            status=_map_playback_status(_extract_str("PlaybackStatus")),
            position_sec=_extract_int("Position") / 1_000_000.0,
            duration_sec=_extract_int("mpris:length") / 1_000_000.0,
            source_app=_format_source_app(player_name),
            genres=genres,
            media_url=_extract_str("xesam:url"),
            art_url=_extract_str("mpris:artUrl"),
        )

    def send_command(self, cmd: str, timeout: float = 2.0) -> bool:
        """Send 'play_pause', 'next', or 'previous' to the active media session."""
        normalized = str(cmd or "").strip().lower()
        if normalized not in _VALID_MEDIA_COMMANDS:
            return False

        if _IS_MACOS:
            return self._send_command_macos(normalized, timeout=timeout)
        if _IS_LINUX:
            return self._send_command_linux(normalized, timeout=timeout)

        if not _WINRT_AVAILABLE:
            return False

        with self._lock:
            loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            return False

        try:
            fut = asyncio.run_coroutine_threadsafe(self._send_command_async(normalized), loop)
            return bool(fut.result(timeout=max(0.1, float(timeout))))
        except Exception:
            return False

    def _send_command_macos(self, cmd: str, timeout: float = 2.0) -> bool:
        mc_bin = _which_unix("media-control")
        if mc_bin:
            mc_map = {
                "play_pause": "toggle-play-pause",
                "next": "next-track",
                "previous": "previous-track",
            }
            proc = _run_cmd([mc_bin, mc_map[cmd]], timeout=timeout)
            if proc and proc.returncode == 0:
                return True

        np_bin = _which_unix("nowplaying-cli")
        if np_bin:
            np_map = {"play_pause": "togglePlayPause", "next": "next", "previous": "previous"}
            proc = _run_cmd([np_bin, np_map[cmd]], timeout=timeout)
            if proc and proc.returncode == 0:
                return True

        osa_bin = _which_unix("osascript")
        if osa_bin:
            osa_map = {"play_pause": "playpause", "next": "next track", "previous": "previous track"}
            action = osa_map[cmd]
            script = (
                'if application "Spotify" is running then\n'
                f'  tell application "Spotify" to {action}\n'
                'else if application "Music" is running then\n'
                f'  tell application "Music" to {action}\n'
                "end if"
            )
            proc = _run_cmd([osa_bin, "-e", script], timeout=timeout)
            return bool(proc and proc.returncode == 0)
        return False

    def _send_command_linux(self, cmd: str, timeout: float = 2.0) -> bool:
        pctl = _which_unix("playerctl")
        if pctl:
            pctl_map = {"play_pause": "play-pause", "next": "next", "previous": "previous"}
            proc = _run_cmd([pctl, pctl_map[cmd]], timeout=timeout)
            if proc and proc.returncode == 0:
                return True

        gdbus = _which_unix("gdbus")
        if gdbus:
            bus = self._find_gdbus_mpris_bus(gdbus)
            if not bus:
                return False
            method_map = {"play_pause": "PlayPause", "next": "Next", "previous": "Previous"}
            proc = self._gdbus_call(
                gdbus,
                bus,
                "/org/mpris/MediaPlayer2",
                f"org.mpris.MediaPlayer2.Player.{method_map[cmd]}",
                timeout=timeout,
            )
            return bool(proc and proc.returncode == 0)
        return False

    async def _send_command_async(self, cmd: str) -> bool:
        if not _WINRT_AVAILABLE or _SessionManager is None:
            return False
        try:
            with self._lock:
                manager = self._manager
            if manager is None:
                manager = await _SessionManager.request_async()
                with self._lock:
                    self._manager = manager
            session = self._select_session(manager)
            if session is None:
                return False
            method_name = {
                "play_pause": "try_toggle_play_pause_async",
                "next": "try_skip_next_async",
                "previous": "try_skip_previous_async",
            }.get(cmd)
            if not method_name:
                return False
            return bool(await getattr(session, method_name)())
        except Exception:
            return False

    async def _poll_loop(self) -> None:
        manager = None
        while not self._stop_event.is_set():
            try:
                if manager is None:
                    manager = await _SessionManager.request_async()
                    with self._lock:
                        self._manager = manager
                snap = await self._read_snapshot(manager)
            except Exception:
                manager = None
                with self._lock:
                    self._manager = None
                snap = _default_snapshot()

            with self._lock:
                self._snapshot = snap
            self._maybe_emit(snap)

            elapsed = 0.0
            while not self._stop_event.is_set():
                target = self._get_poll_interval()
                if elapsed >= target:
                    break
                step = min(0.1, max(0.01, target - elapsed))
                await asyncio.sleep(step)
                elapsed += step

    def _maybe_emit(self, snap: dict[str, Any]) -> None:
        cb = self._callback
        if cb is None:
            return

        # Always emit while playing so the lyrics clock stays smooth; when paused
        # or stopped, only emit on metadata or position changes.
        emit_key = (
            snap.get("title"),
            snap.get("artist"),
            snap.get("album"),
            snap.get("status"),
            round(float(snap.get("positionSec", 0.0)), 2),
            round(float(snap.get("durationSec", 0.0)), 2),
            snap.get("sourceApp"),
            snap.get("mediaKind"),
            bool(snap.get("artworkDataUrl")),
        )
        if snap.get("status") != "playing" and emit_key == self._last_emitted_key:
            return
        self._last_emitted_key = emit_key

        with suppress(Exception):
            cb(dict(snap))

    @staticmethod
    def _session_status(session: Any) -> str:
        with suppress(Exception):
            info = session.get_playback_info()
            if info is not None:
                return _map_playback_status(info.playback_status)
        return "stopped"

    def _select_session(self, manager: Any) -> Any:
        if manager is None:
            return None

        current = None
        with suppress(Exception):
            current = manager.get_current_session()

        if current is not None and self._session_status(current) == "playing":
            return current

        with suppress(Exception):
            sessions = manager.get_sessions()
            if sessions:
                for sess in sessions:
                    if self._session_status(sess) == "playing":
                        return sess
                if current is None:
                    return sessions[0]

        return current

    async def _read_snapshot(self, manager: Any) -> dict[str, Any]:
        session = self._select_session(manager)
        if session is None:
            return _default_snapshot()

        source_app = ""
        with suppress(Exception):
            source_app = _format_source_app(session.source_app_user_model_id)

        status = self._session_status(session)

        title = artist = album = ""
        thumbnail_ref = None
        playback_type: Any = "unknown"
        genres: tuple[str, ...] = ()
        with suppress(Exception):
            props = await session.try_get_media_properties_async()
            if props is not None:
                title = (props.title or "").strip()
                artist = (props.artist or "").strip()
                album = (props.album_title or "").strip()
                thumbnail_ref = props.thumbnail
                with suppress(Exception):
                    playback_type = props.playback_type
                with suppress(Exception):
                    raw_genres = props.genres
                    if raw_genres is not None:
                        genres = tuple(str(g).strip() for g in raw_genres if str(g).strip())

        if _map_playback_type(playback_type) == "unknown":
            with suppress(Exception):
                pb_info = session.get_playback_info()
                if pb_info is not None:
                    playback_type = getattr(pb_info, "playback_type", None)

        position_sec = duration_sec = 0.0
        with suppress(Exception):
            timeline = session.get_timeline_properties()
            if timeline is not None:
                start_sec = _timespan_to_seconds(getattr(timeline, "start_time", None))
                end_sec = _timespan_to_seconds(getattr(timeline, "end_time", None))
                raw_pos = _timespan_to_seconds(getattr(timeline, "position", None))
                if end_sec > start_sec:
                    duration_sec = end_sec - start_sec
                    position_sec = max(0.0, raw_pos - start_sec)
                else:
                    duration_sec = max(0.0, end_sec)
                    position_sec = max(0.0, raw_pos)

                if status == "playing":
                    last_updated = _to_utc_datetime(getattr(timeline, "last_updated_time", None))
                    track_sig = (source_app, title, artist)
                    position_sec, self._winrt_clock_anchor = _extrapolate_position_sec(
                        position_sec,
                        status,
                        last_updated,
                        datetime.now(timezone.utc),
                        self._winrt_clock_anchor,
                        track_sig,
                        time.monotonic(),
                    )
                else:
                    self._winrt_clock_anchor = None

        artwork_data_url = ""
        if title or artist:
            track_key = (source_app, title, artist, album)
            cached = self._cache_get(track_key)
            if cached is not None:
                artwork_data_url = cached
            elif thumbnail_ref is not None:
                artwork_data_url = await self._read_thumbnail_data_url(thumbnail_ref)
                self._cache_put(track_key, artwork_data_url)

        return _build_snapshot(
            title=title,
            artist=artist,
            album=album,
            status=status,
            position_sec=position_sec,
            duration_sec=duration_sec,
            artwork_data_url=artwork_data_url,
            source_app=source_app,
            playback_type=playback_type,
            genres=genres,
        )

    async def _read_thumbnail_data_url(self, thumbnail_ref: Any) -> str:
        stream = None
        try:
            stream = await thumbnail_ref.open_read_async()
            if stream is None:
                return ""
            size = int(getattr(stream, "size", 0) or 0)
            if size <= 0 or size > _MAX_ARTWORK_BYTES:
                return ""

            raw = b""
            if _WinRTBuffer is not None and _InputStreamOptions is not None:
                with suppress(Exception):
                    chunks: list[bytes] = []
                    collected = 0
                    while collected < size:
                        remaining = size - collected
                        buf = _WinRTBuffer(remaining)
                        read_buf = await stream.read_async(buf, remaining, _InputStreamOptions.READ_AHEAD)
                        chunk = bytes(read_buf) if read_buf is not None else b""
                        if not chunk:
                            break
                        chunks.append(chunk)
                        collected += len(chunk)
                    raw = b"".join(chunks)

            if len(raw) < size and _DataReader is not None:
                with suppress(Exception):
                    stream.seek(0)
                    reader = _DataReader(stream)
                    dr_chunks: list[bytes] = []
                    dr_collected = 0
                    while dr_collected < size:
                        loaded = int(await reader.load_async(size - dr_collected) or 0)
                        if loaded <= 0:
                            break
                        arr = bytearray(loaded)
                        reader.read_bytes(arr)
                        dr_chunks.append(bytes(arr))
                        dr_collected += loaded
                    dr_raw = b"".join(dr_chunks)
                    if len(dr_raw) > len(raw):
                        raw = dr_raw

            content_type = str(getattr(stream, "content_type", "") or "")
            return _encode_image_data_url(raw, fallback_mime=content_type)
        except Exception:
            return ""
        finally:
            if stream is not None:
                with suppress(Exception):
                    stream.close()
