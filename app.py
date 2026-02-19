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
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename
from m3u_parser import M3UParser

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


class StreamProcess:
    """Manages an FFmpeg streaming process."""

    def __init__(self, stream_id):
        self.stream_id = stream_id
        self.process = None
        self.source = None
        self.youtube_key = None
        self.status = 'idle'
        self.started_at = None
        self.log = []

    def start_m3u8(self, m3u8_url, youtube_key, quality='1080p'):
        """Start streaming from M3U8 URL to YouTube."""
        self.source = m3u8_url
        self.youtube_key = youtube_key
        self.started_at = datetime.now()
        self.status = 'starting'

        # Parse quality
        bitrate_map = {
            '720p': '2500k',
            '1080p': '4500k',
            '1440p': '9000k',
            '2160p': '18000k'
        }
        bitrate = bitrate_map.get(quality, '4500k')
        resolution = quality if quality in bitrate_map else '1920:1080'

        # YouTube RTMP URL
        rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{youtube_key}"

        # FFmpeg command for M3U8 streaming
        cmd = [
            'ffmpeg',
            '-re',  # Read input at native frame rate
            '-i', m3u8_url,
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-b:v', bitrate,
            '-maxrate', bitrate,
            '-bufsize', f'{int(bitrate[:-1]) * 2}k',
            '-pix_fmt', 'yuv420p',
            '-g', '50',  # Keyframe every 2 seconds at 25fps
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '44100',
            '-f', 'flv',
            rtmp_url
        ]

        return self._start_process(cmd)

    def start_youtube(self, youtube_url, youtube_key, quality='1080p'):
        """Start streaming from YouTube video/live to YouTube."""
        self.source = youtube_url
        self.youtube_key = youtube_key
        self.started_at = datetime.now()
        self.status = 'starting'

        if not HAS_YTDLP:
            self.status = 'error'
            self.log.append("Error: yt-dlp not installed. Cannot stream from YouTube.")
            return False, "yt-dlp yüklü değil. Sunucuya yt-dlp kurun."

        # Get the direct stream URL using yt-dlp
        try:
            self.log.append(f"Fetching stream URL for: {youtube_url}")
            ydl_opts = {
                'format': 'best[height<=?1080]/best',
                'quiet': False,
                'no_warnings': False,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(youtube_url, download=False)
                if 'url' not in info:
                    self.status = 'error'
                    self.log.append("Error: Could not get stream URL from YouTube")
                    return False, "Stream URL alınamadı"

                stream_url = info['url']
                video_title = info.get('title', 'YouTube Video')
                self.log.append(f"Got stream URL for: {video_title}")

        except Exception as e:
            self.status = 'error'
            error_msg = f"Error fetching YouTube video: {str(e)}"
            self.log.append(error_msg)
            return False, error_msg

        # Parse quality
        bitrate_map = {
            '720p': '2500k',
            '1080p': '4500k',
            '1440p': '9000k',
            '2160p': '18000k'
        }
        bitrate = bitrate_map.get(quality, '4500k')

        # YouTube RTMP URL
        rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{youtube_key}"

        # FFmpeg command for YouTube streaming
        # Use -re to stream at native frame rate
        cmd = [
            'ffmpeg',
            '-re',
            '-i', stream_url,
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
            '-f', 'flv',
            rtmp_url
        ]

        return self._start_process(cmd)

    def start_file(self, file_path, youtube_key, quality='1080p', loop=False):
        """Start streaming from video file to YouTube."""
        self.source = file_path
        self.youtube_key = youtube_key
        self.started_at = datetime.now()
        self.status = 'starting'

        bitrate_map = {
            '720p': '2500k',
            '1080p': '4500k',
            '1440p': '9000k',
            '2160p': '18000k'
        }
        bitrate = bitrate_map.get(quality, '4500k')

        rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{youtube_key}"

        cmd = [
            'ffmpeg',
            '-re',  # Read input at native frame rate
        ]

        if loop:
            cmd.extend(['-stream_loop', '-1'])

        cmd.extend([
            '-i', file_path,
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
            '-f', 'flv',
            rtmp_url
        ])

        return self._start_process(cmd)

    def _start_process(self, cmd):
        """Start the FFmpeg process."""
        try:
            self.log.append(f"Starting: {' '.join(cmd)}")
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE
            )
            self.status = 'running'
            return True, "Stream started successfully"
        except FileNotFoundError:
            self.status = 'error'
            self.log.append("Error: FFmpeg not found. Please install FFmpeg.")
            return False, "FFmpeg not found. Please install FFmpeg."
        except Exception as e:
            self.status = 'error'
            self.log.append(f"Error: {str(e)}")
            return False, str(e)

    def stop(self):
        """Stop the streaming process."""
        if self.process:
            try:
                self.process.send_signal(signal.SIGTERM)
                self.process.wait(timeout=5)
            except:
                try:
                    self.process.kill()
                except:
                    pass
            self.process = None
        self.status = 'stopped'
        self.log.append("Stream stopped")

    def is_running(self):
        """Check if the process is still running."""
        return self.process and self.process.poll() is None

    def get_output(self):
        """Get recent FFmpeg output."""
        if not self.process:
            return ""
        return ""


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
        # Check state file for multi-worker support
        state = load_stream_state()
        if stream_id in state:
            del state[stream_id]
            try:
                with open(STREAM_STATE_FILE, 'w') as f:
                    json.dump(state, f)
            except:
                pass
            return jsonify({'success': True, 'message': 'Stream stopped'})
        return jsonify({'success': False, 'error': 'Stream not found'}), 404

    stream.stop()
    del active_streams[stream_id]
    save_stream_state(active_streams)

    return jsonify({'success': True, 'message': 'Stream stopped'})


