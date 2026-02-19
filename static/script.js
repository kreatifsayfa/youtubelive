/**
 * YouTube Live Streamer - Frontend Logic
 */

// State
let currentTab = 'm3u8';
let selectedFileId = null;
let selectedChannel = null;
let youtubeVideoData = null;
let activeStreamId = null;
let statusPollInterval = null;
let iptvChannels = [];
let iptvGroups = [];

// DOM Elements
const youtubeKey = document.getElementById('youtubeKey');
const quality = document.getElementById('quality');
const m3u8Url = document.getElementById('m3u8Url');
const fileInput = document.getElementById('fileInput');
const uploadArea = document.getElementById('uploadArea');
const loopFile = document.getElementById('loopFile');
const startBtn = document.getElementById('startBtn');
const stopBtn = document.getElementById('stopBtn');
const streamStatusCard = document.getElementById('streamStatusCard');
const statusValue = document.getElementById('statusValue');
const sourceValue = document.getElementById('sourceValue');
const startedAtValue = document.getElementById('startedAtValue');
const streamLog = document.getElementById('streamLog');
const uploadedFiles = document.getElementById('uploadedFiles');

// Tab Management
document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        // Update tab buttons
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');

        // Update tab content
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        currentTab = btn.dataset.tab;
        document.getElementById(`${currentTab}-tab`).classList.add('active');

        updateStartButton();
    });
});

// File Upload
uploadArea.addEventListener('click', () => fileInput.click());

uploadArea.addEventListener('dragover', (e) => {
    e.preventDefault();
    uploadArea.classList.add('dragover');
});

uploadArea.addEventListener('dragleave', () => {
    uploadArea.classList.remove('dragover');
});

uploadArea.addEventListener('drop', (e) => {
    e.preventDefault();
    uploadArea.classList.remove('dragover');
    if (e.dataTransfer.files.length > 0) {
        uploadFile(e.dataTransfer.files[0]);
    }
});

fileInput.addEventListener('change', (e) => {
    if (e.target.files.length > 0) {
        uploadFile(e.target.files[0]);
    }
});

async function uploadFile(file) {
    const formData = new FormData();
    formData.append('file', file);

    showNotification(`Yükleniyor: ${file.name}...`, 'info');

    try {
        const response = await fetch('/api/upload', {
            method: 'POST',
            body: formData
        });

        const data = await response.json();

        if (data.success) {
            showNotification(`Yükleme tamamlandı: ${data.filename}`, 'success');
            loadFiles();
        } else {
            showNotification(`Yükleme hatası: ${data.error}`, 'error');
        }
    } catch (error) {
        showNotification(`Yükleme hatası: ${error.message}`, 'error');
    }
}

async function loadFiles() {
    try {
        const response = await fetch('/api/files');
        const data = await response.json();

        if (data.success) {
            renderFiles(data.files);
        }
    } catch (error) {
        console.error('Error loading files:', error);
    }
}

function renderFiles(files) {
    if (files.length === 0) {
        uploadedFiles.innerHTML = '';
        return;
    }

    uploadedFiles.innerHTML = files.map(file => `
        <div class="file-item ${selectedFileId === file.id ? 'selected' : ''}"
             onclick="selectFile('${file.id}')">
            <div class="file-info">
                <div class="file-name">${file.name}</div>
                <div class="file-size">${formatSize(file.size)}</div>
            </div>
            <button class="file-delete" onclick="event.stopPropagation(); deleteFile('${file.id}')">&times;</button>
        </div>
    `).join('');
}

function selectFile(fileId) {
    selectedFileId = fileId;
    loadFiles();
    updateStartButton();
}

async function deleteFile(fileId) {
    try {
        const response = await fetch(`/api/files/${fileId}`, { method: 'DELETE' });
        const data = await response.json();

        if (data.success) {
            if (selectedFileId === fileId) {
                selectedFileId = null;
            }
            loadFiles();
            showNotification('Dosya silindi', 'success');
        }
    } catch (error) {
        showNotification(`Silme hatası: ${error.message}`, 'error');
    }
}

// Start/Stop Stream
startBtn.addEventListener('click', startStream);
stopBtn.addEventListener('click', stopStream);

