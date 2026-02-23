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
from collections import deque
from datetime import datetime
from urllib.parse import urlparse
from flask import Flask, render_template, request, jsonify
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
    '720p': '2500k',
    '1080p': '4500k',
    '1440p': '9000k',
    '2160p': '18000k'
}
DEFAULT_INPUT_USER_AGENT = os.environ.get(
    'STREAM_INPUT_USER_AGENT',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
)


class StreamProcess:
    """Manages an FFmpeg streaming process."""

    def __init__(self, stream_id):
        self.stream_id = stream_id
        self.process = None
        self.process_lock = threading.Lock()
        self.monitor_thread = None
        self.stop_event = threading.Event()
        self.source = None
        self.source_type = None
        self.youtube_key = None
        self.status = 'idle'
        self.started_at = None
        self.log = deque(maxlen=200)
        self.current_cmd = []
        self.fallback_cmd = []
        self.using_fallback = False
        self.auto_restart = False
        self.max_restarts = int(os.environ.get('STREAM_MAX_RESTARTS', 5))
        self.restart_backoff_seconds = int(os.environ.get('STREAM_RESTART_BACKOFF', 3))
        self.restart_attempts = 0
        self.manually_stopped = False
        self.last_exit_code = None
        self.last_error = None
        self.pid = None

    def _append_log(self, message):
        """Add a timestamped entry to stream logs."""
        if not message:
            return
        clean_message = str(message).strip()
        if not clean_message:
            return
        timestamp = datetime.now().strftime('%H:%M:%S')
        self.log.append(f'[{timestamp}] {clean_message}')

    def _get_bitrate(self, quality):
        """Resolve output bitrate by quality preset."""
        return BITRATE_MAP.get(quality, BITRATE_MAP['1080p'])

    @staticmethod
    def _extract_stream_url(info):
        """Best-effort extraction for direct media URL from yt-dlp payload."""
        direct_url = info.get('url')
        if direct_url:
            return direct_url

        formats = info.get('formats') or []
        for fmt in reversed(formats):
            candidate = fmt.get('url')
            if candidate and fmt.get('vcodec') != 'none':
                return candidate

        entries = info.get('entries') or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            candidate = entry.get('url')
            if candidate:
                return candidate
        return None

    def _build_ffmpeg_cmd(self, input_options, source_input, bitrate, rtmp_url):
        """Build a resilient FFmpeg command for YouTube RTMP output."""
        command = ['ffmpeg', '-hide_banner', '-nostats', '-loglevel', 'info']
        command.extend(input_options)
        command.extend(['-i', source_input])
        command.extend([
            '-map', '0:v:0',
            '-map', '0:a:0?',
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-b:v', bitrate,
            '-maxrate', bitrate,
            '-bufsize', f'{int(bitrate[:-1]) * 2}k',
            '-pix_fmt', 'yuv420p',
            '-g', '50',
            '-keyint_min', '50',
            '-sc_threshold', '0',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '44100',
            '-ac', '2',
            '-f', 'flv',
            '-flvflags', 'no_duration_filesize',
            '-rtmp_live', 'live',
            rtmp_url
        ])
        return command

    def _build_compat_ffmpeg_cmd(self, input_options, source_input, bitrate, rtmp_url):
        """Build compatibility-focused FFmpeg command for older builds/sources."""
        command = ['ffmpeg', '-hide_banner', '-loglevel', 'info']
        command.extend(input_options)
        command.extend([
            '-i', source_input,
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-b:v', bitrate,
            '-maxrate', bitrate,
            '-bufsize', f'{int(bitrate[:-1]) * 2}k',
            '-pix_fmt', 'yuv420p',
            '-g', '50',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '44100',
            '-ac', '2',
            '-f', 'flv',
            rtmp_url
        ])
        return command

    def _build_source_headers(self, source_url):
        """Build optional HTTP headers for source hosts requiring referer/origin."""
        try:
            parsed = urlparse(source_url)
            if not parsed.scheme or not parsed.netloc:
                return None
            origin = f'{parsed.scheme}://{parsed.netloc}'
            return f'Referer: {origin}/\r\nOrigin: {origin}\r\n'
        except Exception:
            return None

    def _build_network_input_options(self, source_url, compatibility=False):
        """Build FFmpeg input options for network streams."""
        options = ['-re', '-thread_queue_size', '1024', '-user_agent', DEFAULT_INPUT_USER_AGENT]
        headers = self._build_source_headers(source_url)
        if headers:
            options.extend(['-headers', headers])

        if not compatibility:
            options.extend([
                '-rw_timeout', '15000000',
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_at_eof', '1',
                '-reconnect_delay_max', '8'
            ])
        return options

    def _validate_m3u8_source(self, m3u8_url):
        """Preflight-check M3U8 URL so empty/protected playlists fail fast."""
        try:
            response = requests.get(
                m3u8_url,
                headers={'User-Agent': DEFAULT_INPUT_USER_AGENT},
                timeout=12,
                allow_redirects=True
            )
            response.raise_for_status()
            content = response.text or ''

            if '#EXTM3U' not in content:
                return False, 'M3U8 kaynagi gecersiz: #EXTM3U basligi bulunamadi.'

            lines = [line.strip() for line in content.splitlines() if line.strip()]
            stream_markers = ('#EXTINF', '#EXT-X-STREAM-INF')
            has_marker = any(line.startswith(stream_markers) for line in lines)
            has_non_comment_uri = any(not line.startswith('#') for line in lines)

            if not has_marker and not has_non_comment_uri:
                return False, (
                    'M3U8 playlist bos veya korumali gorunuyor. '
                    'Direkt stream URL kullandiginizdan emin olun.'
                )
            return True, ''
        except Exception as e:
            return False, f'M3U8 kaynak dogrulamasi basarisiz: {e}'

    def start_m3u8(self, m3u8_url, youtube_key, quality='1080p'):
        """Start streaming from M3U8 URL to YouTube."""
        self.source = m3u8_url
        self.source_type = 'm3u8'
        self.youtube_key = youtube_key
        self.started_at = datetime.now()
        self.status = 'starting'
        self.auto_restart = True
        self.restart_attempts = 0
        self.using_fallback = False
        bitrate = self._get_bitrate(quality)

        is_valid, validation_error = self._validate_m3u8_source(m3u8_url)
        if not is_valid:
            self.status = 'error'
            self.last_error = validation_error
            self._append_log(validation_error)
            return False, validation_error

        rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{youtube_key}"

        input_options = self._build_network_input_options(m3u8_url, compatibility=False)
        compat_input_options = self._build_network_input_options(m3u8_url, compatibility=True)

        cmd = self._build_ffmpeg_cmd(input_options, m3u8_url, bitrate, rtmp_url)
        self.fallback_cmd = self._build_compat_ffmpeg_cmd(
            compat_input_options, m3u8_url, bitrate, rtmp_url
        )

        return self._start_process(cmd)

    def start_youtube(self, youtube_url, youtube_key, quality='1080p'):
        """Start streaming from YouTube video/live to YouTube."""
        self.source = youtube_url
        self.source_type = 'youtube'
        self.youtube_key = youtube_key
        self.started_at = datetime.now()
        self.status = 'starting'
        self.auto_restart = True
        self.restart_attempts = 0
        self.using_fallback = False

        if not HAS_YTDLP:
            self.status = 'error'
            self._append_log('Error: yt-dlp not installed. Cannot stream from YouTube.')
            return False, 'yt-dlp yüklü değil. Sunucuya yt-dlp kurun.'

        try:
            self._append_log(f'Fetching stream URL for: {youtube_url}')
            ydl_opts = {
                'format': 'best[height<=?1080]/best',
                'quiet': False,
                'no_warnings': False,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(youtube_url, download=False)
                stream_url = self._extract_stream_url(info)
                if not stream_url:
                    self.status = 'error'
                    self._append_log('Error: Could not get stream URL from YouTube')
                    return False, 'Stream URL alınamadı'

                video_title = info.get('title', 'YouTube Video')
                self._append_log(f'Got stream URL for: {video_title}')

        except Exception as e:
            self.status = 'error'
            error_msg = f'Error fetching YouTube video: {str(e)}'
            self._append_log(error_msg)
            return False, error_msg

        bitrate = self._get_bitrate(quality)
        rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{youtube_key}"

        input_options = self._build_network_input_options(stream_url, compatibility=False)
        compat_input_options = self._build_network_input_options(stream_url, compatibility=True)

        cmd = self._build_ffmpeg_cmd(input_options, stream_url, bitrate, rtmp_url)
        self.fallback_cmd = self._build_compat_ffmpeg_cmd(
            compat_input_options, stream_url, bitrate, rtmp_url
        )

        return self._start_process(cmd)

    def start_file(self, file_path, youtube_key, quality='1080p', loop=False):
        """Start streaming from video file to YouTube."""
        self.source = file_path
        self.source_type = 'file'
        self.youtube_key = youtube_key
        self.started_at = datetime.now()
        self.status = 'starting'
        self.auto_restart = False
        self.restart_attempts = 0
        self.using_fallback = False

        bitrate = self._get_bitrate(quality)
        rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{youtube_key}"

        input_options = [
            '-re',
        ]

        if loop:
            input_options.extend(['-stream_loop', '-1'])

        cmd = self._build_ffmpeg_cmd(input_options, file_path, bitrate, rtmp_url)
        self.fallback_cmd = self._build_compat_ffmpeg_cmd(input_options, file_path, bitrate, rtmp_url)

        return self._start_process(cmd)

    def _start_process(self, cmd):
        """Start the FFmpeg process."""
        try:
            with self.process_lock:
                self.current_cmd = cmd
                self.manually_stopped = False
                self.stop_event.clear()
                self._append_log(f"Starting: {' '.join(cmd)}")
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.PIPE,
                    text=True,
                    bufsize=1
                )
                self.pid = self.process.pid
                self.status = 'running'
            self._start_monitor_thread()
            return True, 'Stream started successfully'
        except FileNotFoundError:
            self.status = 'error'
            self._append_log('Error: FFmpeg not found. Please install FFmpeg.')
            return False, 'FFmpeg not found. Please install FFmpeg.'
        except Exception as e:
            self.status = 'error'
            self.last_error = str(e)
            self._append_log(f'Error: {str(e)}')
            return False, str(e)

    def _start_monitor_thread(self):
        """Start background monitoring thread for stderr consumption and lifecycle."""
        monitor = threading.Thread(target=self._monitor_process, daemon=True)
        self.monitor_thread = monitor
        monitor.start()

    def _monitor_process(self):
        """Consume FFmpeg stderr to prevent buffer lock and detect exits."""
        with self.process_lock:
            process = self.process

        if not process:
            return

        try:
            if process.stderr:
                for line in process.stderr:
                    if self.stop_event.is_set():
                        break
                    self._append_log(line)
        except Exception as monitor_error:
            self._append_log(f'Monitor error: {monitor_error}')

        exit_code = process.poll()
        if exit_code is None:
            try:
                exit_code = process.wait(timeout=1)
            except Exception:
                exit_code = None

        if exit_code is None:
            return

        with self.process_lock:
            if self.process is not process:
                return
            self.last_exit_code = exit_code
            self.process = None
            self.pid = None

        if self.manually_stopped or self.stop_event.is_set():
            if self.status != 'stopped':
                self.status = 'stopped'
                self._append_log('Stream stopped')
            return

        if exit_code == 0:
            self.status = 'stopped'
            self._append_log('FFmpeg exited normally')
            return

        self.status = 'error'
        self.last_error = f'FFmpeg exited with code {exit_code}'
        self._append_log(self.last_error)

        if self.fallback_cmd and not self.using_fallback:
            self.using_fallback = True
            self.current_cmd = list(self.fallback_cmd)
            self.status = 'restarting'
            self._append_log(
                'Primary FFmpeg profili basarisiz oldu, compatibility profile geciliyor'
            )
            if not self.stop_event.wait(1):
                self._restart_process()
            return

        if self.auto_restart and self.restart_attempts < self.max_restarts:
            self.restart_attempts += 1
            delay = min(self.restart_backoff_seconds * self.restart_attempts, 30)
            self.status = 'restarting'
            self._append_log(
                f'Restarting stream in {delay}s '
                f'({self.restart_attempts}/{self.max_restarts})'
            )
            if not self.stop_event.wait(delay):
                self._restart_process()
        else:
            self._append_log('Restart limit reached')

    def _restart_process(self):
        """Restart FFmpeg with the last known command."""
        with self.process_lock:
            if self.manually_stopped or self.stop_event.is_set():
                return
            if not self.current_cmd:
                self.status = 'error'
                self._append_log('Restart failed: command is missing')
                return

            try:
                self.process = subprocess.Popen(
                    self.current_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.PIPE,
                    text=True,
                    bufsize=1
                )
                self.pid = self.process.pid
                self.status = 'running'
                self._append_log(f'FFmpeg restarted (pid: {self.pid})')
            except Exception as restart_error:
                self.status = 'error'
                self.last_error = f'Restart failed: {restart_error}'
                self._append_log(self.last_error)
                return

        self._start_monitor_thread()

    def stop(self):
        """Stop the streaming process."""
        self.manually_stopped = True
        self.auto_restart = False
        self.stop_event.set()

        with self.process_lock:
            process = self.process

        if process:
            try:
                if process.stdin:
                    process.stdin.write('q\n')
                    process.stdin.flush()
            except Exception:
                pass

            try:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=8)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

        with self.process_lock:
            self.process = None
            self.pid = None

        self.status = 'stopped'
        self._append_log('Stream stopped by user')

    def is_running(self):
        """Check if the process is still running."""
        with self.process_lock:
            return bool(self.process and self.process.poll() is None)

    def refresh_runtime_status(self):
        """Refresh stream status based on process state."""
        running = self.is_running()
        if running and self.status not in ('running', 'starting', 'restarting'):
            self.status = 'running'
            return

        if not running and self.status in ('running', 'starting'):
            self.status = 'error'
            if self.last_exit_code is not None:
                self._append_log(f'FFmpeg not running (last code: {self.last_exit_code})')
            else:
                self._append_log('FFmpeg not running')

    def get_output(self):
        """Get recent FFmpeg output."""
        return '\n'.join(self.get_recent_log(20))

    def get_recent_log(self, limit=10):
        """Return latest log records."""
        return list(self.log)[-limit:]


