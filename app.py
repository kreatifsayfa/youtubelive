#!/usr/bin/env python3
"""
YouTube Live Streamer
A simple Flask application to stream to YouTube using FFmpeg.
Supports M3U8 URLs, video files, and direct YouTube streaming.
"""

import os
import subprocess
import signal
import uuid
import re
import shutil
import threading
import queue
import time
import json
import base64
import tempfile
from collections import deque
from datetime import datetime, timedelta
from urllib.parse import urlparse, urljoin, quote_plus
from flask import Flask, render_template, request, jsonify, Response
from werkzeug.utils import secure_filename
from m3u_parser import M3UParser
import requests

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max file size
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['ALLOWED_EXTENSIONS'] = {'mp4', 'mkv', 'mov', 'avi', 'flv', 'wmv', 'webm', 'm3u8'}

# Ensure upload folder exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Store active streams
active_streams = {}

BITRATE_MAP = {
    '720p': '4000k',
    '1080p': '6800k',
    '1440p': '13000k',
    '2160p': '24000k'
}

QUALITY_PRESETS = {
    '720p':  {'width': 1280, 'height': 720,  'bitrate': '4000k',  'fps': 30},
    '1080p': {'width': 1920, 'height': 1080, 'bitrate': '6800k',  'fps': 30},
    '1440p': {'width': 2560, 'height': 1440, 'bitrate': '13000k', 'fps': 30},
    '2160p': {'width': 3840, 'height': 2160, 'bitrate': '24000k', 'fps': 30},
}

# x264 preset for the re-encode. Slower = better quality at the same bitrate but
# more CPU. Default stays CPU-safe ('veryfast'); bump to 'faster'/'fast' if the
# host has spare cores. The big quality wins (B-frames, lookahead, 2s GOP) come
# from dropping -tune zerolatency below, which costs little CPU.
H264_PRESET = os.environ.get('H264_PRESET', 'veryfast')

DEFAULT_INPUT_USER_AGENT = os.environ.get(
    'STREAM_INPUT_USER_AGENT',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
)

STANDBY_TEXT = os.environ.get('STANDBY_TEXT', 'Yayin yeniden baglaniyor...')
STANDBY_BG_COLOR = os.environ.get('STANDBY_BG_COLOR', '#0f1419')
STANDBY_DIR = os.path.join(tempfile.gettempdir(), 'youtubelive_standby')
STANDBY_LOCK = threading.Lock()
STANDBY_CACHE = {}

# How long the source may go without delivering a byte before we fail over to
# standby. With -re the source emits a continuous 1x trickle, so a few seconds
# of margin (covers B-frame/lookahead buffering and brief HLS reload pauses)
# avoids false failovers without making a real outage linger.
SOURCE_DATA_TIMEOUT_SECONDS = float(os.environ.get('SOURCE_DATA_TIMEOUT', '4.0'))
SOURCE_STALL_TIMEOUT_SECONDS = float(os.environ.get('SOURCE_STALL_TIMEOUT', '12.0'))
# Initial warm-up grace BEFORE the source emits its first byte. A live YouTube
# HLS feeder must fetch the manifest, download the first segments and prime the
# x264 encoder, which routinely takes longer than the mid-stream stall budget.
# Killing it too early traps the stream permanently on the standby feed.
SOURCE_START_TIMEOUT_SECONDS = float(os.environ.get('SOURCE_START_TIMEOUT', '30.0'))
# How long a resolved YouTube (googlevideo) URL is trusted before a restart
# triggers a fresh yt-dlp resolve. These URLs stay valid for hours, so reusing
# the cached command on transient restarts avoids stalling the relay with a
# blocking yt-dlp round-trip on every flap.
YT_URL_REFRESH_SECONDS = float(os.environ.get('YT_URL_REFRESH_SECONDS', '1800'))
# Force a fresh resolve after this many consecutive restart attempts even if the
# cached URL is still "young" (covers streams whose tokens expire early).
YT_REFRESH_AFTER_RESTARTS = int(os.environ.get('YT_REFRESH_AFTER_RESTARTS', '3'))
SOURCE_BACKOFF_INITIAL = float(os.environ.get('SOURCE_BACKOFF_INITIAL', '1.0'))
SOURCE_BACKOFF_MAX = float(os.environ.get('SOURCE_BACKOFF_MAX', '20.0'))
PUSHER_BACKOFF_INITIAL = float(os.environ.get('PUSHER_BACKOFF_INITIAL', '1.0'))
PUSHER_BACKOFF_MAX = float(os.environ.get('PUSHER_BACKOFF_MAX', '15.0'))
RELAY_CHUNK_SIZE = int(os.environ.get('RELAY_CHUNK_SIZE', '65536'))
SUPERVISOR_SELECT_TIMEOUT = float(os.environ.get('SUPERVISOR_SELECT_TIMEOUT', '0.4'))
# Bounded buffer (in relay chunks) for an optional secondary RTMP push (e.g.
# simultaneous TikTok). A dedicated writer thread drains it, so if the secondary
# destination stalls the queue fills and we DROP chunks for it rather than
# blocking the relay — the primary (YouTube) push is never slowed by a slow
# secondary. ~64 * 64KB ≈ 4 MB ≈ a few seconds of headroom.
PUSHER2_QUEUE_MAX = int(os.environ.get('PUSHER2_QUEUE_MAX', '64'))

# HLS proxy: required for some IPTV providers (cookie/referer needs) but breaks
# YouTube's 3-tier HLS structure (master -> per-quality -> segments). Default
# YouTube to direct fetch.
HLS_PROXY_FOR_YOUTUBE = os.environ.get('HLS_PROXY_FOR_YOUTUBE', 'false').lower() == 'true'
HLS_PROXY_DISABLED = os.environ.get('HLS_PROXY_DISABLED', 'false').lower() == 'true'

HLS_PROXY_COOKIE_JAR = {}
HLS_PROXY_LOCK = threading.Lock()


def _resolve_drawtext_font():
    """Find a system font usable for FFmpeg drawtext filter."""
    candidates = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/TTF/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
        '/usr/share/fonts/liberation/LiberationSans-Bold.ttf',
        '/usr/share/fonts/google-noto/NotoSans-Bold.ttf',
        '/usr/share/fonts/noto/NotoSans-Bold.ttf',
        '/System/Library/Fonts/Helvetica.ttc',
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _sanitize_color(value, default='black'):
    """Return a value safe to pass to FFmpeg color= filter."""
    if not value:
        return default
    value = value.strip()
    if value.startswith('#'):
        if re.fullmatch(r'#[0-9A-Fa-f]{6}', value):
            return value
        return default
    if re.fullmatch(r'[0-9A-Fa-f]{6}', value):
        return f'#{value}'
    if re.fullmatch(r'[A-Za-z]+', value):
        return value
    return default