async function startStream() {
    const key = youtubeKey.value.trim();
    if (!key) {
        showNotification('YouTube Stream Key gerekli!', 'error');
        return;
    }

    let payload = {
        youtube_key: key,
        quality: quality.value,
        type: currentTab
    };

    if (currentTab === 'm3u8') {
        const url = m3u8Url.value.trim();
        if (!url) {
            showNotification('M3U8 URL gerekli!', 'error');
            return;
        }
        payload.m3u8_url = url;
    } else if (currentTab === 'file') {
        if (!selectedFileId) {
            showNotification('Bir dosya seçin!', 'error');
            return;
        }
        payload.file_id = selectedFileId;
        payload.loop = loopFile.checked;
    } else if (currentTab === 'iptv') {
        if (!selectedChannel) {
            showNotification('Bir kanal seçin!', 'error');
            return;
        }
        // Use IPTV endpoint
        return startIptvStream(key, quality.value);
    } else if (currentTab === 'youtube') {
        if (!youtubeVideoData) {
            showNotification('Önce YouTube video bilgisini yükleyin!', 'error');
            return;
        }
        // Use YouTube restream endpoint
        return startYoutubeStream(key, quality.value);
    }

    startBtn.disabled = true;
    showNotification('Yayın başlatılıyor...', 'info');

    try {
        const response = await fetch('/api/streams/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const data = await response.json();

        if (data.success) {
            activeStreamId = data.stream_id;
            showNotification('Yayın başladı!', 'success');
            updateUI(true);
            startStatusPolling();
        } else {
            showNotification(`Hata: ${data.error}`, 'error');
            startBtn.disabled = false;
        }
    } catch (error) {
        showNotification(`Bağlantı hatası: ${error.message}`, 'error');
        startBtn.disabled = false;
    }
}

async function startIptvStream(key, qualityValue) {
    startBtn.disabled = true;
    showNotification('IPTV yayını başlatılıyor...', 'info');

    try {
        const response = await fetch('/api/iptv/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                channel_url: selectedChannel.url,
                channel_name: selectedChannel.name,
                youtube_key: key,
                quality: qualityValue
            })
        });

        const data = await response.json();

        if (data.success) {
            activeStreamId = data.stream_id;
            showNotification(`${data.channel_name} yayını başladı!`, 'success');
            updateUI(true);
            startStatusPolling();
        } else {
            showNotification(`Hata: ${data.error}`, 'error');
            startBtn.disabled = false;
        }
    } catch (error) {
        showNotification(`Bağlantı hatası: ${error.message}`, 'error');
        startBtn.disabled = false;
    }
}

async function stopStream() {
    if (!activeStreamId) return;

    stopBtn.disabled = true;

    try {
        const response = await fetch(`/api/streams/${activeStreamId}/stop`, {
            method: 'POST'
        });

        const data = await response.json();

        if (data.success) {
            showNotification('Yayın durduruldu', 'success');
            activeStreamId = null;
            updateUI(false);
            stopStatusPolling();
        } else {
            showNotification(`Hata: ${data.error}`, 'error');
        }
    } catch (error) {
        showNotification(`Hata: ${error.message}`, 'error');
    }

    stopBtn.disabled = false;
}

// Status Polling
function startStatusPolling() {
    statusPollInterval = setInterval(pollStatus, 3000);
    pollStatus();
}

function stopStatusPolling() {
    if (statusPollInterval) {
        clearInterval(statusPollInterval);
        statusPollInterval = null;
    }
}

async function pollStatus() {
    if (!activeStreamId) return;

    try {
        const response = await fetch(`/api/streams/${activeStreamId}/status`);
        const data = await response.json();

        if (data.success) {
            statusValue.textContent = getStatusText(data.status);
            statusValue.className = `status-value ${data.status}`;
            sourceValue.textContent = data.source || '-';
            startedAtValue.textContent = data.started_at
                ? new Date(data.started_at).toLocaleString('tr-TR')
                : '-';

            // Update log
            if (data.log && data.log.length > 0) {
                streamLog.innerHTML = data.log.map(entry =>
                    `<div class="log-entry">${entry}</div>`
                ).join('');
                streamLog.scrollTop = streamLog.scrollHeight;
            }

            // Check if stream stopped unexpectedly
            if (!data.is_running && data.status !== 'stopped') {
                showNotification('Yayın beklenmedik şekilde durdu!', 'error');
                activeStreamId = null;
                updateUI(false);
                stopStatusPolling();
            }
        }
    } catch (error) {
        console.error('Status poll error:', error);
    }
}