def allowed_file(filename):
    """Check if file extension is allowed."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']


@app.route('/')
def index():
    """Render main page."""
    return render_template('index.html')


@app.route('/api/streams/start', methods=['POST'])
def start_stream():
    """Start a new stream."""
    data = request.json
    stream_type = data.get('type')
    youtube_key = data.get('youtube_key', '').strip()
    quality = data.get('quality', '1080p')

    if not youtube_key:
        return jsonify({'success': False, 'error': 'YouTube Stream Key is required'}), 400

    stream_id = str(uuid.uuid4())
    stream = StreamProcess(stream_id)

    if stream_type == 'm3u8':
        m3u8_url = data.get('m3u8_url', '').strip()
        if not m3u8_url:
            return jsonify({'success': False, 'error': 'M3U8 URL is required'}), 400
        success, message = stream.start_m3u8(m3u8_url, youtube_key, quality)

    elif stream_type == 'file':
        # For file upload, we expect the file to be uploaded separately
        file_id = data.get('file_id')
        if not file_id or file_id not in uploaded_files:
            return jsonify({'success': False, 'error': 'File not found. Please upload first.'}), 400

        loop = data.get('loop', False)
        success, message = stream.start_file(uploaded_files[file_id]['path'], youtube_key, quality, loop)

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
                'started_at': stream_state.get('started_at'),
                'log': stream_state.get('log', []),
                'pid': stream_state.get('pid'),
                'last_error': stream_state.get('last_error'),
                'using_fallback': stream_state.get('using_fallback', False),
                'restart_attempts': stream_state.get('restart_attempts', 0),
                'max_restarts': stream_state.get('max_restarts', 0)
            })
        return jsonify({'success': False, 'error': 'Stream not found'}), 404

    stream.refresh_runtime_status()
    save_stream_state(active_streams)

    return jsonify({
        'success': True,
        'status': stream.status,
        'is_running': stream.is_running(),
        'source': stream.source,
        'started_at': stream.started_at.isoformat() if stream.started_at else None,
        'log': stream.get_recent_log(20),
        'pid': stream.pid,
        'last_error': stream.last_error,
        'using_fallback': stream.using_fallback,
        'restart_attempts': stream.restart_attempts,
        'max_restarts': stream.max_restarts
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
            'started_at': stream.started_at.isoformat() if stream.started_at else None,
            'pid': stream.pid,
            'last_error': stream.last_error,
            'using_fallback': stream.using_fallback,
            'restart_attempts': stream.restart_attempts,
            'max_restarts': stream.max_restarts
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
            'started_at': stream_state.get('started_at'),
            'pid': stream_state.get('pid'),
            'last_error': stream_state.get('last_error'),
            'using_fallback': stream_state.get('using_fallback', False),
            'restart_attempts': stream_state.get('restart_attempts', 0),
            'max_restarts': stream_state.get('max_restarts', 0)
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
            'source_type': stream.get('source_type', ''),
            'pid': stream.get('pid'),
            'worker_pid': stream.get('worker_pid'),
            'started_at': started_at,
            'uptime_seconds': uptime_seconds,
            'last_error': stream.get('last_error'),
            'using_fallback': stream.get('using_fallback', False),
            'restart_attempts': restart_attempts,
            'max_restarts': int(stream.get('max_restarts', 0) or 0),
            'last_log': last_log
        })

    response = {
        'success': True,
        'server_time': datetime.utcnow().isoformat() + 'Z',
        'worker_pid': WORKER_PID,
        'ffmpeg_available': shutil.which('ffmpeg') is not None,
        'total_streams': len(streams),
        'status_counts': status_counts,
        'restart_total': restart_total,
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
import json
import tempfile
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
        'source_type': stream.source_type,
        'started_at': stream.started_at.isoformat() if stream.started_at else None,
        'log': stream.get_recent_log(20),
        'is_running': stream.is_running(),
        'pid': stream.pid,
        'worker_pid': WORKER_PID,
        'last_error': stream.last_error,
        'using_fallback': stream.using_fallback,
        'restart_attempts': stream.restart_attempts,
        'max_restarts': stream.max_restarts
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

    if not channel_url:
        return jsonify({'success': False, 'error': 'Channel URL is required'}), 400

    if not youtube_key:
        return jsonify({'success': False, 'error': 'YouTube Stream Key is required'}), 400

    stream_id = str(uuid.uuid4())
    stream = StreamProcess(stream_id)

    # Use the existing M3U8 streaming method - IPTV channels are typically M3U8/TS streams
    success, message = stream.start_m3u8(channel_url, youtube_key, quality)

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
    """Get info about a YouTube video/live stream."""
    if not HAS_YTDLP:
        return jsonify({'success': False, 'error': 'yt-dlp yüklü değil. Sunucuya yt-dlp kurun.'}), 500

    data = request.json
    url = data.get('url', '').strip()

    if not url:
        return jsonify({'success': False, 'error': 'YouTube URL gerekli'}), 400

    # Validate YouTube URL
    youtube_pattern = r'(youtube\.com|youtu\.be)'
    if not re.search(youtube_pattern, url):
        return jsonify({'success': False, 'error': 'Geçerli bir YouTube linki girin'}), 400

    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            return jsonify({
                'success': True,
                'info': {
                    'title': info.get('title', 'Bilinmeyen'),
                    'duration': info.get('duration'),
                    'uploader': info.get('uploader', 'Bilinmeyen'),
                    'is_live': info.get('is_live', False),
                    'thumbnail': info.get('thumbnail', ''),
                    'view_count': info.get('view_count', 0),
                }
            })
    except Exception as e:
        return jsonify({'success': False, 'error': f'Video bilgisi alınamadı: {str(e)}'}), 500


@app.route('/api/youtube/stream', methods=['POST'])
def youtube_start_stream():
    """Start streaming a YouTube video/live to our YouTube Live."""
    if not HAS_YTDLP:
        return jsonify({'success': False, 'error': 'yt-dlp yüklü değil. Sunucuya yt-dlp kurun.'}), 500

    data = request.json
    youtube_url = data.get('youtube_url', '').strip()
    youtube_key = data.get('youtube_key', '').strip()
    quality = data.get('quality', '1080p')

    if not youtube_url:
        return jsonify({'success': False, 'error': 'YouTube URL gerekli'}), 400

    if not youtube_key:
        return jsonify({'success': False, 'error': 'YouTube Stream Key gerekli'}), 400

    stream_id = str(uuid.uuid4())
    stream = StreamProcess(stream_id)

    success, message = stream.start_youtube(youtube_url, youtube_key, quality)

    if success:
        active_streams[stream_id] = stream
        save_stream_state(active_streams)
        return jsonify({
            'success': True,
            'stream_id': stream_id,
            'message': 'YouTube restream başlatıldı'
        })
    else:
        return jsonify({'success': False, 'error': message}), 500


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