@app.route('/api/streams/<stream_id>/status', methods=['GET'])
def stream_status(stream_id):
    """Get status of a stream."""
    stream = active_streams.get(stream_id)

    if not stream:
        # Try loading from state file for multi-worker support
        state = load_stream_state()
        if stream_id in state:
            stream_state = state[stream_id]
            return jsonify({
                'success': True,
                'status': stream_state.get('status', 'unknown'),
                'is_running': stream_state.get('is_running', False),
                'source': stream_state.get('source', ''),
                'started_at': stream_state.get('started_at'),
                'log': stream_state.get('log', [])
            })
        return jsonify({'success': False, 'error': 'Stream not found'}), 404

    return jsonify({
        'success': True,
        'status': stream.status,
        'is_running': stream.is_running(),
        'source': stream.source,
        'started_at': stream.started_at.isoformat() if stream.started_at else None,
        'log': stream.log[-10:]
    })


@app.route('/api/streams', methods=['GET'])
def list_streams():
    """List all active streams."""
    streams = []
    for stream_id, stream in active_streams.items():
        streams.append({
            'id': stream_id,
            'status': stream.status,
            'is_running': stream.is_running(),
            'source': stream.source,
            'started_at': stream.started_at.isoformat() if stream.started_at else None
        })
    return jsonify({'success': True, 'streams': streams})


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

def save_stream_state(streams):
    """Save stream state to file for sharing between workers."""
    try:
        state = {}
        for stream_id, stream in streams.items():
            state[stream_id] = {
                'id': stream_id,
                'status': stream.status,
                'source': stream.source,
                'started_at': stream.started_at.isoformat() if stream.started_at else None,
                'log': stream.log[-10:],
                'is_running': stream.is_running()
            }
        with open(STREAM_STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Error saving stream state: {e}")

def load_stream_state():
    """Load stream state from file."""
    try:
        if os.path.exists(STREAM_STATE_FILE):
            with open(STREAM_STATE_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"Error loading stream state: {e}")
    return {}


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
