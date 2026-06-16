import os
import json
import subprocess
import socket
import tempfile
import time
import threading
import signal
import logging
from urllib.parse import urlparse

import requests
from markupsafe import escape
from flask import Flask, request, jsonify, send_from_directory, make_response

app = Flask(__name__, static_folder='static')

# Surface server-side events (mpv spawn failures, bad URLs) rather than swallow them.
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = app.logger

STATIONS_FILE = os.environ.get('STATIONS_FILE', '/data/stations.json')
CITIES_FILE = os.environ.get('CITIES_FILE', '/data/cities.json')
LAYOUT_FILE = os.environ.get('LAYOUT_FILE', '/data/streamdeck_layout.json')
LAST_FILE = os.environ.get('LAST_FILE', '/data/last_station.json')
SXM_CREDS_FILE = '/data/siriusxm.json'
MPV_SOCKET = '/tmp/mpv-socket'

_current_station = None
_current_volume = 80
_mpv_process = None
_mpv_lock = threading.Lock()

_sxm_process = None
_sxm_log_fh = None

try:
    from sxm.client import SXMClient
    SXM_AVAILABLE = True
except ImportError:
    SXM_AVAILABLE = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    dir_name = os.path.dirname(path) or '.'
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _send_mpv_command(cmd: dict) -> bool:
    """Send a JSON command to the mpv IPC socket."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(MPV_SOCKET)
        sock.sendall((json.dumps(cmd) + '\n').encode())
        time.sleep(0.05)
        sock.close()
        return True
    except Exception:
        return False


def _stop_mpv():
    global _mpv_process
    if _mpv_process is not None:
        try:
            _mpv_process.terminate()
            _mpv_process.wait(timeout=3)
        except Exception:
            try:
                _mpv_process.kill()
            except Exception:
                pass
        _mpv_process = None
    try:
        os.unlink(MPV_SOCKET)
    except FileNotFoundError:
        pass


def _start_mpv(url: str) -> bool:
    global _mpv_process

    # Only ever hand mpv an http(s) stream URL (never file://, javascript:, etc.)
    if urlparse(url or '').scheme.lower() not in ('http', 'https'):
        log.warning('Refusing to play non-http(s) URL: %r', url)
        return False

    _stop_mpv()

    env = os.environ.copy()
    audio_out = os.environ.get('AUDIO_OUTPUT', 'pulse')
    cmd = [
        'mpv',
        f'--input-ipc-server={MPV_SOCKET}',
        f'--volume={_current_volume}',
        '--no-video',
        '--really-quiet',
        '--cache=yes',
        '--cache-secs=5',
    ]
    if audio_out == 'alsa':
        alsa_card = os.environ.get('ALSA_CARD', '0')
        cmd += [f'--ao=alsa', f'--audio-device=alsa/hw:{alsa_card},0']
    else:
        cmd += ['--ao=pulse']
    cmd.append(url)
    try:
        _mpv_process = subprocess.Popen(cmd, env=env,
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        log.error('mpv binary not found - cannot play audio (is mpv installed?)')
        _mpv_process = None
        return False
    log.info('mpv playing %s (ao=%s)', url, audio_out)
    return True


# ---------------------------------------------------------------------------
# SXM helpers
# ---------------------------------------------------------------------------

SXM_PROXY_LOG = '/tmp/sxm-proxy.log'


def _stop_sxm_proxy():
    global _sxm_process, _sxm_log_fh
    if _sxm_process is not None:
        try:
            _sxm_process.terminate()
            _sxm_process.wait(timeout=3)
        except Exception:
            try:
                _sxm_process.kill()
            except Exception:
                pass
        _sxm_process = None
    if _sxm_log_fh is not None:
        try:
            _sxm_log_fh.close()
        except Exception:
            pass
        _sxm_log_fh = None


def _start_sxm_proxy(username: str, password: str):
    global _sxm_process, _sxm_log_fh
    _stop_sxm_proxy()
    _sxm_log_fh = open(SXM_PROXY_LOG, 'w')
    # sxm-player 0.2.5 wants credentials as --username/--password (or env vars),
    # NOT positional args. Pass via env so the password never appears in `ps`.
    env = dict(os.environ, SXM_USERNAME=username, SXM_PASSWORD=password)
    _sxm_process = subprocess.Popen(
        ['sxm', '--port', '9999', '--host', '0.0.0.0'],
        stdout=_sxm_log_fh,
        stderr=_sxm_log_fh,
        env=env,
    )


def _sxm_is_logged_in() -> bool:
    if not os.path.exists(SXM_CREDS_FILE):
        return False
    creds = _load_json(SXM_CREDS_FILE, {})
    return bool(creds.get('username') and creds.get('password'))


def _sxm_proxy_running() -> bool:
    if _sxm_process is None:
        return False
    return _sxm_process.poll() is None


def _autostart_sxm():
    """Called at startup — re-launch proxy if credentials exist."""
    if os.path.exists(SXM_CREDS_FILE):
        creds = _load_json(SXM_CREDS_FILE, {})
        username = creds.get('username', '')
        password = creds.get('password', '')
        if username and password:
            _start_sxm_proxy(username, password)


# Hard-coded channel list that mirrors what sxm-player exposes.
# If the sxm library is importable we use it directly; otherwise fall back.
_SXM_CHANNEL_FALLBACK = [
    {'id': 'siriushits1', 'name': 'Hits 1', 'genre': 'Pop'},
    {'id': 'thebeat', 'name': 'The Beat', 'genre': 'Hip-Hop'},
    {'id': 'octane', 'name': 'Octane', 'genre': 'Rock'},
    {'id': 'lithium', 'name': 'Lithium', 'genre': "90's Alt"},
    {'id': '1stwave', 'name': '1st Wave', 'genre': 'New Wave'},
    {'id': 'altcountry', 'name': 'The Highway', 'genre': 'Country'},
    {'id': 'siriuscountry', 'name': 'Prime Country', 'genre': 'Classic Country'},
    {'id': 'willies', 'name': "Willie's Roadhouse", 'genre': 'Country'},
    {'id': 'classicvinyl', 'name': 'Classic Vinyl', 'genre': 'Classic Rock'},
    {'id': 'classicrewind', 'name': 'Classic Rewind', 'genre': 'Classic Rock'},
    {'id': 'thebridge', 'name': 'The Bridge', 'genre': 'Soft Rock'},
    {'id': 'jazzcat', 'name': 'Real Jazz', 'genre': 'Jazz'},
    {'id': 'soultown', 'name': 'Soul Town', 'genre': 'Soul/R&B'},
    {'id': 'thevibe', 'name': 'The Vibe', 'genre': 'R&B'},
    {'id': 'siriusflyby', 'name': 'FlyBy', 'genre': 'Pop'},
    {'id': 'bpmradio', 'name': 'BPM', 'genre': 'Dance/EDM'},
    {'id': 'electricarea', 'name': 'Electric Area', 'genre': 'EDM'},
    {'id': 'cinemagic', 'name': 'Cinemagic', 'genre': 'Soundtracks'},
    {'id': 'pops', 'name': 'Symphony Hall', 'genre': 'Classical'},
    {'id': 'metrocast', 'name': 'Met Opera Radio', 'genre': 'Opera'},
    {'id': 'latinpop', 'name': 'Caliente', 'genre': 'Latin Pop'},
    {'id': 'reggaeton', 'name': 'Pitbull\'s Globalization', 'genre': 'Reggaeton'},
    {'id': 'radiorythme', 'name': 'Radio Rythme', 'genre': 'French'},
    {'id': 'kidsstuff', 'name': "Kid's Stuff", 'genre': 'Kids'},
    {'id': 'hadithhits', 'name': 'Backspin', 'genre': 'Old School Hip-Hop'},
    {'id': 'ch18', 'name': 'Turbo', 'genre': 'Dance'},
    {'id': 'ch22', 'name': 'Shade 45', 'genre': 'Hip-Hop'},
    {'id': 'ch26', 'name': 'Hip-Hop Nation', 'genre': 'Hip-Hop'},
    {'id': 'ch33', 'name': 'The Pulse', 'genre': 'Pop Hits'},
    {'id': 'ch36', 'name': 'Pop2K', 'genre': '2000s Pop'},
]


def _get_sxm_channels():
    """Return list of SXM channel dicts. Uses library if available."""
    if SXM_AVAILABLE:
        try:
            creds = _load_json(SXM_CREDS_FILE, {})
            username = creds.get('username', '')
            password = creds.get('password', '')
            client = SXMClient(username, password)
            raw_channels = client.channels
            channels = []
            for ch in raw_channels:
                ch_id = getattr(ch, 'channel_id', None) or getattr(ch, 'id', '')
                name = getattr(ch, 'name', '') or str(ch)
                genre = getattr(ch, 'genre_name', '') or ''
                channels.append({
                    'id': ch_id,
                    'name': name,
                    'url': f'http://localhost:9999/{ch_id}.m3u8',
                    'genre': genre,
                    'color': '#1a1a8c',
                    'source': 'siriusxm',
                    'slogan': genre,
                    'city': 'SiriusXM',
                })
            return channels
        except Exception:
            pass

    # Fallback to hard-coded list
    return [
        {
            'id': ch['id'],
            'name': ch['name'],
            'url': f"http://localhost:9999/{ch['id']}.m3u8",
            'genre': ch['genre'],
            'color': '#1a1a8c',
            'source': 'siriusxm',
            'slogan': ch['genre'],
            'city': 'SiriusXM',
        }
        for ch in _SXM_CHANNEL_FALLBACK
    ]


# ---------------------------------------------------------------------------
# API – Stations
# ---------------------------------------------------------------------------

@app.route('/api/stations', methods=['GET'])
def api_get_stations():
    return jsonify(_load_json(STATIONS_FILE, []))


@app.route('/api/stations', methods=['POST'])
def api_save_stations():
    data = request.get_json(force=True)
    if not isinstance(data, list):
        return jsonify({'error': 'Expected a JSON array'}), 400
    _save_json(STATIONS_FILE, data)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# API – Cities
# ---------------------------------------------------------------------------

@app.route('/api/cities', methods=['GET'])
def api_get_cities():
    return jsonify(_load_json(CITIES_FILE, []))


@app.route('/api/cities', methods=['POST'])
def api_save_cities():
    data = request.get_json(force=True)
    if not isinstance(data, list):
        return jsonify({'error': 'Expected a JSON array'}), 400
    _save_json(CITIES_FILE, data)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# API – Layout
# ---------------------------------------------------------------------------

@app.route('/api/layout', methods=['GET'])
def api_get_layout():
    return jsonify(_load_json(LAYOUT_FILE, {}))


@app.route('/api/layout', methods=['POST'])
def api_save_layout():
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Expected a JSON object'}), 400
    _save_json(LAYOUT_FILE, data)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# API – Playback
# ---------------------------------------------------------------------------

@app.route('/api/play', methods=['POST'])
def api_play():
    global _current_station
    data = request.get_json(force=True)
    station_id = data.get('id')

    # Inline station (e.g. SiriusXM channels not stored in stations.json)
    if data.get('url'):
        station = {
            'id': station_id or 'sxm-inline',
            'name': data.get('name', 'SiriusXM'),
            'url': data['url'],
            'genre': data.get('genre', ''),
            'color': data.get('color', '#1a1a8c'),
            'source': data.get('source', 'siriusxm'),
            'slogan': data.get('slogan', ''),
            'city': data.get('city', 'SiriusXM'),
        }
        with _mpv_lock:
            if not _start_mpv(station['url']):
                return jsonify({'ok': False, 'error': 'Playback failed to start'})
            _current_station = station
            _save_json(LAST_FILE, station)
        return jsonify({'ok': True, 'station': station})

    stations = _load_json(STATIONS_FILE, [])
    station = next((s for s in stations if s['id'] == station_id), None)
    if not station:
        return jsonify({'error': 'Station not found'}), 404

    with _mpv_lock:
        if not _start_mpv(station['url']):
            return jsonify({'ok': False, 'error': 'Playback failed to start'})
        _current_station = station
        _save_json(LAST_FILE, station)

    return jsonify({'ok': True, 'station': station})


@app.route('/api/stop', methods=['POST'])
def api_stop():
    global _current_station
    with _mpv_lock:
        _stop_mpv()
        _current_station = None
    return jsonify({'ok': True})


@app.route('/api/volume', methods=['POST'])
def api_volume():
    global _current_volume
    data = request.get_json(force=True)
    try:
        vol = int(data.get('volume', 80))
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid volume value'}), 400
    vol = max(0, min(100, vol))
    _current_volume = vol
    _send_mpv_command({'command': ['set_property', 'volume', vol]})
    return jsonify({'ok': True, 'volume': vol})


@app.route('/api/status', methods=['GET'])
def api_status():
    return jsonify({
        'playing': _current_station is not None,
        'station': _current_station,
        'volume': _current_volume,
        'last': _load_json(LAST_FILE, None),
    })


# ---------------------------------------------------------------------------
# API – Now Playing (live track + album art)
# ---------------------------------------------------------------------------
import re as _re

_np_cache = {}

def _read_icy(url, timeout=7):
    try:
        r = requests.get(url, headers={'Icy-MetaData': '1', 'User-Agent': 'iTunes/12'},
                         stream=True, timeout=timeout)
        mi = r.headers.get('icy-metaint')
        if not mi:
            r.close(); return None
        mi = int(mi); r.raw.read(mi)
        n = r.raw.read(1); ln = (n[0] if n else 0) * 16
        meta = r.raw.read(ln).decode('utf-8', 'ignore'); r.close()
        return meta
    except Exception:
        return None

def _parse_icy(meta):
    art = None
    am = _re.search(r'amgArtworkURL="([^"]+)"', meta or '')
    if am:
        art = am.group(1)
    st = _re.search(r"StreamTitle='([^']*)'", meta or '')
    s = (st.group(1) if st else (meta or '')).strip()
    tx = _re.search(r'text="([^"]*)"', s)
    base = _re.sub(r'\s*[A-Za-z_]+="[^"]*"', '', s).strip(' -\t')
    artist = song = None
    if tx:
        song = tx.group(1).strip()
        artist = base or None
        if artist and song and artist.lower() == song.lower():
            artist = None
    else:
        parts = base.split(' - ', 1)
        if len(parts) == 2:
            artist, song = parts[0].strip(), parts[1].strip()
        elif base:
            song = base
    def junky(x):
        return bool(x) and ('="' in x or 'song_spot' in x.lower() or 'mediabaseid' in x.lower() or 'spotinstance' in x.lower())
    if junky(song): song = None
    if junky(artist): artist = None
    return artist, song, art

def _itunes_art(query):
    try:
        r = requests.get('https://itunes.apple.com/search',
                         params={'term': query, 'entity': 'song', 'limit': 1}, timeout=6).json()
        if r.get('results'):
            a = r['results'][0]
            return (a['artworkUrl100'].replace('100x100', '600x600'),
                    a.get('artistName'), a.get('trackName'))
    except Exception:
        pass
    return None, None, None

@app.route('/api/nowplaying')
def api_nowplaying():
    sid = request.args.get('id')
    st = None
    if sid:
        st = next((s for s in _load_json(STATIONS_FILE, []) if s.get('id') == sid), None)
    if st is None:
        st = _current_station
    if not st or not st.get('url'):
        return jsonify({'playing': False})
    url = st['url']
    now = time.time()
    cached = _np_cache.get(url)
    if cached and now - cached[0] < 12:
        return jsonify(cached[1])
    artist = song = art = None
    meta = _read_icy(url)
    if meta:
        artist, song, art = _parse_icy(meta)
    track = bool(artist and song)
    if track:
        ia, ar2, tr2 = _itunes_art((artist + ' ' + song).strip())
        if ia:
            art = ia; artist = ar2 or artist; song = tr2 or song
        elif art and 'iheart' in art:
            art = _re.sub(r'fit\(\d+,\d+\)', 'fit(600,600)', art)
    else:
        artist = song = art = None
    data = {'playing': True, 'station': st.get('name'), 'color': st.get('color'),
            'artist': artist, 'song': song, 'art': art, 'track': track}
    _np_cache[url] = (now, data)
    return jsonify(data)


# ---------------------------------------------------------------------------
# API – SiriusXM
# ---------------------------------------------------------------------------

@app.route('/api/sxm/login', methods=['POST'])
def api_sxm_login():
    data = request.get_json(force=True)
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()

    if not username or not password:
        return jsonify({'error': 'Username and password are required'}), 400

    _save_json(SXM_CREDS_FILE, {'username': username, 'password': password})
    _start_sxm_proxy(username, password)
    return jsonify({'ok': True})


@app.route('/api/sxm/logout', methods=['POST'])
def api_sxm_logout():
    _stop_sxm_proxy()
    try:
        os.unlink(SXM_CREDS_FILE)
    except FileNotFoundError:
        pass
    return jsonify({'ok': True})


@app.route('/api/sxm/status', methods=['GET'])
def api_sxm_status():
    logged_in = _sxm_is_logged_in()
    username = ''
    if logged_in:
        creds = _load_json(SXM_CREDS_FILE, {})
        username = creds.get('username', '')
    return jsonify({
        'logged_in': logged_in,
        'username': username,
        'proxy_running': _sxm_proxy_running(),
    })


@app.route('/api/sxm/logs', methods=['GET'])
def api_sxm_logs():
    try:
        with open(SXM_PROXY_LOG, 'r', errors='replace') as f:
            lines = f.readlines()
        # Return last 100 lines
        return jsonify({'log': ''.join(lines[-100:]), 'running': _sxm_proxy_running()})
    except FileNotFoundError:
        return jsonify({'log': '(no log yet)', 'running': _sxm_proxy_running()})


@app.route('/api/sxm/channels', methods=['GET'])
def api_sxm_channels():
    if not _sxm_is_logged_in():
        return jsonify({'error': 'Not logged in'}), 401
    return jsonify(_get_sxm_channels())


# ---------------------------------------------------------------------------
# API – Search (Radio Browser + SomaFM + Radio Garden)
# ---------------------------------------------------------------------------

RADIO_BROWSER_BASE = 'https://de1.api.radio-browser.info/json'
SOMAFM_API = 'https://api.somafm.com/channels.json'
RADIO_GARDEN_SEARCH = 'https://radio.garden/api/ara/content/search'
RADIO_GARDEN_LISTEN = 'https://radio.garden/api/ara/content/listen/{id}/channel.mp3'


def _search_radiobrowser(q='', city='', limit=24):
    params = {
        'limit': limit,
        'hidebroken': 'true',
        'order': 'clickcount',
        'reverse': 'true',
    }
    if q:
        params['name'] = q
    if city:
        params['state'] = city

    resp = requests.get(
        f'{RADIO_BROWSER_BASE}/stations/search',
        params=params,
        timeout=10,
        headers={'User-Agent': 'OasisRadio/1.0'},
    )
    resp.raise_for_status()
    raw = resp.json()

    results = []
    for s in raw:
        url = s.get('url_resolved') or s.get('url', '')
        if not url:
            continue
        tags = s.get('tags', '') or ''
        genre = tags.split(',')[0].strip().title() if tags else ''
        results.append({
            'id': s.get('stationuuid', ''),
            'name': s.get('name', '').strip(),
            'slogan': s.get('tags', '').replace(',', ' · ')[:60],
            'genre': genre,
            'city': (s.get('state') or s.get('country') or '').strip(),
            'url': url,
            'color': '#e8420a',
            'source': 'radiobrowser',
        })
    return results


def _search_somafm(q=''):
    resp = requests.get(SOMAFM_API, timeout=10,
                        headers={'User-Agent': 'OasisRadio/1.0'})
    resp.raise_for_status()
    data = resp.json()

    q_lower = q.lower()
    results = []
    for ch in data.get('channels', []):
        if q_lower:
            haystack = ' '.join([
                ch.get('title', ''),
                ch.get('genre', ''),
                ch.get('description', ''),
            ]).lower()
            if q_lower not in haystack:
                continue
        # Prefer AAC, then mp3
        url = ''
        playlists = ch.get('playlists', [])
        for pl in playlists:
            if pl.get('format') == 'aac' and 'url' in pl:
                url = pl['url']
                break
        if not url:
            for pl in playlists:
                if pl.get('format') == 'mp3' and 'url' in pl:
                    url = pl['url']
                    break
        if not url and playlists:
            url = playlists[0].get('url', '')
        if not url:
            continue

        results.append({
            'id': 'somafm-' + ch.get('id', ''),
            'name': ch.get('title', '').strip(),
            'slogan': ch.get('description', '')[:60],
            'genre': ch.get('genre', '').strip().title(),
            'city': 'SomaFM',
            'url': url,
            'color': '#2d2d2d',
            'source': 'somafm',
        })
    return results


def _search_radiogarden(q='', lat='', lng=''):
    params = {}
    if q:
        params['q'] = q
    if lat:
        params['lat'] = lat
    if lng:
        params['lng'] = lng

    resp = requests.get(
        RADIO_GARDEN_SEARCH,
        params=params,
        timeout=10,
        headers={'User-Agent': 'OasisRadio/1.0'},
    )
    resp.raise_for_status()
    data = resp.json()

    results = []
    hits = (data.get('hits') or {}).get('hits') or []
    for hit in hits:
        src = hit.get('_source', {})
        ch_id = src.get('channelId') or hit.get('_id', '')
        name = src.get('title', '').strip()
        city_name = (src.get('place') or {}).get('title', '')
        if not ch_id or not name:
            continue
        url = RADIO_GARDEN_LISTEN.format(id=ch_id)
        results.append({
            'id': 'rg-' + ch_id,
            'name': name,
            'slogan': city_name,
            'genre': 'Radio Garden',
            'city': city_name,
            'url': url,
            'color': '#2e7d32',
            'source': 'radiogarden',
        })
    return results


@app.route('/api/search', methods=['GET'])
def api_search():
    q = request.args.get('q', '').strip()
    city = request.args.get('city', '').strip()
    source = request.args.get('source', '').strip().lower()

    if not source:
        source = 'radiobrowser'

    if source == 'radiobrowser':
        try:
            return jsonify(_search_radiobrowser(q=q, city=city))
        except Exception as exc:
            return jsonify({'error': str(exc)}), 502

    if source == 'somafm':
        try:
            return jsonify(_search_somafm(q=q))
        except Exception as exc:
            return jsonify({'error': str(exc)}), 502

    if source == 'radiogarden':
        lat = request.args.get('lat', '').strip()
        lng = request.args.get('lng', '').strip()
        try:
            return jsonify(_search_radiogarden(q=q, lat=lat, lng=lng))
        except Exception as exc:
            return jsonify({'error': str(exc)}), 502

    if source == 'all':
        combined = []
        seen_urls = set()

        def _add(items):
            for item in items:
                if item.get('url') and item['url'] not in seen_urls:
                    seen_urls.add(item['url'])
                    combined.append(item)

        try:
            _add(_search_radiobrowser(q=q, city=city))
        except Exception:
            pass
        try:
            _add(_search_somafm(q=q))
        except Exception:
            pass
        try:
            _add(_search_radiogarden(q=q))
        except Exception:
            pass

        return jsonify(combined)

    return jsonify({'error': f'Unknown source: {source}'}), 400


@app.route('/api/search/somafm', methods=['GET'])
def api_search_somafm():
    q = request.args.get('q', '').strip()
    try:
        return jsonify(_search_somafm(q=q))
    except Exception as exc:
        return jsonify({'error': str(exc)}), 502


@app.route('/api/search/radiogarden', methods=['GET'])
def api_search_radiogarden():
    q = request.args.get('q', '').strip()
    lat = request.args.get('lat', '').strip()
    lng = request.args.get('lng', '').strip()
    try:
        return jsonify(_search_radiogarden(q=q, lat=lat, lng=lng))
    except Exception as exc:
        return jsonify({'error': str(exc)}), 502


# ---------------------------------------------------------------------------
# HTML partials (htmx)
# ---------------------------------------------------------------------------

def _render_result_card(station, saved_ids, saved_urls):
    esc = escape
    is_saved = station['id'] in saved_ids or station.get('url', '') in saved_urls
    is_sxm = station.get('source') == 'siriusxm'
    sxm_badge = f' <span class="sxm-badge">SXM</span>' if is_sxm else ''
    sxm_class = ' sxm-card' if is_sxm else ''
    meta = ' &middot; '.join(
        esc(v) for v in [station.get('genre', ''), station.get('city', '')] if v
    )
    station_json = json.dumps(station).replace("'", "&#39;").replace('"', "&quot;")

    sxm_play_btn = ''
    if is_sxm:
        sxm_play_btn = (
            f'<button class="play-btn sxm-play-btn" title="Play"'
            f' style="width:32px;height:32px;border-radius:50%;border:none;'
            f'background:var(--sxm-blue);color:#fff;cursor:pointer;display:flex;'
            f'align-items:center;justify-content:center;flex-shrink:0"'
            f' onclick="playSxmStation({station_json})">'
            f'<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">'
            f'<polygon points="5 3 19 12 5 21 5 3"/></svg></button>'
        )

    if is_saved:
        add_btn = '<button class="add-btn" disabled>Saved</button>'
    else:
        add_btn = (
            f'<button class="add-btn"'
            f' hx-post="/api/stations/add" hx-swap="outerHTML"'
            f' hx-vals=\'{json.dumps(station)}\'>'
            f'+ Add</button>'
        )

    return (
        f'<div class="result-card{sxm_class}">'
        f'<div class="result-info">'
        f'<div class="result-name">{esc(station["name"])}{sxm_badge}</div>'
        f'<div class="result-meta">{meta}</div>'
        f'</div>'
        f'<div style="display:flex;gap:6px;align-items:center">'
        f'{sxm_play_btn}{add_btn}'
        f'</div></div>'
    )


def _render_results_html(stations_list):
    saved = _load_json(STATIONS_FILE, [])
    saved_ids = {s.get('id') for s in saved}
    saved_urls = {s.get('url') for s in saved}
    if not stations_list:
        return '<div style="color:var(--text-dim);font-size:13px;grid-column:1/-1;padding:10px 0">No results found.</div>'
    return ''.join(
        _render_result_card(s, saved_ids, saved_urls) for s in stations_list
    )


def _render_error_html(msg):
    esc_msg = escape(str(msg))
    return f'<div style="color:#e04040;font-size:13px;grid-column:1/-1;padding:10px 0">Error: {esc_msg}</div>'


@app.route('/api/stations/add', methods=['POST'])
def api_add_station():
    station = request.get_json(force=True)
    if not isinstance(station, dict) or 'id' not in station:
        return _render_error_html('Invalid station data'), 400
    stations = _load_json(STATIONS_FILE, [])
    if not any(s.get('id') == station['id'] for s in stations):
        stations.append(station)
        _save_json(STATIONS_FILE, stations)
    resp = make_response('<button class="add-btn" disabled>Saved</button>')
    resp.headers['HX-Trigger'] = 'stationAdded'
    return resp


@app.route('/partials/somafm', methods=['GET'])
def partials_somafm():
    q = request.args.get('q', '').strip()
    try:
        results = _search_somafm(q=q)
        return _render_results_html(results)
    except Exception as exc:
        return _render_error_html(exc)


@app.route('/partials/search', methods=['GET'])
def partials_search():
    q = request.args.get('q', '').strip()
    city = request.args.get('city', '').strip()
    source = request.args.get('source', '').strip().lower() or 'all'

    try:
        if source == 'radiobrowser':
            results = _search_radiobrowser(q=q, city=city)
        elif source == 'somafm':
            results = _search_somafm(q=q)
        elif source == 'radiogarden':
            lat = request.args.get('lat', '').strip()
            lng = request.args.get('lng', '').strip()
            results = _search_radiogarden(q=q, lat=lat, lng=lng)
        elif source == 'all':
            combined = []
            seen = set()

            def _add(items):
                for item in items:
                    u = item.get('url', '')
                    if u and u not in seen:
                        seen.add(u)
                        combined.append(item)

            try:
                _add(_search_radiobrowser(q=q, city=city))
            except Exception:
                pass
            try:
                _add(_search_somafm(q=q))
            except Exception:
                pass
            try:
                _add(_search_radiogarden(q=q))
            except Exception:
                pass
            results = combined
        else:
            return _render_error_html(f'Unknown source: {source}')
        return _render_results_html(results)
    except Exception as exc:
        return _render_error_html(exc)


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('static', path)


# ---------------------------------------------------------------------------
# Entry point (dev only — prod uses gunicorn)
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    _autostart_sxm()
    app.run(host='0.0.0.0', port=5000, debug=True)
else:
    # Also auto-start when loaded by gunicorn
    _autostart_sxm()