// UI Updates
function updateStartButton() {
    const hasKey = youtubeKey.value.trim().length > 0;
    let hasSource = false;

    if (currentTab === 'm3u8') {
        hasSource = m3u8Url.value.trim().length > 0;
    } else if (currentTab === 'file') {
        hasSource = selectedFileId !== null;
    } else if (currentTab === 'iptv') {
        hasSource = selectedChannel !== null;
    } else if (currentTab === 'youtube') {
        hasSource = youtubeVideoData !== null;
    }

    startBtn.disabled = !hasKey || !hasSource || activeStreamId !== null;
}

function updateUI(streaming) {
    startBtn.disabled = streaming;
    stopBtn.disabled = !streaming;
    streamStatusCard.style.display = streaming ? 'block' : 'none';
}

// Input change listeners
youtubeKey.addEventListener('input', updateStartButton);
m3u8Url.addEventListener('input', updateStartButton);

// ==================== YouTube Restream Functions ====================

const youtubeUrl = document.getElementById('youtubeUrl');
const youtubeVideoInfoCard = document.getElementById('youtubeVideoInfo');
const youtubeVideoTitle = document.getElementById('youtubeVideoTitle');

let youtubeUrlTimeout = null;
youtubeUrl.addEventListener('input', () => {
    clearTimeout(youtubeUrlTimeout);
    const url = youtubeUrl.value.trim();

    if (!url) {
        youtubeVideoInfoCard.style.display = 'none';
        youtubeVideoData = null;
        updateStartButton();
        return;
    }

    // Check if it looks like a YouTube URL
    if (url.includes('youtube.com') || url.includes('youtu.be')) {
        youtubeUrlTimeout = setTimeout(() => fetchYoutubeInfo(url), 1000);
    }
});

async function fetchYoutubeInfo(url) {
    try {
        const response = await fetch('/api/youtube/info', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url })
        });

        const data = await response.json();

        if (data.success) {
            youtubeVideoData = data.info;
            youtubeVideoInfoCard.style.display = 'block';
            youtubeVideoTitle.textContent = youtubeVideoData.title;
            showNotification('Video bilgisi yüklendi', 'success');
            updateStartButton();
        } else {
            showNotification(`Hata: ${data.error}`, 'error');
            youtubeVideoData = null;
            updateStartButton();
        }
    } catch (error) {
        showNotification(`Bağlantı hatası: ${error.message}`, 'error');
        youtubeVideoData = null;
        updateStartButton();
    }
}

async function startYoutubeStream(key, qualityValue) {
    startBtn.disabled = true;
    showNotification('YouTube restream başlatılıyor...', 'info');

    try {
        const response = await fetch('/api/youtube/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                youtube_url: youtubeUrl.value.trim(),
                youtube_key: key,
                quality: qualityValue
            })
        });

        const data = await response.json();

        if (data.success) {
            activeStreamId = data.stream_id;
            showNotification('YouTube restream başladı!', 'success');
            updateUI(true);
            startStatusPolling();
        } else {
            showNotification(`Hata: ${data.error}`, 'error');
            startBtn.disabled = false;
        }
    } catch (error) {
        showNotification(`Bağlantı hatası: ${error.message}`, 'error');
        startBtn.disabled = false;
    }
}

// Helpers
function getStatusText(status) {
    const map = {
        'idle': 'Beklemede',
        'starting': 'Başlatılıyor...',
        'running': 'Yayında',
        'stopped': 'Durduruldu',
        'error': 'Hata'
    };
    return map[status] || status;
}