def ensure_standby_video(quality='1080p', text=None):
    """Generate (and cache) a standby/loop MP4 used when the source is down.

    The file is encoded to match the source feeder's output (same resolution,
    fps, codec settings) so that the pusher can switch between the two with
    minimal disruption to the YouTube RTMP stream.
    """
    preset = QUALITY_PRESETS.get(quality, QUALITY_PRESETS['1080p'])
    raw_text = text or STANDBY_TEXT or 'Yayin yeniden baglaniyor...'
    safe_text = re.sub(r"[\\:'\"]+", ' ', raw_text).strip() or 'Yayin yeniden baglaniyor...'

    os.makedirs(STANDBY_DIR, exist_ok=True)
    # Include rendering-affecting inputs in the cache key so flipping
    # STANDBY_BG_COLOR or swapping fonts actually invalidates the cached MP4.
    cache_font = _resolve_drawtext_font() or 'no-font'
    cache_key = f"{quality}|{safe_text}|{STANDBY_BG_COLOR}|{cache_font}"

    with STANDBY_LOCK:
        cached = STANDBY_CACHE.get(cache_key)
        if cached and os.path.exists(cached) and os.path.getsize(cached) > 0:
            return cached

        safe_name = re.sub(r'[^A-Za-z0-9_-]', '_', f"{quality}_{safe_text}")[:48]
        path = os.path.join(STANDBY_DIR, f"standby_{safe_name}.mp4")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            STANDBY_CACHE[cache_key] = path
            return path

        bg_color = _sanitize_color(STANDBY_BG_COLOR, default='#0f1419')
        font_path = _resolve_drawtext_font()
        font_size = max(36, preset['height'] // 14)
        bitrate = preset['bitrate']

        video_filter_parts = [
            f"color=c={bg_color}:s={preset['width']}x{preset['height']}:r={preset['fps']}:d=10"
        ]
        if font_path:
            video_filter_parts.append(
                f"drawtext=fontfile='{font_path}':text='{safe_text}':"
                f"fontcolor=white:fontsize={font_size}:"
                f"x=(w-text_w)/2:y=(h-text_h)/2"
            )
            video_filter_parts.append(
                f"drawtext=fontfile='{font_path}':text='%{{localtime}}':"
                f"fontcolor=0xaaaaaa:fontsize={max(20, font_size // 2)}:"
                f"x=(w-text_w)/2:y=(h-text_h)/2+{font_size + 24}"
            )
        video_filter = ','.join(video_filter_parts)
        audio_filter = "anullsrc=channel_layout=stereo:sample_rate=44100"

        base_cmd = [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', video_filter,
            '-f', 'lavfi', '-t', '10', '-i', audio_filter,
            '-c:v', 'libx264', '-preset', 'medium', '-tune', 'stillimage',
            '-profile:v', 'main', '-level', '4.0',
            '-b:v', bitrate, '-maxrate', bitrate,
            '-bufsize', f"{int(bitrate[:-1]) * 2}k",
            '-pix_fmt', 'yuv420p',
            '-g', str(preset['fps']), '-keyint_min', str(preset['fps']),
            '-sc_threshold', '0',
            '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
            '-shortest', '-movflags', '+faststart',
            path
        ]

        try:
            subprocess.run(base_cmd, check=True, capture_output=True, timeout=180)
        except Exception as primary_error:
            simple_video_filter = (
                f"color=c={bg_color}:s={preset['width']}x{preset['height']}:"
                f"r={preset['fps']}:d=10"
            )
            fallback_cmd = list(base_cmd)
            fallback_cmd[fallback_cmd.index(video_filter)] = simple_video_filter
            try:
                subprocess.run(fallback_cmd, check=True, capture_output=True, timeout=180)
            except Exception as fallback_error:
                raise RuntimeError(
                    f"Standby video olusturulamadi: {fallback_error}"
                ) from primary_error

        STANDBY_CACHE[cache_key] = path
        return path


def parse_cookie_header(cookie_header):
    """Parse raw cookie header string into dict."""
    cookie_map = {}
    for part in (cookie_header or '').split(';'):
        chunk = part.strip()
        if not chunk or '=' not in chunk:
            continue
        key, value = chunk.split('=', 1)
        cookie_map[key.strip()] = value.strip()
    return cookie_map


def merge_cookie_headers(base_cookie, override_cookie):
    """Merge two raw cookie headers, override_cookie takes precedence."""
    merged = parse_cookie_header(base_cookie)
    merged.update(parse_cookie_header(override_cookie))
    return '; '.join([f'{k}={v}' for k, v in merged.items()])


def build_proxy_cookie_string(previous_cookie, response_cookies):
    """Merge previous cookie string and latest response cookies into one header string."""
    cookie_map = parse_cookie_header(previous_cookie)

    if response_cookies:
        for key, value in response_cookies.items():
            cookie_map[key] = value

    return '; '.join([f'{k}={v}' for k, v in cookie_map.items()])


def build_hls_proxy_url(source_url, cookie_header=None, session_key=None):
    """Build local proxy URL so FFmpeg can consume HLS through Flask."""
    port = int(os.environ.get('PORT', 5000))
    base = f'http://127.0.0.1:{port}/api/hls-proxy'
    query = f'u={quote_plus(source_url)}'
    if cookie_header:
        query += f'&ck={quote_plus(cookie_header)}'
    if session_key:
        query += f'&sk={quote_plus(str(session_key))}'
    return f'{base}?{query}'


def build_secondary_rtmp_url(server_url, stream_key):
    """Join a user-supplied RTMP server URL + stream key into one push URL.

    Used for an optional simultaneous secondary destination (e.g. TikTok which,
    unlike YouTube, hands out BOTH a server URL and a separate key that both
    rotate per session). Returns None if either part is missing or the server
    isn't an rtmp(s) URL.
    """
    server_url = (server_url or '').strip()
    stream_key = (stream_key or '').strip()
    if not server_url or not stream_key:
        return None
    if not server_url.startswith(('rtmp://', 'rtmps://')):
        return None
    return server_url.rstrip('/') + '/' + stream_key.lstrip('/')


_YTDLP_COOKIE_CACHE = {'path': None, 'resolved': False}
_YTDLP_COOKIE_LOCK = threading.Lock()


def get_ytdlp_cookiefile():
    """Return a path to a yt-dlp cookies file if configured, else None.

    YouTube blocks yt-dlp from datacenter/VPS IPs with "Sign in to confirm
    you're not a bot" unless authenticated. Supplying cookies from a logged-in
    session fixes it. Sources (first match wins):
      YTDLP_COOKIES_FILE - path to an existing Netscape cookies.txt
      YTDLP_COOKIES_B64  - base64 of a cookies.txt (best for one-line env vars)
      YTDLP_COOKIES      - raw cookies.txt contents
    The content is written once to a private temp file and cached for reuse.
    """
    with _YTDLP_COOKIE_LOCK:
        if _YTDLP_COOKIE_CACHE['resolved']:
            return _YTDLP_COOKIE_CACHE['path']
        path = None
        explicit = os.environ.get('YTDLP_COOKIES_FILE', '').strip()
        if explicit and os.path.exists(explicit):
            path = explicit
        else:
            content = None
            b64 = os.environ.get('YTDLP_COOKIES_B64', '').strip()
            raw = os.environ.get('YTDLP_COOKIES', '')
            if b64:
                try:
                    content = base64.b64decode(b64).decode('utf-8', 'replace')
                except Exception as e:
                    print(f'[cookies] YTDLP_COOKIES_B64 cozulemedi: {e}', flush=True)
            elif raw.strip():
                content = raw
            if content:
                try:
                    cdir = os.path.join(tempfile.gettempdir(), 'youtubelive_cookies')
                    os.makedirs(cdir, exist_ok=True)
                    cpath = os.path.join(cdir, 'youtube_cookies.txt')
                    with open(cpath, 'w', encoding='utf-8') as f:
                        f.write(content)
                    try:
                        os.chmod(cpath, 0o600)
                    except Exception:
                        pass
                    path = cpath
                except Exception as e:
                    print(f'[cookies] cookie dosyasi yazilamadi: {e}', flush=True)
        _YTDLP_COOKIE_CACHE['path'] = path
        _YTDLP_COOKIE_CACHE['resolved'] = True
        if path:
            print('[cookies] yt-dlp icin cookie dosyasi etkin', flush=True)
        return path


class StreamProcess:
    """Resilient YouTube streamer.

    Architecture (resilient mode, used for live network sources and looping
    files):

        Source FFmpeg  --MPEG-TS-->\\
                                     Python relay --pipe--> Pusher FFmpeg --RTMP--> YouTube
        Standby FFmpeg --MPEG-TS-->/

    The pusher FFmpeg stays alive for the entire stream lifetime. When the
    source feeder dies (network drop, source goes offline, etc.) the relay
    switches to the standby feeder so the YouTube RTMP connection sees
    continuous data and stays online. The source feeder is restarted in the
    background with exponential backoff; when it recovers, the relay
    automatically switches back.

    For finite local files (loop=False) the class falls back to a simple
    single-process pipeline so the stream ends naturally when the file
    finishes.
    """

    def __init__(self, stream_id):
        self.stream_id = stream_id

        # External-facing state (kept for backwards compatibility)
        self.source = None
        self.source_type = None
        self.youtube_key = None
        self.quality = '1080p'
        self.status = 'idle'
        self.started_at = None
        self.log = deque(maxlen=400)
        self.pid = None
        self.last_error = None
        self.last_exit_code = None
        self.using_fallback = False
        self.restart_attempts = 0
        max_restarts_env = os.environ.get('STREAM_MAX_RESTARTS')
        if max_restarts_env and max_restarts_env.isdigit() and int(max_restarts_env) > 0:
            self.max_restarts = int(max_restarts_env)
        else:
            self.max_restarts = 10 ** 9
        self.source_input_url = None
        self.source_cookies = None
        self.auto_restart = True
        self.manually_stopped = False
        self.current_cmd = []
        self.fallback_cmd = []

        # Mode: 'resilient' for failover pipeline, 'simple' for single FFmpeg
        self.mode = 'resilient'

        # Pipeline processes (resilient mode)
        self.pusher_proc = None
        self.source_proc = None
        self.filler_proc = None
        self.standby_video_path = None

        # Simple-mode process (file/loop=False)
        self.simple_proc = None

        # New failover-related state
        self.source_status = 'idle'
        self.pusher_status = 'idle'
        self.using_filler = False
        self.last_source_data_at = None
        self.source_launched_at = None
        self.source_lost_at = None
        self.source_recovered_at = None
        self.source_restart_attempts = 0
        self.pusher_restart_attempts = 0
        self.last_source_error = None
        self.last_pusher_error = None
        self.failover_count = 0
        self.bytes_relayed = 0
        self.loop_file = False

        # YouTube-restream-specific metadata (populated when source_type=='youtube')
        self.youtube_is_live = False
        self.youtube_title = None
        self.youtube_uploader = None
        self.youtube_channel_url = None
        self.youtube_resolved_url = None
        self.youtube_resolved_at = None

        # Cached FFmpeg commands (used by supervisor for restarts)
        self._pusher_cmd = []
        self._filler_cmd = []
        self._source_cmd = []
        # Restart-attempt count at the last YouTube re-resolve; used to gate how
        # often the supervisor pays for a blocking yt-dlp refresh.
        self._last_resolve_restart_count = 0

        # Optional secondary RTMP destination (e.g. simultaneous TikTok push).
        # When unset every secondary code path is guarded off, so a YouTube-only
        # stream behaves byte-for-byte like before. The secondary is best-effort
        # and fully isolated: it has its own pusher process, its own restart
        # backoff, and a drop-on-full writer queue so it can never stall or
        # break the primary push.
        self.secondary_enabled = False
        self.secondary_name = None          # log/status label, e.g. 'tiktok'
        self.secondary_rtmp_url = None
        self.pusher2_proc = None
        self.pusher2_status = 'idle'
        self.pusher2_restart_attempts = 0
        self.pusher2_dropped_chunks = 0
        self.last_pusher2_error = None
        self._pusher2_cmd = []
        self._pusher2_queue = None

        # Threading
        # Thread safety in resilient mode relies on the invariant that the
        # supervisor thread owns the FFmpeg subprocess handles; stop() now
        # joins the supervisor BEFORE tearing pipes down. No explicit lock
        # is needed (and the old self.process_lock was dead code).
        self.stop_event = threading.Event()
        self.supervisor_thread = None
        self.simple_monitor_thread = None

    # ---------- Logging ----------
    def _append_log(self, message, source='app'):
        if not message:
            return
        clean = str(message).strip()
        if not clean:
            return
        timestamp = datetime.now().strftime('%H:%M:%S')
        if source == 'app':
            self.log.append(f'[{timestamp}] {clean}')
        else:
            self.log.append(f'[{timestamp}][{source}] {clean}')

    # ---------- Helpers ----------
    @staticmethod
    def _get_preset(quality):
        return QUALITY_PRESETS.get(quality, QUALITY_PRESETS['1080p'])

    def _get_bitrate(self, quality):
        return BITRATE_MAP.get(quality, BITRATE_MAP['1080p'])

    @staticmethod
    def _extract_stream_url(info):
        """Best-effort extraction for a single playable URL from yt-dlp payload.

        For live streams the HLS master manifest is preferred so FFmpeg can
        follow live segment rotation natively.  For VOD the best combined
        (video+audio) format is selected.
        """
        is_live = bool(info.get('is_live'))

        def _is_m3u8_fmt(fmt):
            url = fmt.get('url') or ''
            proto = (fmt.get('protocol') or '').lower()
            return bool(url) and ('m3u8' in proto or '.m3u8' in url.lower())

        if is_live:
            # 1) Prefer the format yt-dlp's selector already chose. With
            #    download=False yt-dlp still applies the format string and sets
            #    info['url'] to the best m3u8 <=1080p variant.
            direct_proto = (info.get('protocol') or '').lower()
            direct = info.get('url')
            if direct and ('m3u8' in direct_proto or '.m3u8' in direct.lower()):
                return direct
            # 2) Otherwise pick the BEST HLS variant by resolution/bitrate.
            #    yt-dlp lists formats worst-first, so the previous code returned
            #    the lowest quality (sometimes audio-only) playlist, which made
            #    the video map empty and the restream effectively dead.
            m3u8_formats = [f for f in (info.get('formats') or []) if _is_m3u8_fmt(f)]
            video_m3u8 = [
                f for f in m3u8_formats
                if f.get('vcodec') and f.get('vcodec') != 'none'
            ]
            candidates = video_m3u8 or m3u8_formats
            if candidates:
                candidates.sort(
                    key=lambda f: ((f.get('height') or 0), (f.get('tbr') or 0)),
                    reverse=True
                )
                return candidates[0]['url']
            # 3) Fall back to the master manifest if one is exposed.
            manifest = info.get('manifest_url')
            if manifest:
                return manifest

        direct_url = info.get('url')
        if direct_url:
            return direct_url

        formats = info.get('formats') or []

        # Prefer formats that include BOTH video and audio
        combined = [
            fmt for fmt in formats
            if fmt.get('url')
            and fmt.get('vcodec') and fmt.get('vcodec') != 'none'
            and fmt.get('acodec') and fmt.get('acodec') != 'none'
        ]
        if combined:
            combined.sort(
                key=lambda f: ((f.get('height') or 0), (f.get('tbr') or 0)),
                reverse=True
            )
            return combined[0]['url']

        # Fallback: any format with video
        for fmt in reversed(formats):
            candidate = fmt.get('url')
            if candidate and fmt.get('vcodec') != 'none':
                return candidate

        # Playlist-style entries
        entries = info.get('entries') or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            candidate = entry.get('url')
            if candidate:
                return candidate
        return None

    @staticmethod
    def _normalize_youtube_url(url):
        """Normalize variants like youtube.com/@channel into something yt-dlp
        always treats as a live-stream lookup.

        Matches against the URL path only so things like
        ``youtube.com/results?search_query=/live`` aren't mistakenly treated
        as already-live URLs.
        """
        if not url:
            return url
        cleaned = url.strip()
        try:
            parsed = urlparse(cleaned)
        except Exception:
            return cleaned
        path = parsed.path or ''
        # Already explicit /live path: keep as-is
        if re.search(r'(^|/)live/?$', path):
            return cleaned
        # Bare channel URLs (@handle, /c/name, /channel/UC...): append /live so
        # yt-dlp resolves the channel's current live broadcast if one exists.
        channel_patterns = [
            r'youtube\.com/@[^/?#]+/?$',
            r'youtube\.com/c/[^/?#]+/?$',
            r'youtube\.com/user/[^/?#]+/?$',
            r'youtube\.com/channel/[^/?#]+/?$',
        ]
        for pat in channel_patterns:
            if re.search(pat, cleaned):
                return cleaned.rstrip('/') + '/live'
        return cleaned

    @classmethod
    def _resolve_youtube_source(cls, youtube_url):
        """Resolve a YouTube URL (video, /live, channel) to a playable URL.

        Pure resolver — does not touch any instance state, so /api/youtube/info
        can call it without building a fake StreamProcess just to probe URLs.

        Returns a dict with keys: success, url, is_live, is_upcoming, title,
        uploader, channel_url, error.
        """
        if not HAS_YTDLP:
            return {
                'success': False,
                'error': 'yt-dlp yuklu degil. Sunucuya yt-dlp kurun.',
            }

        target = cls._normalize_youtube_url(youtube_url)

        # Player clients are overridable: on a flagged datacenter IP, trying
        # clients like "android,ios,tv" (without "web") sometimes dodges the
        # bot check, but supplying cookies is the reliable fix.
        clients_env = os.environ.get('YTDLP_PLAYER_CLIENTS', '').strip()
        player_clients = (
            [c.strip() for c in clients_env.split(',') if c.strip()]
            if clients_env else ['default', 'web', 'android', 'ios']
        )

        ydl_opts = {
            'format': 'best[protocol*=m3u8][height<=?1080]/best[height<=?1080]/best',
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'noplaylist': True,
            'extractor_args': {
                'youtube': {'player_client': player_clients},
            },
            # Cap network calls so the supervisor thread can't be wedged
            # by a hung extract_info during a YouTube URL refresh.
            'socket_timeout': float(os.environ.get('YTDLP_SOCKET_TIMEOUT', '15')),
        }

        # Cookies authenticate yt-dlp so YouTube stops bot-blocking the server.
        cookiefile = get_ytdlp_cookiefile()
        if cookiefile:
            ydl_opts['cookiefile'] = cookiefile

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target, download=False)
        except Exception as e:
            msg = str(e)
            lowered = msg.lower()
            if 'not a bot' in lowered or 'sign in to confirm' in lowered:
                return {'success': False, 'error': (
                    'YouTube sunucu IP-sini bot olarak engelledi. Cozum: '
                    'YTDLP_COOKIES_B64 ortam degiskenine giris yapmis bir '
                    'YouTube hesabinin cookie-lerini ekleyin.'
                )}
            if 'private' in lowered:
                return {'success': False, 'error': 'Video gizli (private). Erisim yok.'}
            if 'members-only' in lowered or 'members only' in lowered:
                return {'success': False, 'error': 'Sadece uyelere ozel video. Erisim yok.'}
            if 'age' in lowered and 'restrict' in lowered:
                return {'success': False, 'error': 'Yas kisitli icerik. Cookie gerekli.'}
            if 'this live event will begin' in lowered or 'is upcoming' in lowered:
                return {'success': False, 'error': 'Canli yayin henuz baslamadi (planlanan).'}
            if 'unavailable' in lowered:
                return {'success': False, 'error': 'Video erisilemiyor (silinmis veya engelli).'}
            if 'geo' in lowered or 'country' in lowered:
                return {'success': False, 'error': 'Bolgesel kisitlama (geo-block).'}
            return {'success': False, 'error': f'YouTube ayristirma hatasi: {msg[:200]}'}

        if not info:
            return {'success': False, 'error': 'YouTube videosu bulunamadi'}

        # yt-dlp returns a 'playlist'/'multi_video' wrapper for some channel
        # URLs. Drill down to the first entry if needed.
        if info.get('_type') in ('playlist', 'multi_video') and info.get('entries'):
            entries = [e for e in info['entries'] if isinstance(e, dict)]
            if not entries:
                return {'success': False, 'error': 'Aktif canli yayin bulunamadi'}
            info = entries[0]

        if info.get('is_upcoming'):
            return {'success': False, 'error': 'Canli yayin henuz baslamadi (planlanan).'}

        stream_url = cls._extract_stream_url(info)
        if not stream_url:
            return {'success': False, 'error': 'Yayinlanabilir stream URL alinamadi'}

        return {
            'success': True,
            'url': stream_url,
            'is_live': bool(info.get('is_live')),
            'was_live': bool(info.get('was_live')),
            'is_upcoming': bool(info.get('is_upcoming')),
            'title': info.get('title') or 'YouTube',
            'uploader': info.get('uploader') or info.get('channel') or '',
            'channel_url': info.get('channel_url') or info.get('uploader_url') or '',
            'thumbnail': info.get('thumbnail') or '',
            'webpage_url': info.get('webpage_url') or target,
            'error': None,
        }

    @staticmethod
    def _build_source_headers(source_url):
        """Build optional HTTP headers for source hosts requiring referer/origin."""
        try:
            parsed = urlparse(source_url)
            if not parsed.scheme or not parsed.netloc:
                return None
            host = (parsed.hostname or '').lower()
            # YouTube's googlevideo CDN doesn't need a Referer/Origin and can
            # answer 403 when it sees a spoofed one. Send nothing for these.
            if (host.endswith('googlevideo.com')
                    or host.endswith('youtube.com')
                    or host.endswith('ytimg.com')):
                return None
            origin = f'{parsed.scheme}://{parsed.netloc}'
            referer = source_url
            return f'Referer: {referer}\r\nOrigin: {origin}\r\n'
        except Exception:
            return None

    @staticmethod
    def _is_hls_url(source_url):
        if not source_url:
            return False
        lower_url = source_url.lower()
        return '.m3u8' in lower_url or 'application/vnd.apple.mpegurl' in lower_url

    def _build_network_input_options(
        self,
        source_url,
        compatibility=False,
        cookie_header=None,
        hls_tuning=False
    ):
        """Build FFmpeg input options for network streams."""
        # -re paces the input at 1x realtime, which is essential for BOTH live
        # and VOD restreaming. Live HLS delivers data in per-segment bursts: a
        # feeder WITHOUT -re races through each downloaded segment, emits a
        # burst, then sits idle for several seconds waiting for the next segment
        # to appear at the live edge. Those idle gaps look exactly like a dead
        # source to the failover supervisor, so it flaps source<->standby every
        # few seconds and the spliced H.264 gets corrupted ("Packet corrupt",
        # "Out of range weight", reference-count overflow). -re turns the output
        # into a steady 1x trickle so the source stays continuously fresh and
        # failover only fires on a genuine outage.
        options = ['-re']
        options.extend([
            '-thread_queue_size', '2048',
            '-user_agent', DEFAULT_INPUT_USER_AGENT,
            '-protocol_whitelist', 'file,http,https,tcp,tls,crypto'
        ])
        headers = self._build_source_headers(source_url)
        if headers:
            options.extend(['-headers', headers])
        if cookie_header:
            options.extend(['-cookies', cookie_header])

        if hls_tuning and not compatibility:
            options.extend([
                '-live_start_index', '-3',
                '-max_reload', '50',
                '-m3u8_hold_counters', '50',
                '-http_persistent', '0',
                '-http_multiple', '0',
                '-http_seekable', '0',
                '-seg_max_retry', '6'
            ])

        if not compatibility:
            reconnect_opts = [
                '-rw_timeout', '15000000',
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_on_network_error', '1',
                '-reconnect_on_http_error', '4xx,5xx',
                '-reconnect_delay_max', '8'
            ]
            # -reconnect_at_eof is for non-segmented long-running HTTP streams
            # (a single neverending response). For HLS each segment is a finite
            # HTTP fetch and EOF marks the end of that segment; turning this
            # on tight-loops "Will reconnect at N in 0 second(s), error=End of
            # file" at every segment boundary.
            if not hls_tuning:
                reconnect_opts.extend(['-reconnect_at_eof', '1'])
            options.extend(reconnect_opts)
        else:
            options.extend(['-http_persistent', '0'])
        return options

    def _validate_m3u8_source(self, m3u8_url):
        """Preflight-check M3U8 URL and resolve a stable playable input URL."""
        def parse_hls(content, base_url):
            lines = [line.strip() for line in (content or '').splitlines() if line.strip()]
            variants = []
            segments = []

            for idx, line in enumerate(lines):
                if line.startswith('#EXT-X-STREAM-INF'):
                    bandwidth = 0
                    bandwidth_match = re.search(r'BANDWIDTH=(\d+)', line)
                    if bandwidth_match:
                        try:
                            bandwidth = int(bandwidth_match.group(1))
                        except ValueError:
                            bandwidth = 0
                    for next_line in lines[idx + 1:]:
                        if next_line.startswith('#'):
                            continue
                        variants.append({
                            'url': urljoin(base_url, next_line),
                            'bandwidth': bandwidth
                        })
                        break
                elif not line.startswith('#'):
                    segments.append(urljoin(base_url, line))

            return {
                'is_master': bool(variants),
                'variants': variants,
                'segments': segments
            }

        try:
            response = requests.get(
                m3u8_url,
                headers={'User-Agent': DEFAULT_INPUT_USER_AGENT},
                timeout=12,
                allow_redirects=True
            )
            response.raise_for_status()
            content = response.text or ''
            effective_url = response.url or m3u8_url
            cookie_header = '; '.join([f'{k}={v}' for k, v in response.cookies.items()]) or None

            if '#EXTM3U' not in content:
                return False, 'M3U8 kaynagi gecersiz: #EXTM3U basligi bulunamadi.', None

            hls_info = parse_hls(content, effective_url)
            has_segments = len(hls_info['segments']) > 0
            has_variants = len(hls_info['variants']) > 0

            resolved_input_url = effective_url

            if has_variants and not has_segments:
                selected = sorted(
                    hls_info['variants'],
                    key=lambda x: x.get('bandwidth', 0),
                    reverse=True
                )[0]
                candidate_url = selected['url']
                try:
                    variant_response = requests.get(
                        candidate_url,
                        headers={'User-Agent': DEFAULT_INPUT_USER_AGENT},
                        timeout=12,
                        allow_redirects=True
                    )
                    variant_response.raise_for_status()
                    variant_content = variant_response.text or ''
                    if '#EXTM3U' not in variant_content:
                        return False, 'Master playlist variant gecersiz m3u8 dondurdu.', None
                    variant_info = parse_hls(variant_content, variant_response.url or candidate_url)
                    if len(variant_info['segments']) == 0:
                        return False, (
                            'M3U8 kaynagi playlist donduruyor ama segment listesi bos. '
                            'Kaynak aktif degil veya erisim kisitli.'
                        ), None
                    resolved_input_url = variant_response.url or candidate_url
                    variant_cookie_header = '; '.join(
                        [f'{k}={v}' for k, v in variant_response.cookies.items()]
                    ) or None
                    cookie_header = variant_cookie_header or cookie_header
                except Exception as variant_error:
                    return False, f'Variant playlist dogrulamasi basarisiz: {variant_error}', None
            elif not has_segments and not has_variants:
                return False, (
                    'M3U8 playlist bos veya korumali gorunuyor. '
                    'Direkt stream URL kullandiginizdan emin olun.'
                ), None

            return True, '', {
                'resolved_input_url': resolved_input_url,
                'cookie_header': cookie_header
            }
        except Exception as e:
            return False, f'M3U8 kaynak dogrulamasi basarisiz: {e}', None

    # ---------- FFmpeg command builders ----------
    def _build_source_feeder_cmd(self, input_url, preset, extra_input_opts=None):
        """Build a feeder FFmpeg command that outputs normalized MPEG-TS to stdout.

        The output is forced to a known resolution/fps/codec so it matches the
        standby loop and can be cleanly switched at the pusher.
        """
        bitrate = preset['bitrate']
        cmd = ['ffmpeg', '-hide_banner', '-nostats', '-loglevel', 'warning']
        if extra_input_opts:
            cmd.extend(extra_input_opts)
        cmd.extend(['-i', input_url])
        cmd.extend([
            '-map', '0:v:0?', '-map', '0:a:0?',
            '-vf', (
                f"scale={preset['width']}:{preset['height']}:"
                f"force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={preset['width']}:{preset['height']}:-1:-1:color=black,"
                f"fps={preset['fps']},format=yuv420p,setpts=PTS-STARTPTS"
            ),
            '-af', 'aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS',
            # No -tune zerolatency: this is a restream, not an interactive feed,
            # so we keep B-frames, mb-tree and rc-lookahead which dramatically
            # improve quality at the same bitrate (zerolatency disabled all of
            # them, which is why detail/motion-heavy scenes looked mushy).
            '-c:v', 'libx264', '-preset', H264_PRESET,
            '-profile:v', 'high', '-level', '4.2',
            '-b:v', bitrate, '-maxrate', bitrate,
            '-bufsize', f"{int(bitrate[:-1]) * 2}k",
            '-pix_fmt', 'yuv420p',
            # 2-second GOP (YouTube's recommendation) instead of 1s wastes far
            # fewer bits on keyframes, leaving more for actual picture detail.
            '-g', str(preset['fps'] * 2), '-keyint_min', str(preset['fps']),
            '-bf', '3', '-sc_threshold', '0',
            '-c:a', 'aac', '-b:a', '160k', '-ar', '44100', '-ac', '2',
            '-max_muxing_queue_size', '1024',
            '-f', 'mpegts',
            '-mpegts_flags', 'resend_headers+initial_discontinuity',
            'pipe:1'
        ])
        return cmd

    def _build_filler_cmd(self, standby_path):
        """Loop the standby MP4 and emit MPEG-TS to stdout."""
        return [
            'ffmpeg', '-hide_banner', '-nostats', '-loglevel', 'warning',
            '-re', '-stream_loop', '-1', '-i', standby_path,
            '-map', '0:v:0', '-map', '0:a:0?',
            '-c', 'copy',
            '-bsf:v', 'h264_mp4toannexb',
            '-f', 'mpegts',
            '-mpegts_flags', 'resend_headers+initial_discontinuity',
            'pipe:1'
        ]

    def _build_pusher_cmd(self, rtmp_url):
        """Persistent pusher: reads MPEG-TS from stdin, copies to YouTube RTMP."""
        return [
            'ffmpeg', '-hide_banner', '-nostats', '-loglevel', 'info',
            '-fflags', '+genpts+discardcorrupt+nobuffer',
            '-err_detect', 'ignore_err',
            '-avoid_negative_ts', 'make_zero',
            '-analyzeduration', '2000000',
            '-probesize', '2000000',
            '-thread_queue_size', '8192',
            '-f', 'mpegts', '-i', 'pipe:0',
            '-map', '0:v:0', '-map', '0:a:0?',
            '-c', 'copy',
            '-f', 'flv', '-flvflags', 'no_duration_filesize',
            '-rtmp_live', 'live',
            rtmp_url
        ]

    # ---------- Public start_* methods ----------
    def start_m3u8(self, m3u8_url, youtube_key, quality='1080p',
                   secondary_rtmp_url=None, secondary_name=None):
        """Start streaming from M3U8 URL to YouTube (resilient mode)."""
        self.source = m3u8_url
        self.source_type = 'm3u8'
        self.youtube_key = youtube_key
        self.quality = quality
        self.mode = 'resilient'
        self.auto_restart = True

        is_valid, validation_error, preflight = self._validate_m3u8_source(m3u8_url)
        if not is_valid:
            self.status = 'error'
            self.last_error = validation_error
            self._append_log(validation_error)
            return False, validation_error

        resolved_url = preflight.get('resolved_input_url') if preflight else m3u8_url
        self.source_cookies = preflight.get('cookie_header') if preflight else None
        self.source_input_url = build_hls_proxy_url(
            resolved_url, self.source_cookies, session_key=self.stream_id
        )

        preset = self._get_preset(quality)
        rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{youtube_key}"

        input_options = self._build_network_input_options(
            self.source_input_url,
            compatibility=False,
            cookie_header=self.source_cookies,
            hls_tuning=True
        )

        source_cmd = self._build_source_feeder_cmd(
            self.source_input_url, preset, extra_input_opts=input_options
        )
        return self._start_resilient_pipeline(
            source_cmd, rtmp_url, secondary_rtmp_url, secondary_name
        )

    def start_youtube(self, youtube_url, youtube_key, quality='1080p',
                      secondary_rtmp_url=None, secondary_name=None):
        """Start streaming from a YouTube video/live/channel URL to YouTube."""
        self.source = youtube_url
        self.source_type = 'youtube'
        self.youtube_key = youtube_key
        self.quality = quality
        self.mode = 'resilient'
        self.auto_restart = True

        self._append_log(f'YouTube kaynagi cozumleniyor: {youtube_url}')
        resolved = self._resolve_youtube_source(youtube_url)
        if not resolved.get('success'):
            self.status = 'error'
            err = resolved.get('error') or 'YouTube kaynagi cozumlenemedi'
            self.last_error = err
            self.last_source_error = err
            self._append_log(err)
            return False, err

        stream_url = resolved['url']
        self.youtube_is_live = resolved.get('is_live', False)
        self.youtube_title = resolved.get('title')
        self.youtube_uploader = resolved.get('uploader')
        self.youtube_channel_url = resolved.get('channel_url')
        self.youtube_resolved_url = stream_url
        self.youtube_resolved_at = datetime.now()

        tag = 'CANLI' if self.youtube_is_live else 'KAYIT/VOD'
        self._append_log(
            f'[{tag}] {self.youtube_title or "?"} '
            f'(kanal: {self.youtube_uploader or "?"})'
        )

        preset = self._get_preset(quality)
        rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{youtube_key}"
        hls_tuning = self._is_hls_url(stream_url)

        # YouTube uses a 3-tier HLS structure that our internal proxy can't
        # rewrite cleanly (it confuses per-quality playlist URLs with segment
        # URLs and FFmpeg reports "Empty segment" for every variant). Bypass
        # the proxy unless the operator explicitly enabled it via env var.
        use_proxy = (
            hls_tuning
            and not HLS_PROXY_DISABLED
            and HLS_PROXY_FOR_YOUTUBE
            and stream_url.startswith(('http://', 'https://'))
        )
        input_url = (
            build_hls_proxy_url(stream_url, session_key=self.stream_id)
            if use_proxy else stream_url
        )
        self.source_input_url = input_url

        input_options = self._build_network_input_options(
            input_url, compatibility=False, hls_tuning=hls_tuning
        )
        source_cmd = self._build_source_feeder_cmd(
            input_url, preset, extra_input_opts=input_options
        )
        return self._start_resilient_pipeline(
            source_cmd, rtmp_url, secondary_rtmp_url, secondary_name
        )

    def start_file(self, file_path, youtube_key, quality='1080p', loop=False,
                   secondary_rtmp_url=None, secondary_name=None):
        """Start streaming from a local file to YouTube.

        loop=True uses the resilient pipeline (auto-recovers from any glitch).
        loop=False uses a simple single-FFmpeg pipeline so the stream ends
        when the file finishes.
        """
        self.source = file_path
        self.source_type = 'file'
        self.youtube_key = youtube_key
        self.quality = quality
        self.loop_file = loop
        self.auto_restart = bool(loop)

        rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{youtube_key}"
        bitrate = self._get_bitrate(quality)

        if loop:
            self.mode = 'resilient'
            preset = self._get_preset(quality)
            input_opts = ['-re', '-stream_loop', '-1', '-thread_queue_size', '2048']
            self.source_input_url = file_path
            source_cmd = self._build_source_feeder_cmd(
                file_path, preset, extra_input_opts=input_opts
            )
            return self._start_resilient_pipeline(
                source_cmd, rtmp_url, secondary_rtmp_url, secondary_name
            )

        # Finite file: simple single-process pipeline (no failover/fan-out).
        if secondary_rtmp_url:
            self._append_log(
                'Not: tek seferlik (loop kapali) dosya yayininda ikincil hedef '
                'desteklenmiyor; sadece birincil hedefe gonderilecek.'
            )
        self.mode = 'simple'
        self.source_input_url = file_path
        cmd = [
            'ffmpeg', '-hide_banner', '-nostats', '-loglevel', 'info',
            '-re', '-i', file_path,
            '-map', '0:v:0', '-map', '0:a:0?',
            '-c:v', 'libx264', '-preset', 'veryfast',
            '-b:v', bitrate, '-maxrate', bitrate,
            '-bufsize', f"{int(bitrate[:-1]) * 2}k",
            '-pix_fmt', 'yuv420p',
            '-g', '50', '-keyint_min', '50', '-sc_threshold', '0',
            '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
            '-f', 'flv', '-flvflags', 'no_duration_filesize',
            '-rtmp_live', 'live', rtmp_url
        ]
        return self._start_simple_pipeline(cmd)

    # ---------- Resilient pipeline ----------
    def _start_resilient_pipeline(self, source_cmd, rtmp_url,
                                  secondary_rtmp_url=None, secondary_name=None):
        try:
            self.standby_video_path = ensure_standby_video(self.quality)
        except Exception as standby_error:
            self.status = 'error'
            err = f'Standby olusturulamadi: {standby_error}'
            self._append_log(err)
            self.last_error = err
            return False, err

        self.started_at = datetime.now()
        self.status = 'starting'
        self.manually_stopped = False
        self.stop_event.clear()
        self.restart_attempts = 0
        self.source_restart_attempts = 0
        self.pusher_restart_attempts = 0
        self.pusher2_restart_attempts = 0
        self.pusher2_dropped_chunks = 0
        self.using_filler = False
        self.using_fallback = False
        self.failover_count = 0
        self.bytes_relayed = 0
        self.source_status = 'starting'
        self.pusher_status = 'starting'
        self.current_cmd = list(source_cmd)
        self.fallback_cmd = list(source_cmd)
        self.last_source_data_at = None

        self._pusher_cmd = self._build_pusher_cmd(rtmp_url)
        self._filler_cmd = self._build_filler_cmd(self.standby_video_path)
        self._source_cmd = list(source_cmd)

        try:
            self._launch_pusher()
        except Exception as e:
            self._cleanup_pipeline(quiet=True)
            self.status = 'error'
            err = f'Pusher baslatilamadi: {e}'
            self._append_log(err)
            self.last_error = err
            self.last_pusher_error = str(e)
            return False, err

        # Optional secondary destination (e.g. TikTok). A failure to launch it
        # must NOT abort the primary stream — log and carry on; the supervisor
        # will keep retrying it on its own backoff.
        if secondary_rtmp_url:
            self.secondary_enabled = True
            self.secondary_name = secondary_name or 'pusher2'
            self.secondary_rtmp_url = secondary_rtmp_url
            self._pusher2_cmd = self._build_pusher_cmd(secondary_rtmp_url)
            self.pusher2_status = 'starting'
            try:
                self._launch_pusher2()
            except Exception as e:
                self.last_pusher2_error = str(e)
                self.pusher2_status = 'down'
                self._append_log(
                    f'{self.secondary_name} pusher baslatilamadi: {e}',
                    source=self.secondary_name
                )

        try:
            self._launch_filler()
            self.using_filler = True
            self.using_fallback = True
            self.source_status = 'starting'
        except Exception as e:
            self._append_log(f'Standby baslatilamadi: {e}', source='standby')

        try:
            self._launch_source()
        except Exception as e:
            self._append_log(f'Kaynak baslatilamadi: {e}', source='kaynak')
            self.last_source_error = str(e)

        # If neither feeder launched, the pusher has nothing to read; abort
        # instead of silently sitting in 'running' with no data flowing.
        if not (self.source_proc or self.filler_proc):
            self._cleanup_pipeline(quiet=True)
            self.status = 'error'
            err = (
                self.last_source_error
                or 'Hicbir kaynak baslatilamadi (source ve standby basarisiz)'
            )
            self.last_error = err
            return False, err

        self.status = 'running'
        self.supervisor_thread = threading.Thread(
            target=self._supervisor_loop,
            name=f'sup-{self.stream_id[:8]}',
            daemon=True
        )
        self.supervisor_thread.start()
        return True, 'Stream basladi (dayaniklilik modu)'

    def _launch_pusher(self):
        if not self._pusher_cmd:
            raise RuntimeError('pusher command not set')
        self._append_log(f"Pusher baslatiliyor: {' '.join(self._pusher_cmd[:8])} ...")
        self.pusher_proc = subprocess.Popen(
            self._pusher_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0
        )
        self.pid = self.pusher_proc.pid
        self.pusher_status = 'live'
        threading.Thread(
            target=self._consume_stderr,
            args=(self.pusher_proc, 'pusher'),
            daemon=True
        ).start()
        self._append_log(f'Pusher baslatildi (pid: {self.pid})', source='pusher')

    def _launch_pusher2(self):
        """Launch the optional secondary pusher (same proven copy-to-RTMP
        command as the primary, just a different destination)."""
        if not self._pusher2_cmd:
            raise RuntimeError('secondary pusher command not set')
        label = self.secondary_name or 'pusher2'
        proc = subprocess.Popen(
            self._pusher2_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0
        )
        # Fresh queue per (re)launch so stale buffered bytes from a dead pusher
        # are discarded rather than replayed into the new connection.
        q = queue.Queue(maxsize=PUSHER2_QUEUE_MAX)
        self.pusher2_proc = proc
        self._pusher2_queue = q
        self.pusher2_status = 'live'
        threading.Thread(
            target=self._pusher2_writer_loop, args=(proc, q),
            name=f'{label}-w-{self.stream_id[:6]}', daemon=True
        ).start()
        threading.Thread(
            target=self._consume_stderr, args=(proc, label), daemon=True
        ).start()
        self._append_log(f'{label} pusher baslatildi (pid: {proc.pid})', source=label)

    def _pusher2_writer_loop(self, proc, q):
        """Drain the secondary queue into the secondary pusher's stdin.

        Runs on its own thread, so a slow/stalled secondary destination blocks
        only here — never the relay. Exits when the process dies, its stdin
        closes, or stop is requested.
        """
        stdin = proc.stdin
        while not self.stop_event.is_set():
            try:
                chunk = q.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if chunk is None:
                break
            try:
                if stdin is None or stdin.closed:
                    break
                stdin.write(chunk)
            except (BrokenPipeError, OSError, ValueError):
                break
            except Exception:
                break

    def _launch_filler(self):
        if not self._filler_cmd:
            raise RuntimeError('filler command not set')
        self.filler_proc = subprocess.Popen(
            self._filler_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0
        )
        threading.Thread(
            target=self._consume_stderr,
            args=(self.filler_proc, 'standby'),
            daemon=True
        ).start()
        self._append_log(f'Standby baslatildi (pid: {self.filler_proc.pid})', source='standby')

    def _launch_source(self):
        if not self._source_cmd:
            raise RuntimeError('source command not set')
        self._append_log(f"Kaynak baslatiliyor: {self._source_cmd[-1] if self._source_cmd else ''}")
        self.source_proc = subprocess.Popen(
            self._source_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0
        )
        self.source_status = 'starting'
        # Don't mark fresh until the source actually produces a byte.  Otherwise
        # the supervisor would switch to the source feeder and block on read()
        # before any data is available, starving the pusher of input.
        self.last_source_data_at = None
        self.source_launched_at = datetime.now()
        threading.Thread(
            target=self._consume_stderr,
            args=(self.source_proc, 'kaynak'),
            daemon=True
        ).start()
        self._append_log(f'Kaynak baslatildi (pid: {self.source_proc.pid})', source='kaynak')

    def _consume_stderr(self, proc, label):
        try:
            stream = proc.stderr
            if not stream:
                return
            for raw in stream:
                if self.stop_event.is_set():
                    break
                try:
                    line = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else raw
                except Exception:
                    continue
                line = line.strip()
                if not line:
                    continue
                self._append_log(line, source=label)
        except Exception as e:
            try:
                self._append_log(f'Stderr okuma hatasi: {e}', source=label)
            except Exception:
                pass

    # ---------- Supervisor / Relay loop ----------
    def _supervisor_loop(self):
        """Main control loop: manage processes and relay data with failover.

        The loop uses select() to multiplex reads from both feeders so that:
          * Source feeder data is drained even while we still consider source
            unhealthy (otherwise source's stdout fills and blocks).
          * Filler runs continuously when source is unhealthy.
          * Only the currently "active" feeder's bytes are forwarded to the
            pusher; the other's bytes are discarded.
        """
        import select as select_mod

        next_source_restart_at = None
        next_pusher_restart_at = None
        next_pusher2_restart_at = None
        next_filler_restart_at = None
        source_backoff = SOURCE_BACKOFF_INITIAL
        pusher_backoff = PUSHER_BACKOFF_INITIAL
        pusher2_backoff = PUSHER_BACKOFF_INITIAL
        filler_backoff = SOURCE_BACKOFF_INITIAL
        last_active_label = None

        while not self.stop_event.is_set():
            now = datetime.now()

            # 1) Ensure pusher is alive (deferred restart so the relay loop
            #    keeps draining feeders during the backoff window instead of
            #    blocking on stop_event.wait()).
            pusher_alive = self.pusher_proc and self.pusher_proc.poll() is None
            if not pusher_alive:
                if self.stop_event.is_set():
                    break
                if self.pusher_proc is not None:
                    code = self.pusher_proc.poll()
                    self._append_log(
                        f'Pusher dustu (kod: {code}), yeniden baglanacak',
                        source='pusher'
                    )
                    self.pusher_status = 'down'
                    self.pusher_restart_attempts += 1
                    self.last_pusher_error = f'exit code {code}'
                    self._terminate_process(self.pusher_proc, force=True)
                    self.pusher_proc = None
                    self.pid = None
                if next_pusher_restart_at is None:
                    next_pusher_restart_at = now + timedelta(seconds=pusher_backoff)
                elif now >= next_pusher_restart_at:
                    try:
                        self._launch_pusher()
                        pusher_backoff = PUSHER_BACKOFF_INITIAL
                        next_pusher_restart_at = None
                    except Exception as e:
                        self.last_pusher_error = str(e)
                        self._append_log(f'Pusher baslatilamadi: {e}', source='pusher')
                        pusher_backoff = min(pusher_backoff * 2, PUSHER_BACKOFF_MAX)
                        next_pusher_restart_at = now + timedelta(seconds=pusher_backoff)
                # Fall through to keep reading feeders even while pusher is down;
                # bytes will be discarded by the should_write check below.

            # 1b) Same lifecycle for the optional secondary pusher (e.g.
            #     TikTok), but fully independent: if it dies or its key expires
            #     it restarts on its own backoff and never touches the primary
            #     (YouTube) push.
            if self.secondary_enabled:
                label2 = self.secondary_name or 'pusher2'
                pusher2_alive = self.pusher2_proc and self.pusher2_proc.poll() is None
                if not pusher2_alive and not self.stop_event.is_set():
                    if self.pusher2_proc is not None:
                        code = self.pusher2_proc.poll()
                        self._append_log(
                            f'{label2} pusher dustu (kod: {code}), yeniden baglanacak',
                            source=label2
                        )
                        self.pusher2_status = 'down'
                        self.pusher2_restart_attempts += 1
                        self.last_pusher2_error = f'exit code {code}'
                        self._terminate_process(self.pusher2_proc, force=True)
                        self.pusher2_proc = None
                    if next_pusher2_restart_at is None:
                        next_pusher2_restart_at = now + timedelta(seconds=pusher2_backoff)
                    elif now >= next_pusher2_restart_at:
                        try:
                            self._launch_pusher2()
                            pusher2_backoff = PUSHER_BACKOFF_INITIAL
                            next_pusher2_restart_at = None
                        except Exception as e:
                            self.last_pusher2_error = str(e)
                            self._append_log(f'{label2} pusher baslatilamadi: {e}', source=label2)
                            pusher2_backoff = min(pusher2_backoff * 2, PUSHER_BACKOFF_MAX)
                            next_pusher2_restart_at = now + timedelta(seconds=pusher2_backoff)
                elif pusher2_alive:
                    next_pusher2_restart_at = None
                    pusher2_backoff = PUSHER_BACKOFF_INITIAL
                    self.pusher2_status = 'live'

            # 2) Ensure filler is alive (deferred restart with backoff so a
            #    persistent filler failure doesn't hot-loop).
            filler_alive = self.filler_proc and self.filler_proc.poll() is None
            if not filler_alive and not self.stop_event.is_set():
                if self.filler_proc is not None:
                    self._terminate_process(self.filler_proc, force=True)
                    self.filler_proc = None
                if next_filler_restart_at is None:
                    next_filler_restart_at = now + timedelta(seconds=filler_backoff)
                    self._append_log(
                        f'Standby {filler_backoff:.1f}s sonra yeniden baslatilacak',
                        source='standby'
                    )
                elif now >= next_filler_restart_at:
                    try:
                        self._launch_filler()
                        filler_backoff = SOURCE_BACKOFF_INITIAL
                        next_filler_restart_at = None
                    except Exception as e:
                        self._append_log(f'Standby baslatilamadi: {e}', source='standby')
                        filler_backoff = min(filler_backoff * 2, SOURCE_BACKOFF_MAX)
                        next_filler_restart_at = now + timedelta(seconds=filler_backoff)
            elif filler_alive:
                next_filler_restart_at = None
                filler_backoff = SOURCE_BACKOFF_INITIAL

            # 3) Determine source health & kill stalled source
            source_alive = self.source_proc and self.source_proc.poll() is None
            source_data_fresh = False
            if source_alive:
                if self.last_source_data_at:
                    idle = (now - self.last_source_data_at).total_seconds()
                    source_data_fresh = idle < SOURCE_DATA_TIMEOUT_SECONDS
                    stall_limit = SOURCE_STALL_TIMEOUT_SECONDS
                else:
                    # No byte yet: the feeder is still warming up (manifest +
                    # segment fetch + x264 priming). Give it a much longer grace
                    # window so we don't kill a healthy source mid-startup and
                    # trap the stream on the standby feed forever.
                    idle = (now - self.source_launched_at).total_seconds() if self.source_launched_at else 0
                    stall_limit = SOURCE_START_TIMEOUT_SECONDS
                # Kill stalled source so the restart loop can revive it
                if idle > stall_limit:
                    self._append_log(
                        f'Kaynak {idle:.0f}s veri uretmedi, yeniden baglatilacak',
                        source='supervisor'
                    )
                    self._terminate_process(self.source_proc, force=True)
                    self.source_proc = None
                    source_alive = False
                    source_data_fresh = False
                    self.last_source_error = 'source stalled'
            source_healthy = source_alive and source_data_fresh

            # 4) Update failover flag based on health
            if source_healthy:
                if self.using_filler:
                    self.using_filler = False
                    self.using_fallback = False
                    self.source_recovered_at = now
                    self._append_log(
                        'Kaynak geri geldi, canli yayina geri donuluyor',
                        source='supervisor'
                    )
                self.source_status = 'live'
                if last_active_label != 'source':
                    last_active_label = 'source'
                    self._append_log('Aktif feeder: kaynak', source='supervisor')
            else:
                if not self.using_filler:
                    self.using_filler = True
                    self.using_fallback = True
                    self.source_lost_at = now
                    self.failover_count += 1
                    self._append_log(
                        'Kaynak verisi yok - standby devrede, YouTube canli kaliyor',
                        source='supervisor'
                    )
                if not source_alive:
                    self.source_status = 'down'
                else:
                    self.source_status = 'stalled'
                if last_active_label != 'standby':
                    last_active_label = 'standby'
                    self._append_log('Aktif feeder: standby', source='supervisor')

            # 5) Schedule source restart if it's down
            if not source_alive and not self.stop_event.is_set():
                if next_source_restart_at is None:
                    next_source_restart_at = now + timedelta(seconds=source_backoff)
                    self._append_log(
                        f'Kaynak {source_backoff:.1f}s sonra yeniden baglanacak',
                        source='supervisor'
                    )
                elif now >= next_source_restart_at:
                    try:
                        if self.source_proc:
                            self._terminate_process(self.source_proc, force=True)
                            self.source_proc = None
                        self._refresh_source_command_if_needed()
                        self._launch_source()
                        self.source_restart_attempts += 1
                        self.restart_attempts = self.source_restart_attempts
                        source_backoff = min(source_backoff * 1.7, SOURCE_BACKOFF_MAX)
                    except Exception as e:
                        self.last_source_error = str(e)
                        self._append_log(
                            f'Kaynak yeniden baglatma hatasi: {e}',
                            source='supervisor'
                        )
                        source_backoff = min(source_backoff * 2, SOURCE_BACKOFF_MAX)
                    next_source_restart_at = None
            elif source_healthy:
                source_backoff = SOURCE_BACKOFF_INITIAL
                next_source_restart_at = None

            # 6) Multiplexed read from source and filler
            read_targets = []
            if self.source_proc and self.source_proc.poll() is None and self.source_proc.stdout:
                try:
                    read_targets.append(('source', self.source_proc, self.source_proc.stdout.fileno()))
                except Exception:
                    pass
            if self.filler_proc and self.filler_proc.poll() is None and self.filler_proc.stdout:
                try:
                    read_targets.append(('standby', self.filler_proc, self.filler_proc.stdout.fileno()))
                except Exception:
                    pass

            if not read_targets:
                if self.stop_event.wait(0.1):
                    break
                continue

            fds = [t[2] for t in read_targets]
            try:
                ready, _, _ = select_mod.select(fds, [], [], SUPERVISOR_SELECT_TIMEOUT)
            except (ValueError, OSError) as select_error:
                self._append_log(f'select hatasi: {select_error}', source='supervisor')
                if self.stop_event.wait(0.1):
                    break
                continue

            if not ready:
                continue

            broken_pusher = False
            for fd in ready:
                target = next((t for t in read_targets if t[2] == fd), None)
                if not target:
                    continue
                label, proc, _ = target
                try:
                    chunk = os.read(fd, RELAY_CHUNK_SIZE)
                except (OSError, BrokenPipeError) as read_error:
                    self._append_log(f'{label} okuma hatasi: {read_error}', source='supervisor')
                    chunk = b''

                if not chunk:
                    # EOF on this feeder - process is dying or has died
                    if label == 'source':
                        self._append_log('Kaynak akisi kesildi (EOF)', source='supervisor')
                    elif label == 'standby':
                        self._append_log('Standby akisi kesildi (EOF)', source='supervisor')
                    continue

                if label == 'source':
                    self.last_source_data_at = datetime.now()

                # Forward only the currently active feeder's bytes to the pusher;
                # the inactive feeder's bytes are intentionally discarded so its
                # pipe drains and the process never blocks.
                should_write = (
                    (label == 'source' and not self.using_filler) or
                    (label == 'standby' and self.using_filler)
                )
                if not should_write:
                    continue

                try:
                    pusher = self.pusher_proc
                    stdin = pusher.stdin if pusher else None
                    if stdin is None or stdin.closed:
                        broken_pusher = True
                        break
                    stdin.write(chunk)
                    self.bytes_relayed += len(chunk)
                except BrokenPipeError:
                    self._append_log(
                        'Pusher pipe kapali, yeniden baslatilacak',
                        source='supervisor'
                    )
                    broken_pusher = True
                    break
                except Exception as write_error:
                    self._append_log(
                        f'Pusher yazma hatasi: {write_error}',
                        source='supervisor'
                    )

                # Fan the same active bytes out to the secondary pusher through
                # its drop-on-full queue. This never blocks the relay: if the
                # secondary destination falls behind we drop its chunks and the
                # primary (YouTube) push is unaffected.
                if (self.secondary_enabled and self._pusher2_queue is not None
                        and self.pusher2_proc is not None):
                    try:
                        self._pusher2_queue.put_nowait(chunk)
                    except queue.Full:
                        self.pusher2_dropped_chunks += 1

            if broken_pusher:
                self._terminate_process(self.pusher_proc, force=False)
                self.pusher_proc = None
                self.pid = None

        self._append_log('Supervisor sonlandi', source='supervisor')

    def _refresh_source_command_if_needed(self):
        """Re-resolve dynamic sources before restart attempts.

        For YouTube: re-runs yt-dlp so an expired CDN signature gets refreshed.
        For M3U8/IPTV: re-runs the preflight so a rotated signed token or
        new cookie picked up from the master playlist replaces the stale one.
        Either form keeps long-running streams alive once the upstream URL
        expires without operator intervention.
        """
        if not self.source:
            return
        if self.source_type == 'm3u8':
            try:
                is_valid, validation_error, preflight = self._validate_m3u8_source(self.source)
            except Exception as e:
                self._append_log(f'M3U8 yenileme hatasi: {e}', source='supervisor')
                return
            if not is_valid:
                self.last_source_error = validation_error
                self._append_log(
                    f'M3U8 yenileme hatasi: {validation_error}',
                    source='supervisor'
                )
                return
            resolved_url = preflight.get('resolved_input_url') if preflight else self.source
            self.source_cookies = preflight.get('cookie_header') if preflight else self.source_cookies
            hls_tuning = self._is_hls_url(resolved_url)
            use_proxy = (
                hls_tuning
                and not HLS_PROXY_DISABLED
                and resolved_url.startswith(('http://', 'https://'))
            )
            input_url = (
                build_hls_proxy_url(
                    resolved_url, self.source_cookies, session_key=self.stream_id
                ) if use_proxy else resolved_url
            )
            self.source_input_url = input_url
            input_options = self._build_network_input_options(
                input_url, compatibility=False,
                cookie_header=self.source_cookies, hls_tuning=hls_tuning
            )
            preset = self._get_preset(self.quality)
            self._source_cmd = self._build_source_feeder_cmd(
                input_url, preset, extra_input_opts=input_options
            )
            self._append_log('M3U8 kaynagi yeniden dogrulandi', source='supervisor')
            return
        if self.source_type != 'youtube':
            return

        # Re-resolving YouTube is a blocking yt-dlp round-trip that, while it
        # runs, stops the supervisor from relaying the standby feed too (so even
        # the failover gaps). googlevideo URLs stay valid for hours, so for a
        # transient restart we keep the cached command and recover instantly.
        # Only pay for a fresh resolve when the URL is genuinely old or several
        # consecutive restarts suggest the cached URL has actually died.
        age = None
        if self.youtube_resolved_at:
            age = (datetime.now() - self.youtube_resolved_at).total_seconds()
        attempts_since = self.source_restart_attempts - self._last_resolve_restart_count
        if (self._source_cmd
                and age is not None
                and age < YT_URL_REFRESH_SECONDS
                and attempts_since < YT_REFRESH_AFTER_RESTARTS):
            self._append_log(
                f'YouTube URL hala gecerli ({age:.0f}s), onbellekten yeniden baglaniyor',
                source='supervisor'
            )
            return
        self._last_resolve_restart_count = self.source_restart_attempts

        resolved = self._resolve_youtube_source(self.source)
        if not resolved.get('success'):
            err = resolved.get('error') or 'unknown'
            self.last_source_error = err
            self._append_log(f'YouTube URL yenileme hatasi: {err}',
                             source='supervisor')
            return

        new_url = resolved['url']
        self.youtube_is_live = resolved.get('is_live', False)
        self.youtube_title = resolved.get('title') or self.youtube_title
        self.youtube_uploader = resolved.get('uploader') or self.youtube_uploader
        self.youtube_channel_url = (
            resolved.get('channel_url') or self.youtube_channel_url
        )
        self.youtube_resolved_url = new_url
        self.youtube_resolved_at = datetime.now()

        hls_tuning = self._is_hls_url(new_url)
        use_proxy = (
            hls_tuning
            and not HLS_PROXY_DISABLED
            and HLS_PROXY_FOR_YOUTUBE
            and new_url.startswith(('http://', 'https://'))
        )
        input_url = (
            build_hls_proxy_url(new_url, session_key=self.stream_id)
            if use_proxy else new_url
        )
        self.source_input_url = input_url
        input_options = self._build_network_input_options(
            input_url, compatibility=False, hls_tuning=hls_tuning
        )
        preset = self._get_preset(self.quality)
        self._source_cmd = self._build_source_feeder_cmd(
            input_url, preset, extra_input_opts=input_options
        )
        tag = 'CANLI' if self.youtube_is_live else 'VOD'
        self._append_log(
            f'YouTube URL yenilendi [{tag}] - {self.youtube_title or "?"}',
            source='supervisor'
        )

    # ---------- Simple pipeline (single FFmpeg, file/no failover) ----------
    def _start_simple_pipeline(self, cmd):
        self.started_at = datetime.now()
        self.status = 'starting'
        self.manually_stopped = False
        self.stop_event.clear()
        self.current_cmd = list(cmd)
        self.fallback_cmd = list(cmd)
        try:
            self._append_log(f"Starting: {' '.join(cmd[:6])} ...")
            self.simple_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                stdin=subprocess.PIPE, bufsize=0
            )
            self.pid = self.simple_proc.pid
            self.status = 'running'
            self.pusher_status = 'live'
            self.source_status = 'live'
            self.simple_monitor_thread = threading.Thread(
                target=self._simple_monitor_loop, daemon=True
            )
            self.simple_monitor_thread.start()
            return True, 'Stream basladi'
        except FileNotFoundError:
            self.status = 'error'
            self._append_log('Error: FFmpeg not found. Please install FFmpeg.')
            return False, 'FFmpeg not found. Please install FFmpeg.'
        except Exception as e:
            self.status = 'error'
            self.last_error = str(e)
            self._append_log(f'Error: {str(e)}')
            return False, str(e)

    def _simple_monitor_loop(self):
        try:
            stream = self.simple_proc.stderr
            if stream:
                for raw in stream:
                    if self.stop_event.is_set():
                        break
                    try:
                        line = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else raw
                    except Exception:
                        continue
                    line = line.strip()
                    if line:
                        self._append_log(line, source='ffmpeg')
        except Exception as e:
            self._append_log(f'Monitor error: {e}')

        proc = self.simple_proc
        if not proc:
            return
        code = proc.poll()
        if code is None:
            try:
                code = proc.wait(timeout=1)
            except Exception:
                pass
        self.last_exit_code = code
        self.simple_proc = None
        self.pid = None
        if self.manually_stopped or self.stop_event.is_set():
            self.status = 'stopped'
            self._append_log('Stream stopped')
            return
        if code == 0:
            self.status = 'stopped'
            self._append_log('FFmpeg normal sonlandi')
            return
        self.status = 'error'
        self.last_error = f'FFmpeg exited with code {code}'
        self._append_log(self.last_error)

    # ---------- Stop & cleanup ----------
    def stop(self):
        self.manually_stopped = True
        self.auto_restart = False
        self.stop_event.set()
        if self.mode == 'simple':
            self._stop_simple()
        else:
            # Join supervisor BEFORE tearing down pipes. Otherwise the
            # supervisor mid-iteration could see pusher/filler as None and
            # relaunch them after stop() returns, leaking Popen processes
            # and stderr-consumer threads.
            sup = self.supervisor_thread
            if sup and sup.is_alive():
                # Close pusher stdin to unblock supervisor if it's mid-write,
                # then wait for it to observe stop_event and exit.
                pusher = self.pusher_proc
                if pusher and pusher.stdin and not pusher.stdin.closed:
                    try:
                        pusher.stdin.close()
                    except Exception:
                        pass
                pusher2 = self.pusher2_proc
                if pusher2 and pusher2.stdin and not pusher2.stdin.closed:
                    try:
                        pusher2.stdin.close()
                    except Exception:
                        pass
                try:
                    sup.join(timeout=6)
                except Exception:
                    pass
            self._cleanup_pipeline()
        self.status = 'stopped'
        self._append_log('Stream kullanici tarafindan durduruldu')

    def _stop_simple(self):
        proc = self.simple_proc
        if proc:
            try:
                if proc.stdin:
                    try:
                        proc.stdin.write(b'q\n')
                        proc.stdin.flush()
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.simple_proc = None
        self.pid = None

    def _cleanup_pipeline(self, quiet=False):
        """Tear down pusher/filler/source in the right order."""
        for attr in ('source_proc', 'filler_proc'):
            proc = getattr(self, attr, None)
            if proc:
                self._terminate_process(proc, force=True)
                setattr(self, attr, None)
        pusher = self.pusher_proc
        if pusher:
            try:
                if pusher.stdin and not pusher.stdin.closed:
                    pusher.stdin.close()
            except Exception:
                pass
            try:
                pusher.wait(timeout=4)
            except Exception:
                try:
                    pusher.send_signal(signal.SIGTERM)
                    pusher.wait(timeout=3)
                except Exception:
                    try:
                        pusher.kill()
                    except Exception:
                        pass
        self.pusher_proc = None
        pusher2 = self.pusher2_proc
        if pusher2:
            try:
                if pusher2.stdin and not pusher2.stdin.closed:
                    pusher2.stdin.close()
            except Exception:
                pass
            self._terminate_process(pusher2, force=True)
        self.pusher2_proc = None
        self.pid = None
        if not quiet:
            self._append_log('Pipeline temizlendi')

    def _terminate_process(self, proc, force=False):
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2 if force else 4)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=2)
                    except Exception:
                        pass
        except Exception:
            pass

    # ---------- Status ----------
    def is_running(self):
        if self.mode == 'simple':
            return bool(self.simple_proc and self.simple_proc.poll() is None)
        # In resilient mode "running" means the supervisor is alive and
        # managing the pipeline. The pusher subprocess itself may briefly
        # not exist mid-restart; that's not a failure as long as the
        # supervisor will bring it back.
        if self.pusher_proc and self.pusher_proc.poll() is None:
            return True
        if self.supervisor_thread and self.supervisor_thread.is_alive() and not self.manually_stopped:
            return True
        return False

    def refresh_runtime_status(self):
        if self.mode == 'resilient':
            pusher_live = bool(self.pusher_proc and self.pusher_proc.poll() is None)
            sup_live = bool(
                self.supervisor_thread
                and self.supervisor_thread.is_alive()
                and not self.manually_stopped
            )
            if pusher_live:
                if self.status not in ('running', 'starting'):
                    self.status = 'running'
                return
            if sup_live:
                # Pusher is between exits and the supervisor's next launch
                # attempt; surface this as a transient state instead of
                # flipping to 'error' (which makes the frontend tear down
                # its polling UI even though we'll recover within seconds).
                self.status = 'restarting'
                return
            if self.status in ('running', 'starting', 'restarting'):
                self.status = 'error'
                if self.last_pusher_error:
                    self._append_log(f'Pusher calismiyor: {self.last_pusher_error}')
                else:
                    self._append_log('Pusher calismiyor')
            return

        # simple mode: original semantics
        running = self.is_running()
        if running and self.status not in ('running', 'starting', 'restarting'):
            self.status = 'running'
            return
        if not running and self.status in ('running', 'starting'):
            self.status = 'error'
            if self.last_exit_code is not None:
                self._append_log(f'Pusher calismiyor (son kod: {self.last_exit_code})')
            else:
                self._append_log('Pusher calismiyor')

    def get_output(self):
        return '\n'.join(self.get_recent_log(20))

    def get_recent_log(self, limit=10):
        return list(self.log)[-limit:]


