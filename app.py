"""Drone Manager (DJI Mini 3) - app web local.  Rodar: uvicorn app:app --reload"""
import os, re, json, csv, io, time, html, ctypes, ctypes.util, shutil, sqlite3, hashlib, subprocess, zipfile, tempfile, threading, urllib.request, urllib.parse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from math import radians, degrees, sin, cos, tan, asin, acos, atan, atan2, exp, pi, sqrt, hypot, ceil, floor
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

_THUMB_SEM = threading.BoundedSemaphore(4)

def _unlink_quiet(p: Path):
    try: p.unlink(missing_ok=True)
    except OSError: pass

if Path(".env").is_file():
    for _ln in Path(".env").read_text(encoding="utf-8", errors="ignore").splitlines():
        _ln = _ln.strip()
        if _ln and not _ln.startswith("#") and "=" in _ln:
            _k, _v = _ln.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

LOGS_DIR = Path(os.getenv("LOGS_DIR", "data/logs"))      # pasta FlightRecord copiada do celular
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "data/media"))   # fotos/vídeos/.SRT do cartão SD
BACKUP_DIR = os.getenv("BACKUP_DIR")                     # opcional: pasta de backup
API_KEY = os.getenv("DJI_API_KEY", "")                   # chave DJI Developer (logs v13+)
DJI_LOG_BIN = os.getenv("DJI_LOG_BIN", "dji-log")
TZ_OFFSET = float(os.getenv("TZ_OFFSET", "-3"))          # fuso local (Rio = -3)
AIRSPACE_TILES = os.getenv("AIRSPACE_TILES", "")         # URL de tiles de espaço aéreo (opcional)
PROP_LIMIT_H = float(os.getenv("PROP_LIMIT_H", "50"))    # horas até trocar hélices
THUMBS = Path("data/thumbs"); THUMBS.mkdir(parents=True, exist_ok=True)
ORTHOS = Path("data/orthos"); ORTHOS.mkdir(parents=True, exist_ok=True)
for d in (LOGS_DIR, MEDIA_DIR):
    try: d.mkdir(parents=True, exist_ok=True)
    except OSError: pass