function formatSize(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function showNotification(message, type = 'info') {
    const notification = document.createElement('div');
    notification.className = `notification ${type}`;
    notification.textContent = message;
    document.body.appendChild(notification);

    setTimeout(() => {
        notification.style.transition = 'opacity 0.3s';
        notification.style.opacity = '0';
        setTimeout(() => notification.remove(), 300);
    }, 3000);
}

// ==================== IPTV Functions ====================

const iptvPlaylistUrl = document.getElementById('iptvPlaylistUrl');
const loadPlaylistBtn = document.getElementById('loadPlaylistBtn');
const iptvChannelListContainer = document.getElementById('iptvChannelListContainer');
const iptvChannelList = document.getElementById('iptvChannelList');
const channelSearch = document.getElementById('channelSearch');
const channelGroupFilter = document.getElementById('channelGroupFilter');
const channelCount = document.getElementById('channelCount');
const selectedChannelInfo = document.getElementById('selectedChannelInfo');
const selectedChannelName = document.getElementById('selectedChannelName');

// Load Playlist
loadPlaylistBtn.addEventListener('click', loadPlaylist);

async function loadPlaylist() {
    const url = iptvPlaylistUrl.value.trim();
    if (!url) {
        showNotification('Playlist URL gerekli!', 'error');
        return;
    }

    loadPlaylistBtn.disabled = true;
    loadPlaylistBtn.textContent = 'Yükleniyor...';
    showNotification('Playlist yükleniyor, lütfen bekleyin...', 'info');

    try {
        const response = await fetch('/api/iptv/load', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url })
        });

        const data = await response.json();

        if (data.success) {
            showNotification(`${data.count} kanal yüklendi!`, 'success');
            iptvGroups = data.groups || [];
            populateGroupFilter();
            loadChannels();
            iptvChannelListContainer.style.display = 'block';
        } else {
            showNotification(`Hata: ${data.error}`, 'error');
        }
    } catch (error) {
        showNotification(`Bağlantı hatası: ${error.message}`, 'error');
    }

    loadPlaylistBtn.disabled = false;
    loadPlaylistBtn.innerHTML = '<span class="btn-icon">📡</span> Playlist Yükle';
}

function populateGroupFilter() {
    channelGroupFilter.innerHTML = '<option value="">Tüm Kategoriler</option>';
    iptvGroups.forEach(group => {
        const option = document.createElement('option');
        option.value = group;
        option.textContent = group;
        channelGroupFilter.appendChild(option);
    });
}

async function loadChannels() {
    const search = channelSearch.value.trim();
    const group = channelGroupFilter.value;

    try {
        const params = new URLSearchParams();
        if (search) params.set('search', search);
        if (group) params.set('group', group);
        params.set('per_page', '500');

        const response = await fetch(`/api/iptv/channels?${params}`);
        const data = await response.json();

        if (data.success) {
            iptvChannels = data.channels;
            renderChannels(data.channels);
            channelCount.textContent = `${data.total} kanal`;
        }
    } catch (error) {
        console.error('Error loading channels:', error);
    }
}

function renderChannels(channels) {
    if (channels.length === 0) {
        iptvChannelList.innerHTML = '<div style="text-align: center; padding: 20px; color: var(--text-secondary);">Kanal bulunamadı</div>';
        return;
    }

    iptvChannelList.innerHTML = channels.map(ch => `
        <div class="channel-item ${selectedChannel && selectedChannel.id === ch.id ? 'selected' : ''}"
             onclick="selectChannel('${ch.id}')">
            <span class="channel-item-icon">📺</span>
            <span class="channel-item-name">${escapeHtml(ch.name)}</span>
            ${ch.group ? `<span class="channel-item-group">${escapeHtml(ch.group)}</span>` : ''}
        </div>
    `).join('');
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function selectChannel(channelId) {
    const channel = iptvChannels.find(c => c.id === channelId);
    if (!channel) return;

    selectedChannel = channel;
    selectedChannelInfo.style.display = 'block';
    selectedChannelName.textContent = channel.name;

    // Re-render to update selection highlight
    renderChannels(iptvChannels);
    updateStartButton();
}

// Channel search and filter
let searchTimeout = null;
channelSearch.addEventListener('input', () => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(loadChannels, 300);
});

channelGroupFilter.addEventListener('change', loadChannels);

// Initial load
loadFiles();