def allowed_file(filename):
    """Check if file extension is allowed."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']


@app.route('/')
def index():
    """Render main page."""
    return render_template('index.html')


@app.route('/api/hls-proxy', methods=['GET'])
def hls_proxy():
    """Proxy HLS playlists/segments to avoid FFmpeg HTTP quirks on some sources."""
    source_url = request.args.get('u', '').strip()
    cookie_header = request.args.get('ck', '').strip()
    session_key = request.args.get('sk', '').strip()

    if not source_url:
        return jsonify({'success': False, 'error': 'Missing source URL'}), 400

    if not source_url.startswith(('http://', 'https://')):
        return jsonify({'success': False, 'error': 'Invalid source URL'}), 400

    parsed = urlparse(source_url)
    jar_key = session_key or parsed.netloc

    upstream_headers = {
        'User-Agent': DEFAULT_INPUT_USER_AGENT,
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'close'
    }
    source_headers = StreamProcess._build_source_headers(source_url)
    if source_headers:
        for row in source_headers.splitlines():
            if ':' not in row:
                continue
            key, value = row.split(':', 1)
            upstream_headers[key.strip()] = value.strip()
    with HLS_PROXY_LOCK:
        stored_cookie = HLS_PROXY_COOKIE_JAR.get(jar_key, '')
    seed_cookie = merge_cookie_headers(cookie_header, stored_cookie)
    if seed_cookie:
        upstream_headers['Cookie'] = seed_cookie

    try:
        upstream_response = requests.get(
            source_url,
            headers=upstream_headers,
            timeout=15,
            allow_redirects=True
        )
        upstream_response.raise_for_status()
    except Exception as e:
        return jsonify({'success': False, 'error': f'HLS proxy upstream failed: {e}'}), 502

    content_type = upstream_response.headers.get('Content-Type', '')
    body = upstream_response.content or b''
    # urlparse so '?token=...' on the source URL doesn't defeat the m3u8 sniff.
    try:
        source_path = urlparse(source_url).path.lower()
    except Exception:
        source_path = source_url.lower()
    is_playlist = (
        'mpegurl' in content_type.lower()
        or source_path.endswith('.m3u8')
        or source_path.endswith('.m3u')
        or b'#EXTM3U' in body
    )

    merged_cookie = build_proxy_cookie_string(seed_cookie, upstream_response.cookies)
    if merged_cookie:
        with HLS_PROXY_LOCK:
            HLS_PROXY_COOKIE_JAR[jar_key] = merged_cookie

    if not is_playlist:
        return Response(
            body,
            status=upstream_response.status_code,
            content_type=content_type or 'application/octet-stream'
        )

    text = upstream_response.text or ''
    base_url = upstream_response.url or source_url
    out_lines = []
    media_url_count = 0
    session_for_children = session_key or jar_key

    def _rewrite_uri_attrs(directive_line):
        """Proxy URI="..." attributes inside HLS tag directives.

        Without this, #EXT-X-MEDIA (alternate audio), #EXT-X-MAP (init segments)
        and #EXT-X-KEY (decryption keys) URIs would bypass the proxy and hit
        origin directly without any cookies/referer the proxy is forwarding,
        breaking IPTV streams that ship separate audio renditions, fMP4 init,
        or AES-128 encryption.
        """
        def _replace(match):
            inner = match.group(1)
            absolute = urljoin(base_url, inner)
            return f'URI="{build_hls_proxy_url(absolute, session_key=session_for_children)}"'
        return re.sub(r'URI="([^"]+)"', _replace, directive_line)

    URI_TAG_RE = re.compile(
        r'^#EXT-X-(MEDIA|MAP|KEY|SESSION-DATA|SESSION-KEY|I-FRAME-STREAM-INF|PART-INF|PRELOAD-HINT|RENDITION-REPORT)\b'
    )

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
            continue
        if stripped.startswith('#'):
            if URI_TAG_RE.match(stripped) and 'URI="' in stripped:
                out_lines.append(_rewrite_uri_attrs(line))
            else:
                out_lines.append(line)
            continue

        absolute_url = urljoin(base_url, stripped)
        proxied_url = build_hls_proxy_url(
            absolute_url,
            session_key=session_for_children
        )
        out_lines.append(proxied_url)
        media_url_count += 1

    if media_url_count == 0:
        # Empty playlist (no segment lines and no variants) is a valid HLS
        # state for live streams that haven't started yet -- return a valid
        # but empty playlist so FFmpeg retries cleanly instead of treating
        # a 502 JSON body as a malformed segment.
        rewritten = '\n'.join(out_lines) if out_lines else '#EXTM3U'
        return Response(
            rewritten,
            status=200,
            content_type='application/vnd.apple.mpegurl; charset=utf-8',
            headers={'Cache-Control': 'no-cache, no-store, must-revalidate'}
        )

    rewritten = '\n'.join(out_lines)
    return Response(
        rewritten,
        status=200,
        content_type='application/vnd.apple.mpegurl; charset=utf-8',
        headers={'Cache-Control': 'no-cache, no-store, must-revalidate'}
    )


@app.route('/api/streams/start', methods=['POST'])
def start_stream():
    """Start a new stream."""
    data = request.json
    stream_type = data.get('type')
    youtube_key = data.get('youtube_key', '').strip()
    quality = data.get('quality', '1080p')
    secondary_rtmp = build_secondary_rtmp_url(
        data.get('tiktok_url'), data.get('tiktok_key')
    )
    secondary_name = 'tiktok' if secondary_rtmp else None

    if not youtube_key:
        return jsonify({'success': False, 'error': 'YouTube Stream Key is required'}), 400

    stream_id = str(uuid.uuid4())
    stream = StreamProcess(stream_id)

    if stream_type == 'm3u8':
        m3u8_url = data.get('m3u8_url', '').strip()
        if not m3u8_url:
            return jsonify({'success': False, 'error': 'M3U8 URL is required'}), 400
        success, message = stream.start_m3u8(
            m3u8_url, youtube_key, quality,
            secondary_rtmp_url=secondary_rtmp, secondary_name=secondary_name
        )

    elif stream_type == 'file':
        # For file upload, we expect the file to be uploaded separately
        file_id = data.get('file_id')
        if not file_id or file_id not in uploaded_files:
            return jsonify({'success': False, 'error': 'File not found. Please upload first.'}), 400

        loop = data.get('loop', False)
        success, message = stream.start_file(
            uploaded_files[file_id]['path'], youtube_key, quality, loop,
            secondary_rtmp_url=secondary_rtmp, secondary_name=secondary_name
        )

    else:
        return jsonify({'success': False, 'error': 'Invalid stream type'}), 400

    if success:
        active_streams[stream_id] = stream
        save_stream_state(active_streams)
        return jsonify({
            'success': True,
            'stream_id': stream_id,
            'message': message
        })
    else:
        return jsonify({'success': False, 'error': message}), 500


@app.route('/api/streams/<stream_id>/stop', methods=['POST'])
def stop_stream(stream_id):
    """Stop a running stream."""
    stream = active_streams.get(stream_id)

    if not stream:
        state = load_stream_state()
        stream_state = state.get(stream_id)
        if not stream_state:
            return jsonify({'success': False, 'error': 'Stream not found'}), 404

        pid = stream_state.get('pid')
        if pid:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                # Process is already gone.
                pass
            except Exception as kill_error:
                return jsonify({
                    'success': False,
                    'error': f'Unable to stop external stream process: {kill_error}'
                }), 500

        del state[stream_id]
        write_stream_state_snapshot(state)
        return jsonify({'success': True, 'message': 'Stream stopped'})

    stream.stop()
    del active_streams[stream_id]
    save_stream_state(active_streams)

    return jsonify({'success': True, 'message': 'Stream stopped'})


@app.route('/api/streams/<stream_id>/status', methods=['GET'])
def stream_status(stream_id):
    """Get status of a stream."""
    stream = active_streams.get(stream_id)

    if not stream:
        state = load_stream_state()
        if stream_id in state:
            stream_state = state[stream_id]
            return jsonify({
                'success': True,
                'status': stream_state.get('status', 'unknown'),
                'is_running': stream_state.get('is_running', False),
                'source': stream_state.get('source', ''),
                'source_input_url': stream_state.get('source_input_url'),
                'started_at': stream_state.get('started_at'),
                'log': stream_state.get('log', []),
                'pid': stream_state.get('pid'),
                'last_error': stream_state.get('last_error'),
                'using_fallback': stream_state.get('using_fallback', False),
                'restart_attempts': stream_state.get('restart_attempts', 0),
                'max_restarts': stream_state.get('max_restarts', 0),
                'mode': stream_state.get('mode', 'simple'),
                'source_status': stream_state.get('source_status', 'unknown'),
                'pusher_status': stream_state.get('pusher_status', 'unknown'),
                'using_filler': stream_state.get('using_filler', False),
                'failover_count': stream_state.get('failover_count', 0),
                'source_restart_attempts': stream_state.get('source_restart_attempts', 0),
                'pusher_restart_attempts': stream_state.get('pusher_restart_attempts', 0),
                'last_source_error': stream_state.get('last_source_error'),
                'last_pusher_error': stream_state.get('last_pusher_error'),
                'source_type': stream_state.get('source_type'),
                'youtube_is_live': stream_state.get('youtube_is_live', False),
                'youtube_title': stream_state.get('youtube_title'),
                'youtube_uploader': stream_state.get('youtube_uploader'),
                'youtube_channel_url': stream_state.get('youtube_channel_url'),
            })
        return jsonify({'success': False, 'error': 'Stream not found'}), 404

    stream.refresh_runtime_status()
    save_stream_state(active_streams)

    return jsonify({
        'success': True,
        'status': stream.status,
        'is_running': stream.is_running(),
        'source': stream.source,
        'source_input_url': stream.source_input_url,
        'started_at': stream.started_at.isoformat() if stream.started_at else None,
        'log': stream.get_recent_log(20),
        'pid': stream.pid,
        'last_error': stream.last_error,
        'using_fallback': stream.using_fallback,
        'restart_attempts': stream.restart_attempts,
        'max_restarts': stream.max_restarts,
        'mode': stream.mode,
        'source_status': stream.source_status,
        'pusher_status': stream.pusher_status,
        'using_filler': stream.using_filler,
        'failover_count': stream.failover_count,
        'source_restart_attempts': stream.source_restart_attempts,
        'pusher_restart_attempts': stream.pusher_restart_attempts,
        'last_source_error': stream.last_source_error,
        'last_pusher_error': stream.last_pusher_error,
        'bytes_relayed': stream.bytes_relayed,
        'source_type': stream.source_type,
        'youtube_is_live': stream.youtube_is_live,
        'youtube_title': stream.youtube_title,
        'youtube_uploader': stream.youtube_uploader,
        'youtube_channel_url': stream.youtube_channel_url,
        'secondary_enabled': stream.secondary_enabled,
        'secondary_name': stream.secondary_name,
        'pusher2_status': stream.pusher2_status,
        'pusher2_restart_attempts': stream.pusher2_restart_attempts,
        'pusher2_dropped_chunks': stream.pusher2_dropped_chunks,
        'last_pusher2_error': stream.last_pusher2_error,
    })


@app.route('/api/streams', methods=['GET'])
def list_streams():
    """List all active streams."""
    streams = {}
    for stream_id, stream in active_streams.items():
        stream.refresh_runtime_status()
        streams[stream_id] = {
            'id': stream_id,
            'status': stream.status,
            'is_running': stream.is_running(),
            'source': stream.source,
            'source_input_url': stream.source_input_url,
            'started_at': stream.started_at.isoformat() if stream.started_at else None,
            'pid': stream.pid,
            'last_error': stream.last_error,
            'using_fallback': stream.using_fallback,
            'restart_attempts': stream.restart_attempts,
            'max_restarts': stream.max_restarts,
            'mode': stream.mode,
            'source_status': stream.source_status,
            'pusher_status': stream.pusher_status,
            'using_filler': stream.using_filler,
            'failover_count': stream.failover_count,
            'source_restart_attempts': stream.source_restart_attempts,
            'pusher_restart_attempts': stream.pusher_restart_attempts,
        }

    state = load_stream_state()
    for stream_id, stream_state in state.items():
        if stream_id in streams:
            continue
        streams[stream_id] = {
            'id': stream_id,
            'status': stream_state.get('status', 'unknown'),
            'is_running': stream_state.get('is_running', False),
            'source': stream_state.get('source', ''),
            'source_input_url': stream_state.get('source_input_url'),
            'started_at': stream_state.get('started_at'),
            'pid': stream_state.get('pid'),
            'last_error': stream_state.get('last_error'),
            'using_fallback': stream_state.get('using_fallback', False),
            'restart_attempts': stream_state.get('restart_attempts', 0),
            'max_restarts': stream_state.get('max_restarts', 0),
            'mode': stream_state.get('mode', 'simple'),
            'source_status': stream_state.get('source_status', 'unknown'),
            'pusher_status': stream_state.get('pusher_status', 'unknown'),
            'using_filler': stream_state.get('using_filler', False),
            'failover_count': stream_state.get('failover_count', 0),
            'source_restart_attempts': stream_state.get('source_restart_attempts', 0),
            'pusher_restart_attempts': stream_state.get('pusher_restart_attempts', 0),
        }

    save_stream_state(active_streams)
    return jsonify({'success': True, 'streams': list(streams.values())})


@app.route('/api/health', methods=['GET'])
def health_status():
    """Return service and stream health metrics."""
    stream_states = collect_stream_states()
    streams = list(stream_states.values())

    status_counts = {
        'running': 0,
        'starting': 0,
        'restarting': 0,
        'stopped': 0,
        'error': 0,
        'unknown': 0
    }

    restart_total = 0
    stream_metrics = []
    now = datetime.now()

    for stream in streams:
        status = stream.get('status', 'unknown')
        if status not in status_counts:
            status = 'unknown'
        status_counts[status] += 1

        restart_attempts = int(stream.get('restart_attempts', 0) or 0)
        restart_total += restart_attempts

        started_at = stream.get('started_at')
        uptime_seconds = None
        if started_at:
            try:
                started_dt = datetime.fromisoformat(started_at)
                uptime_seconds = max(int((now - started_dt).total_seconds()), 0)
            except ValueError:
                uptime_seconds = None

        log_entries = stream.get('log', [])
        last_log = log_entries[-1] if log_entries else ''

        stream_metrics.append({
            'id': stream.get('id'),
            'status': stream.get('status', 'unknown'),
            'is_running': stream.get('is_running', False),
            'source': stream.get('source', ''),
            'source_input_url': stream.get('source_input_url'),
            'source_type': stream.get('source_type', ''),
            'pid': stream.get('pid'),
            'worker_pid': stream.get('worker_pid'),
            'started_at': started_at,
            'uptime_seconds': uptime_seconds,
            'last_error': stream.get('last_error'),
            'using_fallback': stream.get('using_fallback', False),
            'restart_attempts': restart_attempts,
            'max_restarts': int(stream.get('max_restarts', 0) or 0),
            'last_log': last_log,
            'mode': stream.get('mode', 'simple'),
            'source_status': stream.get('source_status', 'unknown'),
            'pusher_status': stream.get('pusher_status', 'unknown'),
            'using_filler': stream.get('using_filler', False),
            'failover_count': int(stream.get('failover_count', 0) or 0),
            'source_restart_attempts': int(stream.get('source_restart_attempts', 0) or 0),
            'pusher_restart_attempts': int(stream.get('pusher_restart_attempts', 0) or 0),
            'last_source_error': stream.get('last_source_error'),
            'last_pusher_error': stream.get('last_pusher_error'),
        })

    failover_total = sum(int(s.get('failover_count', 0) or 0) for s in streams)
    using_filler_count = sum(1 for s in streams if s.get('using_filler'))

    response = {
        'success': True,
        'server_time': datetime.utcnow().isoformat() + 'Z',
        'worker_pid': WORKER_PID,
        'ffmpeg_available': shutil.which('ffmpeg') is not None,
        'total_streams': len(streams),
        'status_counts': status_counts,
        'restart_total': restart_total,
        'failover_total': failover_total,
        'using_filler_count': using_filler_count,
        'streams': stream_metrics
    }
    return jsonify(response)


@app.route('/api/upload', methods=['POST'])
def upload_file():
    """Upload a video file for streaming."""
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        # Add timestamp to avoid conflicts
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        name, ext = os.path.splitext(filename)
        filename = f"{name}_{timestamp}{ext}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        file_id = str(uuid.uuid4())
        uploaded_files[file_id] = {
            'name': file.filename,
            'path': filepath,
            'size': os.path.getsize(filepath),
            'uploaded_at': datetime.now().isoformat()
        }

        return jsonify({
            'success': True,
            'file_id': file_id,
            'filename': file.filename,
            'size': uploaded_files[file_id]['size']
        })

    return jsonify({'success': False, 'error': 'Invalid file type'}), 400


@app.route('/api/files', methods=['GET'])
def list_files():
    """List uploaded files."""
    files = []
    for file_id, file_info in uploaded_files.items():
        files.append({
            'id': file_id,
            'name': file_info['name'],
            'size': file_info['size'],
            'uploaded_at': file_info['uploaded_at']
        })
    return jsonify({'success': True, 'files': files})


@app.route('/api/files/<file_id>', methods=['DELETE'])
def delete_file(file_id):
    """Delete an uploaded file."""
    if file_id not in uploaded_files:
        return jsonify({'success': False, 'error': 'File not found'}), 404

    file_info = uploaded_files[file_id]
    try:
        os.remove(file_info['path'])
    except:
        pass

    del uploaded_files[file_id]
    return jsonify({'success': True, 'message': 'File deleted'})


# Store uploaded files in memory
uploaded_files = {}

# IPTV state
iptv_parser = M3UParser(timeout=60)
iptv_channels = []
iptv_groups = []
iptv_playlist_url = None

# Stream state file for multi-worker support
STREAM_STATE_FILE = os.path.join(tempfile.gettempdir(), 'youtubelive_streams.json')
state_file_lock = threading.Lock()
WORKER_PID = os.getpid()


def stream_to_state(stream_id, stream):
    """Serialize stream object into a transport-safe dictionary."""
    stream.refresh_runtime_status()
    return {
        'id': stream_id,
        'status': stream.status,
        'source': stream.source,
        'source_input_url': stream.source_input_url,
        'source_type': stream.source_type,
        'started_at': stream.started_at.isoformat() if stream.started_at else None,
        'log': stream.get_recent_log(20),
        'is_running': stream.is_running(),
        'pid': stream.pid,
        'worker_pid': WORKER_PID,
        'last_error': stream.last_error,
        'using_fallback': stream.using_fallback,
        'restart_attempts': stream.restart_attempts,
        'max_restarts': stream.max_restarts,
        'mode': getattr(stream, 'mode', 'simple'),
        'source_status': getattr(stream, 'source_status', 'unknown'),
        'pusher_status': getattr(stream, 'pusher_status', 'unknown'),
        'using_filler': getattr(stream, 'using_filler', False),
        'failover_count': getattr(stream, 'failover_count', 0),
        'source_restart_attempts': getattr(stream, 'source_restart_attempts', 0),
        'pusher_restart_attempts': getattr(stream, 'pusher_restart_attempts', 0),
        'last_source_error': getattr(stream, 'last_source_error', None),
        'last_pusher_error': getattr(stream, 'last_pusher_error', None),
        'bytes_relayed': getattr(stream, 'bytes_relayed', 0),
        'quality': getattr(stream, 'quality', '1080p'),
        'loop_file': getattr(stream, 'loop_file', False),
        'youtube_is_live': getattr(stream, 'youtube_is_live', False),
        'youtube_title': getattr(stream, 'youtube_title', None),
        'youtube_uploader': getattr(stream, 'youtube_uploader', None),
        'youtube_channel_url': getattr(stream, 'youtube_channel_url', None),
        'youtube_resolved_at': (
            stream.youtube_resolved_at.isoformat()
            if getattr(stream, 'youtube_resolved_at', None) else None
        ),
    }


def write_stream_state_snapshot(state):
    """Atomically write stream state snapshot to disk."""
    try:
        with state_file_lock:
            temp_file = f"{STREAM_STATE_FILE}.tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(state, f)
            os.replace(temp_file, STREAM_STATE_FILE)
    except Exception as e:
        print(f"Error writing stream state snapshot: {e}")

def save_stream_state(streams):
    """Save stream state to file for sharing between workers."""
    local_state = {}
    for stream_id, stream in streams.items():
        local_state[stream_id] = stream_to_state(stream_id, stream)

    try:
        with state_file_lock:
            existing = {}
            if os.path.exists(STREAM_STATE_FILE):
                with open(STREAM_STATE_FILE, 'r', encoding='utf-8') as f:
                    existing = json.load(f)

            merged_state = {}
            for stream_id, stream_state in existing.items():
                if str(stream_state.get('worker_pid')) != str(WORKER_PID):
                    merged_state[stream_id] = stream_state

            merged_state.update(local_state)
            temp_file = f"{STREAM_STATE_FILE}.tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(merged_state, f)
            os.replace(temp_file, STREAM_STATE_FILE)
    except Exception as e:
        print(f"Error saving stream state: {e}")

def load_stream_state():
    """Load stream state from file."""
    try:
        with state_file_lock:
            if os.path.exists(STREAM_STATE_FILE):
                with open(STREAM_STATE_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
    except Exception as e:
        print(f"Error loading stream state: {e}")
    return {}


def collect_stream_states():
    """Collect merged stream states from local process and shared state file."""
    local_states = {}
    for stream_id, stream in active_streams.items():
        local_states[stream_id] = stream_to_state(stream_id, stream)

    merged_states = load_stream_state()
    merged_states.update(local_states)
    return merged_states


# ==================== IPTV Endpoints ====================

@app.route('/api/iptv/load', methods=['POST'])
def iptv_load_playlist():
    """Load and parse an M3U playlist from URL."""
    global iptv_channels, iptv_groups, iptv_playlist_url

    data = request.json
    playlist_url = data.get('url', '').strip()

    if not playlist_url:
        return jsonify({'success': False, 'error': 'Playlist URL is required'}), 400

    try:
        channels, content = iptv_parser.parse_from_url(playlist_url)
        groups = iptv_parser.get_groups(channels)

        iptv_channels = channels
        iptv_groups = groups
        iptv_playlist_url = playlist_url

        return jsonify({
            'success': True,
            'count': len(channels),
            'groups': groups,
            'message': f'{len(channels)} kanal yüklendi'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/iptv/channels', methods=['GET'])
def iptv_get_channels():
    """Get list of channels with optional search/filter."""
    search = request.args.get('search', '').strip()
    group = request.args.get('group', '').strip()
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 100))

    if not iptv_channels:
        return jsonify({
            'success': True,
            'channels': [],
            'total': 0,
            'page': page,
            'pages': 0,
            'groups': []
        })

    # Filter channels
    filtered = iptv_parser.filter_channels(iptv_channels, search=search, group=group)

    # Paginate
    total = len(filtered)
    pages = (total + per_page - 1) // per_page
    start = (page - 1) * per_page
    end = start + per_page
    page_channels = filtered[start:end]

    return jsonify({
        'success': True,
        'channels': page_channels,
        'total': total,
        'page': page,
        'pages': pages,
        'groups': iptv_groups
    })


@app.route('/api/iptv/stream', methods=['POST'])
def iptv_start_stream():
    """Start streaming a selected IPTV channel to YouTube."""
    data = request.json
    channel_url = data.get('channel_url', '').strip()
    channel_name = data.get('channel_name', 'IPTV Channel')
    youtube_key = data.get('youtube_key', '').strip()
    quality = data.get('quality', '1080p')
    secondary_rtmp = build_secondary_rtmp_url(
        data.get('tiktok_url'), data.get('tiktok_key')
    )

    if not channel_url:
        return jsonify({'success': False, 'error': 'Channel URL is required'}), 400

    if not youtube_key:
        return jsonify({'success': False, 'error': 'YouTube Stream Key is required'}), 400

    stream_id = str(uuid.uuid4())
    stream = StreamProcess(stream_id)

    # Use the existing M3U8 streaming method - IPTV channels are typically M3U8/TS streams
    success, message = stream.start_m3u8(
        channel_url, youtube_key, quality,
        secondary_rtmp_url=secondary_rtmp,
        secondary_name='tiktok' if secondary_rtmp else None
    )

    if success:
        active_streams[stream_id] = stream
        save_stream_state(active_streams)
        return jsonify({
            'success': True,
            'stream_id': stream_id,
            'channel_name': channel_name,
            'message': f'{channel_name} yayını başlatıldı'
        })
    else:
        return jsonify({'success': False, 'error': message}), 500


@app.route('/api/iptv/clear', methods=['DELETE'])
def iptv_clear_playlist():
    """Clear loaded playlist."""
    global iptv_channels, iptv_groups, iptv_playlist_url

    iptv_channels = []
    iptv_groups = []
    iptv_playlist_url = None

    return jsonify({'success': True, 'message': 'Playlist temizlendi'})


# ==================== YouTube Restream Endpoints ====================

@app.route('/api/youtube/info', methods=['POST'])
def youtube_get_info():
    """Resolve a YouTube URL (video/live/channel) and report its details."""
    if not HAS_YTDLP:
        return jsonify({'success': False, 'error': 'yt-dlp yüklü değil. Sunucuya yt-dlp kurun.'}), 500

    data = request.json
    url = data.get('url', '').strip()

    if not url:
        return jsonify({'success': False, 'error': 'YouTube URL gerekli'}), 400

    youtube_pattern = r'(youtube\.com|youtu\.be)'
    if not re.search(youtube_pattern, url):
        return jsonify({'success': False, 'error': 'Geçerli bir YouTube linki girin'}), 400

    resolved = StreamProcess._resolve_youtube_source(url)
    if not resolved.get('success'):
        return jsonify({
            'success': False,
            'error': resolved.get('error') or 'YouTube videosu cozumlenemedi'
        }), 400

    return jsonify({
        'success': True,
        'info': {
            'title': resolved.get('title') or 'Bilinmeyen',
            'uploader': resolved.get('uploader') or 'Bilinmeyen',
            'is_live': resolved.get('is_live', False),
            'was_live': resolved.get('was_live', False),
            'is_upcoming': resolved.get('is_upcoming', False),
            'thumbnail': resolved.get('thumbnail', ''),
            'channel_url': resolved.get('channel_url', ''),
            'webpage_url': resolved.get('webpage_url') or url,
            'resolved_url': resolved.get('url'),
        }
    })


@app.route('/api/youtube/stream', methods=['POST'])
def youtube_start_stream():
    """Start streaming a YouTube video/live to our YouTube Live."""
    if not HAS_YTDLP:
        return jsonify({'success': False, 'error': 'yt-dlp yüklü değil. Sunucuya yt-dlp kurun.'}), 500

    data = request.json
    youtube_url = data.get('youtube_url', '').strip()
    youtube_key = data.get('youtube_key', '').strip()
    quality = data.get('quality', '1080p')
    secondary_rtmp = build_secondary_rtmp_url(
        data.get('tiktok_url'), data.get('tiktok_key')
    )

    if not youtube_url:
        return jsonify({'success': False, 'error': 'YouTube URL gerekli'}), 400

    if not youtube_key:
        return jsonify({'success': False, 'error': 'YouTube Stream Key gerekli'}), 400

    stream_id = str(uuid.uuid4())
    stream = StreamProcess(stream_id)

    success, message = stream.start_youtube(
        youtube_url, youtube_key, quality,
        secondary_rtmp_url=secondary_rtmp,
        secondary_name='tiktok' if secondary_rtmp else None
    )

    if success:
        active_streams[stream_id] = stream
        save_stream_state(active_streams)
        return jsonify({
            'success': True,
            'stream_id': stream_id,
            'message': 'YouTube restream başlatıldı' + (' + TikTok' if secondary_rtmp else '')
        })
    else:
        return jsonify({'success': False, 'error': message}), 500


def _autostart_runner(source_type, source, key, quality, secondary,
                      secondary_name, lock_path):
    """Background worker that brings up the preconfigured stream, retrying until
    the source (e.g. a live broadcast that may not be on yet at boot) is up."""
    max_tries = int(os.environ.get('AUTOSTART_MAX_TRIES', '30'))
    retry_delay = float(os.environ.get('AUTOSTART_RETRY_DELAY', '20'))
    time.sleep(float(os.environ.get('AUTOSTART_DELAY', '8')))
    attempt = 0
    while True:
        attempt += 1
        try:
            stream_id = str(uuid.uuid4())
            stream = StreamProcess(stream_id)
            if source_type == 'm3u8':
                ok, msg = stream.start_m3u8(
                    source, key, quality,
                    secondary_rtmp_url=secondary, secondary_name=secondary_name
                )
            else:
                ok, msg = stream.start_youtube(
                    source, key, quality,
                    secondary_rtmp_url=secondary, secondary_name=secondary_name
                )
            if ok:
                active_streams[stream_id] = stream
                try:
                    save_stream_state(active_streams)
                except Exception:
                    pass
                print(f'[autostart] yayin basladi ({source_type}): {msg}', flush=True)
                return
            print(f'[autostart] deneme {attempt} basarisiz: {msg}', flush=True)
        except Exception as e:
            print(f'[autostart] deneme {attempt} hata: {e}', flush=True)
        if max_tries and attempt >= max_tries:
            print('[autostart] maksimum deneme asildi, vazgecildi', flush=True)
            try:
                os.remove(lock_path)  # allow a later boot to retry
            except Exception:
                pass
            return
        time.sleep(retry_delay)


def maybe_autostart():
    """Optionally start a preconfigured stream on boot for unattended deploys.

    Driven purely by env vars so NO stream key lives in the (public) repo:
      AUTOSTART_ENABLED, AUTOSTART_YOUTUBE_KEY (destination),
      AUTOSTART_M3U8_URL or AUTOSTART_YOUTUBE_URL (source; M3U8 preferred),
      AUTOSTART_QUALITY, AUTOSTART_TIKTOK_URL, AUTOSTART_TIKTOK_KEY.
    An atomic lock file makes it run at most once per container even across
    multiple gunicorn workers or a worker restart (no duplicate streams).
    """
    key = os.environ.get('AUTOSTART_YOUTUBE_KEY', '').strip()
    m3u8_url = os.environ.get('AUTOSTART_M3U8_URL', '').strip()
    yt_url = os.environ.get('AUTOSTART_YOUTUBE_URL', '').strip()
    # Explicit off-switch only; otherwise a destination key + any source is the
    # signal to start. Logs exactly why it skipped so misconfig is obvious.
    if os.environ.get('AUTOSTART_ENABLED', '').strip().lower() == 'false':
        print('[autostart] AUTOSTART_ENABLED=false -> atlandi', flush=True)
        return
    if not key:
        print('[autostart] atlandi: AUTOSTART_YOUTUBE_KEY yok (hedef anahtar)', flush=True)
        return
    if not (m3u8_url or yt_url):
        print('[autostart] atlandi: kaynak yok '
              '(AUTOSTART_M3U8_URL veya AUTOSTART_YOUTUBE_URL gerekli)', flush=True)
        return
    # Prefer a direct M3U8 source: it needs no yt-dlp, so it sidesteps YouTube's
    # bot/cookie wall that blocks extraction from datacenter IPs.
    if m3u8_url:
        source_type, source = 'm3u8', m3u8_url
    else:
        source_type, source = 'youtube', yt_url
    quality = (os.environ.get('AUTOSTART_QUALITY', '1080p').strip() or '1080p')
    secondary = build_secondary_rtmp_url(
        os.environ.get('AUTOSTART_TIKTOK_URL'),
        os.environ.get('AUTOSTART_TIKTOK_KEY')
    )
    lock_path = os.path.join(tempfile.gettempdir(), 'youtubelive_autostart.lock')
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        return  # another worker / earlier boot already owns autostart
    except Exception:
        return
    print(f'[autostart] etkin ({source_type}), yayin hazirlaniyor...', flush=True)
    threading.Thread(
        target=_autostart_runner,
        args=(source_type, source, key, quality, secondary,
              'tiktok' if secondary else None, lock_path),
        name='autostart', daemon=True
    ).start()


# Run on import so it fires under gunicorn (module imported per worker) as well
# as `python app.py`. No-ops unless AUTOSTART_* env vars are configured.
maybe_autostart()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'

    print("\n" + "="*50)
    print("YouTube Live Streamer")
    print("="*50)
    print(f"\nRunning on: http://0.0.0.0:{port}")
    print(f"Debug mode: {debug}")
    print("\nMake sure FFmpeg is installed on your system.")
    print("Install FFmpeg:")
    print("  - Ubuntu/Debian: sudo apt install ffmpeg")
    print("  - Fedora: sudo dnf install ffmpeg")
    print("  - Arch: sudo pacman -S ffmpeg")
    print("\n" + "="*50 + "\n")

    app.run(host='0.0.0.0', port=port, debug=debug)