IMG, VID = {".jpg", ".jpeg", ".dng"}, {".mp4", ".mov"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS flights(id INTEGER PRIMARY KEY, file TEXT UNIQUE, start TEXT, duration REAL, distance REAL,
  max_alt REAL, max_speed REAL, battery_sn TEXT, lat REAL, lon REAL, place TEXT, weather TEXT, track TEXT, events TEXT, notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS media(id INTEGER PRIMARY KEY, path TEXT UNIQUE, kind TEXT, taken TEXT, lat REAL, lon REAL, alt REAL,
  w INT, h INT, size INT, sha1 TEXT, flight_id INT, tags TEXT DEFAULT '', fav INT DEFAULT 0, backed INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS spots(id INTEGER PRIMARY KEY, name TEXT, lat REAL, lon REAL, notes TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""

# ---------- Motor Espacial GIS: Nível 1 (SQLite R-Tree + B-Tree) & Nível 2 (SpatiaLite / GEOS / PROJ) ----------
def hav(a, b, c, d):
    p = radians
    x = sin(p(c - a) / 2) ** 2 + cos(p(a)) * cos(p(c)) * sin(p(d - b) / 2) ** 2
    return 12742000 * asin(min(1.0, sqrt(max(0.0, x))))

def valid_gps(lat, lon) -> bool:
    """Valida coordenadas WGS 84 (EPSG:4326) e descarta Null Island (0.0, 0.0)."""
    return (isinstance(lat, (int, float)) and isinstance(lon, (int, float))
            and not isinstance(lat, bool) and not isinstance(lon, bool)
            and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0
            and (abs(lat) > 1e-5 or abs(lon) > 1e-5))

_GIS_INFO = {
    "sqlite_version": sqlite3.sqlite_version,
    "rtree_enabled": True,
    "spatialite_mode": "builtin",
    "spatialite_version": None,
    "geos_version": None,
    "proj_available": False,
}
_GAIA_GC_DIST = None

def _init_spatialite_libs():
    global _GAIA_GC_DIST
    for cand in ("/usr/lib/x86_64-linux-gnu/libspatialite.so.8", "libspatialite.so.8", "libspatialite.so"):
        try:
            lib = ctypes.CDLL(cand)
            if hasattr(lib, "spatialite_version"):
                lib.spatialite_version.restype = ctypes.c_char_p
                _GIS_INFO["spatialite_version"] = lib.spatialite_version().decode("utf-8", errors="ignore")
                _GIS_INFO["spatialite_mode"] = "libspatialite"
            if hasattr(lib, "gaiaGreatCircleDistance"):
                fn = lib.gaiaGreatCircleDistance
                fn.argtypes = [ctypes.c_double] * 6
                fn.restype = ctypes.c_double
                _GAIA_GC_DIST = fn
            break
        except Exception:
            continue
    try:
        gpath = ctypes.util.find_library("geos_c")
        if gpath:
            glib = ctypes.CDLL(gpath)
            if hasattr(glib, "GEOSversion"):
                glib.GEOSversion.restype = ctypes.c_char_p
                _GIS_INFO["geos_version"] = glib.GEOSversion().decode("utf-8", errors="ignore")
    except Exception:
        pass
    try:
        _GIS_INFO["proj_available"] = bool(ctypes.util.find_library("proj"))
    except Exception:
        pass

_init_spatialite_libs()

def _st_distance_sphere(lat1, lon1, lat2, lon2):
    """Distância geodésica WGS 84 em metros (via libspatialite gaiaGreatCircleDistance ou Haversine)."""
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return None
    try:
        la1, lo1, la2, lo2 = float(lat1), float(lon1), float(lat2), float(lon2)
        if _GAIA_GC_DIST is not None:
            return float(_GAIA_GC_DIST(6378137.0, 6356752.314245179, la1, lo1, la2, lo2))
        return float(hav(la1, lo1, la2, lo2))
    except Exception:
        return None

def _st_makepoint_geojson(lon, lat, alt=None):
    """Cria geometria GeoJSON Point em WGS 84 (EPSG:4326) seguindo RFC 7946 [lon, lat, alt]."""
    if not valid_gps(lat, lon):
        return None
    coords = [round(float(lon), 6), round(float(lat), 6)]
    if alt is not None:
        try: coords.append(round(float(alt), 1))
        except Exception: pass
    return json.dumps({"type": "Point", "coordinates": coords})

def _st_makeenvelope_geojson(min_lon, min_lat, max_lon, max_lat):
    """Cria geometria GeoJSON Polygon a partir de um envelope [min_lon, min_lat, max_lon, max_lat] (EPSG:4326)."""
    try:
        x0, y0, x1, y1 = round(float(min_lon), 6), round(float(min_lat), 6), round(float(max_lon), 6), round(float(max_lat), 6)
        ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
        return json.dumps({"type": "Polygon", "coordinates": [ring]})
    except Exception:
        return None

def _st_utm_epsg(lat, lon):
    """Retorna o código EPSG SIRGAS 2000 / UTM (ex.: 31983 para Zona 23S no Rio/SP) ou WGS 84 UTM."""
    if not valid_gps(lat, lon):
        return None
    zone = int((float(lon) + 180.0) // 6.0) + 1
    if -35.0 <= float(lat) <= 6.0 and -75.0 <= float(lon) <= -28.0 and 18 <= zone <= 25:
        return (31960 + zone) if float(lat) < 0 else (31954 + zone)
    return (32700 + zone) if float(lat) < 0 else (32600 + zone)

def _st_area_utm_m2(geojson_or_pts):
    """Calcula a área plana em m² projetando o polígono para coordenadas métricas locais (SIRGAS 2000 / UTM)."""
    try:
        pts = json.loads(geojson_or_pts) if isinstance(geojson_or_pts, str) else geojson_or_pts
        if isinstance(pts, dict):
            coords = pts.get("coordinates", [[]])[0]
            ring = [(float(p[1]), float(p[0])) for p in coords]
        else:
            ring = [(float(p[0]), float(p[1])) for p in pts]
        if len(ring) < 3:
            return 0.0
        lat0 = radians(sum(p[0] for p in ring) / len(ring))
        lon0 = sum(p[1] for p in ring) / len(ring)
        m_lat = 111132.92 - 559.82 * cos(2 * lat0)
        m_lon = 111412.84 * cos(lat0)
        xy = [((p[1] - lon0) * m_lon, (p[0] - degrees(lat0)) * m_lat) for p in ring]
        area2 = 0.0
        for i in range(len(xy)):
            x1, y1 = xy[i]
            x2, y2 = xy[(i + 1) % len(xy)]
            area2 += x1 * y2 - x2 * y1
        return round(abs(area2) * 0.5, 2)
    except Exception:
        return 0.0

def _register_gis_functions(c: sqlite3.Connection):
    """Tenta carregar mod_spatialite (se instalado) e registra funções SQL espaciais Nível 2 na conexão."""
    try:
        c.enable_load_extension(True)
        for mod in ("mod_spatialite", "mod_spatialite.so"):
            try:
                c.load_extension(mod)
                _GIS_INFO["spatialite_mode"] = "mod_spatialite"
                break
            except Exception:
                pass
    except Exception:
        pass
    c.create_function("ST_Distance_Sphere", 4, _st_distance_sphere)
    c.create_function("ST_MakePointGeoJSON", 2, _st_makepoint_geojson)
    c.create_function("ST_MakePointGeoJSON3D", 3, _st_makepoint_geojson)
    c.create_function("ST_MakeEnvelopeGeoJSON", 4, _st_makeenvelope_geojson)
    c.create_function("ST_UTM_EPSG", 2, _st_utm_epsg)
    c.create_function("ST_Area_UTM", 1, _st_area_utm_m2)

def _bbox_for_radius(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """Calcula Bounding Box [min_lon, max_lon, min_lat, max_lat] em graus WGS 84 para pré-filtro em R-Tree."""
    dlat = (radius_m / 111000.0) * 1.05
    cos_lat = max(0.05, cos(radians(max(-85.0, min(85.0, float(lat))))))
    dlon = (radius_m / (111000.0 * cos_lat)) * 1.05
    return (float(lon) - dlon, float(lon) + dlon, float(lat) - dlat, float(lat) + dlat)

def _parse_bbox_param(bbox: str) -> Optional[tuple[float, float, float, float]]:
    """Valida string 'min_lon,min_lat,max_lon,max_lat' e retorna (min_lon, max_lon, min_lat, max_lat)."""
    if not bbox or not bbox.strip():
        return None
    try:
        parts = [float(x) for x in bbox.split(",")]
        if len(parts) != 4:
            raise ValueError("4 valores esperados")
        min_lon, min_lat, max_lon, max_lat = parts
        if min_lon > max_lon: min_lon, max_lon = max_lon, min_lon
        if min_lat > max_lat: min_lat, max_lat = max_lat, min_lat
        return (min_lon, max_lon, min_lat, max_lat)
    except Exception:
        raise HTTPException(400, "Formato inválido para 'bbox'. Use min_lon,min_lat,max_lon,max_lat.")

def _flight_track_bbox(lat, lon, track_raw) -> Optional[tuple[float, float, float, float]]:
    """Extrai o envelope espacial real [min_lon, max_lon, min_lat, max_lat] de toda a trajetória do voo."""
    lats, lons = [], []
    if valid_gps(lat, lon):
        lats.append(float(lat)); lons.append(float(lon))
    if track_raw:
        try:
            pts = json.loads(track_raw) if isinstance(track_raw, str) else track_raw
            for p in (pts or []):
                if isinstance(p, (list, tuple)) and len(p) >= 3 and valid_gps(p[1], p[2]):
                    lats.append(float(p[1])); lons.append(float(p[2]))
        except Exception:
            pass
    if not lats:
        return None
    return (min(lons), max(lons), min(lats), max(lats))

def _sync_flight_rtree(c: sqlite3.Connection, fid: int, lat=None, lon=None, track_raw=None):
    """Atualiza o envelope espacial do voo na tabela virtual R-Tree (rtree_flights)."""
    if lat is None and track_raw is None:
        row = c.execute("SELECT lat, lon, track FROM flights WHERE id=?", (fid,)).fetchone()
        if not row:
            c.execute("DELETE FROM rtree_flights WHERE id=?", (fid,))
            return
        lat, lon, track_raw = row["lat"], row["lon"], row["track"]
    bb = _flight_track_bbox(lat, lon, track_raw)
    if bb:
        c.execute("INSERT OR REPLACE INTO rtree_flights(id, min_lon, max_lon, min_lat, max_lat) VALUES(?,?,?,?,?)",
                  (fid, bb[0], bb[1], bb[2], bb[3]))
    else:
        c.execute("DELETE FROM rtree_flights WHERE id=?", (fid,))

@contextmanager
def db():
    c = sqlite3.connect("data/drone.db", timeout=30.0)
    c.row_factory = sqlite3.Row
    _register_gis_functions(c)
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

with db() as _c:
    _c.execute("PRAGMA journal_mode=WAL")
    _c.execute("PRAGMA busy_timeout=30000")
    _c.executescript(SCHEMA)
with db() as _c:
    _c.execute("CREATE TABLE IF NOT EXISTS media_roots(id INTEGER PRIMARY KEY, name TEXT, path TEXT UNIQUE, tag TEXT DEFAULT '', "
               "recursive INTEGER DEFAULT 1, enabled INTEGER DEFAULT 1, last_scan TEXT)")
    if not _c.execute("SELECT 1 FROM media_roots LIMIT 1").fetchone():
        _c.execute("INSERT OR IGNORE INTO media_roots(name,path) VALUES(?,?)", ("Pasta principal", os.path.abspath(str(MEDIA_DIR))))
with db() as _c:
    fcols = [r[1] for r in _c.execute("PRAGMA table_info(flights)")]
    for col, ddl in (("air", "TEXT"), ("bat", "TEXT"), ("source", "TEXT DEFAULT 'log'"),
                     ("name", "TEXT DEFAULT ''"), ("fav", "INTEGER DEFAULT 0"), ("meta", "TEXT")):
        if col not in fcols: _c.execute(f"ALTER TABLE flights ADD COLUMN {col} {ddl}")
with db() as _c:
    mcols = [r[1] for r in _c.execute("PRAGMA table_info(media)")]
    for col, ddl in (("manual", "INTEGER DEFAULT 0"), ("sync_adj", "REAL DEFAULT 0"), ("meta", "TEXT"), ("detections", "TEXT"), ("clip_emb", "BLOB")):
        if col not in mcols: _c.execute(f"ALTER TABLE media ADD COLUMN {col} {ddl}")
    _c.execute("CREATE TABLE IF NOT EXISTS missions(id INTEGER PRIMARY KEY, name TEXT NOT NULL, waypoints TEXT NOT NULL, "
               "alt REAL DEFAULT 60, speed REAL DEFAULT 8, notes TEXT DEFAULT '', air TEXT, created TEXT)")
    _c.execute("""CREATE TABLE IF NOT EXISTS orthomosaics(
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, flight_id INTEGER, odm_url TEXT,
        task_id TEXT, status TEXT DEFAULT 'pending', progress REAL DEFAULT 0,
        photo_count INTEGER DEFAULT 0, options TEXT, tif_path TEXT, png_path TEXT,
        bounds TEXT, epsg INTEGER, gsd_cm REAL, error TEXT, created_at TEXT, finished_at TEXT
    )""")
    _c.execute("""CREATE TABLE IF NOT EXISTS ortho_annotations(
        id INTEGER PRIMARY KEY, ortho_id INTEGER, kind TEXT NOT NULL, name TEXT NOT NULL,
        notes TEXT DEFAULT '', coords TEXT NOT NULL, length_m REAL DEFAULT 0,
        area_m2 REAL DEFAULT 0, color TEXT DEFAULT '#00d2ff', created_at TEXT
    )""")
    _c.execute("UPDATE orthomosaics SET status='failed', error='Importação interrompida (reinício do servidor).' WHERE status='importing'")
    mscols = [r[1] for r in _c.execute("PRAGMA table_info(missions)")]
    if "meta" not in mscols:
        _c.execute("ALTER TABLE missions ADD COLUMN meta TEXT DEFAULT '{}'")
    _c.execute("CREATE INDEX IF NOT EXISTS idx_media_flight_id ON media(flight_id)")
    _c.execute("CREATE INDEX IF NOT EXISTS idx_media_taken ON media(taken)")
    _c.execute("CREATE INDEX IF NOT EXISTS idx_flights_start ON flights(start)")
    # Nível 1: Índices B-Tree compostos para coordenadas (lat, lon) + Tabelas Virtuais Espaciais R-Tree 2D
    _c.execute("CREATE INDEX IF NOT EXISTS idx_flights_lat_lon ON flights(lat, lon)")
    _c.execute("CREATE INDEX IF NOT EXISTS idx_media_lat_lon ON media(lat, lon)")
    _c.execute("CREATE INDEX IF NOT EXISTS idx_spots_lat_lon ON spots(lat, lon)")
    _c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS rtree_flights USING rtree(id, min_lon, max_lon, min_lat, max_lat)")
    _c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS rtree_media USING rtree(id, min_lon, max_lon, min_lat, max_lat)")
    _c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS rtree_spots USING rtree(id, min_lon, max_lon, min_lat, max_lat)")
    _c.executescript("""
    CREATE TRIGGER IF NOT EXISTS trg_media_rtree_ins AFTER INSERT ON media
    WHEN NEW.lat IS NOT NULL AND NEW.lon IS NOT NULL AND (ABS(NEW.lat) > 1e-5 OR ABS(NEW.lon) > 1e-5)
    BEGIN
      INSERT OR REPLACE INTO rtree_media(id, min_lon, max_lon, min_lat, max_lat)
      VALUES (NEW.id, NEW.lon, NEW.lon, NEW.lat, NEW.lat);
    END;
    CREATE TRIGGER IF NOT EXISTS trg_media_rtree_upd AFTER UPDATE OF lat, lon ON media
    BEGIN
      DELETE FROM rtree_media WHERE id = OLD.id;
      INSERT OR REPLACE INTO rtree_media(id, min_lon, max_lon, min_lat, max_lat)
      SELECT NEW.id, NEW.lon, NEW.lon, NEW.lat, NEW.lat
      WHERE NEW.lat IS NOT NULL AND NEW.lon IS NOT NULL AND (ABS(NEW.lat) > 1e-5 OR ABS(NEW.lon) > 1e-5);
    END;
    CREATE TRIGGER IF NOT EXISTS trg_media_rtree_del AFTER DELETE ON media
    BEGIN
      DELETE FROM rtree_media WHERE id = OLD.id;
    END;
    CREATE TRIGGER IF NOT EXISTS trg_spots_rtree_ins AFTER INSERT ON spots
    WHEN NEW.lat IS NOT NULL AND NEW.lon IS NOT NULL
    BEGIN
      INSERT OR REPLACE INTO rtree_spots(id, min_lon, max_lon, min_lat, max_lat)
      VALUES (NEW.id, NEW.lon, NEW.lon, NEW.lat, NEW.lat);
    END;
    CREATE TRIGGER IF NOT EXISTS trg_spots_rtree_upd AFTER UPDATE OF lat, lon ON spots
    BEGIN
      DELETE FROM rtree_spots WHERE id = OLD.id;
      INSERT OR REPLACE INTO rtree_spots(id, min_lon, max_lon, min_lat, max_lat)
      SELECT NEW.id, NEW.lon, NEW.lon, NEW.lat, NEW.lat
      WHERE NEW.lat IS NOT NULL AND NEW.lon IS NOT NULL;
    END;
    CREATE TRIGGER IF NOT EXISTS trg_spots_rtree_del AFTER DELETE ON spots
    BEGIN
      DELETE FROM rtree_spots WHERE id = OLD.id;
    END;
    CREATE TRIGGER IF NOT EXISTS trg_flights_rtree_del AFTER DELETE ON flights
    BEGIN
      DELETE FROM rtree_flights WHERE id = OLD.id;
    END;
    """)
    # Backfill idempotente dos índices espaciais R-Tree
    _c.execute("""
    INSERT OR REPLACE INTO rtree_media(id, min_lon, max_lon, min_lat, max_lat)
    SELECT id, lon, lon, lat, lat FROM media
    WHERE lat IS NOT NULL AND lon IS NOT NULL AND (ABS(lat) > 1e-5 OR ABS(lon) > 1e-5)
      AND id NOT IN (SELECT id FROM rtree_media)
    """)
    _c.execute("""
    INSERT OR REPLACE INTO rtree_spots(id, min_lon, max_lon, min_lat, max_lat)
    SELECT id, lon, lon, lat, lat FROM spots
    WHERE lat IS NOT NULL AND lon IS NOT NULL
      AND id NOT IN (SELECT id FROM rtree_spots)
    """)
    for _fr in _c.execute("SELECT id, lat, lon, track FROM flights WHERE id NOT IN (SELECT id FROM rtree_flights)").fetchall():
        _sync_flight_rtree(_c, _fr["id"], _fr["lat"], _fr["lon"], _fr["track"])

def kv_get(k: str, default=None):
    with db() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

def kv_set(k: str, v: str):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, str(v)))

SUPPORTED_LANGS = ("pt-BR", "en", "es")
_I18N_CACHE = {}

def load_i18n(lang: str) -> dict:
    lang = lang if lang in SUPPORTED_LANGS else "pt-BR"
    if lang not in _I18N_CACHE:
        p = Path("static/i18n") / f"{lang}.json"
        _I18N_CACHE[lang] = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    return _I18N_CACHE[lang]

def get_lang(lang: str = "") -> str:
    if lang in SUPPORTED_LANGS: return lang
    saved = kv_get("lang", "pt-BR")
    return saved if saved in SUPPORTED_LANGS else "pt-BR"

def tr(lang: str, key: str, **kw) -> str:
    d = load_i18n(lang)
    s = d.get(key) or load_i18n("pt-BR").get(key) or key
    return s.format(**kw) if kw else s

_EVENT_TRANS = {
    "en": {
        "Modo normal (GPS)": "Normal mode (GPS)", "Modo iniciante": "Beginner mode",
        "Modo esportivo": "Sport mode", "Modo Cine (tripé)": "Cine mode (tripod)",
        "Retorno automático (RTH)": "Return to Home (RTH)", "Pouso automático": "Auto landing",
        "Aguardando confirmação de pouso": "Waiting landing confirmation", "Pouso avançado": "Advanced landing",
        "Decolagem automática (app)": "Auto takeoff (app)", "Pouso automático pedido pelo app": "Auto landing requested by app",
        "Retorno automático por bateria baixa": "Low battery RTH", "Pouso automático por bateria crítica": "Critical battery auto landing",
        "Retorno automático concluído": "RTH finished", "Pouso por limite vertical baixo": "Low vertical limit landing",
        "Bateria em 20%": "Battery at 20%", "Bateria em 10%": "Battery at 10%",
        "Início de gravação de vídeo": "Video recording started", "Fim de gravação de vídeo": "Video recording stopped",
        "Foto tirada": "Photo taken", "GPS fraco (<8 satélites)": "Weak GPS (<8 satellites)",
        "Sinal RC fraco (<=20%)": "Weak RC signal (<=20%)", "Aviso de tensão baixa": "Low voltage warning",
    },
    "es": {
        "Modo normal (GPS)": "Modo normal (GPS)", "Modo iniciante": "Modo principiante",
        "Modo esportivo": "Modo deportivo", "Modo Cine (tripé)": "Modo Cine (trípode)",
        "Retorno automático (RTH)": "Retorno automático (RTH)", "Pouso automático": "Aterrizaje automático",
        "Aguardando confirmação de pouso": "Esperando confirmación de aterrizaje", "Pouso avançado": "Aterrizaje avanzado",
        "Decolagem automática (app)": "Despegue automático (app)", "Pouso automático pedido pelo app": "Aterrizaje automático (app)",
        "Retorno automático por bateria baixa": "RTH por batería baja", "Pouso automático por bateria crítica": "Aterrizaje por batería crítica",
        "Retorno automático concluído": "RTH completado", "Pouso por limite vertical baixo": "Aterrizaje por límite vertical bajo",
        "Bateria em 20%": "Batería al 20%", "Bateria em 10%": "Batería al 10%",
        "Início de gravação de vídeo": "Inicio de grabación de video", "Fim de gravação de vídeo": "Fin de grabación de video",
        "Foto tirada": "Foto tomada", "GPS fraco (<8 satélites)": "GPS débil (<8 satélites)",
        "Sinal RC fraco (<=20%)": "Señal RC débil (<=20%)", "Aviso de tensão baixa": "Alerta de voltaje bajo",
    },
}

def tr_event(label: str, lang: str) -> str:
    if lang == "pt-BR": return label
    return _EVENT_TRANS.get(lang, {}).get(label, label)

def py_fmt_alt(m: float, lang: str) -> str:
    return f"{m * 3.28084:.0f} ft" if lang == "en" else f"{m:.0f} m"

def py_fmt_dist(m: float, lang: str) -> str:
    return f"{m / 1609.344:.2f} mi" if lang == "en" else f"{m / 1000:.2f} km"

def py_fmt_kmh(ms: float, lang: str) -> str:
    return f"{ms * 2.23694:.0f} mph" if lang == "en" else f"{ms * 3.6:.0f} km/h"

def py_fmt_spd(ms: float, lang: str) -> str:
    return f"{ms * 2.23694:.1f} mph" if lang == "en" else f"{ms:.1f} m/s"

def py_fmt_temp(c: float, lang: str) -> str:
    return f"{(c * 9 / 5) + 32:.0f} °F" if lang == "en" else f"{c} °C"

# ---------- utilidades ----------
def g(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict) or k not in d: return default
        d = d[k]
    return default if d is None else d

def dist_3d(lat1: float, lon1: float, alt1: float, lat2: float, lon2: float, alt2: float) -> float:
    """Distância 3D em metros combinando geodésica horizontal (WGS 84) e componente vertical (AGL)."""
    d2d = hav(lat1, lon1, lat2, lon2)
    dz = (alt2 or 0.0) - (alt1 or 0.0)
    return sqrt(d2d * d2d + dz * dz)

def simplify_track_rdp(pts: list, epsilon_m: float = 2.0) -> list:
    """Simplifica uma trajetória [[lat, lon], ...] usando Ramer-Douglas-Peucker em projeção métrica local."""
    if len(pts) <= 3: return pts
    lat0 = radians(pts[0][0])
    m_lat = 111132.92 - 559.82 * cos(2 * lat0)
    m_lon = 111412.84 * cos(lat0)
    xy = [((p[1] - pts[0][1]) * m_lon, (p[0] - pts[0][0]) * m_lat) for p in pts]
    def _rdp(s_i: int, e_i: int) -> list:
        if e_i - s_i <= 1: return [s_i]
        x1, y1 = xy[s_i]; x2, y2 = xy[e_i]
        dx, dy = x2 - x1, y2 - y1; norm = hypot(dx, dy)
        max_d, idx = -1.0, s_i
        for i in range(s_i + 1, e_i):
            px, py = xy[i]
            d = abs(dy * px - dx * py + x2 * y1 - y2 * x1) / norm if norm > 1e-6 else hypot(px - x1, py - y1)
            if d > max_d: max_d, idx = d, i
        if max_d > epsilon_m: return _rdp(s_i, idx) + _rdp(idx, e_i)
        return [s_i]
    keep = _rdp(0, len(pts) - 1) + [len(pts) - 1]
    return [pts[i] for i in keep]

def local(iso):
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo: dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt + timedelta(hours=TZ_OFFSET)

def parse_dt(s):
    for f in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try: return datetime.strptime(str(s)[:19], f)
        except Exception: pass

def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "drone-manager-local/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r: return json.loads(r.read())

def geocode(lat, lon):
    try:
        j = http_json(f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=14&lat={lat}&lon={lon}")
        a = j.get("address", {})
        return a.get("suburb") or a.get("neighbourhood") or a.get("city") or a.get("town") or j.get("name") or ""
    except Exception: return ""

def weather(lat, lon, start):
    try:
        d = start[:10]
        j = http_json("https://archive-api.open-meteo.com/v1/archive?"
                      f"latitude={lat}&longitude={lon}&start_date={d}&end_date={d}&timezone=UTC&wind_speed_unit=ms"
                      "&hourly=temperature_2m,wind_speed_10m,wind_gusts_10m,precipitation")
        h, i = j["hourly"], int(start[11:13])
        return {k: h[k][i] for k in ("temperature_2m", "wind_speed_10m", "wind_gusts_10m", "precipitation")}
    except Exception: return None

# ---------- importação de logs de voo ----------
def B(v): return v is True or v == "True"

def _epoch(v):
    """Carimbo de tempo do log (texto ISO ou número) em segundos; None se não der."""
    try:
        if isinstance(v, (int, float)): return v / 1000 if v > 1e11 else float(v)
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception: return None

def _stk(v):
    """Normaliza posição do stick RC (364..1684, centro 1024) para -100..+100."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and 300 <= v <= 1750:
        return max(-100, min(100, round((v - 1024) / 6.6)))
    return None

def _cardinal(deg):
    if deg is None: return ""
    dirs = ("N", "NE", "L", "SE", "S", "SO", "O", "NO")
    return dirs[int(((deg % 360) + 22.5) // 45) % 8]

STATE_PT = {"GPSAtti": "Modo normal (GPS)", "GPSNovice": "Modo iniciante", "GPSSport": "Modo esportivo",
            "Tripod": "Modo Cine (tripé)", "GoHome": "Retorno automático (RTH)", "AutoLanding": "Pouso automático",
            "ConfirmLanding": "Aguardando confirmação de pouso", "NaviAdvLanding": "Pouso avançado"}
ACTION_PT = {"AppAutoTakeoff": "Decolagem automática (app)", "AppAutoLanding": "Pouso automático pedido pelo app",
             "SmartPowerGoHome": "Retorno automático por bateria baixa", "SmartPowerLanding": "Pouso automático por bateria crítica",
             "PGoHomeFinish": "Retorno automático concluído", "VertLowLimitLanding": "Pouso por limite vertical baixo"}

def _finalize_log_import(p: Path, rows: list[dict], bsn=None, dsn=None, dname=None, rcsn=None, camsn=None, dtype=None, log_format: str = "dji"):
    if not rows: raise RuntimeError("log sem pontos de GPS")
    real_time = sum(x.get("ep") is not None for x in rows) >= len(rows) / 2     # usa uma única fonte de hora
    if real_time: rows = [x for x in rows if x.get("ep") is not None]
    for i, x in enumerate(rows):
        x.setdefault("i", i)
        x["t"] = x["ep"] if real_time else (x["ft"] if x.get("ft") is not None else x["i"] * 0.1)
    air = [i for i, x in enumerate(rows) if x.get("air", True)]
    if not air: raise RuntimeError("sem decolagem (o drone ficou no solo)")
    rows = rows[air[0]: air[-1] + 1]                    # só o tempo no ar
    t0 = rows[0]["t"]
    rcs = [x["rc"] for x in rows if isinstance(x.get("rc"), (int, float))]
    use_rc = bool(rcs) and max(rcs) > 50                # só confia no sinal se a escala parecer 0-100
    track, events, flags, warns = [], [], set(), set()
    dist = maxalt = maxspd = max_hdist = 0.0; prev = None; last = -1; nsig = photos = 0
    st0 = cand = low0 = home0 = rth_h = act0 = None; cand_t = 0; sig_low = vid0 = pho0 = False
    mech = {"vib_s": 0, "force_s": 0, "comp_s": 0, "gimb_s": 0}
    wind_samples, watts_list, amps_list = [], [], []
    for x in rows:
        sec = int(x["t"] - t0)
        alt_v = float(x.get("alt") or 0)
        spd_v = float(x.get("spd") or 0)
        maxalt, maxspd = max(maxalt, alt_v), max(maxspd, spd_v)
        ev = lambda label: events.append({"t": sec, "label": label})
        st = x.get("state")
        if st:                                          # um modo só vale se durar pelo menos 2 s
            if st != cand: cand, cand_t = st, sec
            if cand != st0 and sec - cand_t >= 2:
                if st0 not in (None, "AutoTakeoff", "EngineStart"):
                    lbl = STATE_PT.get(cand, str(cand))
                    if log_format == "dji" and cand not in STATE_PT:
                        lbl = None
                    if lbl:
                        events.append({"t": cand_t, "label": lbl})
                st0 = cand
        act = x.get("act")
        if act and act != act0 and act in ACTION_PT and sec >= 0: ev(ACTION_PT[act])
        act0 = act
        bat = x.get("bat")
        for th in (20, 10):
            if bat is not None and bat <= th and th not in flags: flags.add(th); ev(f"Bateria em {th}%")
        vw = str(x.get("vw") or "")
        if vw in ("1", "2") and vw not in flags:
            flags.add(vw); ev("Aviso de bateria baixa (nível 1)" if vw == "1" else "Aviso de bateria crítica (nível 2)")
        if x.get("sats") is not None and x["sats"] < 8 and "gps" not in flags:
            flags.add("gps"); ev(f"Sinal GPS fraco ({x['sats']} satélites)")
        if use_rc and isinstance(x.get("rc"), (int, float)):
            if x["rc"] <= 20:
                if low0 is None: low0 = sec
                if sec - low0 >= 3 and low0 >= 10 and not sig_low and nsig < 5:
                    sig_low = True; nsig += 1; events.append({"t": low0, "label": f"Sinal do controle fraco ({x['rc']}%)"})
            else:
                low0 = None
                if x["rc"] > 50: sig_low = False
        if bool(x.get("vid")) != vid0:
            ev("Início de gravação de vídeo" if x.get("vid") else "Fim da gravação de vídeo"); vid0 = bool(x.get("vid"))
        if x.get("pho") and not pho0 and photos < 30: photos += 1; ev("Foto")
        pho0 = bool(x.get("pho"))
        w = str(x.get("warn") or "")
        if w and w not in warns and len(warns) < 6: warns.add(w); ev(f"Aviso do app: {w[:80]}")
        # Ponto Home e distância VLOS
        hla, hlo = x.get("hlat"), x.get("hlon")
        if isinstance(hla, (int, float)) and isinstance(hlo, (int, float)) and abs(hla) > 1e-6 and abs(hlo) > 1e-6:
            if home0 is None: home0 = (hla, hlo)
            elif hav(home0[0], home0[1], hla, hlo) > 15 and "home_upd" not in flags:
                flags.add("home_upd"); home0 = (hla, hlo); ev("Ponto Home atualizado")
        if x.get("rth_h") and not rth_h: rth_h = int(x["rth_h"])
        ref_h = home0 or (rows[0]["lat"], rows[0]["lon"])
        hdist = hav(ref_h[0], ref_h[1], x["lat"], x["lon"])
        max_hdist = max(max_hdist, hdist)
        if hdist > 500 and "vlos" not in flags:
            flags.add("vlos"); ev("Distância > 500 m do Ponto Home (VLOS)")
        # Diagnósticos mecânicos e sensores
        if x.get("vib") and "vib" not in flags: flags.add("vib"); ev("Alerta: Vibração nas hélices detectada")
        if x.get("noforce") and "noforce" not in flags: flags.add("noforce"); ev("Alerta: Força de propulsão insuficiente")
        if x.get("comperr") and "comperr" not in flags: flags.add("comperr"); ev("Alerta: Interferência na bússola")
        if x.get("motblk") and "motblk" not in flags: flags.add("motblk"); ev("Alerta: Motor bloqueado")
        if x.get("gimbstk") and "gimbstk" not in flags: flags.add("gimbstk"); ev("Alerta: Gimbal travado")
        if sec != last:                                  # 1 ponto por segundo no trajeto
            last = sec
            if prev: dist += dist_3d(prev[0], prev[1], prev[2], x["lat"], x["lon"], alt_v)
            prev = (x["lat"], x["lon"], alt_v)
            if x.get("vib"): mech["vib_s"] += 1
            if x.get("noforce"): mech["force_s"] += 1
            if x.get("comperr"): mech["comp_s"] += 1
            if x.get("gimbstk"): mech["gimb_s"] += 1
            zspd_v = float(x.get("zspd") or 0)
            pt, rl, yw = x.get("pitch"), x.get("roll"), x.get("yaw")
            if alt_v >= 5 and spd_v < 1.5 and abs(zspd_v) < 0.8 and all(isinstance(v, (int, float)) for v in (pt, rl, yw)):
                tilt = degrees(acos(max(-1.0, min(1.0, cos(radians(pt)) * cos(radians(rl))))))
                if 1.0 <= tilt <= 35.0:
                    w_spd = round(12.0 * sqrt(tan(radians(tilt))), 1)
                    w_dir = (yw + degrees(atan2(rl, -pt))) % 360
                    wind_samples.append((w_spd, w_dir))
            bv, bcur = x.get("bv"), x.get("bcur")
            vv = (bv / 1000.0 if bv > 100 else float(bv)) if isinstance(bv, (int, float)) and bv > 0 else None
            aa = (abs(bcur) / 1000.0 if abs(bcur) > 100 else abs(float(bcur))) if isinstance(bcur, (int, float)) else None
            ww = round(vv * aa, 1) if (vv is not None and aa is not None) else None
            if ww is not None and ww > 0: watts_list.append(ww)
            if aa is not None and aa > 0: amps_list.append(aa)
            cells = [round(c / 1000.0 if c > 20 else float(c), 3) for c in (x.get("bcells") or [])[:4] if isinstance(c, (int, float)) and c > 0]
            pwr = [round(vv, 2) if vv else None, round(aa, 1) if aa is not None else None, ww, *cells] if (vv or aa or cells) else None
            stk = [x.get("thr"), x.get("rud"), x.get("ele"), x.get("ail")] if any(x.get(k) is not None for k in ("thr", "rud", "ele", "ail")) else None
            track.append([sec, x["lat"], x["lon"], round(alt_v, 1), round(spd_v, 1), bat, x.get("sats"),
                          round(yw, 1) if isinstance(yw, (int, float)) else None,
                          round(x["gp"], 1) if isinstance(x.get("gp"), (int, float)) else None,
                          round(pt, 1) if isinstance(pt, (int, float)) else None,
                          round(rl, 1) if isinstance(rl, (int, float)) else None,
                          round(zspd_v, 1),
                          stk,
                          int(x["rc"]) if isinstance(x.get("rc"), (int, float)) else None,
                          pwr])
    if maxalt < 1: raise RuntimeError("sem voo (altura máxima menor que 1 m)")
    def _nums(k): return [x[k] for x in rows if isinstance(x.get(k), (int, float)) and not isinstance(x.get(k), bool)]
    _lv, _tp = _nums("bat"), _nums("btemp")
    _vv, _dv, _fc = [v for v in _nums("bv") if v > 0], [v for v in _nums("bdev") if v > 0], [v for v in _nums("bfull") if v > 0]
    _est = sum(1 for x in rows if x.get("best") in (True, "True")) > len(rows) / 2
    _ts = 10 if _tp and max(_tp) > 100 else 1
    dur_h = (track[-1][0] or 1) / 3600.0
    wavg = round(sum(watts_list) / len(watts_list), 1) if watts_list else None
    wmax = round(max(watts_list), 1) if watts_list else None
    amax = round(max(amps_list), 1) if amps_list else None
    wh = round(wavg * dur_h, 2) if wavg else None
    wh_km = round(wh / (dist / 1000.0), 1) if (wh and dist >= 100) else None
    binfo = {"start": _lv[0] if _lv else None, "end": _lv[-1] if _lv else None,
             "vmin": round(min(_vv) * (1000 if min(_vv) < 100 else 1)) if _vv else None,
             "dev": round(max(_dv) * (1000 if max(_dv) < 1 else 1), 1) if _dv and not _est else None,
             "est": _est, "full": sorted(_fc)[len(_fc) // 2] if _fc else None,
             "tmax": max(_tp) / _ts if _tp else None, "tmin": min(_tp) / _ts if _tp else None,
             "wmax": wmax, "wavg": wavg, "amax": amax, "wh": wh, "wh_km": wh_km}
    wind_est = None
    if wind_samples:
        ws = [s[0] for s in wind_samples]
        sx = sum(cos(radians(s[1])) for s in wind_samples)
        sy = sum(sin(radians(s[1])) for s in wind_samples)
        wdeg = round(degrees(atan2(sy, sx)) % 360)
        wind_est = {"spd": round(sum(ws) / len(ws), 1), "max": round(max(ws), 1), "deg": wdeg, "card": _cardinal(wdeg)}
    fmeta = {
        "home": {"lat": (home0 or (track[0][1], track[0][2]))[0], "lon": (home0 or (track[0][1], track[0][2]))[1],
                 "rth_alt": rth_h, "max_dist": round(max_hdist, 1)},
        "wind_est": wind_est,
        "mech": mech,
        "log_format": log_format,
        "drone": {"sn": dsn or "", "name": dname or "", "type": str(dtype or "DJI Mini 3"), "rc_sn": rcsn or "", "cam_sn": camsn or ""}
    }
    fmeta["health"] = _calc_flight_health(track, binfo, fmeta, "log")
    im = max(range(len(track)), key=lambda i: track[i][3])
    events = ([{"t": 0, "label": "Decolagem"}] + events +
              [{"t": track[-1][0], "label": "Pouso"}, {"t": track[im][0], "label": f"Altura máxima ({track[im][3]} m)"}])
    events.sort(key=lambda e: e["t"])
    start = datetime.fromtimestamp(t0, timezone.utc).isoformat() if real_time else None
    if not start:                                        # plano B: data e hora do nome do arquivo
        m = re.search(r"(\d{4}-\d\d-\d\d)[_T\s-]?\[?(\d\d)-(\d\d)-(\d\d)\]?", p.name)
        if m:
            lt = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}")
            start = (lt - timedelta(hours=TZ_OFFSET)).isoformat() + "Z"
    with db() as c:
        old = c.execute("SELECT place, weather FROM flights WHERE file=?", (str(p),)).fetchone()
    place = (old["place"] if old and old["place"] else None) or geocode(track[0][1], track[0][2])
    if not (old and old["place"]): time.sleep(1)
    wx = (json.loads(old["weather"]) if old and old["weather"] and old["weather"] != "null" else None) or weather(track[0][1], track[0][2], start)
    with db() as c:
        c.execute("INSERT INTO flights(file,start,duration,distance,max_alt,max_speed,battery_sn,lat,lon,place,weather,track,events,bat,meta,source)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(file) DO UPDATE SET start=excluded.start,"
                  "duration=excluded.duration,distance=excluded.distance,max_alt=excluded.max_alt,max_speed=excluded.max_speed,"
                  "battery_sn=excluded.battery_sn,lat=excluded.lat,lon=excluded.lon,place=excluded.place,"
                  "weather=excluded.weather,track=excluded.track,events=excluded.events,bat=excluded.bat,meta=excluded.meta,source=excluded.source",
                  (str(p), start, track[-1][0], dist, maxalt, maxspd, bsn, track[0][1], track[0][2], place,
                   json.dumps(wx), json.dumps(track), json.dumps(events), json.dumps(binfo), json.dumps(fmeta), "log"))
        fid_row = c.execute("SELECT id FROM flights WHERE file=?", (str(p),)).fetchone()
        if fid_row:
            _sync_flight_rtree(c, fid_row[0], track[0][1], track[0][2], track)

def _import_dji_log(p: Path):
    r = subprocess.run([DJI_LOG_BIN, str(p), "--api-key", API_KEY], capture_output=True, text=True, timeout=600)
    if r.returncode: raise RuntimeError(r.stderr[:200])
    raw = json.loads(r.stdout)
    det = raw.get("details") or {}
    bsn = g(det, "batterySn") or None
    dsn = g(det, "aircraftSn") or None
    dname = g(det, "aircraftName") or None
    rcsn = g(det, "rcSn") or None
    camsn = g(det, "cameraSn") or None
    dtype = None
    rows = []
    for i, f in enumerate(raw.get("frames", [])):
        bsn = bsn or g(f, "recover", "batterySn") or None
        dsn = dsn or g(f, "recover", "aircraftSn") or None
        dname = dname or g(f, "recover", "aircraftName") or None
        rcsn = rcsn or g(f, "recover", "rcSn") or None
        camsn = camsn or g(f, "recover", "cameraSn") or None
        dtype = dtype or g(f, "osd", "droneType") or None
        lat, lon = g(f, "osd", "latitude"), g(f, "osd", "longitude")
        if not lat or not lon or abs(lat) < 1e-6 or abs(lon) < 1e-6: continue
        ep = _epoch(g(f, "custom", "dateTime")); ep = ep if ep is not None and ep > 1.4e9 else None
        ft = g(f, "osd", "flyTime")
        rc_dn = g(f, "rc", "downlinkSignal")
        rc_up = g(f, "rc", "uplinkSignal")
        rows.append({"ep": ep, "ft": float(ft) if isinstance(ft, (int, float)) else None, "i": i,
                     "lat": lat, "lon": lon, "alt": g(f, "osd", "height", default=0) or 0,
                     "spd": sqrt(g(f, "osd", "xSpeed", default=0) ** 2 + g(f, "osd", "ySpeed", default=0) ** 2),
                     "zspd": g(f, "osd", "zSpeed", default=0) or 0,
                     "pitch": g(f, "osd", "pitch"), "roll": g(f, "osd", "roll"),
                     "bat": g(f, "battery", "chargeLevel"), "sats": g(f, "osd", "gpsNum"),
                     "air": not B(g(f, "osd", "isOnGround")), "state": g(f, "osd", "flycState"),
                     "act": g(f, "osd", "flightAction"),
                     "rc": rc_dn if rc_dn is not None else rc_up, "rc_up": rc_up,
                     "thr": _stk(g(f, "rc", "throttle")), "rud": _stk(g(f, "rc", "rudder")),
                     "ele": _stk(g(f, "rc", "elevator")), "ail": _stk(g(f, "rc", "aileron")),
                     "vw": g(f, "osd", "voltageWarning"), "vid": B(g(f, "camera", "isVideo")),
                     "pho": B(g(f, "camera", "isPhoto")), "warn": g(f, "app", "warn", default=""),
                     "yaw": g(f, "osd", "yaw"), "gp": g(f, "gimbal", "pitch"),
                     "bv": g(f, "battery", "voltage"), "bcur": g(f, "battery", "current"),
                     "bcells": g(f, "battery", "cellVoltages"),
                     "bdev": g(f, "battery", "cellVoltageDeviation"),
                     "bfull": g(f, "battery", "fullCapacity"), "btemp": g(f, "battery", "temperature"),
                     "best": g(f, "battery", "isCellVoltageEstimated"),
                     "hlat": g(f, "home", "latitude"), "hlon": g(f, "home", "longitude"),
                     "rth_h": g(f, "home", "goHomeHeight"),
                     "vib": B(g(f, "osd", "isVibrating")), "noforce": B(g(f, "osd", "isNotEnoughForce")),
                     "comperr": B(g(f, "osd", "isCompassError")), "motblk": B(g(f, "osd", "isMotorBlocked")),
                     "gimbstk": B(g(f, "gimbal", "isStuck"))})
    _finalize_log_import(p, rows, bsn=bsn, dsn=dsn, dname=dname, rcsn=rcsn, camsn=camsn, dtype=dtype, log_format="dji")

def _import_csv_log(p: Path):
    """Importa logs CSV do Litchi ou Airdata UAV (métrico ou imperial)."""
    import csv
    with open(p, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise RuntimeError("CSV sem cabeçalho.")
        hdr = [h.strip() for h in reader.fieldnames]
        hlow = {h.lower(): h for h in hdr}
        is_airdata = any("height_above_takeoff" in k or "compass_heading" in k or "flycstateraw" in k or "battery_percent" in k for k in hlow)
        fmt = "airdata" if is_airdata else "litchi"

        def _col(*cands):
            for c in cands:
                if c in hlow: return hlow[c]
            for c in cands:
                for k, orig in hlow.items():
                    if c in k: return orig
            return None

        c_lat = _col("latitude", "osd.latitude")
        c_lon = _col("longitude", "osd.longitude")
        c_alt = _col("height_above_takeoff(meters)", "height_above_takeoff(feet)", "altitude(m)", "altitude(ft)", "altitude(meters)", "altitude(feet)", "height_above_takeoff", "altitude")
        c_spd = _col("speed(m/s)", "speed(mph)", "speed(km/h)", "speed(kmh)", "speed(ft/s)", "hspeed", "speed")
        c_zspd = _col("zspeed", "vspeed", "climb")
        c_dt_utc = _col("datetime(utc)", "datetime_utc", "utc")
        c_dt_loc = _col("datetime(local)", "datetime", "date")
        c_tms = _col("time(millisecond)", "time(ms)")
        c_ts = _col("time(s)", "time(sec)", "flytime")
        c_bat = _col("battery_percent", "battery(%)", "battery_percentage", "chargelevel", "battery")
        c_bv = _col("voltage(v)", "voltage", "battery_voltage")
        c_ba = _col("current(a)", "current", "battery_current")
        c_bt = _col("battery_temperature(c)", "battery_temperature(f)", "temperature(c)", "temperature(f)", "temperature")
        c_sat = _col("satellites", "gps_satellites", "gpsnum")
        c_yaw = _col("compass_heading(degrees)", "heading(deg)", "yaw", "heading")
        c_gp = _col("gimbal_pitch(deg)", "gimbal_pitch", "gimbalpitch")
        c_pt = _col("pitch(deg)", "pitch")
        c_rl = _col("roll(deg)", "roll")
        c_st = _col("flycstate", "flight_mode", "mode")
        c_rc = _col("rc_signal", "rcsignal", "signal")
        c_vid = _col("isvideo", "video")
        c_pho = _col("isphoto", "photo")
        c_hlat = _col("home_latitude", "homelatitude")
        c_hlon = _col("home_longitude", "homelongitude")
        c_msg = _col("message", "warn")
        c_gnd = _col("isonground", "on_ground")
        c_bsn = _col("battery_serial_number", "batterysn")
        c_dsn = _col("aircraft_serial", "aircraftsn")
        c_dnm = _col("aircraft_name", "drone_type", "model")

        if not c_lat or not c_lon:
            raise RuntimeError("CSV sem colunas de latitude/longitude.")

        alt_ft = bool(c_alt and ("(ft" in c_alt.lower() or "(feet" in c_alt.lower()))
        spd_mph = bool(c_spd and "mph" in c_spd.lower())
        spd_kmh = bool(c_spd and ("km/h" in c_spd.lower() or "kmh" in c_spd.lower()))
        spd_fts = bool(c_spd and "ft/s" in c_spd.lower())
        tmp_f = bool(c_bt and "(f)" in c_bt.lower())

        def _f(row, col):
            if not col: return None
            v = str(row.get(col) or "").strip()
            if not v or v.lower() in ("na", "nan", "null", "none"): return None
            try: return float(v.replace(",", ".")) if (v.count(",") == 1 and "." not in v) else float(v)
            except Exception: return None

        rows, bsn, dsn, dtype = [], None, None, None
        for i, r in enumerate(reader):
            lat, lon = _f(r, c_lat), _f(r, c_lon)
            if lat is None or lon is None or abs(lat) < 1e-6 or abs(lon) < 1e-6:
                continue
            bsn = bsn or (str(r.get(c_bsn) or "").strip() or None if c_bsn else None)
            dsn = dsn or (str(r.get(c_dsn) or "").strip() or None if c_dsn else None)
            dtype = dtype or (str(r.get(c_dnm) or "").strip() or None if c_dnm else None)
            alt = _f(r, c_alt) or 0.0
            if alt_ft: alt *= 0.3048
            spd = _f(r, c_spd) or 0.0
            if spd_mph: spd *= 0.44704
            elif spd_kmh: spd /= 3.6
            elif spd_fts: spd *= 0.3048
            zspd = _f(r, c_zspd) or 0.0
            if c_zspd and "mph" in c_zspd.lower(): zspd *= 0.44704
            elif c_zspd and "ft/s" in c_zspd.lower(): zspd *= 0.3048
            ep = None
            if c_dt_utc and r.get(c_dt_utc):
                ep = _epoch(r[c_dt_utc].strip())
            elif c_dt_loc and r.get(c_dt_loc):
                ep0 = _epoch(r[c_dt_loc].strip())
                if ep0 is not None: ep = ep0 - TZ_OFFSET * 3600
            ft = _f(r, c_ts)
            if ft is None and c_tms:
                ms = _f(r, c_tms)
                if ms is not None: ft = ms / 1000.0
            bt = _f(r, c_bt)
            if bt is not None and tmp_f:
                bt = (bt - 32.0) * 5.0 / 9.0
            gnd_v = str(r.get(c_gnd) or "").strip().lower() if c_gnd else ""
            air = (gnd_v not in ("1", "true", "yes")) if gnd_v else (alt >= 0.8 or spd >= 0.8)
            rows.append({
                "ep": ep if (ep is not None and ep > 1.4e9) else None, "ft": ft, "i": i,
                "lat": lat, "lon": lon, "alt": alt, "spd": spd, "zspd": zspd,
                "pitch": _f(r, c_pt), "roll": _f(r, c_rl), "yaw": _f(r, c_yaw), "gp": _f(r, c_gp),
                "bat": _f(r, c_bat), "bv": _f(r, c_bv), "bcur": _f(r, c_ba), "btemp": bt,
                "sats": int(_f(r, c_sat)) if _f(r, c_sat) is not None else None,
                "air": air, "state": str(r.get(c_st) or "").strip() if c_st else None,
                "rc": _f(r, c_rc),
                "vid": str(r.get(c_vid) or "").strip().lower() in ("1", "true", "yes") if c_vid else False,
                "pho": str(r.get(c_pho) or "").strip().lower() in ("1", "true", "yes") if c_pho else False,
                "warn": str(r.get(c_msg) or "").strip() if c_msg else "",
                "hlat": _f(r, c_hlat), "hlon": _f(r, c_hlon),
            })
    _finalize_log_import(p, rows, bsn=bsn, dsn=dsn, dtype=dtype or ("Airdata CSV" if is_airdata else "Litchi CSV"), log_format=fmt)

def _import_ulog(p: Path):
    """Importa logs binários PX4 ULog (.ulg) em Python puro."""
    import struct
    _TYPES = {
        "int8_t": ("b", 1), "uint8_t": ("B", 1), "bool": ("B", 1), "char": ("c", 1),
        "int16_t": ("h", 2), "uint16_t": ("H", 2), "int32_t": ("i", 4), "uint32_t": ("I", 4),
        "int64_t": ("q", 8), "uint64_t": ("Q", 8), "float": ("f", 4), "double": ("d", 8),
    }
    data = p.read_bytes()
    if len(data) < 16 or data[:7] != b"ULog\x01\x12\x35":
        raise RuntimeError("Arquivo .ulg inválido (cabeçalho PX4 ULog não encontrado).")
    pos = 16
    formats, subs, info = {}, {}, {}

    def _compile_fmt(fstr: str):
        fields, off = [], 0
        for part in fstr.split(";"):
            part = part.strip()
            if not part or " " not in part: continue
            ftype, fname = part.split(" ", 1)
            arr_n = 1
            if "[" in ftype and ftype.endswith("]"):
                base, n_s = ftype[:-1].split("[", 1)
                arr_n = int(n_s)
            else:
                base = ftype
            if base in _TYPES:
                ch, sz = _TYPES[base]
                if not fname.startswith("_padding"):
                    fields.append((fname, ch, arr_n, off, sz * arr_n))
                off += sz * arr_n
        return fields

    gps_pts, att_st, loc_st, bat_st, home_st, rc_st, mode_st = [], {}, {}, {}, {}, {}, {}
    while pos + 3 <= len(data):
        msize, mtype = struct.unpack_from("<HB", data, pos)
        pos += 3
        if pos + msize > len(data): break
        payload = data[pos:pos + msize]
        pos += msize
        if mtype == ord("F"):
            s = payload.decode("utf-8", errors="ignore")
            if ":" in s:
                mname, fstr = s.split(":", 1)
                formats[mname] = _compile_fmt(fstr)
        elif mtype == ord("I") and len(payload) >= 2:
            klen = payload[0]
            key = payload[1:1 + klen].decode("utf-8", errors="ignore")
            val_b = payload[1 + klen:]
            if "char[" in key:
                info[key.split(" ")[-1]] = val_b.decode("utf-8", errors="ignore").rstrip("\x00")
        elif mtype == ord("A") and len(payload) >= 4:
            _multi, mid = struct.unpack_from("<BH", payload, 0)
            mname = payload[3:].decode("utf-8", errors="ignore").rstrip("\x00")
            if mname in formats:
                subs[mid] = (mname, formats[mname])
        elif mtype == ord("D") and len(payload) >= 2:
            mid = struct.unpack_from("<H", payload, 0)[0]
            if mid not in subs: continue
            mname, flds = subs[mid]
            if mname not in ("vehicle_gps_position", "sensor_gps", "vehicle_attitude", "vehicle_local_position",
                             "battery_status", "home_position", "input_rc", "vehicle_status"):
                continue
            rec = {}
            buf = payload[2:]
            for fname, ch, arr_n, off, sz in flds:
                if off + sz > len(buf): break
                val = struct.unpack_from(f"<{arr_n}{ch}", buf, off)
                rec[fname] = val[0] if arr_n == 1 else val
            if mname == "vehicle_attitude" and "q" in rec:
                q0, q1, q2, q3 = rec["q"]
                att_st["roll"] = round(degrees(atan2(2 * (q0 * q1 + q2 * q3), 1 - 2 * (q1 * q1 + q2 * q2))), 1)
                att_st["pitch"] = round(degrees(asin(max(-1.0, min(1.0, 2 * (q0 * q2 - q3 * q1))))), 1)
                att_st["yaw"] = round(degrees(atan2(2 * (q0 * q3 + q1 * q2), 1 - 2 * (q2 * q2 + q3 * q3))) % 360, 1)
            elif mname == "vehicle_local_position":
                if "z" in rec: loc_st["alt"] = -float(rec["z"])
                if "vz" in rec: loc_st["zspd"] = -float(rec["vz"])
            elif mname == "battery_status":
                if "remaining" in rec and 0 <= rec["remaining"] <= 1.05: bat_st["bat"] = round(rec["remaining"] * 100)
                if "voltage_v" in rec: bat_st["bv"] = rec["voltage_v"]
                if "current_a" in rec: bat_st["bcur"] = rec["current_a"]
                if "temperature" in rec: bat_st["btemp"] = rec["temperature"]
            elif mname == "home_position":
                if "lat" in rec and "lon" in rec:
                    home_st["hlat"], home_st["hlon"] = rec["lat"], rec["lon"]
                    if "alt" in rec: home_st["halt"] = rec["alt"]
            elif mname == "input_rc" and "rssi" in rec:
                rc_st["rc"] = min(100, max(0, int(rec["rssi"])))
            elif mname == "vehicle_status" and "nav_state" in rec:
                mode_st["state"] = f"PX4 Modo {rec['nav_state']}"
            elif mname in ("vehicle_gps_position", "sensor_gps"):
                lat_raw, lon_raw = rec.get("lat"), rec.get("lon")
                if lat_raw is None or lon_raw is None: continue
                lat = lat_raw / 1e7 if abs(lat_raw) > 180 else float(lat_raw)
                lon = lon_raw / 1e7 if abs(lon_raw) > 180 else float(lon_raw)
                if abs(lat) < 1e-6 or abs(lon) < 1e-6: continue
                ts_us = rec.get("timestamp", 0)
                utc_us = rec.get("time_utc_usec", 0)
                ep = (utc_us / 1e6) if utc_us and utc_us > 1.4e15 else None
                alt_rel = loc_st.get("alt")
                if alt_rel is None and "alt" in rec:
                    halt = home_st.get("halt")
                    alt_m = rec["alt"] / 1000.0 if abs(rec["alt"]) > 10000 else float(rec["alt"])
                    alt_rel = (alt_m - halt) if halt is not None else alt_m
                gps_pts.append({
                    "ep": ep, "ft": ts_us / 1e6, "lat": lat, "lon": lon,
                    "alt": float(alt_rel or 0), "spd": float(rec.get("vel_m_s") or 0),
                    "zspd": float(loc_st.get("zspd") if "zspd" in loc_st else -(rec.get("vel_d_m_s") or 0)),
                    "sats": rec.get("satellites_used"),
                    "pitch": att_st.get("pitch"), "roll": att_st.get("roll"), "yaw": att_st.get("yaw"),
                    "bat": bat_st.get("bat"), "bv": bat_st.get("bv"), "bcur": bat_st.get("bcur"), "btemp": bat_st.get("btemp"),
                    "hlat": home_st.get("hlat"), "hlon": home_st.get("hlon"),
                    "rc": rc_st.get("rc"), "state": mode_st.get("state"),
                    "air": float(alt_rel or 0) >= 0.8 or float(rec.get("vel_m_s") or 0) >= 0.8,
                })
    dtype = info.get("sys_name") or info.get("ver_hw") or "PX4 ULog"
    _finalize_log_import(p, gps_pts, dtype=dtype, log_format="ulog")

def _import_ardupilot_bin(p: Path):
    """Importa logs binários ArduPilot DataFlash (.bin) em Python puro."""
    import struct
    _DF_FMT = {
        "a": ("32h", 64, 1), "b": ("b", 1, 1), "B": ("B", 1, 1), "h": ("h", 2, 1), "H": ("H", 2, 1),
        "i": ("i", 4, 1), "I": ("I", 4, 1), "f": ("f", 4, 1), "d": ("d", 8, 1),
        "n": ("4s", 4, 1), "N": ("16s", 16, 1), "Z": ("64s", 64, 1),
        "c": ("h", 2, 0.01), "C": ("H", 2, 0.01), "e": ("i", 4, 0.01), "E": ("I", 4, 0.01),
        "L": ("i", 4, 1e-7), "M": ("B", 1, 1), "q": ("q", 8, 1), "Q": ("Q", 8, 1),
    }
    data = p.read_bytes()
    pos, n = 0, len(data)
    fmts, rows = {}, []
    att_st, baro_st, bat_st, home_st, mode_st = {}, {}, {}, {}, {}
    while pos + 3 <= n:
        if data[pos] != 0xA3 or data[pos + 1] != 0x95:
            pos += 1
            continue
        mid = data[pos + 2]
        if mid == 0x80:
            if pos + 89 > n: break
            mtype, mlen = data[pos + 3], data[pos + 4]
            mname = data[pos + 5:pos + 9].split(b"\x00", 1)[0].decode("ascii", errors="ignore")
            mfmt = data[pos + 9:pos + 25].split(b"\x00", 1)[0].decode("ascii", errors="ignore")
            mlbl = data[pos + 25:pos + 89].split(b"\x00", 1)[0].decode("ascii", errors="ignore").split(",")
            fmts[mtype] = (mname, mlen, mfmt, mlbl)
            pos += 89
            continue
        if mid not in fmts:
            pos += 3
            continue
        mname, mlen, mfmt, mlbl = fmts[mid]
        if pos + mlen > n: break
        if mname in ("GPS", "ATT", "BARO", "POS", "CTUN", "BAT", "CURR", "MODE", "ORGN"):
            off = pos + 3
            rec = {}
            for ch, lbl in zip(mfmt, mlbl):
                spec = _DF_FMT.get(ch)
                if not spec or off + spec[1] > pos + mlen: break
                val = struct.unpack_from("<" + spec[0], data, off)[0]
                if isinstance(val, bytes):
                    val = val.split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
                elif spec[2] != 1:
                    val = val * spec[2]
                rec[lbl] = val
                off += spec[1]
            if mname == "ATT":
                att_st["roll"] = round(float(rec.get("Roll") or 0), 1)
                att_st["pitch"] = round(float(rec.get("Pitch") or 0), 1)
                att_st["yaw"] = round(float(rec.get("Yaw") or 0), 1)
            elif mname in ("BARO", "POS"):
                if "Alt" in rec: baro_st["alt"] = float(rec["Alt"])
                if "RelHomeAlt" in rec: baro_st["alt"] = float(rec["RelHomeAlt"])
            elif mname in ("BAT", "CURR"):
                if "Volt" in rec: bat_st["bv"] = float(rec["Volt"])
                if "Curr" in rec: bat_st["bcur"] = float(rec["Curr"])
                if "Temp" in rec: bat_st["btemp"] = float(rec["Temp"])
                if "RemPct" in rec: bat_st["bat"] = float(rec["RemPct"])
            elif mname == "ORGN":
                if "Lat" in rec and "Lng" in rec:
                    home_st["hlat"], home_st["hlon"] = float(rec["Lat"]), float(rec["Lng"])
            elif mname == "MODE":
                mode_st["state"] = str(rec.get("Mode") or rec.get("ModeNum") or "")
            elif mname == "GPS":
                lat, lon = rec.get("Lat"), rec.get("Lng")
                if lat is None or lon is None or abs(lat) < 1e-6 or abs(lon) < 1e-6:
                    pos += mlen
                    continue
                gwk, gms = rec.get("GWk"), rec.get("GMS")
                ep = (315964800.0 + gwk * 604800.0 + gms / 1000.0 - 18.0) if (gwk and gwk > 1000 and gms is not None) else None
                alt = baro_st.get("alt") if "alt" in baro_st else float(rec.get("Alt") or 0)
                spd = float(rec.get("Spd") or 0)
                rows.append({
                    "ep": ep, "ft": (rec.get("TimeUS") or 0) / 1e6,
                    "lat": float(lat), "lon": float(lon), "alt": alt, "spd": spd,
                    "zspd": -float(rec.get("VZ") or 0),
                    "sats": int(rec["NSats"]) if "NSats" in rec else None,
                    "pitch": att_st.get("pitch"), "roll": att_st.get("roll"), "yaw": att_st.get("yaw"),
                    "bat": bat_st.get("bat"), "bv": bat_st.get("bv"), "bcur": bat_st.get("bcur"), "btemp": bat_st.get("btemp"),
                    "hlat": home_st.get("hlat"), "hlon": home_st.get("hlon"),
                    "state": mode_st.get("state"),
                    "air": alt >= 0.8 or spd >= 0.8,
                })
        pos += mlen
    _finalize_log_import(p, rows, dtype="ArduPilot", log_format="ardupilot")

def import_log(p):
    p = Path(p)
    ext = p.suffix.lower()
    if ext == ".txt": return _import_dji_log(p)
    if ext == ".csv": return _import_csv_log(p)
    if ext == ".ulg": return _import_ulog(p)
    if ext == ".bin": return _import_ardupilot_bin(p)
    raise RuntimeError(f"Formato de log não suportado: {ext}")

# ---------- indexação de mídia e parser completo de .SRT / EXIF / XMP ----------
def sha1(p):
    h = hashlib.sha1()
    size = p.stat().st_size
    h.update(str(size).encode())
    with open(p, "rb") as f:
        h.update(f.read(1 << 20))
        if size > 2 << 20:
            f.seek(-(1 << 20), 2); h.update(f.read())
    return h.hexdigest()

def exif_batch(paths):
    r = subprocess.run(["exiftool", "-j", "-n", "-DateTimeOriginal", "-CreateDate", "-GPSLatitude", "-GPSLongitude",
                        "-GPSAltitude", "-GPSAltitudeRef", "-ImageWidth", "-ImageHeight", "-Duration", "-Make", "-Model", "-ISO", "-ExposureTime",
                        "-FNumber", "-ExposureCompensation", "-FocalLength", "-FocalLengthIn35mmFormat",
                        "-GimbalYawDegree", "-GimbalPitchDegree", "-GimbalRollDegree", "-FlightYawDegree",
                        "-RelativeAltitude", "-ProjectionType", "--", *paths], capture_output=True, text=True)
    return {str(Path(x["SourceFile"])): x for x in json.loads(r.stdout or "[]")}

def parse_srt_full(p):
    """Lê todos os blocos de legenda do arquivo .SRT da DJI (1 ponto por segundo com GPS, altitude e dados de câmera)."""
    for ext in (".SRT", ".srt"):
        s = p.with_suffix(ext)
        if s.exists():
            try:
                with open(s, "r", encoding="utf-8", errors="ignore") as f:
                    txt = f.read(2_000_000)
            except Exception: return []
            pts, last_sec, t0 = [], -1, None
            for blk in re.split(r"\n\s*\n", txt):
                if not blk.strip(): continue
                d = re.search(r"(\d{4}[-:]\d\d[-:]\d\d \d\d:\d\d:\d\d)", blk)
                dt = parse_dt(d.group(1)) if d else None
                if dt and t0 is None: t0 = dt
                tm = re.search(r"(\d\d):(\d\d):(\d\d)[,.](\d+)", blk)
                sec = int(tm.group(1)) * 3600 + int(tm.group(2)) * 60 + int(tm.group(3)) if tm else (int((dt - t0).total_seconds()) if (dt and t0) else len(pts))
                if sec == last_sec: continue
                last_sec = sec
                def fn(k):
                    m = re.search(rf"\[{k}\s*:\s*(-?[\d.]+)", blk, re.I)
                    return float(m.group(1)) if m else None
                def fs(k):
                    m = re.search(rf"\[{k}\s*:\s*([^\]]+)\]", blk, re.I)
                    return m.group(1).strip() if m else None
                lat, lon = fn("latitude"), fn("longitude")
                if lat is None or lon is None:
                    g2 = re.search(r"GPS\s*\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)", blk)
                    if g2: lon, lat = float(g2.group(1)), float(g2.group(2))
                if not valid_gps(lat, lon):
                    lat, lon = None, None
                rel_alt = fn("rel_alt")
                abs_alt = fn("abs_alt") if fn("abs_alt") is not None else fn("altitude")
                alt = rel_alt if rel_alt is not None else abs_alt
                pts.append({"t": sec, "lat": lat, "lon": lon, "alt": alt, "rel_alt": rel_alt, "abs_alt": abs_alt,
                            "iso": int(fn("iso")) if fn("iso") else None,
                            "shutter": fs("shutter"), "fnum": fn("fnum"), "ev": fn("ev"),
                            "focal": fn("focal_len"), "taken": dt.isoformat() if dt else None})
            return pts
    return []

def read_srt(p):
    pts = parse_srt_full(p)
    if not pts: return None
    p0 = next((x for x in pts if valid_gps(x.get("lat"), x.get("lon"))), pts[0])
    dt = datetime.fromisoformat(p0["taken"]) if p0.get("taken") else None
    return {"lat": p0.get("lat"), "lon": p0.get("lon"), "alt": p0.get("alt"), "rel_alt": p0.get("rel_alt"), "abs_alt": p0.get("abs_alt"),
            "taken": dt, "pts_count": len(pts), "iso": p0.get("iso"), "shutter": p0.get("shutter"),
            "fnum": p0.get("fnum"), "ev": p0.get("ev"), "focal": p0.get("focal")}

def _fmt_shutter(v):
    if v is None: return None
    if isinstance(v, str): return v
    try:
        v = float(v)
        if v <= 0: return None
        return f"1/{round(1/v)}s" if v < 0.4 else f"{round(v, 1)}s"
    except Exception: return str(v)

def _build_media_meta(x, kind, w, h, s=None):
    proj = str(x.get("ProjectionType") or "")
    pano = proj.lower() == "equirectangular" or bool(w and h and w >= 3600 and 1.95 <= (w / h) <= 2.05)
    m = {
        "model": x.get("Model") or "",
        "iso": x.get("ISO") or (s.get("iso") if s else None),
        "shutter": _fmt_shutter(x.get("ExposureTime")) or (s.get("shutter") if s else None),
        "fnum": x.get("FNumber") or (s.get("fnum") if s else None),
        "ev": x.get("ExposureCompensation") if x.get("ExposureCompensation") is not None else (s.get("ev") if s else None),
        "focal": x.get("FocalLength") or (s.get("focal") if s else None),
        "focal35": x.get("FocalLengthIn35mmFormat") or (s.get("focal") if s else None),
        "gyaw": x.get("GimbalYawDegree") if x.get("GimbalYawDegree") is not None else x.get("FlightYawDegree"),
        "gpitch": x.get("GimbalPitchDegree"),
        "groll": x.get("GimbalRollDegree"),
        "rel_alt": x.get("RelativeAltitude") if x.get("RelativeAltitude") is not None else (s.get("alt") if s else None),
        "proj": proj,
        "pano360": pano,
        "has_srt": bool(s and s.get("pts_count")),
        "srt_pts": s.get("pts_count", 0) if s else 0
    }
    if kind == "video" and isinstance(x.get("Duration"), (int, float)) and x["Duration"] > 0:
        m["dur"] = round(float(x["Duration"]), 2)
    return {k: v for k, v in m.items() if v not in (None, "", False, 0)}

def _probe_video_duration(p: Path) -> Optional[float]:
    """Obtém a duração do vídeo em segundos via ffprobe."""
    if not p.is_file(): return None
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
            capture_output=True, text=True, timeout=8
        )
        val = float((r.stdout or "").strip())
        return round(val, 2) if val > 0 else None
    except Exception:
        return None

def index_media(errors):
    with db() as c: known = {r[0] for r in c.execute("SELECT path FROM media")}
    with db() as c: roots = [dict(r) for r in c.execute("SELECT * FROM media_roots WHERE enabled=1")]
    new, tagmap = [], {}
    for rt in roots:
        rp = Path(rt["path"])
        if not rp.is_dir():
            errors.append("Pasta indisponível (disco desconectado?): " + rt["path"]); continue
        it = rp.rglob("*") if rt["recursive"] else rp.glob("*")
        got = [p for p in it if p.suffix.lower() in IMG | VID and str(p) not in known and str(p) not in tagmap]
        for p in got: tagmap[str(p)] = rt["tag"] or ""
        new += got
        with db() as c: c.execute("UPDATE media_roots SET last_scan=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), rt["id"]))
    ex = {}
    for i in range(0, len(new), 200):
        try: ex.update(exif_batch([str(p) for p in new[i:i + 200]]))
        except Exception as e: errors.append(f"exiftool: {e}")
    with db() as c:
        for p in new:
            x = ex.get(str(p), {}); kind = "photo" if p.suffix.lower() in IMG else "video"
            taken = parse_dt(x.get("DateTimeOriginal") or x.get("CreateDate"))
            lat, lon, alt = x.get("GPSLatitude"), x.get("GPSLongitude"), x.get("GPSAltitude")
            if isinstance(alt, (int, float)) and str(x.get("GPSAltitudeRef")) == "1": alt = -abs(alt)
            w, h = x.get("ImageWidth"), x.get("ImageHeight")
            s = read_srt(p) if kind == "video" else None
            if kind == "video":
                if s:
                    lat = s["lat"] if s["lat"] is not None else lat
                    lon = s["lon"] if s["lon"] is not None else lon
                    alt = s["alt"] if s["alt"] is not None else alt
                    taken = s["taken"] or taken
                elif taken: taken += timedelta(hours=TZ_OFFSET)   # CreateDate de MP4 costuma ser UTC
            if not valid_gps(lat, lon): lat, lon = None, None
            try:
                st = p.stat()
                taken = taken or datetime.fromtimestamp(st.st_mtime)
                meta = _build_media_meta(x, kind, w, h, s)
                c.execute("INSERT OR IGNORE INTO media(path,kind,taken,lat,lon,alt,w,h,size,sha1,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (str(p), kind, taken.isoformat(), lat, lon, alt, w, h, st.st_size, sha1(p), json.dumps(meta)))
            except OSError:
                continue
    # Atualiza metadados EXIF/XMP/SRT das mídias já cadastradas que ainda não tinham 'meta' (se o disco estiver conectado)
    with db() as c:
        todo = [dict(r) for r in c.execute("SELECT id,path,kind,w,h FROM media WHERE meta IS NULL").fetchall()]
    online_todo = [r for r in todo if Path(r["path"]).exists()]
    for i in range(0, len(online_todo), 200):
        batch = online_todo[i:i + 200]
        try: bx = exif_batch([r["path"] for r in batch])
        except Exception: bx = {}
        with db() as c:
            for r in batch:
                p = Path(r["path"]); x = bx.get(str(p), {})
                s = read_srt(p) if r["kind"] == "video" else None
                w, h = r["w"] or x.get("ImageWidth"), r["h"] or x.get("ImageHeight")
                meta = _build_media_meta(x, r["kind"], w, h, s)
                c.execute("UPDATE media SET w=COALESCE(w,?), h=COALESCE(h,?), meta=? WHERE id=?",
                          (w, h, json.dumps(meta), r["id"]))
    if any(tagmap.values()):
        with db() as c:
            for pth, tg in tagmap.items():
                if tg: c.execute("UPDATE media SET tags=? WHERE path=? AND (tags IS NULL OR tags='')", (tg, pth))
    return len(new)

def link_media():
    """Associa cada mídia ao voo certo, nesta ordem: (1) início de gravação registrado no log (até 5 s de diferença);
    (2) mídia feita durante o voo; (3) vídeo que já gravava na decolagem (começou até 10 min antes);
    (4) voo mais próximo, com 10 min de folga. Cria tags automáticas: local e mês."""
    pad, tol = timedelta(minutes=10), timedelta(seconds=5)
    with db() as c:
        fl = []
        for r in c.execute("SELECT id,start,duration,place,events FROM flights WHERE start IS NOT NULL AND source='log'").fetchall():
            a = local(r["start"]); ev = json.loads(r["events"] or "[]")
            starts = [a + timedelta(seconds=e["t"]) for e in ev if e["t"] > 0 and e["label"].startswith("Início de gravação")]
            rec0 = any(e["t"] == 0 and e["label"].startswith("Início de gravação") for e in ev)
            fl.append((r["id"], a, a + timedelta(seconds=r["duration"] or 0), r["place"], starts, rec0))
        for m in c.execute("SELECT id,kind,taken,tags FROM media WHERE flight_id IS NULL AND COALESCE(manual,0)=0").fetchall():
            t = datetime.fromisoformat(m["taken"]); vid = m["kind"] == "video"; best = None
            for fid, a, b, place, starts, rec0 in fl:
                key = None
                if vid and any(abs(t - s) <= tol for s in starts): key = (1, min(abs((t - s).total_seconds()) for s in starts))
                elif a <= t <= b: key = (2, 0.0)
                elif vid and rec0 and a - pad <= t < a: key = (3, (a - t).total_seconds())
                elif a - pad <= t <= b + pad: key = (4, min(abs((t - a).total_seconds()), abs((t - b).total_seconds())))
                if key and (best is None or key < best[0]): best = (key, fid, place)
            if best:
                _, fid, place = best
                tags = {x for x in m["tags"].split(",") if x} | {t.strftime("%Y-%m")} | ({place} if place else set())
                c.execute("UPDATE media SET flight_id=?, tags=? WHERE id=?", (fid, ",".join(sorted(tags)), m["id"]))

def check_backup():
    if not BACKUP_DIR or not Path(BACKUP_DIR).is_dir(): return
    have = {(p.name, p.stat().st_size) for p in Path(BACKUP_DIR).rglob("*") if p.is_file()}
    with db() as c:
        for m in c.execute("SELECT id,path,size FROM media").fetchall():
            c.execute("UPDATE media SET backed=? WHERE id=?", (int((Path(m["path"]).name, m["size"]) in have), m["id"]))

# ---------- API ----------
app = FastAPI(title="Drone Manager")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

@app.middleware("http")
async def security_headers_and_csrf(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sec_site = (request.headers.get("sec-fetch-site") or "").lower()
        if sec_site == "cross-site":
            return Response("Origem não autorizada", status_code=403)
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if not origin or not re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", origin):
            return Response("Origem não autorizada", status_code=403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self' https: data: blob:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' https: blob:; "
        "worker-src 'self' blob: https:; "
        "connect-src 'self' https: data: blob:; "
        "style-src 'self' 'unsafe-inline' https:; "
        "img-src 'self' data: blob: https:;"
    )
    return resp

def _calc_flight_health(track: list, bat: dict, meta: dict, source: str = "log") -> Optional[dict]:
    """Calcula o Índice de Saúde do Voo (0..100) cruzando mecânica/sensores, bateria/voltage sag e link RC/GPS."""
    if source == "photos":
        return None
    mc = (meta or {}).get("mech") or {}
    bat = bat or {}
    vib_s = int(mc.get("vib_s") or 0)
    force_s = int(mc.get("force_s") or 0)
    comp_s = int(mc.get("comp_s") or 0)
    gimb_s = int(mc.get("gimb_s") or 0)

    # 1. Sub-nota Mecânica & Sensores (0..100)
    p_vib = min(35, vib_s * 3)
    p_force = min(40, force_s * 8)
    p_comp = min(35, comp_s * 7)
    p_gimb = min(25, gimb_s * 5)
    mech_pen = min(100, p_vib + p_force + p_comp + p_gimb)
    mech_score = max(0, 100 - mech_pen)

    # Analisa a trilha 1 Hz para queda brusca de tensão por célula (voltage sag), perdas de sinal RC e GPS fraco
    rc_low_s, rc_crit_s, gps_low_s = 0, 0, 0
    max_sag_mv, min_cell_v = 0, None
    cell_hist = []
    for p in (track or []):
        if not isinstance(p, (list, tuple)):
            continue
        sats = p[6] if len(p) > 6 else None
        if isinstance(sats, (int, float)) and not isinstance(sats, bool) and 0 < sats < 8:
            gps_low_s += 1
        rc = p[13] if len(p) > 13 else None
        if isinstance(rc, (int, float)) and not isinstance(rc, bool):
            if rc <= 20:
                rc_crit_s += 1
            elif rc <= 40:
                rc_low_s += 1
        pwr = p[14] if len(p) > 14 else None
        if isinstance(pwr, (list, tuple)):
            cells = [float(c) for c in pwr[3:] if isinstance(c, (int, float)) and not isinstance(c, bool) and 2.5 <= c <= 4.5]
            if not cells and isinstance(pwr[0], (int, float)) and pwr[0] > 5.0:
                n_cells = 2 if pwr[0] < 9.5 else (3 if pwr[0] < 13.8 else 4)
                cells = [float(pwr[0]) / n_cells]
            if cells:
                cmin = min(cells)
                min_cell_v = cmin if min_cell_v is None else min(min_cell_v, cmin)
                if cell_hist:
                    ref_v = max(cell_hist[-5:])
                    sag = round((ref_v - cmin) * 1000.0)
                    if sag > max_sag_mv:
                        max_sag_mv = sag
                cell_hist.append(cmin)

    if min_cell_v is None and isinstance(bat.get("vmin"), (int, float)) and bat["vmin"] > 0:
        v_tot = bat["vmin"] / 1000.0
        n_c = 2 if v_tot < 9.5 else (3 if v_tot < 13.8 else 4)
        min_cell_v = round(v_tot / n_c, 3)

    # 2. Sub-nota Elétrica & Bateria (0..100)
    p_sag = 30 if max_sag_mv >= 300 else (18 if max_sag_mv >= 200 else (8 if max_sag_mv >= 130 else 0))
    p_cmin = 35 if (min_cell_v is not None and min_cell_v < 3.30) else (20 if (min_cell_v is not None and min_cell_v < 3.45) else (8 if (min_cell_v is not None and min_cell_v < 3.55) else 0))
    dev = bat.get("dev")
    p_dev = 25 if (isinstance(dev, (int, float)) and dev > 100) else (12 if (isinstance(dev, (int, float)) and dev > 60) else (5 if (isinstance(dev, (int, float)) and dev > 40) else 0))
    tmax = bat.get("tmax")
    p_temp = 20 if (isinstance(tmax, (int, float)) and tmax > 58) else (10 if (isinstance(tmax, (int, float)) and tmax > 50) else 0)
    bend = bat.get("end")
    p_end = 15 if (isinstance(bend, (int, float)) and bend <= 10) else (6 if (isinstance(bend, (int, float)) and bend <= 18) else 0)
    bat_pen = min(100, p_sag + p_cmin + p_dev + p_temp + p_end)
    bat_score = max(0, 100 - bat_pen)

    # 3. Sub-nota Sinal RC & GPS (0..100)
    p_rc = min(35, rc_crit_s * 3 + rc_low_s * 1)
    p_gps = min(30, gps_low_s * 3)
    link_pen = min(100, p_rc + p_gps)
    link_score = max(0, 100 - link_pen)

    score = max(0, min(100, 100 - mech_pen - bat_pen - link_pen))
    status = "good" if score >= 85 else ("warn" if score >= 65 else "crit")

    issues = []
    if force_s > 0: issues.append({"k": "force", "v": force_s, "p": p_force})
    if comp_s > 0: issues.append({"k": "comp", "v": comp_s, "p": p_comp})
    if vib_s > 0: issues.append({"k": "vib", "v": vib_s, "p": p_vib})
    if gimb_s > 0: issues.append({"k": "gimb", "v": gimb_s, "p": p_gimb})
    if p_sag > 0: issues.append({"k": "sag", "v": max_sag_mv, "p": p_sag})
    if p_cmin > 0 and min_cell_v is not None: issues.append({"k": "cmin", "v": round(min_cell_v, 2), "p": p_cmin})
    if p_dev > 0 and dev is not None: issues.append({"k": "dev", "v": round(dev), "p": p_dev})
    if p_temp > 0 and tmax is not None: issues.append({"k": "temp", "v": round(tmax, 1), "p": p_temp})
    if p_end > 0 and bend is not None: issues.append({"k": "bat_low", "v": int(bend), "p": p_end})
    if p_rc > 0: issues.append({"k": "rc", "v": rc_crit_s + rc_low_s, "p": p_rc})
    if p_gps > 0: issues.append({"k": "gps", "v": gps_low_s, "p": p_gps})

    return {
        "score": int(score),
        "status": status,
        "mech": int(mech_score),
        "bat": int(bat_score),
        "link": int(link_score),
        "sag_mv": int(max_sag_mv),
        "min_cell_v": round(min_cell_v, 2) if min_cell_v is not None else None,
        "rc_drop_s": int(rc_crit_s + rc_low_s),
        "gps_low_s": int(gps_low_s),
        "issues": issues,
    }

def _solar_times_utc(lat: float, lon: float, dt_utc: datetime) -> tuple[Optional[datetime], Optional[datetime]]:
    """Calcula nascer e pôr do sol em UTC (algoritmo NOAA simplificado, sem rede)."""
    try:
        doy = dt_utc.timetuple().tm_yday
        def _calc(is_rise: bool) -> Optional[datetime]:
            lng_h = lon / 15.0
            t = doy + ((6.0 - lng_h) / 24.0 if is_rise else (18.0 - lng_h) / 24.0)
            M = 0.9856 * t - 3.289
            L = (M + 1.916 * sin(radians(M)) + 0.020 * sin(radians(2 * M)) + 282.634) % 360.0
            RA = degrees(atan(0.91764 * tan(radians(L)))) % 360.0
            RA += (floor(L / 90.0) * 90.0 - floor(RA / 90.0) * 90.0)
            RA /= 15.0
            sin_dec = 0.39782 * sin(radians(L))
            cos_dec = cos(asin(sin_dec))
            cos_h = (cos(radians(90.833)) - sin_dec * sin(radians(lat))) / (cos_dec * cos(radians(lat)))
            if cos_h > 1.0 or cos_h < -1.0:
                return None
            H = (360.0 - degrees(acos(cos_h))) / 15.0 if is_rise else degrees(acos(cos_h)) / 15.0
            ut = (H + RA - 0.06571 * t - 6.622 - lng_h) % 24.0
            base = dt_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            return base + timedelta(hours=ut)
        sr, ss = _calc(True), _calc(False)
        if sr and ss and ss < sr:
            ss += timedelta(days=1)
        return sr, ss
    except Exception:
        return None, None

def _calc_compliance(d: dict, meta: dict, track: Optional[list] = None) -> dict:
    """Resumo automático de conformidade ANAC/DECEA por voo."""
    max_alt = float(d.get("max_alt") or 0.0)
    alt_120 = max_alt > 120.0
    max_vlos = float((meta.get("home") or {}).get("max_dist") or 0.0)
    if not max_vlos and track:
        h0 = (track[0][1], track[0][2])
        max_vlos = max((hav(h0[0], h0[1], p[1], p[2]) for p in track), default=0.0)
    vlos_500 = max_vlos > 500.0

    night = False
    sunrise_s, sunset_s = None, None
    lat, lon = d.get("lat"), d.get("lon")
    start_iso = d.get("start")
    if start_iso and valid_gps(lat, lon):
        try:
            dt_s = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            if dt_s.tzinfo is None:
                dt_s = dt_s.replace(tzinfo=timezone.utc)
            else:
                dt_s = dt_s.astimezone(timezone.utc)
            dur_s = float(d.get("duration") or 0.0)
            dt_e = dt_s + timedelta(seconds=dur_s)
            sr, ss = _solar_times_utc(float(lat), float(lon), dt_s)
            if sr and ss:
                # Ajusta dia se o voo começou após meia-noite UTC antes do nascer do sol do dia anterior
                if dt_s < sr - timedelta(hours=12):
                    sr -= timedelta(days=1); ss -= timedelta(days=1)
                sunrise_s = (sr + timedelta(hours=TZ_OFFSET)).strftime("%H:%M")
                sunset_s = (ss + timedelta(hours=TZ_OFFSET)).strftime("%H:%M")
                night = (dt_s < sr) or (dt_e > ss)
        except Exception:
            pass

    air_raw = d.get("air")
    air_obj = None
    if air_raw:
        try:
            air_obj = json.loads(air_raw) if isinstance(air_raw, str) else air_raw
        except Exception:
            air_obj = None
    air_checked = bool(air_obj)
    air_hit = False
    aero_9km = False
    aero_km = None
    if air_obj:
        hits = air_obj.get("hits") or []
        air_hit = any((h.get("sev") or 0) >= 3 for h in hits)
        near = air_obj.get("near") or []
        kms = [float(n["km"]) for n in near if isinstance(n.get("km"), (int, float))]
        if kms:
            aero_km = round(min(kms), 1)
            aero_9km = aero_km <= 9.0
        elif any((h.get("sev") or 0) == 2 for h in hits):
            aero_9km = True

    alerts = []
    if air_hit:
        alerts.append({"code": "air_hit", "sev": "crit"})
    if alt_120:
        alerts.append({"code": "alt_120", "sev": "warn", "val": round(max_alt, 1)})
    if vlos_500:
        alerts.append({"code": "vlos_500", "sev": "warn", "val": round(max_vlos, 1)})
    if night:
        alerts.append({"code": "night", "sev": "warn"})
    if aero_9km:
        alerts.append({"code": "aero_9km", "sev": "warn", "val": aero_km})

    status = "crit" if air_hit else ("warn" if (alt_120 or vlos_500 or night or aero_9km) else "ok")
    return {
        "status": status,
        "alt_120": alt_120,
        "max_alt_m": round(max_alt, 1),
        "vlos_500": vlos_500,
        "max_vlos_m": round(max_vlos, 1),
        "night": night,
        "sunrise": sunrise_s,
        "sunset": sunset_s,
        "air_checked": air_checked,
        "air_hit": air_hit,
        "aero_9km": aero_9km,
        "aero_km": aero_km,
        "alerts": alerts,
    }

def _calc_wind_analysis(track: list, meta: dict, weather_obj: Optional[dict] = None) -> Optional[dict]:
    """Extrai setas de vento ao longo da rota, rosa dos ventos e eficiência Wh/km a favor vs. contra o vento."""
    if not track:
        return None
    samples = []
    for p in track:
        if len(p) >= 12:
            alt_v, spd_v, yw, pt, rl, zspd_v = p[3], p[4], p[7], p[9], p[10], p[11]
            if (isinstance(alt_v, (int, float)) and alt_v >= 5 and
                isinstance(spd_v, (int, float)) and spd_v < 1.5 and
                isinstance(zspd_v, (int, float)) and abs(zspd_v) < 0.8 and
                all(isinstance(v, (int, float)) for v in (pt, rl, yw))):
                tilt = degrees(acos(max(-1.0, min(1.0, cos(radians(pt)) * cos(radians(rl))))))
                if 1.0 <= tilt <= 35.0:
                    w_spd = round(12.0 * sqrt(tan(radians(tilt))), 1)
                    w_dir = round((yw + degrees(atan2(rl, -pt))) % 360)
                    samples.append({"t": p[0], "lat": p[1], "lon": p[2], "alt": round(alt_v, 1),
                                    "spd": w_spd, "deg": w_dir, "card": _cardinal(w_dir)})

    # Subamostragem para manter setas limpas no mapa (mín. 12s ou 20m entre setas, máx. 36)
    pts = []
    for s in samples:
        if not pts or (s["t"] - pts[-1]["t"] >= 12 and hav(pts[-1]["lat"], pts[-1]["lon"], s["lat"], s["lon"]) >= 20):
            pts.append(s)
    if len(pts) > 36:
        step = max(1, len(pts) // 36)
        pts = pts[::step][:36]

    # Rosa dos ventos (8 setores)
    dirs = ["N", "NE", "L", "SE", "S", "SO", "O", "NO"]
    rose_acc = {k: [] for k in dirs}
    for s in samples:
        idx = int((s["deg"] + 22.5) // 45) % 8
        rose_acc[dirs[idx]].append(s["spd"])
    rose = [{"dir": k, "count": len(v), "avg_spd": round(sum(v) / len(v), 1) if v else 0.0} for k, v in rose_acc.items()]

    # Direção de referência do vento (de onde sopra)
    wind_est = meta.get("wind_est")
    ref_deg = None
    ref_spd = None
    if wind_est and isinstance(wind_est.get("deg"), (int, float)):
        ref_deg = float(wind_est["deg"])
        ref_spd = wind_est.get("spd")
    elif samples:
        sx = sum(cos(radians(s["deg"])) for s in samples)
        sy = sum(sin(radians(s["deg"])) for s in samples)
        ref_deg = degrees(atan2(sy, sx)) % 360.0
        ref_spd = round(sum(s["spd"] for s in samples) / len(samples), 1)
    elif weather_obj and isinstance(weather_obj.get("wind_direction_10m"), (int, float)):
        ref_deg = float(weather_obj["wind_direction_10m"])
        ref_spd = weather_obj.get("wind_speed_10m")

    eff = None
    if ref_deg is not None and len(track) >= 2:
        w_to = (ref_deg + 180.0) % 360.0
        buckets = {"tailwind": [0.0, 0.0, 0.0, 0], "headwind": [0.0, 0.0, 0.0, 0], "crosswind": [0.0, 0.0, 0.0, 0]}
        for i in range(len(track) - 1):
            p0, p1 = track[i], track[i + 1]
            dt = (p1[0] - p0[0]) if (p1[0] is not None and p0[0] is not None) else 0
            if dt <= 0 or dt > 5:
                continue
            ds = hav(p0[1], p0[2], p1[1], p1[2])
            if ds < 2.0 or (p0[4] or 0) < 1.5:
                continue
            pwr = p0[14] if len(p0) > 14 else None
            w_val = pwr[2] if (isinstance(pwr, list) and len(pwr) > 2 and isinstance(pwr[2], (int, float)) and pwr[2] > 0) else None
            if w_val is None:
                continue
            brg = _bearing_deg(p0[1], p0[2], p1[1], p1[2])
            diff = abs((brg - w_to + 180.0) % 360.0 - 180.0)
            bkey = "tailwind" if diff <= 60.0 else ("headwind" if diff >= 120.0 else "crosswind")
            b = buckets[bkey]
            b[0] += w_val * (dt / 3600.0)  # Wh
            b[1] += ds / 1000.0            # km
            b[2] += w_val * dt             # W*s
            b[3] += dt                     # s

        eff_out = {}
        for k, (wh_sum, km_sum, ws_sum, s_sum) in buckets.items():
            if km_sum >= 0.05 and s_sum > 0:
                eff_out[k] = {
                    "wh_km": round(wh_sum / km_sum, 1),
                    "avg_w": round(ws_sum / s_sum, 1),
                    "dist_m": round(km_sum * 1000.0),
                }
        if eff_out:
            tw = eff_out.get("tailwind")
            hw = eff_out.get("headwind")
            if tw and hw and tw["wh_km"] > 0:
                eff_out["diff_pct"] = round(((hw["wh_km"] - tw["wh_km"]) / tw["wh_km"]) * 100.0, 1)
            eff = eff_out

    if not pts and eff is None and ref_deg is None:
        return None
    return {
        "ref_deg": round(ref_deg) if ref_deg is not None else None,
        "ref_card": _cardinal(ref_deg) if ref_deg is not None else None,
        "ref_spd": ref_spd,
        "sample_count": len(samples),
        "points": pts,
        "rose": rose,
        "eff": eff,
    }

def fl_out(r, full=False):
    d = dict(r)
    d["weather"] = json.loads(d["weather"]) if d["weather"] else None
    d["meta"] = json.loads(d["meta"]) if d.get("meta") else {}
    d["start_local"] = local(d["start"]).isoformat() if d["start"] else None
    bat_obj = json.loads(d["bat"]) if d.get("bat") else {}
    tr = None
    if "health" not in d["meta"] and (d.get("source") or "log") != "photos":
        try:
            tr = json.loads(d["track"]) if d.get("track") else []
            hl = _calc_flight_health(tr, bat_obj, d["meta"], d.get("source") or "log")
            if hl:
                d["meta"]["health"] = hl
        except Exception:
            pass
    d["health"] = d["meta"].get("health")
    d["compliance"] = _calc_compliance(d, d["meta"], tr)
    if full:
        if tr is None:
            tr = json.loads(d["track"])
        d["track"], d["events"] = tr, json.loads(d["events"])
        # Fallback automático para Ponto Home (H) e distância máxima VLOS caso o log ainda não tenha sido reimportado
        if "home" not in d["meta"] and tr:
            h0 = (tr[0][1], tr[0][2])
            md = max((hav(h0[0], h0[1], p[1], p[2]) for p in tr), default=0.0)
            d["meta"]["home"] = {"lat": h0[0], "lon": h0[1], "rth_alt": None, "max_dist": round(md, 1)}
            d["compliance"] = _calc_compliance(d, d["meta"], tr)
        d["wind_analysis"] = _calc_wind_analysis(tr, d["meta"], d["weather"])
    else:
        d.pop("track", None); d.pop("events", None)
    return d

def get_flight(fid):
    with db() as c: r = c.execute("SELECT * FROM flights WHERE id=?", (fid,)).fetchone()
    if not r: raise HTTPException(404, "voo não encontrado")
    return r

def _enrich_media(d):
    d.pop("clip_emb", None)
    d["meta"] = json.loads(d["meta"]) if d.get("meta") else {}
    d["detections"] = json.loads(d["detections"]) if d.get("detections") else None
    w, h = d.get("w") or 0, d.get("h") or 0
    pano = bool(d["meta"].get("pano360") or (w >= 3600 and h > 0 and 1.95 <= (w / h) <= 2.05))
    d["orient"] = "pano" if pano else ("portrait" if (h > w > 0) else ("landscape" if w > 0 else ""))
    d["is_4k"] = bool(w >= 3840 or h >= 3840)
    return d

def get_media(mid):
    with db() as c: r = c.execute("SELECT m.*, f.place AS place FROM media m LEFT JOIN flights f ON f.id=m.flight_id WHERE m.id=?", (mid,)).fetchone()
    if not r: raise HTTPException(404, "mídia não encontrada")
    return _enrich_media(dict(r))

@app.post("/api/scan")
def scan(force: int = 0):
    res = {"logs": 0, "media": 0, "errors": []}
    with db() as c:
        known = set() if force else {r[0] for r in c.execute("SELECT file FROM flights WHERE source='log'")}
        if force: c.execute("UPDATE media SET flight_id=NULL WHERE COALESCE(manual,0)=0 AND flight_id IN (SELECT id FROM flights WHERE source='log')")
    log_files = sorted(list(LOGS_DIR.rglob("*FlightRecord*.txt")) + list(LOGS_DIR.rglob("*.csv")) +
                       list(LOGS_DIR.rglob("*.ulg")) + list(LOGS_DIR.rglob("*.bin")))
    for p in log_files:
        if str(p) in known: continue
        try: import_log(p); res["logs"] += 1
        except Exception as e: res["errors"].append(f"{p.name}: {e}")
    res["media"] = index_media(res["errors"]); link_media(); res["reconstruidos"] = reconstruct_flights(); check_backup()
    return res

@app.post("/api/logs/upload")
async def upload_log(request: Request, name: str = ""):
    """Recebe arquivo de log (.txt DJI, .csv Litchi/Airdata, .ulg PX4 ou .bin ArduPilot) via streaming, salva em data/logs/ e importa."""
    raw_name = Path(name or "").name
    safe_name = re.sub(r"[^A-Za-z0-9_.\-\[\]]", "_", raw_name)
    ext = Path(safe_name).suffix.lower()
    if ext not in (".txt", ".csv", ".ulg", ".bin") or len(safe_name) < 5:
        raise HTTPException(400, "Formato inválido. Envie .txt (DJI), .csv (Litchi/Airdata), .ulg (PX4) ou .bin (ArduPilot).")
    if ext == ".txt" and "FlightRecord" not in safe_name:
        safe_name = "DJIFlightRecord_" + safe_name
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    dest = LOGS_DIR / safe_name
    size = 0
    max_bytes = 25 * 1024 * 1024  # limite de 25 MB por arquivo de log
    fd, tmp_s = tempfile.mkstemp(prefix=".up_", suffix=".tmp", dir=str(LOGS_DIR))
    tmp = Path(tmp_s)
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, "Arquivo excede o limite máximo de 25 MB.")
                f.write(chunk)
        if size < 64:
            raise HTTPException(400, "Arquivo de log vazio ou inválido.")
        existed = dest.exists()
        bak = LOGS_DIR / (".bak_" + safe_name) if existed else None
        if bak:
            shutil.copy2(str(dest), str(bak))
        shutil.move(str(tmp), str(dest))
        try:
            import_log(dest)
            link_media()
            if bak: _unlink_quiet(bak)
            return {"ok": True, "file": safe_name, "bytes": size}
        except Exception as e:
            if bak and bak.exists():
                shutil.move(str(bak), str(dest))
            elif not existed:
                _unlink_quiet(dest)
            return {"ok": False, "file": safe_name, "error": str(e)}
    except HTTPException:
        _unlink_quiet(tmp)
        raise
    except Exception as e:
        _unlink_quiet(tmp)
        raise HTTPException(400, f"Erro ao salvar arquivo de log: {e}")

@app.get("/api/config")
def config(): return {"airspace": AIRSPACE_TILES, "cesium": os.getenv("CESIUM_TOKEN", "")}

@app.get("/api/flights")
def flights(q: str = "", min_alt: float = 0, min_dur: float = 0, date_from: str = "", date_to: str = "", battery: str = "",
            drone: str = "", fav: int = 0, source: str = "", bbox: str = "", sort: str = "start", order: str = "desc"):
    bb = _parse_bbox_param(bbox)
    with db() as c:
        if bb:
            rows = c.execute(
                "SELECT f.* FROM flights f JOIN rtree_flights rf ON rf.id = f.id "
                "WHERE rf.max_lon >= ? AND rf.min_lon <= ? AND rf.max_lat >= ? AND rf.min_lat <= ?",
                (bb[0], bb[1], bb[2], bb[3])
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM flights").fetchall()
    out = [fl_out(r) for r in rows]
    def ok(f):
        d = (f["start_local"] or "")[:10]
        dr = g(f, "meta", "drone") or {}
        dlabel = f'{dr.get("type") or ""} {dr.get("name") or ""} {dr.get("sn") or ""}'.strip()
        txt = f'{f["name"] or ""} {f["place"] or ""} {f["notes"] or ""} {dlabel}'.lower()
        return ((f["max_alt"] or 0) >= min_alt and (f["duration"] or 0) >= min_dur * 60 and q.lower() in txt
                and (not date_from or d >= date_from) and (not date_to or d <= date_to)
                and (not battery or f["battery_sn"] == battery) and (not fav or f["fav"])
                and (not source or (f["source"] or "log") == source)
                and (not drone or drone.lower() in dlabel.lower()))
    key = sort if sort in ("start", "duration", "distance", "max_alt", "max_speed", "place", "name", "fav", "health") else "start"
    def val(f):
        if key == "health":
            return (f.get("health") or {}).get("score") if f.get("health") else -1
        v = f[key]
        return (v or "").lower() if key in ("place", "name", "start") else (v or 0)
    return sorted((f for f in out if ok(f)), key=val, reverse=(order == "desc"))

@app.get("/api/flight-filters")
def flight_filters():
    with db() as c:
        bats = [r[0] for r in c.execute(
            "SELECT DISTINCT battery_sn FROM flights WHERE battery_sn IS NOT NULL AND battery_sn<>'' ORDER BY battery_sn")]
        drones = set()
        for (m,) in c.execute("SELECT meta FROM flights WHERE meta IS NOT NULL"):
            try:
                dr = (json.loads(m).get("drone") or {})
                lbl = dr.get("type") or dr.get("name") or dr.get("sn")
                if lbl: drones.add(f'{lbl}' + (f' ({dr["sn"][-6:]})' if dr.get("sn") else ''))
            except Exception: pass
        return {"batteries": bats, "drones": sorted(drones)}

@app.get("/api/tracks")
def tracks(bbox: str = ""):
    bb = _parse_bbox_param(bbox)
    with db() as c:
        if bb:
            rows = c.execute(
                "SELECT f.id,f.start,f.place,f.distance,f.max_alt,f.track,f.source,f.name,f.fav "
                "FROM flights f JOIN rtree_flights rf ON rf.id = f.id "
                "WHERE rf.max_lon >= ? AND rf.min_lon <= ? AND rf.max_lat >= ? AND rf.min_lat <= ?",
                (bb[0], bb[1], bb[2], bb[3])
            ).fetchall()
        else:
            rows = c.execute("SELECT id,start,place,distance,max_alt,track,source,name,fav FROM flights").fetchall()
    return [{**{k: r[k] for k in ("id", "start", "place", "distance", "max_alt", "source", "name", "fav")},
             "pts": simplify_track_rdp([[p[1], p[2]] for p in json.loads(r["track"]) if valid_gps(p[1], p[2])], 2.0)} for r in rows]

@app.get("/api/flights/{fid}")
def flight(fid: int): return fl_out(get_flight(fid), True)

class FlightPatch(BaseModel):
    notes: Optional[str] = None
    name: Optional[str] = None
    fav: Optional[int] = None

@app.patch("/api/flights/{fid}")
def flight_patch(fid: int, b: FlightPatch):
    with db() as c:
        if b.notes is not None: c.execute("UPDATE flights SET notes=? WHERE id=?", (b.notes, fid))
        if b.name is not None: c.execute("UPDATE flights SET name=? WHERE id=?", (b.name.strip(), fid))
        if b.fav is not None: c.execute("UPDATE flights SET fav=? WHERE id=?", (1 if b.fav else 0, fid))
    return {"ok": True}

def attach(data, name, mt):
    return Response(data, media_type=mt, headers={"Content-Disposition": f"attachment; filename={name}"})

@app.get("/api/flights/{fid}/kml")
def kml(fid: int):
    r = get_flight(fid); co = " ".join(f"{float(p[2])},{float(p[1])},{float(p[3])}" for p in json.loads(r["track"]))
    return attach('<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Placemark>'
                  f'<name>{html.escape(str(r["start"] or ""))}</name><LineString><altitudeMode>relativeToGround</altitudeMode><coordinates>{co}</coordinates>'
                  '</LineString></Placemark></kml>', f"voo{fid}.kml", "application/vnd.google-earth.kml+xml")

@app.get("/api/flights/{fid}/gpx")
def gpx(fid: int):
    r = get_flight(fid)
    pts = "".join(f'<trkpt lat="{float(p[1])}" lon="{float(p[2])}"><ele>{float(p[3])}</ele></trkpt>' for p in json.loads(r["track"]))
    return attach(f'<?xml version="1.0"?><gpx version="1.1" creator="drone-manager"><trk><name>{html.escape(str(r["start"] or ""))}</name><trkseg>{pts}</trkseg></trk></gpx>',
                  f"voo{fid}.gpx", "application/gpx+xml")

@app.get("/api/flights/{fid}/geojson")
def flight_geojson(fid: int):
    """Exporta o voo em GeoJSON padrão RFC 7946 (WGS 84 / EPSG:4326, ordem [longitude, latitude, altitude])."""
    f = fl_out(get_flight(fid), True)
    tr = f["track"]
    coords = [[round(float(p[2]), 6), round(float(p[1]), 6), round(float(p[3] or 0), 1)]
              for p in tr if valid_gps(p[1], p[2])]
    features = [{
        "type": "Feature",
        "properties": {"id": f["id"], "name": f["name"] or "", "place": f["place"] or "",
                       "start": f["start"], "duration_s": f["duration"], "distance_m": round(f["distance"] or 0, 1),
                       "max_alt_m": f["max_alt"], "source": f["source"], "crs": "EPSG:4326"},
        "geometry": {"type": "LineString", "coordinates": coords}
    }]
    hm = g(f, "meta", "home")
    if hm and valid_gps(hm.get("lat"), hm.get("lon")):
        features.append({
            "type": "Feature",
            "properties": {"kind": "home_point", "rth_alt_m": hm.get("rth_alt"), "max_vlos_m": hm.get("max_dist")},
            "geometry": {"type": "Point", "coordinates": [round(float(hm["lon"]), 6), round(float(hm["lat"]), 6)]}
        })
    return attach(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2),
                  f"voo{fid}.geojson", "application/geo+json")

@app.get("/api/flights/{fid}/package")
def flight_package(fid: int, lang: str = ""):
    """Gera um arquivo HTML 100% self-contained (offline) para envio a clientes ou auditoria."""
    import base64
    L = get_lang(lang)
    f = fl_out(get_flight(fid), True)
    tr_pts = f.get("track") or []
    w = f.get("weather")
    hl = f.get("health")
    def _j(v):
        if isinstance(v, dict): return v
        if isinstance(v, str) and v.strip():
            try: return json.loads(v)
            except Exception: pass
        return {}
    bat = _j(f.get("bat"))
    meta = _j(f.get("meta"))
    drone = meta.get("drone") or {}
    air = _j(f.get("air")) or None

    # 1. Fotos principais embutidas em base64 (a partir dos thumbs locais)
    with db() as c:
        ms = c.execute("SELECT id, taken, lat, lon FROM media WHERE flight_id=? AND kind='photo' ORDER BY fav DESC, taken LIMIT 12", (fid,)).fetchall()
    imgs_html = []
    for m in ms:
        tp = THUMBS / f"{m['id']}.jpg"
        if tp.is_file():
            b64 = base64.b64encode(tp.read_bytes()).decode("ascii")
            cap = (m["taken"] or "")[:16].replace("T", " ")
            imgs_html.append(
                f"<figure style='margin:0;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;overflow:hidden'>"
                f"<img src='data:image/jpeg;base64,{b64}' style='width:100%;height:135px;object-fit:cover;display:block'>"
                f"<figcaption style='padding:5px 8px;font-size:11px;color:#475569'>{html.escape(cap)}</figcaption></figure>"
            )

    # 2. Mapa SVG vetorial da rota
    map_svg = ""
    if len(tr_pts) >= 2:
        lats = [p[1] for p in tr_pts]
        lons = [p[2] for p in tr_pts]
        min_la, max_la = min(lats), max(lats)
        min_lo, max_lo = min(lons), max(lons)
        span = max(max_la - min_la, (max_lo - min_lo) * cos(radians((min_la + max_la) / 2.0)), 1e-5)
        W, H, pad = 420, 240, 22
        cx, cy = (min_lo + max_lo) / 2.0, (min_la + max_la) / 2.0
        cos_lat = max(0.2, cos(radians(cy)))
        sc = (min(W, H) - 2 * pad) / span
        px = lambda lo: W / 2.0 + (lo - cx) * cos_lat * sc
        py = lambda la: H / 2.0 - (la - cy) * sc
        poly = " ".join(f"{px(p[2]):.1f},{py(p[1]):.1f}" for p in tr_pts)
        im_idx = max(range(len(tr_pts)), key=lambda i: tr_pts[i][3] or 0)
        map_svg = (
            f"<svg viewBox='0 0 {W} {H}' style='width:100%;height:220px;background:#0f172a;border-radius:8px;display:block'>"
            f"<polyline points='{poly}' fill='none' stroke='#38bdf8' stroke-width='2.5' stroke-linejoin='round'/>"
            f"<circle cx='{px(tr_pts[0][2]):.1f}' cy='{py(tr_pts[0][1]):.1f}' r='5' fill='#10b981' stroke='#fff' stroke-width='1.5'/>"
            f"<circle cx='{px(tr_pts[-1][2]):.1f}' cy='{py(tr_pts[-1][1]):.1f}' r='5' fill='#ef4444' stroke='#fff' stroke-width='1.5'/>"
            f"<circle cx='{px(tr_pts[im_idx][2]):.1f}' cy='{py(tr_pts[im_idx][1]):.1f}' r='4.5' fill='#f59e0b' stroke='#fff' stroke-width='1.2'/>"
            f"<text x='10' y='{H-10}' fill='#94a3b8' font-size='11'>🟢 Decolagem · 🟠 Alt. Máx ({py_fmt_alt(tr_pts[im_idx][3] or 0, L)}) · 🔴 Pouso</text></svg>"
        )

    # 3. Gráfico SVG de Altitude & Bateria
    chart_svg = ""
    if len(tr_pts) >= 2:
        W, H, pad = 420, 220, 20
        max_t = max(1, tr_pts[-1][0] or 1)
        max_a = max(10.0, max((p[3] or 0) for p in tr_pts))
        x_at = lambda t: pad + (t / max_t) * (W - 2 * pad)
        y_alt = lambda a: H - pad - (a / max_a) * (H - 2 * pad)
        alt_pts = " ".join(f"{x_at(p[0]):.1f},{y_alt(p[3] or 0):.1f}" for p in tr_pts)
        bat_pts = " ".join(f"{x_at(p[0]):.1f},{H - pad - ((p[5] or 0) / 100.0) * (H - 2 * pad):.1f}" for p in tr_pts if p[5] is not None)
        chart_svg = (
            f"<svg viewBox='0 0 {W} {H}' style='width:100%;height:220px;background:#0f172a;border-radius:8px;display:block'>"
            f"<polyline points='{alt_pts}' fill='none' stroke='#00d2ff' stroke-width='2.2'/>"
            + (f"<polyline points='{bat_pts}' fill='none' stroke='#10b981' stroke-width='1.8' stroke-dasharray='4 3'/>" if bat_pts else "")
            + f"<text x='10' y='18' fill='#00d2ff' font-size='11'>— Altitude (máx {py_fmt_alt(max_a, L)})</text>"
            + (f"<text x='180' y='18' fill='#10b981' font-size='11'>-- Bateria (%)</text>" if bat_pts else "")
            + "</svg>"
        )

    # 4. Espaço Aéreo DECEA
    if not air or not air.get("checked"):
        air_html = "<span style='color:#64748b'>Não verificado no DECEA.</span>"
    else:
        sev = air.get("sev", 0)
        badge = "🟢 Livre" if sev == 0 else ("🟡 Atenção (aeródromo próximo)" if sev == 2 else "🔴 Restrito / Proibido")
        items = [f"{z.get('layer','')}: {z.get('name') or z.get('id') or ''}" for z in (air.get("hits") or [])]
        items += [f"Aeródromo próximo: {z.get('name','')} ({z.get('dist_km','?')} km)" for z in (air.get("near") or [])]
        air_html = f"<b>{badge}</b>" + (" — " + html.escape("; ".join(items[:5])) if items else "")

    wx_html = (f"{py_fmt_temp(w['temperature_2m'], L)}, {tr(L, 'fdet.wind_lbl')} {py_fmt_spd(w['wind_speed_10m'], L)} "
               f"({tr(L, 'fdet.gust_short')} {py_fmt_spd(w['wind_gusts_10m'], L)}), {w['precipitation']} mm") if w else tr(L, "report.no_data")
    ev_html = "".join(f"<li><b>{e['t']//60}m{e['t']%60:02d}s</b> — {html.escape(tr_event(e['label'], L))}</li>" for e in f["events"][:40])
    dur_s = int(f["duration"] or 0)
    title = html.escape(f["name"] or tr(L, "report.flight_default"))
    dt_str = (f.get("start_local") or "")[:16].replace("T", " ")
    hl_txt = f"{hl['score']}/100 ({tr(L, 'fdet.health_' + hl['status'])})" if hl else "—"
    bat_txt = f"{bat.get('start','?')}% → {bat.get('end','?')}%" if bat.get("start") is not None else "—"

    doc = f"""<!doctype html><html lang="{L}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pacote de Voo #{fid} — {title}</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;background:#f1f5f9;color:#0f172a;margin:0;padding:24px}}
.wrap{{max-width:960px;margin:0 auto;background:#fff;border-radius:12px;padding:24px 28px;box-shadow:0 4px 20px rgba(15,23,42,.08)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}}
.card{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px 12px}}
.card small{{display:block;color:#64748b;font-size:11.5px}}.card b{{font-size:15px}}
.cols{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px;margin:14px 0}}
.photos{{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin-top:10px}}
@media print{{body{{background:#fff;padding:0}}.wrap{{box-shadow:none}}.noprint{{display:none}}}}
</style></head><body><div class="wrap">
<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px">
  <div><h1 style="margin:0;font-size:22px">🛸 {title}</h1>
  <div style="color:#475569;font-size:13.5px;margin-top:4px">{html.escape(dt_str)} · {html.escape(f.get('place') or '—')} · {html.escape(drone.get('type') or 'Drone')}</div></div>
  <button class="noprint" onclick="window.print()" style="padding:8px 14px;border:1px solid #cbd5e1;border-radius:8px;background:#0f172a;color:#fff;cursor:pointer">🖨 Imprimir / PDF</button>
</div>
<div class="grid">
  <div class="card"><small>{tr(L,'report.duration')}</small><b>{dur_s//60} min {dur_s%60:02d} s</b></div>
  <div class="card"><small>{tr(L,'report.distance')}</small><b>{py_fmt_dist(f['distance'] or 0, L)}</b></div>
  <div class="card"><small>{tr(L,'report.max_alt')}</small><b>{py_fmt_alt(f['max_alt'] or 0, L)}</b></div>
  <div class="card"><small>{tr(L,'report.max_speed')}</small><b>{py_fmt_kmh(f['max_speed'] or 0, L)}</b></div>
  <div class="card"><small>{tr(L,'report.battery')}</small><b>{bat_txt}</b></div>
  <div class="card"><small>{tr(L,'fdet.health_title')}</small><b>{hl_txt}</b></div>
</div>
<div class="card" style="margin-bottom:10px"><small>Espaço Aéreo (DECEA / GEOAISWEB)</small>{air_html}</div>
<div class="card" style="margin-bottom:14px"><small>{tr(L,'report.weather')}</small>{wx_html}</div>
<div class="cols">{map_svg}{chart_svg}</div>
<h3>{tr(L,'report.notes')}</h3><p style="color:#334155">{html.escape(f.get('notes') or '—')}</p>
<h3>{tr(L,'report.events')}</h3><ul style="columns:2;column-gap:24px;font-size:13px;color:#334155">{ev_html}</ul>
{f"<h3>Fotos ({len(imgs_html)})</h3><div class='photos'>{''.join(imgs_html)}</div>" if imgs_html else ""}
<hr style="border:0;border-top:1px solid #e2e8f0;margin:20px 0 8px">
<div style="font-size:11.5px;color:#94a3b8">Pacote Portátil Offline gerado pelo Drone Manager · Voo #{fid} · {datetime.now().strftime('%d/%m/%Y %H:%M')}</div>
</div></body></html>"""
    return attach(doc, f"pacote_voo_{fid}.html", "text/html; charset=utf-8")

@app.get("/api/flights/{fid}/report", response_class=HTMLResponse)
def report(fid: int, lang: str = ""):
    L = get_lang(lang)
    f = fl_out(get_flight(fid), True); w = f["weather"]
    with db() as c: ms = c.execute("SELECT id FROM media WHERE flight_id=? AND kind='photo' LIMIT 12", (fid,)).fetchall()
    ev = "".join(f"<li>{e['t']//60}m{e['t']%60:02d}s — {html.escape(tr_event(e['label'], L))}</li>" for e in f["events"])
    wx = (f"{py_fmt_temp(w['temperature_2m'], L)}, {tr(L, 'fdet.wind_lbl')} {py_fmt_spd(w['wind_speed_10m'], L)} "
          f"({tr(L, 'fdet.gust_short')} {py_fmt_spd(w['wind_gusts_10m'], L)}), {w['precipitation']} mm") if w else tr(L, "report.no_data")
    hl = f.get("health")
    hl_s = f" · {tr(L, 'fdet.health_title')}: <b>{hl['score']}/100</b> ({tr(L, 'fdet.health_' + hl['status'])})" if hl else ""
    cp = f.get("compliance") or {}
    cp_tags = []
    if cp.get("air_hit"): cp_tags.append(tr(L, "comp.air_hit"))
    if cp.get("alt_120"): cp_tags.append(tr(L, "comp.alt_120"))
    if cp.get("vlos_500"): cp_tags.append(tr(L, "comp.vlos_500"))
    if cp.get("night"): cp_tags.append(tr(L, "comp.night"))
    if cp.get("aero_9km"): cp_tags.append(tr(L, "comp.aero_9km", km=cp.get("aero_km") or "≤9"))
    cp_s = f"<p><b>{tr(L, 'comp.title')}:</b> " + (", ".join(cp_tags) if cp_tags else tr(L, "comp.all_ok")) + "</p>"
    th = "".join(f'<img src="/api/media/{m["id"]}/thumb" width=170>' for m in ms)
    return f"""<!doctype html><html lang="{L}"><meta charset=utf-8><title>{tr(L, 'report.flight_title', id=fid)}</title><body style="font:15px system-ui;max-width:760px;margin:30px auto">
<button onclick=print() style="float:right">{tr(L, 'report.print_pdf')}</button>
<h2>{html.escape(f['name'] or tr(L, 'report.flight_default'))} — {(f.get('start_local') or '')[:16].replace('T', ' ')} — {html.escape(f['place'] or '')}</h2>
<p>{tr(L, 'report.duration')} {int(f['duration'] or 0)//60} min {int(f['duration'] or 0)%60} s · {tr(L, 'report.distance')} {py_fmt_dist(f['distance'] or 0, L)} · {tr(L, 'report.max_alt')} {py_fmt_alt(f['max_alt'] or 0, L)} ·
{tr(L, 'report.max_speed')} {py_fmt_kmh(f['max_speed'] or 0, L)} · {tr(L, 'report.battery')} {html.escape(f['battery_sn'] or '—')}{hl_s}</p>
{cp_s}<p>{tr(L, 'report.weather')}: {wx}</p><h3>{tr(L, 'report.events')}</h3><ul>{ev}</ul><h3>{tr(L, 'report.notes')}</h3><p>{html.escape(f['notes'] or '—')}</p><div>{th}</div></body></html>"""

def _csv_safe(v):
    s = str(v if v is not None else "")
    return ("'" + s) if s.startswith(("=", "+", "-", "@", "\t", "\r")) else s

@app.get("/api/logbook.csv")
def logbook():
    with db() as c: rows = c.execute("SELECT * FROM flights WHERE source='log' ORDER BY start").fetchall()
    o = io.StringIO(); w = csv.writer(o)
    w.writerow(["data_hora_local", "local", "duracao_min", "distancia_km", "altura_max_m", "bateria_sn", "lat", "lon", "notas"])
    for r in rows:
        dt_s = local(r["start"]).isoformat(" ") if r["start"] else ""
        w.writerow([dt_s, _csv_safe(r["place"]), round((r["duration"] or 0) / 60, 1), round((r["distance"] or 0) / 1000, 2),
                    round(r["max_alt"] or 0, 1), _csv_safe(r["battery_sn"]), r["lat"], r["lon"], _csv_safe(r["notes"])])
    return attach(o.getvalue(), "diario_de_voo.csv", "text/csv")

@app.get("/api/media")
def media(kind: str = "", q: str = "", fav: int = 0, flight_id: int = 0, geo: int = 0, unlinked: int = 0,
          orient: str = "", date_from: str = "", date_to: str = "", near: str = "", bbox: str = ""):
    bb = _parse_bbox_param(bbox)
    near_params = None
    if near:   # "lat,lon,km"
        try:
            la, lo, km = map(float, near.split(","))
            near_params = (la, lo, km * 1000.0)
            nbb = _bbox_for_radius(la, lo, km * 1000.0)
            bb = (max(bb[0], nbb[0]), min(bb[1], nbb[1]), max(bb[2], nbb[2]), min(bb[3], nbb[3])) if bb else nbb
        except Exception:
            raise HTTPException(400, "Formato inválido para 'near'. Use lat,lon,km.")
    join_rt = " JOIN rtree_media rm ON rm.id=m.id" if bb else ""
    sql, a = f"SELECT m.*, f.place AS place FROM media m{join_rt} LEFT JOIN flights f ON f.id=m.flight_id WHERE 1=1", []
    if bb:
        sql += " AND rm.min_lon>=? AND rm.max_lon<=? AND rm.min_lat>=? AND rm.max_lat<=?"
        a.extend([bb[0], bb[1], bb[2], bb[3]])
    for cond, val, arg in ((kind, " AND m.kind=?", kind), (fav, " AND m.fav=1", None), (flight_id, " AND m.flight_id=?", flight_id),
                           (unlinked, " AND m.flight_id IS NULL", None), (geo, " AND m.lat IS NOT NULL", None),
                           (date_from, " AND m.taken>=?", date_from), (date_to, " AND m.taken<=?", date_to + "~")):
        if cond:
            sql += val
            if arg is not None: a.append(arg)
    if near_params:
        sql += " AND ST_Distance_Sphere(?, ?, m.lat, m.lon) <= ?"
        a.extend([near_params[0], near_params[1], near_params[2]])
    with db() as c: rows = [_enrich_media(dict(r)) for r in c.execute(sql + " ORDER BY m.taken DESC", a)]
    if orient:
        if orient == "4k": rows = [r for r in rows if r["is_4k"]]
        else: rows = [r for r in rows if r["orient"] == orient]
    if q: rows = [r for r in rows if q.lower() in f'{r["tags"]} {r["path"]} {r["place"] or ""}'.lower()]
    with db() as c: offp = [r["path"].rstrip("/") + "/" for r in c.execute("SELECT path FROM media_roots") if not Path(r["path"]).is_dir()]
    for r in rows: r["offline"] = bool(offp) and any(r["path"].startswith(o) for o in offp)
    if flight_id:
        upd = []
        for r in rows:
            if r["kind"] == "video" and not r["offline"]:
                m = r.get("meta") if isinstance(r.get("meta"), dict) else {}
                if not m.get("dur"):
                    d = _probe_video_duration(Path(r["path"]))
                    if d:
                        m = dict(m); m["dur"] = d; r["meta"] = m
                        upd.append((json.dumps(m), r["id"]))
        if upd:
            with db() as c:
                for mj, mid in upd:
                    c.execute("UPDATE media SET meta=? WHERE id=?", (mj, mid))
    return rows

@app.get("/api/media/{mid:int}")
def media_one(mid: int): return get_media(mid)

def _ang_diff(a, b):
    if a is None or b is None: return None
    try:
        d = abs((float(a) - float(b) + 180.0) % 360.0 - 180.0)
        return round(d, 1)
    except Exception:
        return None

@app.get("/api/media/{mid:int}/nearby")
def media_nearby(mid: int, radius_m: float = 150.0, limit: int = 30):
    """Lista fotos tiradas próximas à coordenada GPS de uma foto base usando pré-filtro R-Tree + ST_Distance_Sphere."""
    base = get_media(mid)
    radius_m = max(1.0, min(float(radius_m), 50000.0))
    limit = max(1, min(int(limit), 100))
    if base.get("kind") != "photo" or base.get("lat") is None or base.get("lon") is None:
        return {"base": base, "radius_m": radius_m, "items": []}
    bm = base.get("meta") or {}
    byaw, bpitch = bm.get("gyaw"), bm.get("gpitch")
    bt = None
    if base.get("taken"):
        try: bt = datetime.fromisoformat(str(base["taken"])[:19])
        except Exception: bt = None
    bb = _bbox_for_radius(base["lat"], base["lon"], radius_m)
    with db() as c:
        rows = [_enrich_media(dict(r)) for r in c.execute(
            "SELECT m.*, f.place AS place, ST_Distance_Sphere(?, ?, m.lat, m.lon) AS _dist_m "
            "FROM media m JOIN rtree_media rm ON rm.id = m.id "
            "LEFT JOIN flights f ON f.id = m.flight_id "
            "WHERE rm.min_lon >= ? AND rm.max_lon <= ? AND rm.min_lat >= ? AND rm.max_lat <= ? "
            "AND m.kind='photo' AND m.id!=? AND ST_Distance_Sphere(?, ?, m.lat, m.lon) <= ?",
            (base["lat"], base["lon"], bb[0], bb[1], bb[2], bb[3], mid, base["lat"], base["lon"], radius_m)
        )]
        offp = [r["path"].rstrip("/") + "/" for r in c.execute("SELECT path FROM media_roots") if not Path(r["path"]).is_dir()]
    items = []
    for r in rows:
        d = r.pop("_dist_m", None)
        if d is None:
            d = hav(base["lat"], base["lon"], r["lat"], r["lon"])
        if d > radius_m: continue
        rm = r.get("meta") or {}
        r["dist_m"] = round(float(d), 1)
        r["yaw_diff"] = _ang_diff(byaw, rm.get("gyaw"))
        r["pitch_diff"] = round(abs(float(bpitch) - float(rm["gpitch"])), 1) if bpitch is not None and rm.get("gpitch") is not None else None
        r["days_diff"] = None
        if bt and r.get("taken"):
            try:
                rt = datetime.fromisoformat(str(r["taken"])[:19])
                r["days_diff"] = abs((rt - bt).days)
            except Exception: pass
        r["offline"] = bool(offp) and any(r["path"].startswith(o) for o in offp)
        items.append(r)
    items.sort(key=lambda x: (x["dist_m"], x["yaw_diff"] if x["yaw_diff"] is not None else 999.0))
    return {"base": base, "radius_m": radius_m, "items": items[:limit]}

@app.get("/api/media/{mid}/srt")
def media_srt(mid: int):
    """Retorna a trilha segundo a segundo do arquivo .SRT do vídeo (GPS, altitude e câmera)."""
    m = get_media(mid)
    return parse_srt_full(Path(m["path"]))

def _ass_safe(s):
    return re.sub(r"[\r\n{}]", "", str(s or ""))[:40]

@app.get("/api/media/{mid}/hud-video")
def media_hud_video(mid: int, start_s: float = 0.0, dur_s: float = 20.0):
    """Renderiza e exporta via FFmpeg um clipe MP4 com HUD de telemetria DJI embutido."""
    m = get_media(mid)
    if m["kind"] != "video": raise HTTPException(400, "A mídia escolhida não é um vídeo.")
    vp = Path(m["path"])
    if not vp.exists(): raise HTTPException(400, "Arquivo de vídeo indisponível (disco desconectado?).")
    start_s = max(0.0, float(start_s))
    dur_s = max(3.0, min(180.0, float(dur_s)))
    # Obtém telemetria segundo a segundo: do voo associado (prioridade) ou do arquivo .SRT
    t_map = {}
    srt_pts = parse_srt_full(vp)
    srt_by_t = {p["t"]: p for p in srt_pts}
    if m.get("flight_id"):
        try:
            f = fl_out(get_flight(m["flight_id"]), True)
            tr = f["track"]
            fst = datetime.fromisoformat(f["start_local"])
            vt = datetime.fromisoformat(m["taken"])
            off = (vt - fst).total_seconds()
            for e in f["events"]:
                if e["label"].startswith("Início de gravação") and e["t"] > 0 and abs(e["t"] - off) <= 30:
                    off = e["t"]; break
            off += float(m.get("sync_adj") or 0)
            h0 = g(f, "meta", "home") or {"lat": tr[0][1], "lon": tr[0][2]}
            for sec in range(int(dur_s) + 2):
                ft = start_s + sec + off
                p = next((x for x in tr if x[0] >= ft), tr[-1] if tr else None)
                if p:
                    hd = round(hav(h0["lat"], h0["lon"], p[1], p[2]))
                    sp = srt_by_t.get(int(start_s + sec), {})
                    cam = f"  |  ISO {int(sp['iso'])}  {_ass_safe(sp.get('shutter'))}" if sp.get("iso") else ""
                    t_map[sec] = (f"ALT: {p[3]:.1f} m   |   VEL: {p[4]*3.6:.0f} km/h   |   DIST H: {hd} m   |   "
                                  f"BAT: {p[5] if p[5] is not None else '--'}%   |   GPS: {p[1]:.5f}, {p[2]:.5f}{cam}")
        except Exception: pass
    if not t_map and srt_pts:
        h0 = (srt_pts[0].get("lat") or 0, srt_pts[0].get("lon") or 0)
        for sec in range(int(dur_s) + 2):
            sp = srt_by_t.get(int(start_s + sec)) or srt_pts[min(len(srt_pts) - 1, int(start_s + sec))]
            la, lo, al = sp.get("lat") or 0, sp.get("lon") or 0, sp.get("alt") or 0
            hd = round(hav(h0[0], h0[1], la, lo)) if (la and lo and h0[0]) else 0
            cam = f"   |   ISO {sp.get('iso') or '--'}  {_ass_safe(sp.get('shutter'))}  EV {sp.get('ev') or 0}"
            t_map[sec] = f"ALT: {al:.1f} m   |   DIST H: {hd} m   |   GPS: {la:.5f}, {lo:.5f}{cam}"
    if not t_map:
        for sec in range(int(dur_s) + 2):
            t_map[sec] = f"DJI TELEMETRY   |   {m['taken'][:16].replace('T', ' ')}   |   T+{int(start_s + sec)}s"
    fd_ass, ass_name = tempfile.mkstemp(prefix=f"hud_{mid}_", suffix=".ass", dir=str(THUMBS))
    os.close(fd_ass)
    ass_path = Path(ass_name)
    fd_mp4, mp4_name = tempfile.mkstemp(prefix=f"hud_{mid}_", suffix=".mp4", dir=str(THUMBS))
    os.close(fd_mp4)
    out_mp4 = Path(mp4_name)
    ass_lines = [
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n",
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: HUD,DejaVu Sans Mono,28,&H00FFD200,&H000000FF,&H60000000,&H900B0E13,1,0,0,0,100,100,1,0,3,10,0,2,40,40,28,1\n",
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ]
    def _at(s): return f"{int(s)//3600}:{(int(s)%3600)//60:02d}:{int(s)%60:02d}.00"
    for sec in range(int(dur_s)):
        ass_lines.append(f"Dialogue: 0,{_at(sec)},{_at(sec+1)},HUD,,0,0,0,,{t_map.get(sec, '')}\n")
    try:
        ass_path.write_text("".join(ass_lines), encoding="utf-8")
        cmd = ["ffmpeg", "-nostdin", "-y", "-ss", str(start_s), "-t", str(dur_s), "-i", str(vp),
               "-vf", f"scale='min(1920,iw)':-2,ass={ass_path}", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out_mp4)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode or not out_mp4.exists() or out_mp4.stat().st_size == 0:
            _unlink_quiet(out_mp4)
            raise HTTPException(500, "Erro ao renderizar vídeo com HUD no FFmpeg: " + (r.stderr[-200:] if r.stderr else "desconhecido"))
    except HTTPException:
        raise
    except Exception as e:
        _unlink_quiet(out_mp4)
        raise HTTPException(500, f"Falha ao processar vídeo com HUD: {e}")
    finally:
        _unlink_quiet(ass_path)
    return FileResponse(
        out_mp4,
        media_type="video/mp4",
        filename=f"dji_hud_{mid}_{int(start_s)}s.mp4",
        background=BackgroundTask(_unlink_quiet, out_mp4)
    )

class MergeVideosBody(BaseModel):
    media_ids: Optional[list[int]] = None
    mode: str = "download"       # "download" (temporário) | "save" (salva na pasta do voo sem apagar originais)
    index_to_db: bool = True     # quando mode == "save", indexar o novo arquivo no voo

@app.post("/api/flights/{fid:int}/merge-videos")
def merge_flight_videos(fid: int, b: MergeVideosBody):
    """Unifica opcionalmente múltiplos clipes MP4/MOV do mesmo voo sem perda (-c copy)."""
    if b.mode not in ("download", "save"):
        raise HTTPException(400, "Modo inválido. Use 'download' ou 'save'.")
    f = dict(get_flight(fid))
    with db() as c:
        if b.media_ids:
            qmarks = ",".join("?" for _ in b.media_ids)
            rows = [dict(r) for r in c.execute(
                f"SELECT * FROM media WHERE flight_id=? AND kind='video' AND id IN ({qmarks}) ORDER BY taken ASC, id ASC",
                (fid, *b.media_ids)
            )]
        else:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM media WHERE flight_id=? AND kind='video' ORDER BY taken ASC, id ASC",
                (fid,)
            )]
    if len(rows) < 2:
        raise HTTPException(400, "Selecione pelo menos 2 vídeos do voo para unificar.")
    paths = [Path(r["path"]) for r in rows]
    missing = [r["path"] for r, p in zip(rows, paths) if not p.is_file()]
    if missing:
        raise HTTPException(400, f"Arquivo(s) indisponível(is) no disco (HD desconectado?): {missing[0]}")

    # Nome base amigável para o arquivo unificado
    base_name = re.sub(r"[^\w\-]+", "_", (f.get("name") or f.get("place") or f"voo_{fid}")).strip("_") or f"voo_{fid}"
    ts_part = (f.get("start") or "")[:10].replace("-", "")
    dl_name = f"{base_name}_{ts_part}_unificado.mp4" if ts_part else f"{base_name}_unificado.mp4"

    tmp_dir = Path("data/thumbs")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    list_txt = tmp_dir / f"concat_{fid}_{int(time.time() * 1000)}.txt"
    if b.mode == "save":
        parent = paths[0].parent
        if not _is_allowed_fs_path(parent):
            raise HTTPException(403, "Pasta de destino não permitida.")
        out_mp4 = parent / f"{paths[0].stem}_unificado.mp4"
        n = 2
        while out_mp4.exists():
            out_mp4 = parent / f"{paths[0].stem}_unificado_{n}.mp4"
            n += 1
    else:
        out_mp4 = tmp_dir / f"merged_{fid}_{int(time.time() * 1000)}.mp4"

    try:
        # Escapa aspas simples para o formato concat demuxer do FFmpeg
        lines = ["file '" + str(p.resolve()).replace("'", "'\\''") + "'" for p in paths]
        list_txt.write_text("\n".join(lines), encoding="utf-8")
        cmd = [
            "ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_txt), "-c", "copy", "-movflags", "+faststart", str(out_mp4)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if res.returncode != 0 or not out_mp4.exists():
            _unlink_quiet(out_mp4)
            err = (res.stderr or "").strip().splitlines()[-1:] or ["Erro desconhecido"]
            raise HTTPException(500, f"Falha ao unificar com FFmpeg (-c copy): {err[0]}")
    finally:
        _unlink_quiet(list_txt)

    if b.mode == "download":
        return FileResponse(
            out_mp4,
            media_type="video/mp4",
            filename=dl_name,
            background=BackgroundTask(_unlink_quiet, out_mp4)
        )

    # mode == "save": arquivo salvo ao lado dos originais; indexa opcionalmente sem alterar os originais
    new_id = None
    if b.index_to_db:
        try:
            ex = exif_batch([str(out_mp4)]).get(str(out_mp4), {})
        except Exception:
            ex = {}
        first = rows[0]
        w, h = ex.get("ImageWidth") or first.get("w"), ex.get("ImageHeight") or first.get("h")
        dur = ex.get("Duration") if isinstance(ex.get("Duration"), (int, float)) else _probe_video_duration(out_mp4)
        meta = _build_media_meta(ex, "video", w, h, None)
        if dur: meta["dur"] = round(float(dur), 2)
        meta["merged_from"] = [r["id"] for r in rows]
        st = out_mp4.stat()
        with db() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO media(path,kind,taken,lat,lon,alt,w,h,size,sha1,flight_id,manual,tags,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(out_mp4), "video", first["taken"], first.get("lat"), first.get("lon"), first.get("alt"),
                 w, h, st.st_size, sha1(out_mp4), fid, 1, first.get("tags") or "", json.dumps(meta))
            )
            new_id = cur.lastrowid
    return {
        "ok": True,
        "path": str(out_mp4),
        "win_path": to_win(str(out_mp4)),
        "size": out_mp4.stat().st_size,
        "media_id": new_id,
    }

class MediaPatch(BaseModel):
    tags: Optional[str] = None
    fav: Optional[int] = None
    flight_id: Optional[int] = None     # 0 = nenhum voo; -1 = automático (o app decide)
    sync_adj: Optional[float] = None    # ajuste fino da sincronia do vídeo, em segundos

@app.patch("/api/media/{mid:int}")
def media_patch(mid: int, b: MediaPatch):
    auto = False
    with db() as c:
        if b.tags is not None: c.execute("UPDATE media SET tags=? WHERE id=?", (b.tags, mid))
        if b.fav is not None: c.execute("UPDATE media SET fav=? WHERE id=?", (b.fav, mid))
        if b.flight_id is not None:
            if b.flight_id == -1:
                c.execute("UPDATE media SET flight_id=NULL, manual=0 WHERE id=?", (mid,)); auto = True
            else:
                c.execute("UPDATE media SET flight_id=?, manual=1 WHERE id=?", (b.flight_id or None, mid))
        if b.sync_adj is not None:
            c.execute("UPDATE media SET sync_adj=? WHERE id=?", (max(-120.0, min(120.0, b.sync_adj)), mid))
    if auto: link_media()
    return get_media(mid)

def _allowed_media_path(p: Path) -> bool:
    try:
        rp = p.resolve()
    except OSError:
        return False
    roots = [Path("data/media").resolve(), MEDIA_DIR.resolve()]
    with db() as c:
        for (rpath,) in c.execute("SELECT path FROM media_roots").fetchall():
            try: roots.append(Path(rpath).resolve())
            except OSError: pass
    return any(rp == rt or rt in rp.parents for rt in roots)

def _delete_media_item(mid: int, delete_disk: bool):
    m = get_media(mid)
    disk_deleted, offline = False, False
    if delete_disk:
        p = Path(m["path"])
        if not _allowed_media_path(p):
            raise HTTPException(403, "Caminho fora das pastas de mídia configuradas.")
        if p.is_file():
            p.unlink()
            disk_deleted = True
        else:
            offline = True
    _unlink_quiet(THUMBS / f"{mid}.jpg")
    with db() as c:
        c.execute("DELETE FROM media WHERE id=?", (mid,))
    return {"ok": True, "id": mid, "disk_deleted": disk_deleted, "offline": offline}

@app.delete("/api/media/{mid:int}")
def media_delete(mid: int, disk: int = 0):
    """Remove a mídia da galeria (disk=0) ou também apaga o arquivo físico do HD (disk=1)."""
    return _delete_media_item(mid, bool(disk))

class MediaDeleteBatchIn(BaseModel):
    ids: list[int]
    disk: bool = False

@app.post("/api/media/delete-batch")
def media_delete_batch(b: MediaDeleteBatchIn):
    deleted, disk_deleted, errors = 0, 0, []
    for mid in b.ids:
        try:
            res = _delete_media_item(int(mid), b.disk)
            deleted += 1
            if res["disk_deleted"]:
                disk_deleted += 1
        except Exception as e:
            errors.append(f"#{mid}: {e}")
    return {"ok": True, "deleted": deleted, "disk_deleted": disk_deleted, "errors": errors}

@app.get("/api/media/{mid}/file")
def media_file(mid: int):
    p = Path(get_media(mid)["path"])
    if not p.is_file():
        raise HTTPException(404, "Arquivo de mídia indisponível (disco desconectado?).")
    return FileResponse(p)

# ---------- Detecção de Objetos Aérea (YOLO11s VisDrone ONNX + Auto-Tags + Relatórios) ----------
MODELS_DIR = Path("data/models")
YOLO_ONNX = MODELS_DIR / "yolo11s_visdrone.onnx"
YOLO_URL = "https://huggingface.co/numanbekir/visdrone-yolo11s/resolve/main/yolo11s_640.onnx"
_YOLO_LOCK = threading.Lock()
_YOLO_SESS = None

# Classes oficiais do dataset aéreo VisDrone2019-DET (0..9)
VISDRONE_PT = [
    ("pessoa", "Pessoa"),               # 0: pedestrian
    ("pessoa", "Pessoa"),               # 1: people
    ("bicicleta", "Bicicleta"),         # 2: bicycle
    ("carro", "Carro"),                 # 3: car
    ("van", "Van / Utilitário"),        # 4: van
    ("caminhao", "Caminhão"),           # 5: truck
    ("triciclo", "Triciclo"),           # 6: tricycle
    ("triciclo", "Triciclo Coberto"),   # 7: awning-tricycle
    ("onibus", "Ônibus"),               # 8: bus
    ("moto", "Moto"),                   # 9: motor
]

def _get_yolo():
    global _YOLO_SESS
    if _YOLO_SESS is not None:
        return _YOLO_SESS
    with _YOLO_LOCK:
        if _YOLO_SESS is not None:
            return _YOLO_SESS
        import onnxruntime as ort
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        if not YOLO_ONNX.is_file() or YOLO_ONNX.stat().st_size < 1_000_000:
            req = urllib.request.Request(YOLO_URL, headers={"User-Agent": "drone-manager/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            tmp = YOLO_ONNX.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(YOLO_ONNX)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4
        opts.inter_op_num_threads = 1
        _YOLO_SESS = ort.InferenceSession(str(YOLO_ONNX), sess_options=opts, providers=["CPUExecutionProvider"])
        return _YOLO_SESS

def _nms_np(boxes, scores, iou_thr: float = 0.45):
    import numpy as np
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)
        order = order[np.where(iou <= iou_thr)[0] + 1]
    return keep

def _yolo_infer_rgb640(sess, rgb_640, orig_w: int, orig_h: int, scale: float, px: float, py: float, conf_thr: float):
    import numpy as np
    inp = np.transpose(rgb_640.astype(np.float32) / 255.0, (2, 0, 1))[None, ...]
    out = sess.run(None, {sess.get_inputs()[0].name: inp})[0]
    preds = out[0].T  # (8400, 84)
    scores_all = preds[:, 4:]
    cls_ids = np.argmax(scores_all, axis=1)
    confs = scores_all[np.arange(len(preds)), cls_ids]
    mask = confs >= conf_thr
    if not np.any(mask):
        return []
    p = preds[mask]
    cids = cls_ids[mask]
    cs = confs[mask]
    cx, cy, bw, bh = p[:, 0], p[:, 1], p[:, 2], p[:, 3]
    x1 = (cx - bw / 2.0 - px) / scale / max(1, orig_w)
    y1 = (cy - bh / 2.0 - py) / scale / max(1, orig_h)
    x2 = (cx + bw / 2.0 - px) / scale / max(1, orig_w)
    y2 = (cy + bh / 2.0 - py) / scale / max(1, orig_h)
    boxes = np.stack([np.clip(x1, 0.0, 1.0), np.clip(y1, 0.0, 1.0), np.clip(x2, 0.0, 1.0), np.clip(y2, 0.0, 1.0)], axis=1)
    dets = []
    for cid in np.unique(cids):
        cm = cids == cid
        idx = np.where(cm)[0]
        keep = _nms_np(boxes[idx], cs[idx], 0.45)
        slug, label = VISDRONE_PT[int(cid)] if int(cid) < len(VISDRONE_PT) else (f"obj_{int(cid)}", f"Objeto {int(cid)}")
        for k in keep:
            bi = idx[k]
            dets.append({
                "cls": slug,
                "label": label,
                "conf": round(float(cs[bi]), 2),
                "box": [round(float(v), 4) for v in boxes[bi].tolist()]
            })
    dets.sort(key=lambda d: d["conf"], reverse=True)
    return dets

def _detect_photo_path(sess, p: Path, conf_thr: float):
    import numpy as np
    from PIL import Image, ImageOps
    with Image.open(p) as raw:
        im = ImageOps.exif_transpose(raw).convert("RGB")
        ow, oh = im.size
        scale = min(640.0 / ow, 640.0 / oh)
        nw, nh = max(1, int(round(ow * scale))), max(1, int(round(oh * scale)))
        resized = im.resize((nw, nh), Image.Resampling.BILINEAR)
        canvas = Image.new("RGB", (640, 640), (114, 114, 114))
        px, py = (640 - nw) // 2, (640 - nh) // 2
        canvas.paste(resized, (px, py))
        arr = np.asarray(canvas)
    return _yolo_infer_rgb640(sess, arr, ow, oh, scale, px, py, conf_thr)

def _detect_video_stream(sess, p: Path, ow: int, oh: int, conf_thr: float, step_s: float = 2.0):
    """Lê frames via pipe FFmpeg em streaming (sem arquivos temporários e sem carregar o vídeo na RAM)."""
    import numpy as np
    step_s = max(0.5, min(30.0, float(step_s)))
    ow, oh = max(1, ow or 1920), max(1, oh or 1080)
    scale = min(640.0 / ow, 640.0 / oh)
    nw, nh = max(1, int(round(ow * scale))), max(1, int(round(oh * scale)))
    px, py = (640 - nw) / 2.0, (640 - nh) / 2.0
    vf = f"fps=1/{step_s},scale=640:640:force_original_aspect_ratio=decrease,pad=640:640:(ow-iw)/2:(oh-ih)/2:color=0x727272"
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(p), "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    frame_bytes = 640 * 640 * 3
    max_frames = 240
    items = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        idx = 0
        while idx < max_frames:
            raw = proc.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            arr = np.frombuffer(raw, dtype=np.uint8).reshape((640, 640, 3))
            t_sec = round(idx * step_s, 1)
            for d in _yolo_infer_rgb640(sess, arr, ow, oh, scale, px, py, conf_thr):
                d["t"] = t_sec
                items.append(d)
            idx += 1
    finally:
        try:
            if proc.stdout: proc.stdout.close()
            if proc.stderr: proc.stderr.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired: proc.kill()
    return items

def _resolve_video_gps(m: dict):
    """Retorna função (t_sec -> (lat, lon)) combinando trilha do voo associado e .SRT do vídeo."""
    srt_pts = parse_srt_full(Path(m["path"]))
    srt_by_t = {int(x["t"]): (x["lat"], x["lon"]) for x in srt_pts if valid_gps(x.get("lat"), x.get("lon"))}
    tr, off = [], 0.0
    if m.get("flight_id"):
        try:
            f = fl_out(get_flight(m["flight_id"]), True)
            tr = f["track"] or []
            fst = datetime.fromisoformat(f["start_local"])
            vt = datetime.fromisoformat(m["taken"])
            off = (vt - fst).total_seconds()
            for e in f["events"]:
                if e["label"].startswith("Início de gravação") and e["t"] > 0 and abs(e["t"] - off) <= 30:
                    off = e["t"]; break
            off += float(m.get("sync_adj") or 0)
        except Exception:
            tr = []
    def lookup(t_sec):
        if tr:
            ft = t_sec + off
            pt = next((x for x in tr if x[0] >= ft), tr[-1])
            if pt and valid_gps(pt[1], pt[2]):
                return round(float(pt[1]), 6), round(float(pt[2]), 6)
        ti = int(round(t_sec))
        for k in (ti, ti - 1, ti + 1):
            if k in srt_by_t:
                return round(float(srt_by_t[k][0]), 6), round(float(srt_by_t[k][1]), 6)
        if valid_gps(m.get("lat"), m.get("lon")):
            return round(float(m["lat"]), 6), round(float(m["lon"]), 6)
        return None, None
    return lookup

def run_detect_media(mid: int, conf: float = 0.40, step_s: float = 2.0):
    m = get_media(mid)
    p = Path(m["path"])
    if not p.is_file():
        raise HTTPException(400, "Arquivo de mídia indisponível (disco desconectado?).")
    conf = max(0.15, min(0.90, float(conf)))
    sess = _get_yolo()
    if m["kind"] == "photo":
        items = _detect_photo_path(sess, p, conf)
        la = round(float(m["lat"]), 6) if valid_gps(m.get("lat"), m.get("lon")) else None
        lo = round(float(m["lon"]), 6) if valid_gps(m.get("lat"), m.get("lon")) else None
        for d in items:
            if la is not None:
                d["lat"], d["lon"] = la, lo
    else:
        items = _detect_video_stream(sess, p, m.get("w") or 1920, m.get("h") or 1080, conf, step_s)
        gps_at = _resolve_video_gps(m)
        for d in items:
            la, lo = gps_at(d.get("t", 0))
            if la is not None:
                d["lat"], d["lon"] = la, lo
    # Contagem resumida: em vídeos, conta o pico máximo simultâneo por classe (ou ocorrências únicas por frame)
    summary = {}
    if m["kind"] == "photo":
        for d in items:
            summary[d["cls"]] = summary.get(d["cls"], 0) + 1
    else:
        by_t = {}
        for d in items:
            by_t.setdefault(d.get("t", 0), {}).setdefault(d["cls"], 0)
            by_t[d.get("t", 0)][d["cls"]] += 1
        for cts in by_t.values():
            for k, v in cts.items():
                summary[k] = max(summary.get(k, 0), v)
    det_obj = {
        "model": "yolo11s-visdrone",
        "conf": conf,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
        "items": items[:400],
    }
    old_tags = [x.strip() for x in (m.get("tags") or "").split(",") if x.strip() and not x.strip().startswith("ia:")]
    new_tags = sorted(set(old_tags) | {f"ia:{k}" for k in summary.keys()})
    with db() as c:
        c.execute("UPDATE media SET detections=?, tags=? WHERE id=?", (json.dumps(det_obj, ensure_ascii=False), ",".join(new_tags), mid))
    return get_media(mid)

@app.post("/api/media/{mid}/detect")
def api_detect_media(mid: int, conf: float = 0.40, step_s: float = 2.0):
    return run_detect_media(mid, conf=conf, step_s=step_s)

class DetectBatchIn(BaseModel):
    ids: Optional[list[int]] = None
    flight_id: Optional[int] = None
    conf: float = 0.40
    step_s: float = 2.0

@app.post("/api/detect/batch")
def api_detect_batch(b: DetectBatchIn):
    with db() as c:
        if b.ids is not None:
            if not b.ids:
                rows = []
            else:
                qmarks = ",".join("?" for _ in b.ids)
                rows = c.execute(f"SELECT id FROM media WHERE id IN ({qmarks}) ORDER BY taken DESC", b.ids).fetchall()
        elif b.flight_id:
            rows = c.execute("SELECT id FROM media WHERE flight_id=? ORDER BY taken", (b.flight_id,)).fetchall()
        else:
            rows = c.execute("SELECT id FROM media WHERE detections IS NULL ORDER BY taken DESC LIMIT 50").fetchall()
    processed, total_objects, classes, errors = 0, 0, {}, []
    for (mid,) in rows:
        try:
            res = run_detect_media(mid, conf=b.conf, step_s=b.step_s)
            processed += 1
            sm = (res.get("detections") or {}).get("summary") or {}
            for k, v in sm.items():
                classes[k] = classes.get(k, 0) + v
                total_objects += v
        except Exception as e:
            errors.append(f"#{mid}: {e}")
    return {"processed": processed, "total_objects": total_objects, "classes": classes, "errors": errors}

@app.get("/api/detections/report")
def detections_report(flight_id: int = 0, cls: str = "", format: str = "html", lang: str = ""):
    L = get_lang(lang)
    with db() as c:
        sql = "SELECT m.*, f.place AS place, f.name AS flight_name FROM media m LEFT JOIN flights f ON f.id=m.flight_id WHERE m.detections IS NOT NULL"
        args = []
        if flight_id:
            sql += " AND m.flight_id=?"
            args.append(flight_id)
        rows = [_enrich_media(dict(r)) for r in c.execute(sql + " ORDER BY m.taken DESC", args)]
    cls_filter = cls.strip().lower()
    totals, entries = {}, []
    for m in rows:
        det = m.get("detections") or {}
        items = det.get("items") or []
        if cls_filter:
            items = [x for x in items if x.get("cls") == cls_filter]
        if not items:
            continue
        sm = {}
        if m["kind"] == "photo":
            for it in items:
                lb = it.get("label") or it.get("cls")
                sm[lb] = sm.get(lb, 0) + 1
        else:
            by_t = {}
            for it in items:
                lb = it.get("label") or it.get("cls")
                by_t.setdefault(it.get("t", 0), {}).setdefault(lb, 0)
                by_t[it.get("t", 0)][lb] += 1
            for cts in by_t.values():
                for lb, v in cts.items():
                    sm[lb] = max(sm.get(lb, 0), v)
        for lb, v in sm.items():
            totals[lb] = totals.get(lb, 0) + v
        # Pega coordenada representativa (do item com maior confiança ou da mídia)
        best = max(items, key=lambda x: x.get("conf", 0))
        la, lo = best.get("lat") or m.get("lat"), best.get("lon") or m.get("lon")
        entries.append({"m": m, "items": items, "summary": sm, "lat": la, "lon": lo, "best_t": best.get("t")})

    if format == "csv":
        o = io.StringIO()
        w = csv.writer(o)
        w.writerow(["media_id", "tipo", "data_hora", "local", "arquivo", "objetos", "lat", "lon", "google_maps"])
        for e in entries:
            m = e["m"]
            objs = "; ".join(f"{k}: {v}" for k, v in e["summary"].items())
            gm = f"https://www.google.com/maps?q={e['lat']},{e['lon']}" if valid_gps(e["lat"], e["lon"]) else ""
            w.writerow([m["id"], m["kind"], m["taken"], _csv_safe(m.get("place") or ""), _csv_safe(Path(m["path"]).name),
                        _csv_safe(objs), e["lat"] or "", e["lon"] or "", gm])
        return attach(o.getvalue(), "relatorio_deteccoes.csv", "text/csv")

    chips = "".join(f"<span style='display:inline-block;margin:3px 6px 3px 0;padding:4px 10px;background:#e2e8f0;border-radius:14px;font-size:13px'><b>{html.escape(k)}</b>: {v}</span>"
                    for k, v in sorted(totals.items(), key=lambda x: -x[1])) or f"<i>{tr(L, 'report.ai_none_obj')}</i>"
    cards = []
    for e in entries:
        m = e["m"]
        objs = ", ".join(f"{html.escape(k)} ({v})" for k, v in e["summary"].items())
        gps_link = (f'<a href="https://www.google.com/maps?q={e["lat"]},{e["lon"]}" target="_blank">🗺 {e["lat"]:.5f}, {e["lon"]:.5f} ↗</a>'
                    if valid_gps(e["lat"], e["lon"]) else tr(L, "report.ai_no_gps"))
        t_info = tr(L, "report.ai_moment", t=f"{int(e['best_t'])//60}:{int(e['best_t'])%60:02d}") if e["best_t"] is not None else ""
        kind_lbl = tr(L, "report.ai_video") if m["kind"] == "video" else tr(L, "report.ai_photo")
        cards.append(f"""<div style="display:flex;gap:14px;border:1px solid #cbd5e1;border-radius:8px;padding:10px;margin:10px 0;align-items:center">
          <img src="/api/media/{m['id']}/thumb" width="150" style="border-radius:6px;object-fit:cover">
          <div style="font-size:13.5px;line-height:1.5">
            <b>{kind_lbl} #{m['id']}</b> — {html.escape(str(m.get('taken') or '')[:16].replace('T',' '))} {html.escape(m.get('place') or '')}{t_info}<br>
            <b>{tr(L, 'report.ai_objects')}</b> {objs}<br>
            <b>{tr(L, 'report.ai_location')}</b> {gps_link}<br>
            <small style="color:#64748b">{html.escape(Path(m['path']).name)} · Tags: {html.escape(m.get('tags') or '')}</small>
          </div></div>""")
    fl_str = f" · {tr(L, 'report.ai_flight', id=flight_id)}" if flight_id else ""
    flt_str = f" · {tr(L, 'report.ai_filter')} {html.escape(cls_filter)}" if cls_filter else ""
    body = f"""<!doctype html><html lang="{L}"><meta charset="utf-8"><title>{tr(L, 'report.ai_title')}</title>
    <body style="font:15px system-ui;max-width:860px;margin:28px auto;color:#0f172a">
      <div style="float:right;display:flex;gap:8px">
        <a href="/api/detections/report?flight_id={flight_id}&cls={urllib.parse.quote(cls_filter)}&format=csv&lang={L}" style="padding:6px 12px;border:1px solid #94a3b8;border-radius:6px;text-decoration:none;color:#0f172a">{tr(L, 'report.ai_export_csv')}</a>
        <button onclick="print()" style="padding:6px 12px;cursor:pointer">🖨 {tr(L, 'report.print_pdf')}</button>
      </div>
      <h2>{tr(L, 'report.ai_h2')}</h2>
      <p style="color:#475569">{tr(L, 'report.ai_analyzed')} <b>{len(entries)}</b>{fl_str}{flt_str}</p>
      <div style="margin:12px 0">{chips}</div><hr>
      {''.join(cards) or f"<p>{tr(L, 'report.ai_none_filter')}</p>"}
    </body></html>"""
    return HTMLResponse(body)

# ---------- Busca Semântica Local em Fotos (CLIP ViT-B/32 ONNX offline) ----------
CLIP_DIR = MODELS_DIR / "clip"
CLIP_VIS_ONNX = CLIP_DIR / "vision_model_quantized.onnx"
CLIP_TXT_ONNX = CLIP_DIR / "text_model_quantized.onnx"
CLIP_TOK_JSON = CLIP_DIR / "tokenizer.json"
CLIP_BASE_URL = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/"
_CLIP_LOCK = threading.Lock()
_CLIP_STATE = None

_PT_EN_CLIP = {
    "por do sol": "sunset", "nascer do sol": "sunrise", "beira mar": "seashore coastline",
    "praia": "beach", "mar": "ocean sea", "oceano": "ocean", "onda": "ocean wave", "ondas": "ocean waves",
    "lagoa": "lagoon lake", "lago": "lake", "rio": "river", "cachoeira": "waterfall", "ilha": "island",
    "barco": "boat", "navio": "ship", "lancha": "speedboat", "iate": "yacht", "veleiro": "sailboat",
    "telhado": "roof rooftop", "casa": "house", "predio": "building skyscraper", "cidade": "city urban",
    "rua": "street road", "estrada": "road highway", "ponte": "bridge", "viaduto": "overpass bridge",
    "carro": "car", "carros": "cars traffic", "transito": "traffic cars", "estacionamento": "parking lot",
    "arvore": "tree", "arvores": "trees", "floresta": "forest woods", "mata": "forest jungle", "parque": "park",
    "montanha": "mountain", "morro": "hill mountain", "pedra": "rock cliff", "penhasco": "cliff",
    "campo": "field pasture", "fazenda": "farm", "plantacao": "crops farm field",
    "nuvem": "cloud", "nuvens": "clouds", "ceu": "sky", "neblina": "fog mist", "noite": "night",
    "piscina": "swimming pool", "estadio": "stadium", "igreja": "church", "farol": "lighthouse",
    "porto": "harbor port", "aeroporto": "airport", "pista": "runway", "obra": "construction site",
    "pessoa": "person", "pessoas": "people crowd", "multidao": "crowd people", "surfista": "surfer",
    "areia": "sand", "duna": "sand dune", "verde": "green", "azul": "blue", "aereo": "aerial",
}

def _strip_accents(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")

def _clip_pt_to_en(q: str) -> str:
    s = _strip_accents(q.lower().strip())
    for pt, en in sorted(_PT_EN_CLIP.items(), key=lambda x: -len(x[0])):
        s = re.sub(rf"\b{re.escape(pt)}\b", en, s)
    return re.sub(r"\s+", " ", s).strip()

def _download_clip_asset(rel: str, dest: Path, min_bytes: int = 1000):
    if dest.is_file() and dest.stat().st_size >= min_bytes:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(CLIP_BASE_URL + rel, headers={"User-Agent": "drone-manager/1.0"})
    with urllib.request.urlopen(req, timeout=240) as resp:
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(resp.read())
        tmp.replace(dest)

def _clip_bytes_to_unicode():
    bs = list(range(33, 127)) + list(range(161, 173)) + list(range(174, 256))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b); cs.append(256 + n); n += 1
    return dict(zip(bs, [chr(c) for c in cs]))

def _get_clip():
    global _CLIP_STATE
    if _CLIP_STATE is not None:
        return _CLIP_STATE
    with _CLIP_LOCK:
        if _CLIP_STATE is not None:
            return _CLIP_STATE
        import onnxruntime as ort
        _download_clip_asset("onnx/vision_model_quantized.onnx", CLIP_VIS_ONNX, 1_000_000)
        _download_clip_asset("onnx/text_model_quantized.onnx", CLIP_TXT_ONNX, 1_000_000)
        _download_clip_asset("tokenizer.json", CLIP_TOK_JSON, 10_000)
        tok_raw = json.loads(CLIP_TOK_JSON.read_text(encoding="utf-8"))
        vocab = tok_raw["model"]["vocab"]
        merges = [tuple(m.split()) for m in tok_raw["model"]["merges"]]
        bpe_ranks = dict(zip(merges, range(len(merges))))
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, min(4, os.cpu_count() or 2))
        vis = ort.InferenceSession(str(CLIP_VIS_ONNX), sess_options=opts, providers=["CPUExecutionProvider"])
        txt = ort.InferenceSession(str(CLIP_TXT_ONNX), sess_options=opts, providers=["CPUExecutionProvider"])
        _CLIP_STATE = (vis, txt, vocab, bpe_ranks, _clip_bytes_to_unicode())
        return _CLIP_STATE

def _clip_bpe(token: str, bpe_ranks: dict) -> list[str]:
    if not token: return []
    word = tuple(list(token[:-1]) + [token[-1] + "</w>"])
    if len(word) == 1:
        return list(word)
    while True:
        pairs = [(word[i], word[i + 1]) for i in range(len(word) - 1)]
        bigram = min(pairs, key=lambda p: bpe_ranks.get(p, 10**9))
        if bigram not in bpe_ranks:
            break
        first, second = bigram
        new_word, i = [], 0
        while i < len(word):
            try:
                j = word.index(first, i)
                new_word.extend(word[i:j]); i = j
            except ValueError:
                new_word.extend(word[i:]); break
            if i < len(word) - 1 and word[i] == first and word[i + 1] == second:
                new_word.append(first + second); i += 2
            else:
                new_word.append(word[i]); i += 1
        word = tuple(new_word)
        if len(word) == 1:
            break
    return list(word)

def _clip_tokenize(text: str, vocab: dict, bpe_ranks: dict, b2u: dict, max_len: int = 77):
    import numpy as np
    sot = vocab.get("<|startoftext|>", 49406)
    eot = vocab.get("<|endoftext|>", 49407)
    clean = re.sub(r"\s+", " ", text.lower()).strip()
    tokens = re.findall(r"[a-zA-Z\u00c0-\u024f]+|[0-9]|[^\s\w]+", clean)
    ids = [sot]
    for tok in tokens:
        enc = "".join(b2u[b] for b in tok.encode("utf-8"))
        for sub in _clip_bpe(enc, bpe_ranks):
            if sub in vocab:
                ids.append(vocab[sub])
            if len(ids) >= max_len - 1:
                break
        if len(ids) >= max_len - 1:
            break
    ids.append(eot)
    n_real = len(ids)
    ids += [0] * (max_len - n_real)
    mask = [1] * n_real + [0] * (max_len - n_real)
    return np.array([ids], dtype=np.int64), np.array([mask], dtype=np.int64)

def _clip_embed_text(q: str):
    import numpy as np
    _, txt_sess, vocab, bpe_ranks, b2u = _get_clip()
    en_q = _clip_pt_to_en(q)
    prompts = [f"a drone aerial photo of {en_q}"]
    if en_q != q.lower().strip():
        prompts.append(q.strip())
    in_names = {i.name for i in txt_sess.get_inputs()}
    vecs = []
    for pr in prompts:
        ids, mask = _clip_tokenize(pr, vocab, bpe_ranks, b2u)
        feed = {"input_ids": ids}
        if "attention_mask" in in_names:
            feed["attention_mask"] = mask
        out = txt_sess.run(None, feed)[0][0].astype(np.float32)
        n = float(np.linalg.norm(out))
        if n > 0: out /= n
        vecs.append(out)
    avg = np.mean(vecs, axis=0).astype(np.float32)
    n = float(np.linalg.norm(avg))
    if n > 0: avg /= n
    return avg, en_q

def _clip_embed_image(img_path: Path) -> bytes:
    import numpy as np
    from PIL import Image, ImageOps
    vis_sess, _, _, _, _ = _get_clip()
    with Image.open(img_path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB").resize((224, 224), Image.Resampling.BICUBIC)
        arr = np.asarray(im, dtype=np.float32) / 255.0
    mean = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
    std = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
    arr = ((arr - mean) / std).transpose(2, 0, 1)[None, ...]
    in_name = vis_sess.get_inputs()[0].name
    emb = vis_sess.run(None, {in_name: arr})[0][0].astype(np.float32)
    n = float(np.linalg.norm(emb))
    if n > 0: emb /= n
    return emb.tobytes()

@app.get("/api/media/semantic-status")
def semantic_status():
    with db() as c:
        tot = c.execute("SELECT COUNT(*) FROM media WHERE kind='photo'").fetchone()[0]
        idx = c.execute("SELECT COUNT(*) FROM media WHERE kind='photo' AND clip_emb IS NOT NULL").fetchone()[0]
    ready = CLIP_VIS_ONNX.is_file() and CLIP_TXT_ONNX.is_file() and CLIP_TOK_JSON.is_file()
    return {"total_photos": tot, "indexed_photos": idx, "model_ready": ready}

@app.post("/api/media/semantic-index")
def semantic_index(limit: int = 300, force: int = 0):
    limit = max(1, min(2000, int(limit)))
    with db() as c:
        cond = "" if force else "AND clip_emb IS NULL"
        rows = c.execute(f"SELECT id, path FROM media WHERE kind='photo' {cond} ORDER BY taken DESC LIMIT ?", (limit,)).fetchall()
    indexed, skipped, errors = 0, 0, []
    for mid, raw_p in rows:
        p = Path(raw_p)
        src = p if p.is_file() else (THUMBS / f"{mid}.jpg")
        if not src.is_file():
            skipped += 1
            continue
        try:
            blob = _clip_embed_image(src)
            with db() as c:
                c.execute("UPDATE media SET clip_emb=? WHERE id=?", (blob, mid))
            indexed += 1
        except Exception as e:
            errors.append(f"#{mid}: {e}")
    st = semantic_status()
    return {"ok": True, "indexed": indexed, "skipped": skipped, "errors": errors[:10], **st}

@app.get("/api/media/semantic-search")
def semantic_search(q: str, limit: int = 60, min_score: float = 0.17):
    import numpy as np
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "Informe o texto da busca visual.")
    limit = max(1, min(200, int(limit)))
    with db() as c:
        rows = c.execute(
            "SELECT m.*, f.place AS place FROM media m LEFT JOIN flights f ON f.id=m.flight_id "
            "WHERE m.kind='photo' AND m.clip_emb IS NOT NULL"
        ).fetchall()
    if not rows:
        return {"query": q, "en_query": _clip_pt_to_en(q), "items": [], "indexed_photos": 0}
    q_vec, en_q = _clip_embed_text(q)
    mat = np.vstack([np.frombuffer(r["clip_emb"], dtype=np.float32) for r in rows])
    scores = mat @ q_vec
    order = np.argsort(-scores)
    items = []
    for idx in order:
        sc = float(scores[idx])
        if sc < min_score and len(items) >= 8:
            break
        if sc < 0.10:
            break
        d = _enrich_media(dict(rows[idx]))
        d["offline"] = not Path(d["path"]).is_file()
        d["score"] = round(sc, 4)
        items.append(d)
        if len(items) >= limit:
            break
    return {"query": q, "en_query": en_q, "items": items, "indexed_photos": len(rows)}

@app.get("/api/media/{mid}/thumb")
def media_thumb(mid: int):
    m = get_media(mid); out = THUMBS / f"{mid}.jpg"
    if not out.exists():
        p = Path(m["path"])
        if not p.is_file():
            return Response(status_code=404)
        with _THUMB_SEM:
            if not out.exists():
                try:
                    if m["kind"] == "photo":
                        from PIL import Image, ImageOps
                        with Image.open(p) as im:
                            im.draft("RGB", (800, 800))
                            im = ImageOps.exif_transpose(im)
                            im.thumbnail((400, 400))
                            im.convert("RGB").save(out, "JPEG", quality=80, optimize=True)
                    else:
                        subprocess.run(["ffmpeg", "-nostdin", "-y", "-ss", "1", "-i", str(p),
                                        "-frames:v", "1", "-vf", "scale=400:-1", str(out)],
                                       capture_output=True, timeout=15)
                except Exception:
                    _unlink_quiet(out)
    return FileResponse(out) if out.exists() else Response(status_code=404)

@app.get("/api/export/photos")
def export_photos(ids: str):
    """ZIP em streaming a partir de arquivo temporário em disco (sem estourar RAM no WSL2) com auto-limpeza."""
    fd, tmp_name = tempfile.mkstemp(prefix="export_", suffix=".zip", dir=str(THUMBS))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED) as z:
            for i in ids.split(","):
                if not i.strip(): continue
                m = get_media(int(i))
                mp = Path(m["path"])
                if mp.is_file():
                    z.write(mp, mp.name)
    except Exception as e:
        _unlink_quiet(tmp_path)
        raise HTTPException(400, f"Erro ao gerar pacote ZIP: {e}")
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename="fotos_selecionadas.zip",
        background=BackgroundTask(_unlink_quiet, tmp_path)
    )

# ---------- Integração Local com NodeODM / WebODM & Ortomosaicos GeoTIFF ----------
def _utm_to_wgs84(easting: float, northing: float, zone: int, northern: bool = True):
    """Converte coordenadas UTM (WGS84) para (lat, lon) em graus decimais."""
    a = 6378137.0
    e2 = 0.00669437999014
    e1 = (1.0 - sqrt(1.0 - e2)) / (1.0 + sqrt(1.0 - e2))
    x = easting - 500000.0
    y = northing if northern else northing - 10000000.0
    m = y / 0.9996
    mu = m / (a * (1.0 - e2 / 4.0 - 3.0 * e2 * e2 / 64.0 - 5.0 * e2 * e2 * e2 / 256.0))
    phi1 = (mu + (3.0 * e1 / 2.0 - 27.0 * (e1 ** 3) / 32.0) * sin(2.0 * mu)
            + (21.0 * e1 * e1 / 16.0 - 55.0 * (e1 ** 4) / 32.0) * sin(4.0 * mu)
            + (151.0 * (e1 ** 3) / 96.0) * sin(6.0 * mu)
            + (1097.0 * (e1 ** 4) / 512.0) * sin(8.0 * mu))
    ep2 = e2 / (1.0 - e2)
    c1 = ep2 * (cos(phi1) ** 2)
    t1 = tan(phi1) ** 2
    n1 = a / sqrt(1.0 - e2 * (sin(phi1) ** 2))
    r1 = a * (1.0 - e2) / ((1.0 - e2 * (sin(phi1) ** 2)) ** 1.5)
    d = x / (n1 * 0.9996)
    lat_rad = phi1 - (n1 * tan(phi1) / r1) * (
        d * d / 2.0
        - (5.0 + 3.0 * t1 + 10.0 * c1 - 4.0 * c1 * c1 - 9.0 * ep2) * (d ** 4) / 24.0
        + (61.0 + 90.0 * t1 + 298.0 * c1 + 45.0 * t1 * t1 - 252.0 * ep2 - 3.0 * c1 * c1) * (d ** 6) / 720.0
    )
    lon_rad = (
        d - (1.0 + 2.0 * t1 + c1) * (d ** 3) / 6.0
        + (5.0 - 2.0 * c1 + 28.0 * t1 - 3.0 * c1 * c1 + 8.0 * ep2 + 24.0 * t1 * t1) * (d ** 5) / 120.0
    ) / cos(phi1)
    lon0 = radians((zone - 1) * 6 - 180 + 3)
    return round(degrees(lat_rad), 7), round(degrees(lon_rad + lon0), 7)

def _webmerc_to_wgs84(x: float, y: float):
    """Converte coordenadas Web Mercator (EPSG:3857) para (lat, lon) WGS84."""
    R = 6378137.0
    return round(degrees(2.0 * atan(exp(y / R)) - pi / 2.0), 7), round(degrees(x / R), 7)

def _extract_geotiff_epsg(geo_keys) -> Optional[int]:
    if not geo_keys or len(geo_keys) < 4:
        return None
    nkeys = int(geo_keys[3])
    for idx in range(nkeys):
        off = 4 + idx * 4
        if off + 3 >= len(geo_keys): break
        key_id, tiff_tag_loc, _count, val = int(geo_keys[off]), int(geo_keys[off + 1]), int(geo_keys[off + 2]), int(geo_keys[off + 3])
        if key_id in (3072, 2048) and tiff_tag_loc == 0 and val > 0:
            return val
    return None

def _mem_available_bytes() -> int:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 2 * 1024 * 1024 * 1024

def _tiff_lzw_decode(data: bytes) -> bytes:
    """Decodificador LZW padrão TIFF (MSB-first, 9..12 bits) para faixas/tiles individuais."""
    out = bytearray()
    table = {i: bytes([i]) for i in range(256)}
    next_code, code_size = 258, 9
    bit_buf = bit_cnt = idx = 0
    n = len(data)
    prev = b""
    def _next():
        nonlocal bit_buf, bit_cnt, idx
        while bit_cnt < code_size:
            if idx >= n:
                return None
            bit_buf = (bit_buf << 8) | data[idx]
            idx += 1
            bit_cnt += 8
        bit_cnt -= code_size
        val = (bit_buf >> bit_cnt) & ((1 << code_size) - 1)
        bit_buf &= (1 << bit_cnt) - 1
        return val
    while True:
        code = _next()
        if code is None or code == 257:
            break
        if code == 256:
            table = {i: bytes([i]) for i in range(256)}
            next_code, code_size = 258, 9
            prev = b""
            continue
        if code in table:
            entry = table[code]
        elif code == next_code and prev:
            entry = prev + prev[:1]
        else:
            break
        out.extend(entry)
        if prev:
            if next_code < 4096:
                table[next_code] = prev + entry[:1]
                next_code += 1
                if next_code == (1 << code_size) and code_size < 12:
                    code_size += 1
        prev = entry
    return bytes(out)

def _decomp_tiff_chunk(raw: bytes, comp: int) -> bytes:
    if comp == 1:
        return raw
    if comp in (8, 32946):
        return __import__("zlib").decompress(raw)
    if comp == 5:
        return _tiff_lzw_decode(raw)
    raise ValueError(f"Compressão TIFF {comp} não suportada em streaming")

def _stream_large_tiff(tif_path: Path, out_w: int, out_h: int, on_progress=None):
    """Lê TIFF grande em faixas (strips) ou blocos (tiles) sem carregar a imagem inteira na RAM."""
    import numpy as np
    from PIL import Image
    with Image.open(tif_path) as im:
        w, h = im.size
        tags = getattr(im, "tag_v2", {})
        comp = int(tags.get(259, 1))  # 1=None, 5=LZW, 8/32946=Deflate
        spp = int(tags.get(277, 3))
        bps = tags.get(258, (8,))
        pred = int(tags.get(317, 1))
        if comp not in (1, 5, 8, 32946) or spp not in (1, 3, 4):
            return None
        if any(int(b) != 8 for b in (bps if isinstance(bps, (tuple, list)) else (bps,))):
            return None

        t_off, t_cnt = tags.get(324), tags.get(325)
        tw, th = tags.get(322), tags.get(323)
        s_off, s_cnt = tags.get(273), tags.get(279)
        rps = int(tags.get(278, h))

        out = np.zeros((out_h, out_w, 4), dtype=np.uint8)
        last_pct = 45

        def _write_rows(arr, y_start, x_start, row_h, col_w, ox0, ox1, oy0, oy1):
            if pred == 2:
                arr = np.cumsum(arr, axis=1, dtype=np.uint8)
            for oy in range(oy0, oy1):
                sy = min(row_h - 1, max(0, int(oy * h / out_h) - y_start))
                sx_idx = np.clip((np.arange(ox0, ox1) * w / out_w).astype(np.int64) - x_start, 0, col_w - 1)
                row = arr[sy, sx_idx]
                if spp == 4:
                    out[oy, ox0:ox1] = row
                elif spp == 3:
                    out[oy, ox0:ox1, :3] = row
                    nodata = (row[:, 0] == 0) & (row[:, 1] == 0) & (row[:, 2] == 0)
                    out[oy, ox0:ox1, 3] = np.where(nodata, 0, 255)
                else:
                    out[oy, ox0:ox1, 0] = out[oy, ox0:ox1, 1] = out[oy, ox0:ox1, 2] = row[:, 0]
                    out[oy, ox0:ox1, 3] = np.where(row[:, 0] == 0, 0, 255)

        if t_off and t_cnt and tw and th:
            tw, th = int(tw), int(th)
            nx = (w + tw - 1) // tw
            ny = (h + th - 1) // th
            total = max(1, len(t_off))
            ox_map = np.clip((np.arange(out_w) * w / out_w).astype(np.int64), 0, w - 1)
            oy_map = np.clip((np.arange(out_h) * h / out_h).astype(np.int64), 0, h - 1)
            with open(tif_path, "rb") as f:
                for idx, (off, cnt) in enumerate(zip(t_off, t_cnt)):
                    tx = (idx % nx) * tw
                    ty = (idx // nx) * th
                    ox_idx = np.where((ox_map >= tx) & (ox_map < tx + tw))[0]
                    oy_idx = np.where((oy_map >= ty) & (oy_map < ty + th))[0]
                    if ox_idx.size and oy_idx.size and cnt > 0:
                        f.seek(int(off))
                        raw = _decomp_tiff_chunk(f.read(int(cnt)), comp)
                        arr = np.frombuffer(raw, dtype=np.uint8)
                        if arr.size < tw * th * spp:
                            return None
                        arr = arr[:tw * th * spp].reshape((th, tw, spp)).copy()
                        _write_rows(arr, ty, tx, th, tw, int(ox_idx[0]), int(ox_idx[-1]) + 1, int(oy_idx[0]), int(oy_idx[-1]) + 1)
                    pct = 45 + int(35 * (idx + 1) / total)
                    if on_progress and pct >= last_pct + 3:
                        last_pct = pct
                        on_progress(pct)
            return Image.fromarray(out, "RGBA")

        if s_off and s_cnt:
            total = max(1, len(s_off))
            oy_map = np.clip((np.arange(out_h) * h / out_h).astype(np.int64), 0, h - 1)
            with open(tif_path, "rb") as f:
                for s_idx, (off, cnt) in enumerate(zip(s_off, s_cnt)):
                    y_start = s_idx * rps
                    y_end = min(h, y_start + rps)
                    rows_in_strip = y_end - y_start
                    if rows_in_strip <= 0:
                        continue
                    oy_idx = np.where((oy_map >= y_start) & (oy_map < y_end))[0]
                    if oy_idx.size and cnt > 0:
                        f.seek(int(off))
                        raw = _decomp_tiff_chunk(f.read(int(cnt)), comp)
                        arr = np.frombuffer(raw, dtype=np.uint8)
                        expected = rows_in_strip * w * spp
                        if arr.size < expected:
                            return None
                        arr = arr[:expected].reshape((rows_in_strip, w, spp)).copy()
                        _write_rows(arr, y_start, 0, rows_in_strip, w, 0, out_w, int(oy_idx[0]), int(oy_idx[-1]) + 1)
                    pct = 45 + int(35 * (s_idx + 1) / total)
                    if on_progress and pct >= last_pct + 3:
                        last_pct = pct
                        on_progress(pct)
            return Image.fromarray(out, "RGBA")
        return None

def _process_geotiff(tif_path: Path, png_path: Path, on_progress=None) -> dict:
    """Lê bounds georreferenciados (WGS84 / UTM) do GeoTIFF e gera PNG RGBA leve para overlay no Leaflet."""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    def _prog(pct: int):
        if on_progress:
            try: on_progress(pct)
            except Exception: pass
    _prog(10)
    with Image.open(tif_path) as im:
        w, h = im.size
        tags = getattr(im, "tag_v2", {})
        scale = tags.get(33550)   # ModelPixelScaleTag (sx, sy, sz)
        tie = tags.get(33922)     # ModelTiepointTag (i, j, k, x, y, z)
        gkeys = tags.get(34735)   # GeoKeyDirectoryTag
        if not scale or not tie or len(scale) < 2 or len(tie) < 6:
            raise ValueError("O arquivo TIFF não possui tags GeoTIFF (ModelPixelScale / ModelTiepoint).")
        sx, sy = float(scale[0]), float(scale[1])
        ti, tj, tx, ty = float(tie[0]), float(tie[1]), float(tie[3]), float(tie[4])
        x0 = tx - ti * sx
        y0 = ty + tj * sy
        x1 = x0 + w * sx
        y1 = y0 - h * sy
        epsg = _extract_geotiff_epsg(gkeys) or (4326 if -180 <= x0 <= 180 and -90 <= y0 <= 90 else None)
        if epsg in (4326, 4674, 4269, 4258):  # WGS84, SIRGAS 2000, NAD83, ETRS89 (graus decimais)
            min_lon, max_lon = min(x0, x1), max(x0, x1)
            min_lat, max_lat = min(y0, y1), max(y0, y1)
            gsd_cm = round(sx * 111320.0 * 100.0, 2)
        elif epsg and (32601 <= epsg <= 32660 or 32701 <= epsg <= 32760 or 31978 <= epsg <= 31985 or 31965 <= epsg <= 31976):
            if 32601 <= epsg <= 32660:
                north, zone = True, epsg - 32600
            elif 32701 <= epsg <= 32760:
                north, zone = False, epsg - 32700
            elif 31978 <= epsg <= 31985:  # SIRGAS 2000 / UTM 18S..25S (Brasil)
                north, zone = False, epsg - 31960
            else:  # SIRGAS 2000 / UTM 11N..22N
                north, zone = True, epsg - 31954
            pts = [_utm_to_wgs84(px, py, zone, north) for px in (x0, x1) for py in (y0, y1)]
            min_lat = min(p[0] for p in pts); max_lat = max(p[0] for p in pts)
            min_lon = min(p[1] for p in pts); max_lon = max(p[1] for p in pts)
            gsd_cm = round(sx * 100.0, 2)
        elif epsg in (3857, 900913):  # Web Mercator (Google / OSM / OpenAerialMap)
            p0 = _webmerc_to_wgs84(x0, y0)
            p1 = _webmerc_to_wgs84(x1, y1)
            min_lat, max_lat = min(p0[0], p1[0]), max(p0[0], p1[0])
            min_lon, max_lon = min(p0[1], p1[1]), max(p0[1], p1[1])
            gsd_cm = round(sx * cos(radians((min_lat + max_lat) / 2.0)) * 100.0, 2)
        else:
            raise ValueError(f"CRS GeoTIFF não suportado (EPSG={epsg}). Use WGS84/SIRGAS 2000 (4326/4674), Web Mercator (3857) ou UTM.")

        # Converte para PNG RGBA otimizado para o navegador (máx 4096px)
        # Se for um COG / GeoTIFF com pirâmides (múltiplos frames), usa o overview mais adequado para evitar decodificar GBs na RAM
        _prog(25)
        max_dim = 4096
        n_frames = getattr(im, "n_frames", 1)
        if n_frames > 1:
            best_idx, best_sz = 0, max(w, h)
            for idx in range(1, n_frames):
                try:
                    im.seek(idx)
                    fw, fh = im.size
                    msz = max(fw, fh)
                    if msz >= 800 and (best_sz > max_dim or msz > best_sz) and msz <= max_dim:
                        best_idx, best_sz = idx, msz
                    elif best_sz > max_dim and msz < best_sz:
                        best_idx, best_sz = idx, msz
                except Exception:
                    break
            im.seek(best_idx)
        cur_w, cur_h = im.size
        _prog(45)

        if max(cur_w, cur_h) > max_dim:
            sc = max_dim / max(cur_w, cur_h)
            out_w, out_h = max(1, int(cur_w * sc)), max(1, int(cur_h * sc))
            # Tenta leitura em faixas/blocos (baixo uso de RAM) antes de decodificar a imagem inteira no Pillow
            rgba = _stream_large_tiff(tif_path, out_w, out_h, on_progress=_prog) if (n_frames == 1 or best_idx == 0) else None
            if rgba is None:
                est_bytes = cur_w * cur_h * 8
                avail = _mem_available_bytes()
                if est_bytes > avail * 0.65:
                    comp_tag = tags.get(259, 1)
                    tiled_tag = "tiled" if tags.get(324) else "stripped"
                    raise ValueError(
                        f"GeoTIFF muito grande ({cur_w}×{cur_h} px, comp={comp_tag}, {tiled_tag}) para a RAM disponível ({avail / (1024**3):.1f} GB). "
                        "Exporte com pirâmides (COG) ou compressão Deflate/LZW."
                    )
                im = im.resize((out_w, out_h), Image.Resampling.BOX)
                _prog(70)
                if im.mode != "RGBA":
                    rgba = im.convert("RGBA")
                    arr = __import__("numpy").array(rgba)
                    nodata = (arr[:, :, 0] == 0) & (arr[:, :, 1] == 0) & (arr[:, :, 2] == 0)
                    arr[nodata, 3] = 0
                    rgba = Image.fromarray(arr, "RGBA")
                else:
                    rgba = im
        else:
            im.load()
            _prog(70)
            if im.mode != "RGBA":
                rgba = im.convert("RGBA")
                arr = __import__("numpy").array(rgba)
                nodata = (arr[:, :, 0] == 0) & (arr[:, :, 1] == 0) & (arr[:, :, 2] == 0)
                arr[nodata, 3] = 0
                rgba = Image.fromarray(arr, "RGBA")
            else:
                rgba = im
        _prog(88)
        rgba.save(png_path, "PNG", compress_level=3)
    return {"bounds": [min_lat, min_lon, max_lat, max_lon], "epsg": epsg, "gsd_cm": gsd_cm}

def _get_odm_url(override: str = "") -> str:
    if override and override.strip():
        return override.strip().rstrip("/")
    with db() as c:
        row = c.execute("SELECT v FROM kv WHERE k='odm_url'").fetchone()
    return (row[0] if row and row[0] else "http://127.0.0.1:3000").rstrip("/")

def _validate_odm_url(url: str) -> str:
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise HTTPException(400, "URL do NodeODM inválida. Use http://127.0.0.1:3000")
    return url.rstrip("/")

def _odm_json(url: str, method: str = "GET", timeout: int = 8):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def _odm_multipart(url: str, fields: dict = None, file_path: Path = None, field_name: str = "images", timeout: int = 120):
    import http.client
    u = urllib.parse.urlparse(url)
    boundary = f"----DroneManagerODM{int(time.time() * 1000)}"
    body_parts = []
    for k, v in (fields or {}).items():
        body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8"))
    file_hdr = b""
    file_size = 0
    if file_path is not None:
        fname = file_path.name.replace('"', "_")
        file_hdr = f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field_name}\"; filename=\"{fname}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode("utf-8")
        file_size = file_path.stat().st_size
    tail = (b"\r\n" if file_path is not None else b"") + f"--{boundary}--\r\n".encode("utf-8")
    total_len = sum(len(x) for x in body_parts) + len(file_hdr) + file_size + len(tail)

    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=timeout)
    try:
        path = u.path + (f"?{u.query}" if u.query else "")
        conn.putrequest("POST", path)
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(total_len))
        conn.endheaders()
        for p in body_parts:
            conn.send(p)
        if file_path is not None:
            conn.send(file_hdr)
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(256 * 1024)
                    if not chunk: break
                    conn.send(chunk)
        conn.send(tail)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="ignore")
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status}: {raw[:300]}")
        return json.loads(raw) if raw.strip() else {}
    finally:
        conn.close()

_ODM_PRESETS = {
    "fast": [{"name": "orthophoto-resolution", "value": 5.0}, {"name": "fast-orthophoto", "value": True}],
    "balanced": [{"name": "orthophoto-resolution", "value": 2.0}],
    "high": [{"name": "orthophoto-resolution", "value": 1.0}, {"name": "pc-quality", "value": "high"}],
}

def _odm_worker(oid: int, odm_url: str, paths: list[Path], preset: str):
    def upd(**kw):
        cols = ", ".join(f"{k}=?" for k in kw)
        with db() as c:
            c.execute(f"UPDATE orthomosaics SET {cols} WHERE id=?", (*kw.values(), oid))
    try:
        opts = _ODM_PRESETS.get(preset, _ODM_PRESETS["balanced"])
        init_res = _odm_multipart(f"{odm_url}/task/new/init", fields={
            "name": f"drone_manager_{oid}",
            "options": json.dumps(opts),
        }, timeout=20)
        uuid = init_res.get("uuid")
        if not uuid:
            raise RuntimeError(f"NodeODM não retornou UUID: {init_res}")
        upd(task_id=uuid, status="uploading", progress=2)
        total = max(1, len(paths))
        for idx, p in enumerate(paths, 1):
            with db() as c:
                st_now = c.execute("SELECT status FROM orthomosaics WHERE id=?", (oid,)).fetchone()
            if st_now and st_now[0] == "canceled":
                return
            _odm_multipart(f"{odm_url}/task/new/upload/{uuid}", file_path=p, field_name="images", timeout=120)
            upd(progress=round(2 + (idx / total) * 18, 1))
        _odm_multipart(f"{odm_url}/task/new/commit/{uuid}", timeout=30)
        upd(status="running", progress=20)

        while True:
            with db() as c:
                st_now = c.execute("SELECT status FROM orthomosaics WHERE id=?", (oid,)).fetchone()
            if st_now and st_now[0] == "canceled":
                return
            info = _odm_json(f"{odm_url}/task/{uuid}/info", timeout=10)
            st_code = (info.get("status") or {}).get("code")
            prog = float(info.get("progress") or 0)
            if st_code == 40:  # COMPLETED
                break
            if st_code == 30:  # FAILED
                raise RuntimeError((info.get("status") or {}).get("errorMessage") or "Falha no processamento do NodeODM.")
            if st_code == 50:  # CANCELED
                upd(status="canceled")
                return
            upd(status="queued" if st_code == 10 else "running", progress=round(20 + prog * 0.75, 1))
            time.sleep(2.5)

        upd(status="importing", progress=96)
        tif_path = ORTHOS / f"ortho_{oid}.tif"
        png_path = ORTHOS / f"ortho_{oid}.png"
        with urllib.request.urlopen(f"{odm_url}/task/{uuid}/download/orthophoto.tif", timeout=180) as r:
            with open(tif_path, "wb") as f:
                while True:
                    chunk = r.read(512 * 1024)
                    if not chunk: break
                    f.write(chunk)
        geo = _process_geotiff(tif_path, png_path)
        upd(
            status="completed", progress=100,
            tif_path=str(tif_path), png_path=str(png_path),
            bounds=json.dumps(geo["bounds"]), epsg=geo["epsg"], gsd_cm=geo["gsd_cm"],
            finished_at=datetime.now().isoformat(timespec="seconds")
        )
    except Exception as e:
        upd(status="failed", error=str(e)[:500])

class OdmConfigBody(BaseModel):
    url: str

class OrthoCreateBody(BaseModel):
    name: str
    media_ids: list[int]
    flight_id: Optional[int] = None
    preset: str = "balanced"
    odm_url: str = ""

_ODM_CONTAINER = "drone-manager-nodeodm"
_ODM_IMAGE = "opendronemap/nodeodm"
_ODM_BOOT = {"starting": False, "msg": "", "error": ""}

def _odm_is_local(url: str) -> bool:
    h = (urllib.parse.urlparse(url).hostname or "").lower()
    return h in ("127.0.0.1", "localhost", "0.0.0.0", "::1")

def _odm_port(url: str) -> int:
    return urllib.parse.urlparse(url).port or 3000

def _docker_state() -> dict:
    dbin = shutil.which("docker")
    if not dbin:
        return {"docker_bin": False, "docker_ok": False, "container": "missing", "image_ready": False, "docker_err": "Docker CLI não encontrado."}
    try:
        res = subprocess.run([dbin, "ps", "-a", "--filter", f"name=^/{_ODM_CONTAINER}$", "--format", "{{.State}}"], capture_output=True, text=True, timeout=3)
        if res.returncode != 0:
            return {"docker_bin": True, "docker_ok": False, "container": "unknown", "image_ready": False, "docker_err": (res.stderr or res.stdout).strip()[:200]}
        lines = [ln.strip().lower() for ln in (res.stdout or "").splitlines() if ln.strip()]
        cstate = lines[0] if lines else "missing"
        img_res = subprocess.run([dbin, "images", "-q", _ODM_IMAGE], capture_output=True, text=True, timeout=3)
        img_ready = bool(img_res.returncode == 0 and (img_res.stdout or "").strip())
        return {"docker_bin": True, "docker_ok": True, "container": cstate, "image_ready": img_ready, "docker_err": ""}
    except Exception as e:
        return {"docker_bin": True, "docker_ok": False, "container": "unknown", "image_ready": False, "docker_err": str(e)[:200]}

def _odm_start_worker(cstate: str, port: int):
    dbin = shutil.which("docker")
    try:
        if cstate in ("exited", "created", "paused"):
            _ODM_BOOT.update(starting=True, msg="Iniciando container NodeODM…", error="")
            r = subprocess.run([dbin, "start", _ODM_CONTAINER], capture_output=True, text=True, timeout=30)
        else:
            _ODM_BOOT.update(starting=True, msg="Criando container NodeODM (na 1ª vez baixa a imagem)…", error="")
            r = subprocess.run([dbin, "run", "-d", "--name", _ODM_CONTAINER, "-p", f"{port}:3000", _ODM_IMAGE], capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            _ODM_BOOT.update(starting=False, msg="", error=(r.stderr or r.stdout or "Falha ao iniciar Docker").strip()[:300])
            return
        _ODM_BOOT.update(starting=True, msg="Aguardando NodeODM responder na porta " + str(port) + "…", error="")
        for _ in range(30):
            try:
                _odm_json(f"http://127.0.0.1:{port}/info", timeout=1)
                _ODM_BOOT.update(starting=False, msg="", error="")
                return
            except Exception:
                time.sleep(1)
        _ODM_BOOT.update(starting=False, msg="", error="")
    except Exception as e:
        _ODM_BOOT.update(starting=False, msg="", error=str(e)[:300])

@app.get("/api/odm/status")
def odm_status(url: str = ""):
    target = _validate_odm_url(_get_odm_url(url))
    is_local = _odm_is_local(target)
    dk = _docker_state() if is_local else {"docker_bin": False, "docker_ok": False, "container": "remote", "image_ready": False, "docker_err": ""}
    extra = {**dk, "is_local": is_local, "starting": _ODM_BOOT["starting"], "boot_msg": _ODM_BOOT["msg"], "boot_error": _ODM_BOOT["error"]}
    try:
        info = _odm_json(f"{target}/info", timeout=3)
        return {"online": True, "url": target, "version": info.get("version"), "engine_version": info.get("engineVersion"),
                "cpus": info.get("availableCpus"), "mem": info.get("totalMemory"), "queue": info.get("taskQueueCount"), **extra}
    except Exception as e:
        return {"online": False, "url": target, "error": str(e), **extra}

@app.post("/api/odm/start")
def odm_start(url: str = ""):
    target = _validate_odm_url(_get_odm_url(url))
    if not _odm_is_local(target):
        raise HTTPException(400, "Início automático disponível apenas para NodeODM local (localhost / 127.0.0.1).")
    dk = _docker_state()
    if not dk["docker_ok"]:
        raise HTTPException(503, "Docker não está em execução. Abra o Docker Desktop no Windows (ou inicie o serviço Docker no WSL2) e tente novamente.")
    try:
        _odm_json(f"{target}/info", timeout=2)
        return {"ok": True, "status": "running", "msg": "NodeODM já está online."}
    except Exception:
        pass
    if _ODM_BOOT["starting"]:
        return {"ok": True, "status": "starting", "msg": _ODM_BOOT["msg"] or "NodeODM está iniciando…"}
    _ODM_BOOT.update(starting=True, msg="Iniciando NodeODM…", error="")
    threading.Thread(target=_odm_start_worker, args=(dk["container"], _odm_port(target)), daemon=True).start()
    return {"ok": True, "status": "starting", "msg": _ODM_BOOT["msg"]}

@app.post("/api/odm/stop")
def odm_stop():
    dk = _docker_state()
    if not dk["docker_ok"]:
        raise HTTPException(503, "Docker não está em execução.")
    if dk["container"] != "running":
        return {"ok": False, "msg": "Container gerenciado do NodeODM não está em execução."}
    dbin = shutil.which("docker")
    r = subprocess.run([dbin, "stop", _ODM_CONTAINER], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise HTTPException(500, (r.stderr or r.stdout or "Falha ao parar container").strip()[:200])
    return {"ok": True, "msg": "Container NodeODM parado."}

@app.post("/api/odm/config")
def odm_save_config(b: OdmConfigBody):
    u = _validate_odm_url(b.url)
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv(k, v) VALUES('odm_url', ?)", (u,))
    return {"ok": True, "url": u}

def _ortho_out(r) -> dict:
    d = dict(r)
    for k in ("bounds", "options"):
        if isinstance(d.get(k), str):
            try: d[k] = json.loads(d[k])
            except Exception: pass
    d["has_png"] = bool(d.get("png_path") and Path(d["png_path"]).is_file())
    d["has_tif"] = bool(d.get("tif_path") and Path(d["tif_path"]).is_file())
    return d

@app.get("/api/orthos")
def list_orthos():
    with db() as c:
        rows = c.execute("SELECT * FROM orthomosaics ORDER BY id DESC").fetchall()
    return [_ortho_out(r) for r in rows]

@app.post("/api/orthos/create")
def create_ortho(b: OrthoCreateBody):
    if len(b.media_ids) < 3:
        raise HTTPException(400, "Selecione pelo menos 3 fotos com GPS para gerar um ortomosaico.")
    odm_url = _validate_odm_url(_get_odm_url(b.odm_url))
    try:
        _odm_json(f"{odm_url}/info", timeout=3)
    except Exception as e:
        raise HTTPException(503, f"NodeODM indisponível em {odm_url}: {e}")
    with db() as c:
        qmarks = ",".join("?" for _ in b.media_ids)
        rows = [dict(r) for r in c.execute(f"SELECT id, path, kind FROM media WHERE kind='photo' AND id IN ({qmarks})", b.media_ids)]
    paths = [Path(r["path"]) for r in rows if Path(r["path"]).is_file()]
    if len(paths) < 3:
        raise HTTPException(400, "Pelo menos 3 fotos precisam estar disponíveis no disco (HD conectado?).")
    preset = b.preset if b.preset in _ODM_PRESETS else "balanced"
    name = (b.name or "").strip() or f"Ortomosaico {datetime.now().strftime('%d/%m %H:%M')}"
    with db() as c:
        cur = c.execute(
            "INSERT INTO orthomosaics(name,flight_id,odm_url,status,progress,photo_count,options,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (name, b.flight_id, odm_url, "pending", 0, len(paths), json.dumps({"preset": preset}), datetime.now().isoformat(timespec="seconds"))
        )
        oid = cur.lastrowid
        if b.odm_url.strip():
            c.execute("INSERT OR REPLACE INTO kv(k, v) VALUES('odm_url', ?)", (odm_url,))
    threading.Thread(target=_odm_worker, args=(oid, odm_url, paths, preset), daemon=True).start()
    with db() as c:
        return _ortho_out(c.execute("SELECT * FROM orthomosaics WHERE id=?", (oid,)).fetchone())

@app.post("/api/orthos/import-tif")
async def import_ortho_tif(request: Request, name: str = "", flight_id: int = 0):
    """Importa um arquivo GeoTIFF (.tif/.tiff) já gerado externamente e cria a camada para o mapa."""
    clean_name = (name or "").strip() or f"GeoTIFF {datetime.now().strftime('%d/%m %H:%M')}"
    with db() as c:
        cur = c.execute(
            "INSERT INTO orthomosaics(name,flight_id,status,progress,created_at) VALUES(?,?,?,?,?)",
            (clean_name, flight_id or None, "importing", 50, datetime.now().isoformat(timespec="seconds"))
        )
        oid = cur.lastrowid
    tif_path = ORTHOS / f"ortho_{oid}.tif"
    png_path = ORTHOS / f"ortho_{oid}.png"
    max_bytes = 8 * 1024 * 1024 * 1024  # 8 GB
    size = 0
    try:
        with open(tif_path, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, "GeoTIFF excede o limite de 600 MB.")
                f.write(chunk)
        if size < 64:
            raise HTTPException(400, "Arquivo GeoTIFF vazio ou inválido.")
        geo = _process_geotiff(tif_path, png_path)
        with db() as c:
            c.execute(
                "UPDATE orthomosaics SET status='completed', progress=100, tif_path=?, png_path=?, bounds=?, epsg=?, gsd_cm=?, finished_at=? WHERE id=?",
                (str(tif_path), str(png_path), json.dumps(geo["bounds"]), geo["epsg"], geo["gsd_cm"],
                 datetime.now().isoformat(timespec="seconds"), oid)
            )
            return _ortho_out(c.execute("SELECT * FROM orthomosaics WHERE id=?", (oid,)).fetchone())
    except Exception as e:
        _unlink_quiet(tif_path); _unlink_quiet(png_path)
        with db() as c:
            c.execute("DELETE FROM orthomosaics WHERE id=?", (oid,))
        if isinstance(e, HTTPException): raise
        raise HTTPException(400, f"Falha ao importar GeoTIFF: {e}")

class OrthoLocalBody(BaseModel):
    path: str
    name: str = ""
    flight_id: Optional[int] = None

def _import_local_worker(oid: int, tif_path: Path, png_path: Path):
    def _upd_prog(pct: int):
        with db() as c:
            c.execute("UPDATE orthomosaics SET progress=? WHERE id=? AND status='importing'", (pct, oid))
    try:
        geo = _process_geotiff(tif_path, png_path, on_progress=_upd_prog)
        with db() as c:
            c.execute(
                "UPDATE orthomosaics SET status='completed', progress=100, tif_path=?, png_path=?, bounds=?, epsg=?, gsd_cm=?, finished_at=? WHERE id=?",
                (str(tif_path), str(png_path), json.dumps(geo["bounds"]), geo["epsg"], geo["gsd_cm"],
                 datetime.now().isoformat(timespec="seconds"), oid)
            )
    except Exception as e:
        import traceback; traceback.print_exc()
        _unlink_quiet(png_path)
        with db() as c:
            c.execute("UPDATE orthomosaics SET status='failed', error=? WHERE id=?", (str(e)[:500], oid))

@app.post("/api/orthos/import-local")
def import_ortho_local(b: OrthoLocalBody):
    """Importa um GeoTIFF grande direto do caminho no disco em background com acompanhamento de %."""
    p = Path(norm_path(b.path.strip()))
    if not _is_allowed_fs_path(p) or not p.is_file():
        raise HTTPException(400, "Arquivo GeoTIFF não encontrado ou pasta não permitida.")
    clean_name = (b.name or "").strip() or p.stem
    with db() as c:
        cur = c.execute(
            "INSERT INTO orthomosaics(name,flight_id,status,progress,tif_path,created_at) VALUES(?,?,?,?,?,?)",
            (clean_name, b.flight_id or None, "importing", 5, str(p), datetime.now().isoformat(timespec="seconds"))
        )
        oid = cur.lastrowid
        out = _ortho_out(c.execute("SELECT * FROM orthomosaics WHERE id=?", (oid,)).fetchone())
    png_path = ORTHOS / f"ortho_{oid}.png"
    threading.Thread(target=_import_local_worker, args=(oid, p, png_path), daemon=True).start()
    return out

@app.post("/api/orthos/{oid:int}/cancel")
def cancel_ortho(oid: int):
    with db() as c:
        r = c.execute("SELECT * FROM orthomosaics WHERE id=?", (oid,)).fetchone()
        if not r: raise HTTPException(404, "Ortomosaico não encontrado.")
        c.execute("UPDATE orthomosaics SET status='canceled' WHERE id=?", (oid,))
    if r["task_id"] and r["odm_url"]:
        try: _odm_multipart(f"{r['odm_url'].rstrip('/')}/task/{r['task_id']}/cancel", timeout=5)
        except Exception: pass
    return {"ok": True}

@app.delete("/api/orthos/{oid:int}")
def delete_ortho(oid: int):
    with db() as c:
        r = c.execute("SELECT tif_path, png_path FROM orthomosaics WHERE id=?", (oid,)).fetchone()
        if not r: raise HTTPException(404, "Ortomosaico não encontrado.")
        c.execute("DELETE FROM orthomosaics WHERE id=?", (oid,))
    for p in (r["tif_path"], r["png_path"]):
        if p:
            pp = Path(p)
            try:
                if pp.resolve().is_relative_to(ORTHOS.resolve()):
                    _unlink_quiet(pp)
            except Exception:
                pass
    return {"ok": True}

def _line_length_m(pts: list[list[float]], closed: bool = False) -> float:
    if len(pts) < 2: return 0.0
    total = sum(hav(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]) for i in range(len(pts) - 1))
    if closed and len(pts) >= 3:
        total += hav(pts[-1][0], pts[-1][1], pts[0][0], pts[0][1])
    return round(total, 2)

def _poly_area_m2(pts: list[list[float]]) -> float:
    if len(pts) < 3: return 0.0
    lat0 = sum(p[0] for p in pts) / len(pts)
    kx = 111320.0 * cos(radians(lat0))
    ky = 110540.0
    xy = [(p[1] * kx, p[0] * ky) for p in pts]
    acc = sum(xy[i][0] * xy[(i + 1) % len(xy)][1] - xy[(i + 1) % len(xy)][0] * xy[i][1] for i in range(len(xy)))
    return round(abs(acc) * 0.5, 2)

class AnnotationIn(BaseModel):
    kind: str
    name: str
    notes: str = ""
    coords: list[list[float]]
    ortho_id: Optional[int] = None
    color: str = "#00d2ff"

def _annot_out(r) -> dict:
    d = dict(r)
    if isinstance(d.get("coords"), str):
        try: d["coords"] = json.loads(d["coords"])
        except Exception: d["coords"] = []
    return d

@app.get("/api/annotations")
def list_annotations(ortho_id: Optional[int] = None):
    with db() as c:
        if ortho_id is not None:
            rows = c.execute("SELECT * FROM ortho_annotations WHERE ortho_id=? ORDER BY id DESC", (ortho_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM ortho_annotations ORDER BY id DESC").fetchall()
    return [_annot_out(r) for r in rows]

@app.post("/api/annotations")
def create_annotation(b: AnnotationIn):
    kind = b.kind.strip().lower()
    if kind not in ("line", "polygon", "pin"):
        raise HTTPException(400, "Tipo inválido. Use line, polygon ou pin.")
    pts = [[round(float(p[0]), 7), round(float(p[1]), 7)] for p in b.coords if len(p) >= 2 and valid_gps(p[0], p[1])]
    if kind == "pin" and len(pts) < 1: raise HTTPException(400, "Informe 1 coordenada para o pino.")
    if kind == "line" and len(pts) < 2: raise HTTPException(400, "Informe pelo menos 2 pontos para medir distância.")
    if kind == "polygon" and len(pts) < 3: raise HTTPException(400, "Informe pelo menos 3 pontos para medir área.")
    length_m = _line_length_m(pts, closed=(kind == "polygon")) if kind in ("line", "polygon") else 0.0
    area_m2 = _poly_area_m2(pts) if kind == "polygon" else 0.0
    name = (b.name or "").strip() or ("Pino de Inspeção" if kind == "pin" else ("Área Medida" if kind == "polygon" else "Distância Medida"))
    color = b.color if re.match(r"^#[0-9a-fA-F]{6}$", b.color or "") else "#00d2ff"
    now = datetime.now().isoformat(timespec="seconds")
    with db() as c:
        cur = c.execute(
            "INSERT INTO ortho_annotations(ortho_id,kind,name,notes,coords,length_m,area_m2,color,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (b.ortho_id, kind, name, (b.notes or "").strip(), json.dumps(pts), length_m, area_m2, color, now)
        )
        return _annot_out(c.execute("SELECT * FROM ortho_annotations WHERE id=?", (cur.lastrowid,)).fetchone())

@app.delete("/api/annotations/{aid:int}")
def delete_annotation(aid: int):
    with db() as c:
        c.execute("DELETE FROM ortho_annotations WHERE id=?", (aid,))
    return {"ok": True}

@app.get("/api/annotations/geojson")
def annotations_geojson(ortho_id: Optional[int] = None):
    items = list_annotations(ortho_id=ortho_id)
    feats = []
    for a in items:
        pts = a["coords"]
        props = {"id": a["id"], "kind": a["kind"], "name": a["name"], "notes": a["notes"],
                 "length_m": a["length_m"], "area_m2": a["area_m2"], "area_ha": round(a["area_m2"] / 10000.0, 4),
                 "ortho_id": a["ortho_id"], "created_at": a["created_at"]}
        if a["kind"] == "pin" and pts:
            geom = {"type": "Point", "coordinates": [pts[0][1], pts[0][0]]}
        elif a["kind"] == "line" and len(pts) >= 2:
            geom = {"type": "LineString", "coordinates": [[p[1], p[0]] for p in pts]}
        elif a["kind"] == "polygon" and len(pts) >= 3:
            ring = [[p[1], p[0]] for p in pts]
            if ring[0] != ring[-1]: ring.append(ring[0])
            geom = {"type": "Polygon", "coordinates": [ring]}
        else:
            continue
        feats.append({"type": "Feature", "properties": props, "geometry": geom})
    return attach(json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False, indent=2),
                  "anotacoes_gis.geojson", "application/geo+json")

@app.get("/api/annotations/report", response_class=HTMLResponse)
def annotations_report(ortho_id: Optional[int] = None):
    items = list_annotations(ortho_id=ortho_id)
    rows_html = "".join(
        f"<tr><td>{html.escape(a['kind'])}</td><td><b>{html.escape(a['name'])}</b><br><small>{html.escape(a['notes'])}</small></td>"
        f"<td>{a['length_m']:.1f} m</td><td>{a['area_m2']:.1f} m² ({a['area_m2']/10000.0:.3f} ha)</td>"
        f"<td>{a['coords'][0][0]:.5f}, {a['coords'][0][1]:.5f}</td></tr>"
        for a in items if a["coords"]
    ) or "<tr><td colspan='5'>Nenhuma medição ou anotação cadastrada.</td></tr>"
    return (
        "<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'>"
        "<title>Relatório GIS — Medições & Anotações</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:28px;color:#0f172a}h1{font-size:20px;margin:0 0 6px}"
        "table{width:100%;border-collapse:collapse;margin-top:14px;font-size:13px}"
        "th,td{border:1px solid #cbd5e1;padding:8px 10px;text-align:left}th{background:#f1f5f9}"
        "@media print{.noprint{display:none}}</style></head><body>"
        "<div style='display:flex;justify-content:space-between;align-items:center'>"
        f"<div><h1>Relatório GIS — Medições &amp; Anotações</h1><div style='color:#475569;font-size:13px'>Total: {len(items)} item(ns) · Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}</div></div>"
        "<button class='noprint' onclick='window.print()' style='padding:8px 14px;cursor:pointer'>🖨 Imprimir / PDF</button></div>"
        "<table><thead><tr><th>Tipo</th><th>Nome / Notas</th><th>Distância / Perímetro</th><th>Área</th><th>Coordenada Ref.</th></tr></thead>"
        f"<tbody>{rows_html}</tbody></table></body></html>"
    )

@app.get("/api/orthos/{oid:int}/png")
def ortho_png(oid: int):
    with db() as c:
        r = c.execute("SELECT png_path FROM orthomosaics WHERE id=?", (oid,)).fetchone()
    if not r or not r["png_path"] or not Path(r["png_path"]).is_file():
        raise HTTPException(404, "Preview PNG do ortomosaico não encontrado.")
    return FileResponse(r["png_path"], media_type="image/png")

@app.get("/api/orthos/{oid:int}/tif")
def ortho_tif(oid: int):
    with db() as c:
        r = c.execute("SELECT name, tif_path FROM orthomosaics WHERE id=?", (oid,)).fetchone()
    if not r or not r["tif_path"] or not Path(r["tif_path"]).is_file():
        raise HTTPException(404, "Arquivo GeoTIFF não encontrado.")
    safe = re.sub(r"[^\w\-]+", "_", r["name"] or f"ortho_{oid}").strip("_") or f"ortho_{oid}"
    return FileResponse(r["tif_path"], media_type="image/tiff", filename=f"{safe}.tif")

@app.get("/api/stats")
def stats():
    with db() as c:
        fl = [dict(r) for r in c.execute("SELECT id,start,duration,distance,max_alt,battery_sn,place,meta FROM flights WHERE source='log'")]
        m = [dict(r) for r in c.execute("SELECT kind,path,sha1,backed,flight_id FROM media")]
        row = c.execute("SELECT v FROM kv WHERE k='prop_base'").fetchone()
    hours = sum((f["duration"] or 0) for f in fl) / 3600; base = float(row[0]) if row else 0.0
    bats, drones, mech_alerts = {}, {}, []
    for f in fl:
        dur = f["duration"] or 0
        b = bats.setdefault(f["battery_sn"] or "desconhecida", {"sn": f["battery_sn"] or "desconhecida", "flights": 0, "hours": 0})
        b["flights"] += 1; b["hours"] += dur / 3600
        mt = json.loads(f["meta"]) if f.get("meta") else {}
        dr = mt.get("drone") or {}
        key = dr.get("sn") or dr.get("type") or "DJI Mini 3"
        dn = drones.setdefault(key, {"sn": dr.get("sn") or "—", "name": dr.get("name") or "", "type": dr.get("type") or "DJI Mini 3", "flights": 0, "hours": 0.0, "km": 0.0})
        dn["flights"] += 1; dn["hours"] += dur / 3600; dn["km"] += (f["distance"] or 0) / 1000
        mc = mt.get("mech") or {}
        if any(mc.get(k, 0) > 0 for k in ("vib_s", "force_s", "comp_s", "gimb_s")):
            st_s = local(f["start"]).isoformat()[:16].replace("T", " ") if f.get("start") else ""
            mech_alerts.append({"id": f["id"], "start": st_s, "place": f["place"] or "", **mc})
    dups = {}
    for x in m: dups.setdefault(x["sha1"], []).append(x["path"])
    return {"flights": len(fl), "hours": hours, "km": sum((f["distance"] or 0) for f in fl) / 1000,
            "photos": sum(x["kind"] == "photo" for x in m), "videos": sum(x["kind"] == "video" for x in m),
            "max_alt": max(((f["max_alt"] or 0) for f in fl), default=0),
            "by_month": sorted(Counter(f["start"][:7] for f in fl if f["start"]).items()),
            "places": Counter(f["place"] for f in fl if f["place"]).most_common(8),
            "batteries": list(bats.values()),
            "drones": list(drones.values()),
            "mech_alerts": mech_alerts[-10:],
            "props": {"hours": hours - base, "limit": PROP_LIMIT_H},
            "library": {"duplicates": [v for v in dups.values() if len(v) > 1], "unlinked": sum(x["flight_id"] is None for x in m),
                        "not_backed": sum(not x["backed"] for x in m) if BACKUP_DIR else None}}

@app.post("/api/props/reset")
def props_reset():
    total = stats()["hours"]
    with db() as c: c.execute("INSERT OR REPLACE INTO kv VALUES('prop_base',?)", (str(total),))
    return {"ok": True}

class LangReq(BaseModel):
    lang: str

@app.get("/api/lang")
def get_lang_api():
    return {"lang": get_lang()}

@app.post("/api/lang")
def set_lang_api(req: LangReq):
    if req.lang not in SUPPORTED_LANGS:
        raise HTTPException(400, "Unsupported language")
    kv_set("lang", req.lang)
    return {"ok": True, "lang": req.lang}

class Spot(BaseModel):
    name: str; lat: float; lon: float; notes: str = ""

@app.get("/api/spots")
def spots_list(bbox: str = ""):
    bb = _parse_bbox_param(bbox)
    with db() as c:
        if bb:
            return [dict(r) for r in c.execute(
                "SELECT s.* FROM spots s JOIN rtree_spots rs ON rs.id = s.id "
                "WHERE rs.min_lon >= ? AND rs.max_lon <= ? AND rs.min_lat >= ? AND rs.max_lat <= ?",
                (bb[0], bb[1], bb[2], bb[3])
            )]
        return [dict(r) for r in c.execute("SELECT * FROM spots")]

@app.get("/api/gis/status")
def gis_status():
    """Diagnóstico completo do motor espacial Nível 1 (B-Tree + R-Tree) e Nível 2 (SpatiaLite / GEOS / PROJ)."""
    with db() as c:
        rf = c.execute("SELECT COUNT(*), MIN(min_lon), MIN(min_lat), MAX(max_lon), MAX(max_lat) FROM rtree_flights").fetchone()
        rm = c.execute("SELECT COUNT(*), MIN(min_lon), MIN(min_lat), MAX(max_lon), MAX(max_lat) FROM rtree_media").fetchone()
        rs = c.execute("SELECT COUNT(*) FROM rtree_spots").fetchone()[0]
        tf = c.execute("SELECT COUNT(*) FROM flights").fetchone()[0]
        tm = c.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        ts = c.execute("SELECT COUNT(*) FROM spots").fetchone()[0]
        lons = [x for x in (rf[1], rm[1], rf[3], rm[3]) if x is not None]
        lats = [x for x in (rf[2], rm[2], rf[4], rm[4]) if x is not None]
        extent = None
        if len(lons) >= 2 and len(lats) >= 2:
            min_lon, max_lon, min_lat, max_lat = min(lons), max(lons), min(lats), max(lats)
            c_lat, c_lon = (min_lat + max_lat) / 2.0, (min_lon + max_lon) / 2.0
            epsg = _st_utm_epsg(c_lat, c_lon)
            env_json = _st_makeenvelope_geojson(min_lon, min_lat, max_lon, max_lat)
            area_km2 = round(_st_area_utm_m2(env_json) / 1_000_000.0, 2) if env_json else 0.0
            diag_km = round((_st_distance_sphere(min_lat, min_lon, max_lat, max_lon) or 0.0) / 1000.0, 2)
            extent = {
                "bbox": [round(min_lon, 6), round(min_lat, 6), round(max_lon, 6), round(max_lat, 6)],
                "area_km2": area_km2,
                "diagonal_km": diag_km,
                "utm_epsg": f"EPSG:{epsg}" if epsg else "EPSG:4326",
            }
    return {
        "level1": {
            "engine": "SQLite R-Tree 2D + B-Tree Composite (lat, lon)",
            "rtree_flights": rf[0],
            "total_flights": tf,
            "rtree_media": rm[0],
            "total_media": tm,
            "rtree_spots": rs,
            "total_spots": ts,
        },
        "level2": {
            "engine": "SpatiaLite / OGC Spatial Functions",
            "mode": _GIS_INFO["spatialite_mode"],
            "spatialite_version": _GIS_INFO["spatialite_version"] or "5.1.0-compat",
            "geos_version": _GIS_INFO["geos_version"],
            "proj_available": _GIS_INFO["proj_available"],
            "crs_default": "WGS 84 (EPSG:4326)",
            "functions": ["ST_Distance_Sphere", "ST_MakePointGeoJSON", "ST_MakeEnvelopeGeoJSON", "ST_UTM_EPSG", "ST_Area_UTM"],
        },
        "sqlite_version": _GIS_INFO["sqlite_version"],
        "extent": extent,
    }

@app.post("/api/gis/rebuild")
def gis_rebuild():
    """Reconstrói integralmente os índices espaciais R-Tree de voos, mídias e locais favoritos."""
    with db() as c:
        c.execute("DELETE FROM rtree_flights")
        c.execute("DELETE FROM rtree_media")
        c.execute("DELETE FROM rtree_spots")
        c.execute("""
        INSERT INTO rtree_media(id, min_lon, max_lon, min_lat, max_lat)
        SELECT id, lon, lon, lat, lat FROM media
        WHERE lat IS NOT NULL AND lon IS NOT NULL AND (ABS(lat) > 1e-5 OR ABS(lon) > 1e-5)
        """)
        c.execute("""
        INSERT INTO rtree_spots(id, min_lon, max_lon, min_lat, max_lat)
        SELECT id, lon, lon, lat, lat FROM spots
        WHERE lat IS NOT NULL AND lon IS NOT NULL
        """)
        for r in c.execute("SELECT id, lat, lon, track FROM flights").fetchall():
            _sync_flight_rtree(c, r["id"], r["lat"], r["lon"], r["track"])
    return {"ok": True, **gis_status()}

@app.post("/api/spots")
def spots_add(s: Spot):
    with db() as c: c.execute("INSERT INTO spots(name,lat,lon,notes) VALUES(?,?,?,?)", (s.name, s.lat, s.lon, s.notes))
    return {"ok": True}

@app.delete("/api/spots/{sid}")
def spots_del(sid: int):
    with db() as c: c.execute("DELETE FROM spots WHERE id=?", (sid,))
    return {"ok": True}

def reconstruct_flights(gap_min=30):
    """Cria voos a partir de: (1) vídeos sem voo que possuem trilha .SRT completa;
    (2) fotos com GPS sem log (anteriores ao primeiro log)."""
    n = 0
    with db() as c:
        first = c.execute("SELECT MIN(start) FROM flights WHERE source='log'").fetchone()[0]
        lim = local(first).isoformat() if first else "9999"
        vids = c.execute("SELECT id,path,taken,tags FROM media WHERE flight_id IS NULL AND COALESCE(manual,0)=0 AND kind='video' ORDER BY taken").fetchall()
    # 1. Reconstrução de voos via arquivo .SRT completo de vídeos
    for v in vids:
        vp = Path(v["path"])
        if not vp.exists(): continue
        pts = [x for x in parse_srt_full(vp) if valid_gps(x.get("lat"), x.get("lon"))]
        if len(pts) < 3: continue
        t0 = datetime.fromisoformat(v["taken"])
        track, dist, maxalt, maxspd, prev = [], 0.0, 0.0, 0.0, None
        for x in pts:
            sec = int(x["t"])
            alt = float(x.get("alt") or 0.0)
            spd = 0.0
            if prev:
                d = dist_3d(prev[1], prev[2], prev[3], x["lat"], x["lon"], alt)
                dt = max(1, sec - prev[0])
                spd = d / dt
                if spd > 35.0: continue  # descarta glitch/salto espúrio de GPS no .SRT
                dist += d
            maxalt, maxspd = max(maxalt, alt), max(maxspd, spd)
            prev = (sec, x["lat"], x["lon"], alt)
            track.append([sec, x["lat"], x["lon"], round(alt, 1), round(spd, 1), None, None])
        start = (t0 - timedelta(hours=TZ_OFFSET)).isoformat() + "Z"
        key = "srt:" + str(v["id"])
        with db() as c:
            if c.execute("SELECT 1 FROM flights WHERE file=?", (key,)).fetchone(): continue
        place = geocode(track[0][1], track[0][2]); time.sleep(1)
        with db() as c:
            c.execute("INSERT OR IGNORE INTO flights(file,start,duration,distance,max_alt,max_speed,lat,lon,place,weather,track,events,source)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'srt')",
                      (key, start, track[-1][0], dist, maxalt, maxspd, track[0][1], track[0][2], place,
                       json.dumps(weather(track[0][1], track[0][2], start)), json.dumps(track),
                       json.dumps([{"t": 0, "label": "Início do vídeo (.SRT)"}, {"t": track[-1][0], "label": "Fim do vídeo (.SRT)"}])))
            fid = c.execute("SELECT id FROM flights WHERE file=?", (key,)).fetchone()[0]
            _sync_flight_rtree(c, fid, track[0][1], track[0][2], track)
            tags = {x for x in (v["tags"] or "").split(",") if x} | {t0.strftime("%Y-%m")} | ({place} if place else set())
            c.execute("UPDATE media SET flight_id=?, tags=? WHERE id=?", (fid, ",".join(sorted(tags)), v["id"]))
        n += 1
    # 2. Reconstrução de voos via agrupamento de fotos com GPS
    with db() as c:
        rows = c.execute("SELECT id,taken,lat,lon,tags FROM media WHERE flight_id IS NULL AND COALESCE(manual,0)=0 AND kind='photo' "
                         "AND lat IS NOT NULL AND lon IS NOT NULL AND ABS(lat)+ABS(lon)>0.001 AND taken<? ORDER BY taken",
                         (lim,)).fetchall()
    groups, cur = [], []
    for r in rows:
        t = datetime.fromisoformat(r["taken"])
        if cur and ((t - cur[-1][0]).total_seconds() > gap_min * 60
                    or hav(cur[-1][1]["lat"], cur[-1][1]["lon"], r["lat"], r["lon"]) > 5000):
            groups.append(cur); cur = []
        cur.append((t, r))
    if cur: groups.append(cur)
    for grp in groups:
        if len(grp) < 2: continue
        t0 = grp[0][0]; track, dist, prev = [], 0.0, None
        for t, r in grp:
            if prev: dist += hav(prev["lat"], prev["lon"], r["lat"], r["lon"])
            prev = r
            track.append([int((t - t0).total_seconds()), r["lat"], r["lon"], 0, 0, None, None])
        start = (t0 - timedelta(hours=TZ_OFFSET)).isoformat() + "Z"
        place = geocode(track[0][1], track[0][2]); time.sleep(1)
        key = "photos:" + str(grp[0][1]["id"])
        with db() as c:
            c.execute("INSERT OR IGNORE INTO flights(file,start,duration,distance,max_alt,max_speed,lat,lon,place,weather,track,events,source)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'photos')",
                      (key, start, track[-1][0], dist, 0.0, 0.0, track[0][1], track[0][2], place,
                       json.dumps(weather(track[0][1], track[0][2], start)), json.dumps(track),
                       json.dumps([{"t": 0, "label": "Primeira foto"}, {"t": track[-1][0], "label": "Última foto"}])))
            fid = c.execute("SELECT id FROM flights WHERE file=?", (key,)).fetchone()[0]
            _sync_flight_rtree(c, fid, track[0][1], track[0][2], track)
            for t, r in grp:
                tags = {x for x in (r["tags"] or "").split(",") if x} | {t.strftime("%Y-%m")} | ({place} if place else set())
                c.execute("UPDATE media SET flight_id=?, tags=? WHERE id=?", (fid, ",".join(sorted(tags)), r["id"]))
        n += 1
    return n

@app.get("/api/battery-health")
def battery_health():
    with db() as c:
        rows = c.execute("SELECT start,duration,battery_sn,bat FROM flights WHERE source='log' AND battery_sn IS NOT NULL "
                         "AND battery_sn<>'' AND start IS NOT NULL ORDER BY start").fetchall()
    out = {}
    for r in rows:
        b = json.loads(r["bat"]) if r["bat"] else {}
        o = out.setdefault(r["battery_sn"], {"sn": r["battery_sn"], "flights": []})
        o["flights"].append({"start": local(r["start"]).isoformat(), "min": round((r["duration"] or 0) / 60, 1), **b})
    return list(out.values())

AIR_WMS = "https://geoaisweb.decea.mil.br/geoserver/ows"

@app.get("/api/airspace/info")
def airspace_info(lat: float, lon: float, layers: str):
    """Pergunta ao GeoServer do DECEA (GEOAISWEB) quais áreas de espaço aéreo existem no ponto clicado."""
    if not re.fullmatch(r"ICA:[A-Za-z0-9_]+(,ICA:[A-Za-z0-9_]+)*", layers):
        raise HTTPException(400, "camadas inválidas")
    d = 0.0006
    q = urllib.parse.urlencode({"service": "WMS", "version": "1.1.1", "request": "GetFeatureInfo", "layers": layers,
                                "query_layers": layers, "styles": "", "srs": "EPSG:4326",
                                "bbox": f"{lon - d},{lat - d},{lon + d},{lat + d}", "width": 101, "height": 101,
                                "x": 50, "y": 50, "info_format": "application/json", "feature_count": 15})
    try: return http_json(AIR_WMS + "?" + q)
    except Exception as e: return {"error": str(e), "features": []}

import urllib.parse as _up
AIRCK_WMS = "https://geoaisweb.decea.mil.br/geoserver/ows"
AIR_NEAR_KM = 9.0   # referência minha (não é regra): aeródromo mais perto que isso gera "atenção"
AIR_CHECK = [("ICA:eac_p", "Área proibida", 3), ("ICA:eac_r", "Área restrita", 3), ("ICA:eac_d", "Área perigosa", 2),
             ("ICA:CTR", "Zona de controle (CTR)", 3), ("ICA:ATZ", "Zona de tráfego de aeródromo (ATZ)", 3),
             ("ICA:fiz", "Zona de informação de voo (FIZ)", 2)]

def _wfs(layer, bb):
    q = _up.urlencode({"service": "WFS", "version": "1.0.0", "request": "GetFeature", "typeName": layer,
                       "outputFormat": "application/json", "bbox": "%f,%f,%f,%f,EPSG:4326" % bb, "maxFeatures": 300})
    return http_json(AIRCK_WMS + "?" + q).get("features", [])

def _pip(x, y, ring):
    ins = False; j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi, xj, yj = ring[i][0], ring[i][1], ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi: ins = not ins
        j = i
    return ins

def _polys(g):
    if not g: return []
    t = g.get("type")
    if t == "Polygon": return [g["coordinates"]]
    if t == "MultiPolygon": return list(g["coordinates"])
    if t == "GeometryCollection": return [p for x in g.get("geometries", []) for p in _polys(x)]
    return []

def _ring_bbox(r):
    if not r: return (0.0, 0.0, 0.0, 0.0)
    xs, ys = [p[0] for p in r], [p[1] for p in r]
    return min(xs), min(ys), max(xs), max(ys)

def _centers(g):
    if not g: return []
    t, c = g.get("type"), g.get("coordinates")
    if t == "Point": return [(c[0], c[1])]
    if t == "MultiPoint": return [(p[0], p[1]) for p in c]
    ring = c[0] if t == "Polygon" else (c[0][0] if t == "MultiPolygon" else None)
    if ring: return [(sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))]
    return []

def _label(props):
    for k in ("name", "nome", "NAME", "NOME", "designator", "ident", "icao", "codigo", "nm"):
        v = props.get(k)
        if isinstance(v, str) and v.strip(): return v.strip()
    for v in props.values():
        if isinstance(v, str) and v.strip(): return v.strip()
    return ""

def _props(p):
    return dict([(k, v) for k, v in p.items() if isinstance(v, (str, int, float)) and not isinstance(v, bool) and v != ""][:8])

def air_check_pts(pts):
    """Verifica intersecção de uma lista de pontos [[t, lat, lon, alt], ...] contra camadas WFS do DECEA."""
    res = {"checked": datetime.now().isoformat(timespec="seconds"), "hits": [], "near": [], "errors": [], "sev": 0}
    if not pts:
        res["failed"] = True
        return res
    lats, lons = [p[1] for p in pts], [p[2] for p in pts]; pad = 0.15
    bb = (min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad)
    ok = 0
    for layer, label, sev in AIR_CHECK:
        try: feats = _wfs(layer, bb); ok += 1
        except Exception as e: res["errors"].append(layer + ": " + str(e)[:80]); continue
        for f in feats:
            props = f.get("properties") or {}; ts = []
            for poly in _polys(f.get("geometry")):
                if not poly or not poly[0]: continue
                x0, y0, x1, y1 = _ring_bbox(poly[0])
                for p in pts:
                    if x0 <= p[2] <= x1 and y0 <= p[1] <= y1 and _pip(p[2], p[1], poly[0]) and not any(_pip(p[2], p[1], h) for h in poly[1:]):
                        ts.append(p[0])
            if ts:
                res["hits"].append({"layer": layer, "label": label, "name": _label(props), "first_t": min(ts), "last_t": max(ts), "props": _props(props)})
                res["sev"] = max(res["sev"], sev)
    try:
        near = []
        for f in _wfs("ICA:airport_heliport", bb):
            props = f.get("properties") or {}
            for lo, la in _centers(f.get("geometry")):
                near.append({"name": _label(props), "km": round(min(hav(p[1], p[2], la, lo) for p in pts) / 1000, 1)})
        ok += 1
        res["near"] = [n for n in sorted(near, key=lambda n: n["km"])[:3] if n["km"] <= 20]
        if res["near"] and res["near"][0]["km"] <= AIR_NEAR_KM: res["sev"] = max(res["sev"], 2)
    except Exception as e: res["errors"].append("aeródromos: " + str(e)[:80])
    res["failed"] = ok == 0
    return res

def air_check(fid):
    r = get_flight(fid); tr = json.loads(r["track"])
    pts = (tr[::3] + [tr[-1]]) if len(tr) > 300 else tr
    return air_check_pts(pts)

@app.post("/api/flights/{fid}/airspace-check")
def airspace_check(fid: int):
    res = air_check(fid)
    if res["failed"]: raise HTTPException(502, "não foi possível consultar o DECEA: " + "; ".join(res["errors"][:2]))
    with db() as c: c.execute("UPDATE flights SET air=? WHERE id=?", (json.dumps(res), fid))
    return res

# ---------- Planejador de Missões Pré-Voo (Waypoints + Estimativa de Bateria + DECEA) ----------
class MissionIn(BaseModel):
    name: str
    waypoints: list[list[float]]
    alt: float = 60.0
    speed: float = 8.0
    notes: str = ""
    meta: dict | None = None

def _mission_out(r):
    d = dict(r)
    wps = json.loads(d["waypoints"] or "[]")
    d["waypoints"] = wps
    d["air"] = json.loads(d["air"]) if d.get("air") else None
    try:
        d["meta"] = json.loads(d.get("meta") or "{}") if isinstance(d.get("meta"), str) else (d.get("meta") or {})
    except Exception:
        d["meta"] = {}
    alt = float(d.get("alt") or 60.0)
    spd = max(1.0, float(d.get("speed") or 8.0))
    dist, max_vlos = 0.0, 0.0
    if wps:
        h0 = wps[0]
        for i, w in enumerate(wps):
            max_vlos = max(max_vlos, hav(h0[0], h0[1], w[0], w[1]))
            if i > 0:
                a1 = wps[i - 1][2] if len(wps[i - 1]) > 2 else alt
                a2 = w[2] if len(w) > 2 else alt
                dist += dist_3d(wps[i - 1][0], wps[i - 1][1], a1, w[0], w[1], a2)
    climb_s = (alt * 2.0) / 2.5
    dur_s = round(dist / spd + len(wps) * 2.5 + climb_s)
    bat_min = float(d["meta"].get("drone_bat_min") or 25.0)
    # Estimativa conservadora (tempo seguro por bateria com margem de vento/RTH)
    est_bat = max(3, round((dur_s / (bat_min * 60.0)) * 100))
    d["distance"] = round(dist, 1)
    d["max_vlos"] = round(max_vlos, 1)
    d["duration"] = dur_s
    d["est_battery_pct"] = min(100, est_bat)
    d["batteries"] = int(d["meta"].get("batteries") or max(1, ceil(dur_s / (bat_min * 60.0 * 0.85))))
    return d

def _get_mission(mid: int):
    with db() as c: r = c.execute("SELECT * FROM missions WHERE id=?", (mid,)).fetchone()
    if not r: raise HTTPException(404, "missão não encontrada")
    return _mission_out(r)

class ElevationIn(BaseModel):
    waypoints: list[list[float]]

@app.post("/api/missions/elevation")
def mission_elevation(b: ElevationIn):
    """Consulta elevação do terreno (DEM) via Open-Meteo para os waypoints da missão."""
    wps = [[float(w[0]), float(w[1])] for w in b.waypoints if len(w) >= 2 and valid_gps(w[0], w[1])]
    if len(wps) < 2:
        raise HTTPException(400, "Informe pelo menos 2 waypoints válidos.")
    n = len(wps)
    if n <= 100:
        sample_idx = list(range(n))
    else:
        sample_idx = sorted(set([int(i * (n - 1) / 99) for i in range(100)]))
    lats = ",".join(f"{wps[i][0]:.6f}" for i in sample_idx)
    lons = ",".join(f"{wps[i][1]:.6f}" for i in sample_idx)
    try:
        j = http_json(f"https://api.open-meteo.com/v1/elevation?latitude={lats}&longitude={lons}")
        elev_s = j.get("elevation") or []
        if len(elev_s) != len(sample_idx):
            raise ValueError("Resposta incompleta da API de elevação.")
    except Exception as e:
        raise HTTPException(502, f"Falha ao consultar elevação do terreno: {e}")

    if n <= 100:
        ground = [round(float(v), 1) for v in elev_s]
    else:
        ground = []
        si = 0
        for i in range(n):
            while si + 1 < len(sample_idx) and sample_idx[si + 1] < i:
                si += 1
            i0, i1 = sample_idx[si], sample_idx[min(si + 1, len(sample_idx) - 1)]
            if i1 == i0:
                ground.append(round(float(elev_s[si]), 1))
            else:
                f = (i - i0) / (i1 - i0)
                ground.append(round(float(elev_s[si]) * (1 - f) + float(elev_s[si + 1]) * f, 1))
    home_elev = ground[0]
    rel = [round(e - home_elev, 1) for e in ground]
    return {
        "home_elev": home_elev,
        "ground_elev": ground,
        "rel_ground": rel,
        "min_rel": min(rel),
        "max_rel": max(rel),
    }

@app.get("/api/geocode")
def geocode_search(q: str = ""):
    """Busca endereços e locais via Nominatim (OpenStreetMap) para a barra de pesquisa do mapa."""
    term = (q or "").strip()
    if len(term) < 2:
        return []
    try:
        url = f"https://nominatim.openstreetmap.org/search?format=jsonv2&addressdetails=1&limit=6&q={urllib.parse.quote_plus(term)}"
        res = http_json(url)
        return [{
            "label": r.get("display_name") or term,
            "lat": float(r["lat"]),
            "lon": float(r["lon"]),
            "bbox": [float(x) for x in r["boundingbox"]] if isinstance(r.get("boundingbox"), list) and len(r["boundingbox"]) == 4 else None,
            "type": r.get("type") or r.get("class") or "",
        } for r in (res or []) if "lat" in r and "lon" in r]
    except Exception as e:
        raise HTTPException(502, f"Falha ao buscar endereço: {e}")

@app.get("/api/missions")
def missions_list():
    with db() as c: return [_mission_out(r) for r in c.execute("SELECT * FROM missions ORDER BY id DESC").fetchall()]

@app.post("/api/missions")
def missions_add(b: MissionIn):
    wps = [[round(float(w[0]), 6), round(float(w[1]), 6), round(float(w[2] if len(w) > 2 else b.alt), 1)]
           for w in b.waypoints if len(w) >= 2 and valid_gps(w[0], w[1])]
    if len(wps) < 2:
        raise HTTPException(400, "A missão precisa ter pelo menos 2 waypoints válidos no mapa.")
    alt = max(5.0, min(500.0, float(b.alt)))
    spd = max(1.0, min(16.0, float(b.speed)))
    now = datetime.now().isoformat(timespec="seconds")
    meta_s = json.dumps(b.meta or {}, ensure_ascii=False)
    with db() as c:
        cur = c.execute("INSERT INTO missions(name,waypoints,alt,speed,notes,meta,created) VALUES(?,?,?,?,?,?,?)",
                        (b.name.strip() or "Missão planejada", json.dumps(wps), alt, spd, b.notes.strip(), meta_s, now))
        return _mission_out(c.execute("SELECT * FROM missions WHERE id=?", (cur.lastrowid,)).fetchone())

@app.delete("/api/missions/{mid}")
def missions_del(mid: int):
    with db() as c: c.execute("DELETE FROM missions WHERE id=?", (mid,))
    return {"ok": True}

@app.post("/api/missions/{mid}/airspace-check")
def missions_air_check(mid: int):
    m = _get_mission(mid)
    wps = m["waypoints"]
    spd = max(1.0, float(m["speed"] or 8.0))
    # Interpola pontos a cada ~40 m ao longo dos segmentos para verificação contínua nos polígonos do DECEA
    pts, cur_t = [[0, wps[0][0], wps[0][1], wps[0][2]]], 0
    for i in range(1, len(wps)):
        a, b = wps[i - 1], wps[i]
        seg_d = hav(a[0], a[1], b[0], b[1])
        steps = max(1, min(50, int(seg_d // 40) + 1))
        for k in range(1, steps + 1):
            f = k / steps
            cur_t += int(max(1, (seg_d / steps) / spd))
            pts.append([cur_t, a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f])
    if len(pts) > 350:
        step = max(1, len(pts) // 300)
        pts = pts[::step] + [pts[-1]]
    res = air_check_pts(pts)
    if res["failed"]: raise HTTPException(502, "não foi possível consultar o DECEA: " + "; ".join(res["errors"][:2]))
    with db() as c: c.execute("UPDATE missions SET air=? WHERE id=?", (json.dumps(res), mid))
    return res

@app.get("/api/missions/{mid}/kml")
def mission_kml(mid: int):
    m = _get_mission(mid)
    co = " ".join(f"{float(w[1])},{float(w[0])},{float(w[2])}" for w in m["waypoints"])
    return attach('<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Placemark>'
                  f'<name>{html.escape(m["name"])}</name><LineString><altitudeMode>relativeToGround</altitudeMode><coordinates>{co}</coordinates>'
                  '</LineString></Placemark></kml>', f"missao_{mid}.kml", "application/vnd.google-earth.kml+xml")

@app.get("/api/missions/{mid}/gpx")
def mission_gpx(mid: int):
    m = _get_mission(mid)
    pts = "".join(f'<rtept lat="{float(w[0])}" lon="{float(w[1])}"><ele>{float(w[2])}</ele><name>WP{i+1}</name></rtept>' for i, w in enumerate(m["waypoints"]))
    return attach(f'<?xml version="1.0"?><gpx version="1.1" creator="drone-manager"><rte><name>{html.escape(m["name"])}</name>{pts}</rte></gpx>',
                  f"missao_{mid}.gpx", "application/gpx+xml")

@app.get("/api/missions/{mid}/geojson")
def mission_geojson(mid: int):
    m = _get_mission(mid)
    coords = [[round(float(w[1]), 6), round(float(w[0]), 6), round(float(w[2]), 1)] for w in m["waypoints"]]
    meta = m.get("meta") or {}
    props = {"id": m["id"], "name": m["name"], "distance_m": m["distance"], "duration_s": m["duration"],
             "est_battery_pct": m["est_battery_pct"], "batteries": m["batteries"], "max_vlos_m": m["max_vlos"], "crs": "EPSG:4326"}
    for k in ("mode", "gsd_cm", "front_overlap", "side_overlap", "est_photos", "area_ha", "angle"):
        if k in meta: props[k] = meta[k]
    feats = [{"type": "Feature", "properties": props, "geometry": {"type": "LineString", "coordinates": coords}}]
    poly = meta.get("polygon")
    if isinstance(poly, list) and len(poly) >= 3:
        ring = [[round(float(p[1]), 6), round(float(p[0]), 6)] for p in poly]
        if ring[0] != ring[-1]: ring.append(ring[0])
        feats.append({"type": "Feature", "properties": {"name": m["name"] + " (Área)"},
                      "geometry": {"type": "Polygon", "coordinates": [ring]}})
    feat = {"type": "FeatureCollection", "features": feats}
    return attach(json.dumps(feat, ensure_ascii=False, indent=2), f"missao_{mid}.geojson", "application/geo+json")

_WPML_DRONES = {
    "mini3": (91, 0, 81),
    "mini4": (91, 0, 81),
    "mavic3": (77, 0, 52),
    "p4": (60, 0, 39),
}

def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcula o azimute em graus [-180, 180] de (lat1, lon1) para (lat2, lon2)."""
    p1, p2, dl = radians(lat1), radians(lat2), radians(lon2 - lon1)
    b = degrees(atan2(sin(dl) * cos(p2), cos(p1) * sin(p2) - sin(p1) * cos(p2) * cos(dl)))
    return ((b + 180.0) % 360.0) - 180.0

def _wpml_mission_config(drone_enum: int, drone_sub: int, payload_enum: int, spd: float) -> str:
    return (
        "<wpml:missionConfig>"
        "<wpml:flyToWaylineMode>safely</wpml:flyToWaylineMode>"
        "<wpml:finishAction>goHome</wpml:finishAction>"
        "<wpml:exitOnRCLost>goContinue</wpml:exitOnRCLost>"
        "<wpml:executeRCLostAction>goBack</wpml:executeRCLostAction>"
        "<wpml:takeOffSecurityHeight>20</wpml:takeOffSecurityHeight>"
        f"<wpml:globalTransitionalSpeed>{spd:.1f}</wpml:globalTransitionalSpeed>"
        f"<wpml:droneInfo><wpml:droneEnumValue>{drone_enum}</wpml:droneEnumValue>"
        f"<wpml:droneSubEnumValue>{drone_sub}</wpml:droneSubEnumValue></wpml:droneInfo>"
        f"<wpml:payloadInfo><wpml:payloadEnumValue>{payload_enum}</wpml:payloadEnumValue>"
        "<wpml:payloadPositionIndex>0</wpml:payloadPositionIndex></wpml:payloadInfo>"
        "</wpml:missionConfig>"
    )

def _wpml_actions(idx: int, total: int, mode: str, gimbal_pitch: float) -> str:
    """Gera actionGroup WPML (gimbal no WP0 + disparo de foto em grade/órbita)."""
    acts = []
    if idx == 0 or mode == "orbit":
        acts.append(
            f"<wpml:action><wpml:actionId>{len(acts)}</wpml:actionId>"
            "<wpml:actionActuatorFunc>gimbalRotate</wpml:actionActuatorFunc>"
            "<wpml:actionActuatorFuncParam><wpml:gimbalHeadingYawBase>aircraft</wpml:gimbalHeadingYawBase>"
            "<wpml:gimbalRotateMode>absoluteAngle</wpml:gimbalRotateMode>"
            "<wpml:gimbalPitchRotateEnable>1</wpml:gimbalPitchRotateEnable>"
            f"<wpml:gimbalPitchRotateAngle>{gimbal_pitch:.1f}</wpml:gimbalPitchRotateAngle>"
            "<wpml:gimbalRollRotateEnable>0</wpml:gimbalRollRotateEnable><wpml:gimbalRollRotateAngle>0</wpml:gimbalRollRotateAngle>"
            "<wpml:gimbalYawRotateEnable>0</wpml:gimbalYawRotateEnable><wpml:gimbalYawRotateAngle>0</wpml:gimbalYawRotateAngle>"
            "<wpml:gimbalRotateTimeEnable>0</wpml:gimbalRotateTimeEnable><wpml:gimbalRotateTime>0</wpml:gimbalRotateTime>"
            "<wpml:payloadPositionIndex>0</wpml:payloadPositionIndex></wpml:actionActuatorFuncParam></wpml:action>"
        )
    if mode in ("grid", "double_grid", "orbit"):
        acts.append(
            f"<wpml:action><wpml:actionId>{len(acts)}</wpml:actionId>"
            "<wpml:actionActuatorFunc>takePhoto</wpml:actionActuatorFunc>"
            "<wpml:actionActuatorFuncParam><wpml:payloadPositionIndex>0</wpml:payloadPositionIndex>"
            "<wpml:useGlobalPayloadLensIndex>1</wpml:useGlobalPayloadLensIndex></wpml:actionActuatorFuncParam></wpml:action>"
        )
    if not acts:
        return ""
    return (
        f"<wpml:actionGroup><wpml:actionGroupId>{idx}</wpml:actionGroupId>"
        f"<wpml:actionGroupStartIndex>{idx}</wpml:actionGroupStartIndex>"
        f"<wpml:actionGroupEndIndex>{idx}</wpml:actionGroupEndIndex>"
        "<wpml:actionGroupMode>sequence</wpml:actionGroupMode>"
        "<wpml:actionTrigger><wpml:actionTriggerType>reachPoint</wpml:actionTriggerType></wpml:actionTrigger>"
        f"{''.join(acts)}</wpml:actionGroup>"
    )

def _build_wpml_kmz(m: dict) -> bytes:
    """Empacota a missão em KMZ nativo DJI WPML (wpmz/template.kml + wpmz/waylines.wpml)."""
    wps = m["waypoints"]
    alt_def = float(m.get("alt") or 60.0)
    spd = max(1.0, min(15.0, float(m.get("speed") or 8.0)))
    meta = m.get("meta") or {}
    mode = meta.get("mode") or "waypoints"
    drone_enum, drone_sub, payload_enum = _WPML_DRONES.get(meta.get("drone_preset") or "mini3", (91, 0, 81))
    center = meta.get("center") if isinstance(meta.get("center"), list) and len(meta.get("center")) >= 2 else None
    turn_mode = "coordinateTurn" if mode == "orbit" else "toPointAndStopWithDiscontinuityCurvature"
    now_ms = int(time.time() * 1000)
    m_cfg = _wpml_mission_config(drone_enum, drone_sub, payload_enum, spd)

    tpl_pms, wpl_pms = [], []
    for i, w in enumerate(wps):
        lat, lon = float(w[0]), float(w[1])
        w_alt = float(w[2]) if len(w) > 2 else alt_def
        if mode == "orbit" and center:
            hdg = _bearing_deg(lat, lon, float(center[0]), float(center[1]))
            d_poi = hav(lat, lon, float(center[0]), float(center[1]))
            gp = max(-90.0, min(0.0, -degrees(atan2(w_alt, d_poi)))) if d_poi > 1.0 else -35.0
            hdg_xml = (
                "<wpml:waypointHeadingParam><wpml:waypointHeadingMode>smoothTransition</wpml:waypointHeadingMode>"
                f"<wpml:waypointHeadingAngle>{hdg:.1f}</wpml:waypointHeadingAngle>"
                "<wpml:waypointPoiPoint>0.000000,0.000000,0.000000</wpml:waypointPoiPoint>"
                "<wpml:waypointHeadingAngleEnable>1</wpml:waypointHeadingAngleEnable>"
                "<wpml:waypointHeadingPathMode>followBadArc</wpml:waypointHeadingPathMode></wpml:waypointHeadingParam>"
            )
        else:
            gp = -90.0 if mode in ("grid", "double_grid") else -15.0
            hdg_xml = (
                "<wpml:waypointHeadingParam><wpml:waypointHeadingMode>followWayline</wpml:waypointHeadingMode>"
                "<wpml:waypointHeadingAngle>0</wpml:waypointHeadingAngle>"
                "<wpml:waypointPoiPoint>0.000000,0.000000,0.000000</wpml:waypointPoiPoint>"
                "<wpml:waypointHeadingAngleEnable>0</wpml:waypointHeadingAngleEnable>"
                "<wpml:waypointHeadingPathMode>followBadArc</wpml:waypointHeadingPathMode></wpml:waypointHeadingParam>"
            )

        turn_xml = (
            f"<wpml:waypointTurnParam><wpml:waypointTurnMode>{turn_mode}</wpml:waypointTurnMode>"
            "<wpml:waypointTurnDampingDist>0.2</wpml:waypointTurnDampingDist></wpml:waypointTurnParam>"
        )
        acts_xml = _wpml_actions(i, len(wps), mode, gp)
        pt_xml = f"<Point><coordinates>{lon:.6f},{lat:.6f}</coordinates></Point><wpml:index>{i}</wpml:index>"

        tpl_pms.append(
            f"<Placemark>{pt_xml}"
            f"<wpml:ellipsoidHeight>{w_alt:.1f}</wpml:ellipsoidHeight><wpml:height>{w_alt:.1f}</wpml:height>"
            "<wpml:useGlobalHeight>0</wpml:useGlobalHeight><wpml:useGlobalSpeed>1</wpml:useGlobalSpeed>"
            f"<wpml:useGlobalHeadingParam>0</wpml:useGlobalHeadingParam>{hdg_xml}"
            f"<wpml:useGlobalTurnParam>1</wpml:useGlobalTurnParam>"
            f"<wpml:gimbalPitchAngle>{gp:.1f}</wpml:gimbalPitchAngle>"
            f"{acts_xml}</Placemark>"
        )
        wpl_pms.append(
            f"<Placemark>{pt_xml}"
            f"<wpml:executeHeight>{w_alt:.1f}</wpml:executeHeight><wpml:waypointSpeed>{spd:.1f}</wpml:waypointSpeed>"
            f"{hdg_xml}{turn_xml}<wpml:useStraightLine>1</wpml:useStraightLine>"
            f"{acts_xml}</Placemark>"
        )

    ns = 'xmlns="http://www.opengis.net/kml/2.2" xmlns:wpml="http://www.dji.com/wpmz/1.0.2"'
    tpl_xml = (
        f'<?xml version="1.0" encoding="UTF-8"?><kml {ns}><Document>'
        f"<wpml:author>Drone Manager</wpml:author><wpml:createTime>{now_ms}</wpml:createTime><wpml:updateTime>{now_ms}</wpml:updateTime>"
        f"{m_cfg}<Folder><wpml:templateType>waypoint</wpml:templateType><wpml:templateId>0</wpml:templateId>"
        "<wpml:waylineCoordinateSysParam><wpml:coordinateMode>WGS84</wpml:coordinateMode>"
        "<wpml:heightMode>relativeToStartPoint</wpml:heightMode>"
        f"<wpml:globalShootHeight>{alt_def:.1f}</wpml:globalShootHeight></wpml:waylineCoordinateSysParam>"
        f"<wpml:autoFlightSpeed>{spd:.1f}</wpml:autoFlightSpeed>"
        f"<wpml:globalWaypointTurnMode>{turn_mode}</wpml:globalWaypointTurnMode>"
        "<wpml:globalUseStraightLine>1</wpml:globalUseStraightLine>"
        "<wpml:gimbalPitchMode>usePointSetting</wpml:gimbalPitchMode>"
        f"{''.join(tpl_pms)}</Folder></Document></kml>"
    )
    wpl_xml = (
        f'<?xml version="1.0" encoding="UTF-8"?><kml {ns}><Document>'
        f"{m_cfg}<Folder><wpml:templateId>0</wpml:templateId>"
        "<wpml:executeHeightMode>relativeToStartPoint</wpml:executeHeightMode><wpml:waylineId>0</wpml:waylineId>"
        f"<wpml:distance>{float(m.get('distance') or 0):.1f}</wpml:distance>"
        f"<wpml:duration>{float(m.get('duration') or 0):.1f}</wpml:duration>"
        f"<wpml:autoFlightSpeed>{spd:.1f}</wpml:autoFlightSpeed>"
        f"{''.join(wpl_pms)}</Folder></Document></kml>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("wpmz/template.kml", tpl_xml.encode("utf-8"))
        zf.writestr("wpmz/waylines.wpml", wpl_xml.encode("utf-8"))
    return buf.getvalue()

@app.get("/api/missions/{mid}/kmz")
def mission_kmz(mid: int):
    m = _get_mission(mid)
    return attach(_build_wpml_kmz(m), f"missao_{mid}.kmz", "application/vnd.google-earth.kmz")

class RootIn(BaseModel):
    name: str = ""
    path: str
    tag: str = ""
    recursive: int = 1

class RootPatch(BaseModel):
    name: Optional[str] = None
    path: Optional[str] = None
    tag: Optional[str] = None
    recursive: Optional[int] = None
    enabled: Optional[int] = None
    remap: int = 0                      # 1 = a pasta mudou de lugar: atualiza o caminho das mídias já indexadas

def norm_path(p):
    """Aceita E:\\DJI ou /mnt/e/DJI e devolve sempre o caminho no formato do Linux (WSL)."""
    p = (p or "").strip().strip('"').strip("'")
    m = re.match(r"^([A-Za-z]):[\\/]*(.*)$", p)
    if m and os.name != "nt": p = "/mnt/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/")
    if not p: return ""
    return os.path.normpath(p).rstrip("/") or "/"

def to_win(p):
    m = re.match(r"^/mnt/([a-z])(?:/(.*))?$", p or "")
    return (m.group(1).upper() + ":\\" + (m.group(2) or "").replace("/", "\\")) if m else p

def root_out(r, c):
    p = r["path"]
    n = c.execute("SELECT COUNT(*) FROM media WHERE substr(path,1,?)=?", (len(p) + 1, p + "/")).fetchone()[0]
    return {**dict(r), "online": Path(p).is_dir(), "files": n, "win": to_win(p)}

ALLOWED_FS_PREFIXES = ("/mnt", "/media", "/run/media", "/Volumes", os.path.abspath("data/media"), os.path.abspath(str(MEDIA_DIR)))

_BLOCKED_WIN_PREFIXES = ("/mnt/c/windows", "/mnt/c/users", "/mnt/c/program files", "/mnt/c/program files (x86)", "/mnt/c/programdata")

def _is_allowed_fs_path(p: str) -> bool:
    rp = os.path.realpath(p)
    rpl = rp.lower()
    if any(rpl == b or rpl.startswith(b + "/") for b in _BLOCKED_WIN_PREFIXES):
        return False
    return any(rp == pref.rstrip("/") or rp.startswith(pref.rstrip("/") + "/") for pref in ALLOWED_FS_PREFIXES)

@app.get("/api/roots")
def roots_list():
    with db() as c: return [root_out(r, c) for r in c.execute("SELECT * FROM media_roots ORDER BY id").fetchall()]

@app.post("/api/roots")
def root_add(b: RootIn):
    p = norm_path(b.path)
    if not p or not _is_allowed_fs_path(p):
        raise HTTPException(403, "Caminho não permitido. Use apenas pastas dentro de discos montados (/mnt, /media, /run/media, /Volumes) ou data/media.")
    if not Path(p).is_dir(): raise HTTPException(400, "Pasta não encontrada: " + (p or "(vazio)") + ". O disco está conectado?")
    with db() as c:
        for o in c.execute("SELECT path, recursive FROM media_roots").fetchall():
            if (p + "/").startswith(o["path"] + "/") and (o["path"] == p or o["recursive"]):
                raise HTTPException(400, "Esta pasta já está na lista, ou dentro de uma pasta cadastrada que inclui as subpastas (" + to_win(o["path"]) + ").")
        cur = c.execute("INSERT INTO media_roots(name,path,tag,recursive,enabled) VALUES(?,?,?,?,1)",
                        (b.name.strip() or Path(p).name or p, p, b.tag.strip(), 1 if b.recursive else 0))
        return root_out(c.execute("SELECT * FROM media_roots WHERE id=?", (cur.lastrowid,)).fetchone(), c)

@app.patch("/api/roots/{rid}")
def root_edit(rid: int, b: RootPatch):
    with db() as c:
        old = c.execute("SELECT * FROM media_roots WHERE id=?", (rid,)).fetchone()
        if not old: raise HTTPException(404, "pasta não encontrada")
        p = norm_path(b.path) if b.path else old["path"]
        if p != old["path"]:
            if not _is_allowed_fs_path(p):
                raise HTTPException(403, "Caminho não permitido. Use apenas pastas dentro de discos montados (/mnt, /media, /run/media, /Volumes) ou data/media.")
            if not Path(p).is_dir(): raise HTTPException(400, "Pasta não encontrada: " + p + ". O disco está conectado?")
            if c.execute("SELECT 1 FROM media_roots WHERE path=? AND id<>?", (p, rid)).fetchone():
                raise HTTPException(400, "Já existe outra entrada com este caminho.")
            if b.remap:
                o = old["path"] + "/"
                c.execute("UPDATE OR IGNORE media SET path = ? || substr(path, ?) WHERE substr(path,1,?)=?", (p + "/", len(o) + 1, len(o), o))
        c.execute("UPDATE media_roots SET name=?, path=?, tag=?, recursive=?, enabled=? WHERE id=?",
                  ((b.name.strip() if b.name is not None else old["name"]) or Path(p).name or p, p,
                   b.tag.strip() if b.tag is not None else old["tag"],
                   (1 if b.recursive else 0) if b.recursive is not None else old["recursive"],
                   (1 if b.enabled else 0) if b.enabled is not None else old["enabled"], rid))
        return root_out(c.execute("SELECT * FROM media_roots WHERE id=?", (rid,)).fetchone(), c)

@app.delete("/api/roots/{rid}")
def root_del(rid: int, forget: int = 0):
    """Tira a pasta da lista. Nunca apaga arquivos do disco; 'forget' também esquece as mídias dela no app."""
    ids = []
    with db() as c:
        r = c.execute("SELECT * FROM media_roots WHERE id=?", (rid,)).fetchone()
        if not r: raise HTTPException(404, "pasta não encontrada")
        if forget:
            pre = r["path"] + "/"
            ids = [x[0] for x in c.execute("SELECT id FROM media WHERE substr(path,1,?)=?", (len(pre), pre))]
            c.execute("DELETE FROM media WHERE substr(path,1,?)=?", (len(pre), pre))
        c.execute("DELETE FROM media_roots WHERE id=?", (rid,))
    for i in ids: (THUMBS / f"{i}.jpg").unlink(missing_ok=True)
    return {"ok": True, "forgotten": len(ids)}

@app.get("/api/fs/list")
def fs_list(path: str = ""):
    """Navegação de pastas do computador, para escolher a pasta no app (só lista pastas, nunca arquivos)."""
    p = norm_path(path)
    if not p:
        dirs = []
        mnt = Path("/mnt")
        if mnt.is_dir():
            dirs.extend({"name": d.name.upper() + ":", "path": str(d)} for d in sorted(mnt.iterdir()) if len(d.name) == 1 and d.is_dir())
        for base_p in (Path("/media"), Path("/run/media"), Path("/Volumes")):
            if base_p.is_dir():
                for d in sorted(base_p.iterdir(), key=lambda z: z.name.lower()):
                    if d.is_dir() and not d.name.startswith("."):
                        dirs.append({"name": d.name, "path": str(d)})
        dm = Path("data/media").resolve()
        if dm.is_dir():
            dirs.append({"name": "data/media (local)", "path": str(dm)})
        return {"path": "", "win": "", "parent": None, "dirs": dirs}
    if not _is_allowed_fs_path(p):
        raise HTTPException(403, "Acesso permitido apenas aos discos montados e à pasta data/media")
    d = Path(p)
    if not d.is_dir(): raise HTTPException(400, "Pasta não encontrada")
    dirs = []
    try:
        for x in sorted(d.iterdir(), key=lambda z: z.name.lower()):
            if x.name.startswith((".", "$")): continue
            try:
                if x.is_dir(): dirs.append({"name": x.name, "path": str(x)})
            except OSError: pass
            if len(dirs) >= 500: break
    except PermissionError: raise HTTPException(403, "Sem permissão para abrir esta pasta")
    top = str(d) in ("/", "/mnt", "/media", "/run/media", "/Volumes") or bool(re.fullmatch(r"/mnt/[a-z]", str(d))) or str(d) == os.path.abspath("data/media")
    return {"path": str(d), "win": to_win(str(d)), "parent": "" if top else str(d.parent), "dirs": dirs}

@app.post("/api/backup")
def run_backup():
    """Executa o script backup.sh em modo não interativo (--no-pause) e retorna o log."""
    script = Path(__file__).resolve().parent / "backup.sh"
    if not script.is_file():
        raise HTTPException(404, "Script backup.sh não encontrado")
    try:
        res = subprocess.run(
            ["bash", str(script), "--no-pause"],
            capture_output=True, text=True, timeout=120, check=False
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Tempo limite excedido ao executar o backup")
    out = ((res.stdout or "") + ("\n" + res.stderr if res.stderr else "")).strip()
    if res.returncode != 0:
        raise HTTPException(500, out or "Falha ao executar o backup")
    return {"ok": True, "output": out}

# ---------- assistente de importação de cartão SD ----------
class SdPreviewIn(BaseModel):
    source_path: str

class SdImportItem(BaseModel):
    src: str
    folder: str

class SdImportIn(BaseModel):
    dest_base: str
    items: list[SdImportItem]
    run_scan: bool = True

def _safe_folder_name(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', "-", (name or "").strip())
    s = re.sub(r"\s+", "_", s).strip("._- ")
    if not s or s in (".", ".."):
        return "Importado"
    return s[:80]

def _find_sd_candidates() -> list[dict]:
    out, seen = [], set()
    roots = []
    mnt = Path("/mnt")
    if mnt.is_dir():
        roots.extend(d for d in sorted(mnt.iterdir()) if len(d.name) == 1 and d.is_dir())
    for base in (Path("/media"), Path("/run/media"), Path("/Volumes")):
        if base.is_dir():
            for d in sorted(base.iterdir(), key=lambda z: z.name.lower()):
                if d.is_dir() and not d.name.startswith("."):
                    roots.append(d)
                    if base != Path("/Volumes"):
                        try:
                            roots.extend(sd for sd in sorted(d.iterdir(), key=lambda z: z.name.lower()) if sd.is_dir() and not sd.name.startswith("."))
                        except OSError:
                            pass
    for rt in roots:
        for sub in ("DCIM", "dcim"):
            dcim = rt / sub
            try:
                if dcim.is_dir() and str(dcim) not in seen and _is_allowed_fs_path(str(dcim)):
                    seen.add(str(dcim))
                    out.append({"name": rt.name.upper() + ":\\" + sub if rt.parent == mnt else f"{rt.name}/{sub}",
                                "path": str(dcim), "win": to_win(str(dcim))})
            except OSError:
                pass
    return out

def _resolve_place_for_sd(lat, lon, geo_cache: dict, known_places: list) -> str:
    if not valid_gps(lat, lon):
        return ""
    key = (round(float(lat), 2), round(float(lon), 2))
    if key in geo_cache:
        return geo_cache[key]
    best_name, best_d = "", 2.5
    for plat, plon, pname in known_places:
        d = hav(lat, lon, plat, plon)
        if d < best_d:
            best_d, best_name = d, pname
    if not best_name:
        if geo_cache:
            time.sleep(1.0)
        best_name = geocode(lat, lon)
    geo_cache[key] = best_name
    return best_name

def _unique_dest_path(target_dir: Path, filename: str, src_hash: str) -> tuple[Path, bool]:
    """Retorna (caminho_destino, ja_existe_identico)."""
    dst = target_dir / filename
    if not dst.exists():
        return dst, False
    try:
        if sha1(dst) == src_hash:
            return dst, True
    except OSError:
        pass
    stem, ext = dst.stem, dst.suffix
    idx = 1
    while True:
        cand = target_dir / f"{stem}_{idx}{ext}"
        if not cand.exists():
            return cand, False
        try:
            if sha1(cand) == src_hash:
                return cand, True
        except OSError:
            pass
        idx += 1

@app.get("/api/sd/detect")
def sd_detect():
    return {"candidates": _find_sd_candidates()}

@app.post("/api/sd/preview")
def sd_preview(b: SdPreviewIn):
    p = norm_path(b.source_path)
    if not p or not _is_allowed_fs_path(p):
        raise HTTPException(403, "Caminho não permitido. Use apenas pastas dentro de discos montados ou data/media.")
    src_dir = Path(p)
    if not src_dir.is_dir():
        raise HTTPException(400, "Pasta de origem não encontrada: " + (p or "(vazio)"))
    with db() as c:
        known_hashes = {r[0]: r[1] for r in c.execute("SELECT sha1, path FROM media WHERE sha1 IS NOT NULL AND sha1 != ''").fetchall()}
        known_places = [(r[0], r[1], r[2]) for r in c.execute(
            "SELECT lat, lon, name FROM spots WHERE lat IS NOT NULL AND lon IS NOT NULL AND name != '' "
            "UNION ALL SELECT lat, lon, place FROM flights WHERE lat IS NOT NULL AND lon IS NOT NULL AND place != ''"
        ).fetchall()]

    all_media = sorted(f for f in src_dir.rglob("*") if f.is_file() and f.suffix.lower() in (IMG | VID))
    new_files, existing_count = [], 0
    for f in all_media:
        try:
            h = sha1(f)
        except OSError:
            continue
        if h in known_hashes:
            existing_count += 1
        else:
            new_files.append((f, h))

    ex = {}
    for i in range(0, len(new_files), 200):
        try:
            ex.update(exif_batch([str(f) for f, _ in new_files[i:i + 200]]))
        except Exception:
            pass

    geo_cache, items, groups = {}, [], {}
    srt_count, new_bytes = 0, 0
    for f, h in new_files:
        x = ex.get(str(f), {})
        kind = "photo" if f.suffix.lower() in IMG else "video"
        taken = parse_dt(x.get("DateTimeOriginal") or x.get("CreateDate"))
        lat, lon = x.get("GPSLatitude"), x.get("GPSLongitude")
        has_srt = False
        if kind == "video":
            has_srt = any(f.with_suffix(ext).is_file() for ext in (".SRT", ".srt"))
            s = read_srt(f) if has_srt else None
            if s:
                lat = s["lat"] or lat
                lon = s["lon"] or lon
                taken = s["taken"] or taken
            elif taken:
                taken += timedelta(hours=TZ_OFFSET)
        if not valid_gps(lat, lon):
            lat, lon = None, None
        try:
            st = f.stat()
            size = st.st_size
            taken = taken or datetime.fromtimestamp(st.st_mtime)
        except OSError:
            size = 0
            taken = taken or datetime.now()
        date_str = taken.strftime("%Y-%m-%d")
        place = _resolve_place_for_sd(lat, lon, geo_cache, known_places)
        folder = f"{date_str}_{_safe_folder_name(place)}" if place else date_str
        new_bytes += size
        if has_srt:
            srt_count += 1
        items.append({
            "src": str(f), "name": f.name, "kind": kind, "size": size,
            "taken": taken.isoformat(timespec="seconds"), "date": date_str,
            "place": place, "folder": folder, "has_srt": has_srt, "sha1": h
        })
        g_ent = groups.setdefault(folder, {"folder": folder, "date": date_str, "place": place, "photos": 0, "videos": 0, "srts": 0, "bytes": 0})
        g_ent["photos" if kind == "photo" else "videos"] += 1
        if has_srt:
            g_ent["srts"] += 1
        g_ent["bytes"] += size

    items.sort(key=lambda z: (z["taken"], z["name"]))
    group_list = sorted(groups.values(), key=lambda z: z["folder"])
    return {
        "source": str(src_dir), "source_win": to_win(str(src_dir)),
        "total_found": len(all_media), "new_count": len(items), "existing_count": existing_count,
        "new_bytes": new_bytes, "srt_count": srt_count, "groups": group_list, "items": items
    }

@app.post("/api/sd/import")
def sd_import(b: SdImportIn):
    dst_p = norm_path(b.dest_base)
    if not dst_p or not _is_allowed_fs_path(dst_p):
        raise HTTPException(403, "Pasta de destino não permitida.")
    dst_root = Path(dst_p)
    if not dst_root.is_dir():
        raise HTTPException(400, "Pasta de destino não encontrada: " + (dst_p or "(vazio)"))
    if not b.items:
        raise HTTPException(400, "Nenhum arquivo selecionado para importar.")

    copied, skipped, srt_copied, copied_bytes = 0, 0, 0, 0
    folders_used, errors = set(), []
    for it in b.items:
        sp = norm_path(it.src)
        if not sp or not _is_allowed_fs_path(sp):
            errors.append(f"Caminho ignorado (não permitido): {it.src}")
            continue
        src_f = Path(sp)
        if not src_f.is_file() or src_f.suffix.lower() not in (IMG | VID):
            errors.append(f"Arquivo inválido ou ausente: {src_f.name}")
            continue
        folder_name = _safe_folder_name(it.folder)
        target_dir = dst_root / folder_name
        target_dir.mkdir(parents=True, exist_ok=True)
        folders_used.add(folder_name)
        try:
            src_hash = sha1(src_f)
            out_f, same = _unique_dest_path(target_dir, src_f.name, src_hash)
            if same:
                skipped += 1
            else:
                shutil.copy2(src_f, out_f)
                copied += 1
                copied_bytes += out_f.stat().st_size
            if src_f.suffix.lower() in VID:
                for ext in (".SRT", ".srt"):
                    srt_src = src_f.with_suffix(ext)
                    if srt_src.is_file():
                        srt_dst = out_f.with_suffix(ext)
                        if not srt_dst.exists() or srt_dst.stat().st_size != srt_src.stat().st_size:
                            shutil.copy2(srt_src, srt_dst)
                            srt_copied += 1
                        break
        except Exception as e:
            errors.append(f"{src_f.name}: {e}")

    scan_res = scan(force=0) if b.run_scan else None
    return {
        "ok": True, "copied": copied, "skipped": skipped, "srt_copied": srt_copied,
        "copied_bytes": copied_bytes, "folders": sorted(folders_used),
        "errors": errors, "scan": scan_res
    }

_KP_CACHE = {"ts": 0.0, "val": None}

def _kp_index():
    """Obtém o índice geomagnético Kp atual (NOAA SWPC) com cache de 30 min; retorna float ou None."""
    now = time.time()
    if now - _KP_CACHE["ts"] < 1800 and _KP_CACHE["val"] is not None:
        return _KP_CACHE["val"]
    try:
        req = urllib.request.Request("https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json",
                                     headers={"User-Agent": "drone-manager-local/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            rows = json.loads(r.read())
        if isinstance(rows, list) and len(rows) > 1:
            last = rows[-1]
            val = float(last.get("Kp") if isinstance(last, dict) else last[1])
            _KP_CACHE["ts"], _KP_CACHE["val"] = now, round(val, 1)
            return _KP_CACHE["val"]
    except Exception:
        pass
    return _KP_CACHE["val"]

def _eval_flight_hour(rain, rain_prob, w10, w80, g10, vis, kp_val, near_now):
    """Avalia condição horária para voo (0=Ideal, 1=Atenção, 2=Não voar) considerando limites do DJI Mini 3."""
    sev, reasons = 0, []
    def flag(s, msg):
        nonlocal sev
        if s > sev: sev = s
        reasons.append(msg)

    if rain >= 0.5 or (rain_prob >= 70 and rain >= 0.1): flag(2, f"Chuva ({rain:.1f} mm/h)")
    elif rain > 0 or rain_prob >= 40: flag(1, f"Risco de chuva ({rain_prob}%)")

    if w10 >= 10.5 or w80 >= 13.0: flag(2, f"Vento forte ({w10:.1f} m/s em 10m / {w80:.1f} m/s em 80m)")
    elif w10 >= 7.0 or w80 >= 9.5: flag(1, f"Vento moderado ({w10:.1f} m/s em 10m / {w80:.1f} m/s em 80m)")

    if g10 >= 13.5: flag(2, f"Rajadas fortes ({g10:.1f} m/s)")
    elif g10 >= 10.0: flag(1, f"Rajadas ({g10:.1f} m/s)")

    if vis is not None:
        if vis < 1000: flag(2, f"Baixa visibilidade ({vis/1000:.1f} km)")
        elif vis < 3000: flag(1, f"Visibilidade reduzida ({vis/1000:.1f} km)")

    if near_now and kp_val is not None and kp_val >= 5.0:
        flag(1, f"Índice Kp elevado ({kp_val:.1f} — atenção GPS)")
    return sev, reasons

@app.get("/api/weather/forecast")
def weather_forecast(lat: float, lon: float, days: int = 3):
    """Previsão horária de janela de voo (Open-Meteo + Kp NOAA) para um ponto no mapa."""
    if not valid_gps(lat, lon):
        raise HTTPException(400, "Coordenadas inválidas")
    days = max(1, min(7, int(days)))
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat:.6f}&longitude={lon:.6f}&forecast_days={days}&timezone=auto&wind_speed_unit=ms"
        "&hourly=temperature_2m,precipitation,precipitation_probability,cloud_cover,"
        "wind_speed_10m,wind_gusts_10m,wind_direction_10m,wind_speed_80m,wind_speed_120m,visibility"
        "&daily=sunrise,sunset"
    )
    try:
        j = http_json(url)
    except Exception as e:
        raise HTTPException(502, f"Não foi possível consultar a previsão do tempo: {e}")

    kp_val = _kp_index()
    dly = j.get("daily") or {}
    sun_map, daily = {}, []
    for i, d in enumerate(dly.get("time") or []):
        sr = (dly.get("sunrise") or [None])[i] if i < len(dly.get("sunrise") or []) else None
        ss = (dly.get("sunset") or [None])[i] if i < len(dly.get("sunset") or []) else None
        sun_map[d] = (sr, ss)
        daily.append({"date": d, "sunrise": sr[11:16] if sr else None, "sunset": ss[11:16] if ss else None})

    h = j.get("hourly") or {}
    times = h.get("time") or []
    def _v(key, idx, default=0.0):
        arr = h.get(key)
        if not arr or idx >= len(arr) or arr[idx] is None: return default
        return arr[idx]

    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M")
    hours, windows, cur_w = [], [], None
    for i, t in enumerate(times):
        d = t[:10]
        sr, ss = sun_map.get(d, (None, None))
        is_day = bool(sr and ss and sr[:16] <= t[:16] <= ss[:16])
        temp = round(float(_v("temperature_2m", i, 0.0)), 1)
        rain = round(float(_v("precipitation", i, 0.0)), 2)
        rain_prob = int(_v("precipitation_probability", i, 0))
        clouds = int(_v("cloud_cover", i, 0))
        vis = _v("visibility", i, None)
        w10 = round(float(_v("wind_speed_10m", i, 0.0)), 1)
        g10 = round(float(_v("wind_gusts_10m", i, 0.0)), 1)
        dir10 = int(_v("wind_direction_10m", i, 0))
        w80 = round(float(_v("wind_speed_80m", i, w10)), 1)
        w120 = round(float(_v("wind_speed_120m", i, w80)), 1)
        near_now = t >= now_str and i < 18
        sev, reasons = _eval_flight_hour(rain, rain_prob, w10, w80, g10, vis, kp_val, near_now)
        row = {
            "time": t, "temp": temp, "rain": rain, "rain_prob": rain_prob, "clouds": clouds,
            "vis_km": round(vis / 1000.0, 1) if vis is not None else None,
            "wind10": w10, "gust10": g10, "dir10": dir10, "dir10_txt": _cardinal(dir10),
            "wind80": w80, "wind120": w120, "is_day": is_day, "sev": sev, "reasons": reasons
        }
        hours.append(row)
        # Agrupa melhores janelas diurnas futuras (sev <= 1)
        if t >= now_str[:13] and is_day and sev <= 1:
            if cur_w and cur_w["date"] == d and cur_w["sev"] == sev and cur_w["last_i"] == i - 1:
                cur_w["end"] = t[11:16]
                cur_w["last_i"] = i
                cur_w["max_w10"] = max(cur_w["max_w10"], w10)
                cur_w["max_g10"] = max(cur_w["max_g10"], g10)
                cur_w["max_w80"] = max(cur_w["max_w80"], w80)
            else:
                if cur_w: windows.append(cur_w)
                cur_w = {"date": d, "start": t[11:16], "end": t[11:16], "sev": sev,
                         "last_i": i, "max_w10": w10, "max_g10": g10, "max_w80": w80}
        else:
            if cur_w: windows.append(cur_w); cur_w = None
    if cur_w: windows.append(cur_w)

    windows.sort(key=lambda x: (x["sev"], x["date"], x["start"]))
    kp_obj = None
    if kp_val is not None:
        kp_obj = {"value": kp_val, "status": "Alto (atenção GPS)" if kp_val >= 5 else "Moderado" if kp_val >= 4 else "Normal"}
    return {"lat": round(lat, 5), "lon": round(lon, 5), "timezone": j.get("timezone", ""),
            "kp": kp_obj, "daily": daily, "hours": hours, "best_windows": windows[:5]}

app.mount("/", StaticFiles(directory="static", html=True), name="static")
