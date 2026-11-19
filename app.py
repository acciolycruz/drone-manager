"""Drone Manager (DJI Mini 3) - app web local.  Rodar: uvicorn app:app --reload"""
import os, re, json, csv, io, time, html, sqlite3, hashlib, subprocess, zipfile, tempfile, threading, urllib.request, urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from math import radians, degrees, sin, cos, tan, asin, acos, atan2, sqrt, hypot
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

def db():
    c = sqlite3.connect("data/drone.db"); c.row_factory = sqlite3.Row; return c
with db() as _c: _c.executescript(SCHEMA)
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
    for col, ddl in (("manual", "INTEGER DEFAULT 0"), ("sync_adj", "REAL DEFAULT 0"), ("meta", "TEXT"), ("detections", "TEXT")):
        if col not in mcols: _c.execute(f"ALTER TABLE media ADD COLUMN {col} {ddl}")
    _c.execute("CREATE TABLE IF NOT EXISTS missions(id INTEGER PRIMARY KEY, name TEXT NOT NULL, waypoints TEXT NOT NULL, "
               "alt REAL DEFAULT 60, speed REAL DEFAULT 8, notes TEXT DEFAULT '', air TEXT, created TEXT)")
    _c.execute("CREATE INDEX IF NOT EXISTS idx_media_flight_id ON media(flight_id)")
    _c.execute("CREATE INDEX IF NOT EXISTS idx_media_taken ON media(taken)")
    _c.execute("CREATE INDEX IF NOT EXISTS idx_flights_start ON flights(start)")

# ---------- utilidades ----------
def g(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict) or k not in d: return default
        d = d[k]
    return default if d is None else d

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

def import_log(p):
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
    if not rows: raise RuntimeError("log sem pontos de GPS")
    real_time = sum(x["ep"] is not None for x in rows) >= len(rows) / 2     # usa uma única fonte de hora
    if real_time: rows = [x for x in rows if x["ep"] is not None]
    for x in rows: x["t"] = x["ep"] if real_time else (x["ft"] if x["ft"] is not None else x["i"] * 0.1)
    air = [i for i, x in enumerate(rows) if x["air"]]
    if not air: raise RuntimeError("sem decolagem (o drone ficou no solo)")
    rows = rows[air[0]: air[-1] + 1]                    # só o tempo no ar
    t0 = rows[0]["t"]
    rcs = [x["rc"] for x in rows if isinstance(x["rc"], (int, float))]
    use_rc = bool(rcs) and max(rcs) > 50                # só confia no sinal se a escala parecer 0-100
    track, events, flags, warns = [], [], set(), set()
    dist = maxalt = maxspd = max_hdist = 0.0; prev = None; last = -1; nsig = photos = 0
    st0 = cand = low0 = home0 = rth_h = None; cand_t = 0; sig_low = vid0 = pho0 = False
    mech = {"vib_s": 0, "force_s": 0, "comp_s": 0, "gimb_s": 0}
    wind_samples, watts_list, amps_list = [], [], []
    for x in rows:
        sec = int(x["t"] - t0)
        maxalt, maxspd = max(maxalt, x["alt"]), max(maxspd, x["spd"])
        ev = lambda label: events.append({"t": sec, "label": label})
        st = x["state"]
        if st:                                          # um modo só vale se durar pelo menos 2 s
            if st != cand: cand, cand_t = st, sec
            if cand != st0 and sec - cand_t >= 2:
                if st0 not in (None, "AutoTakeoff", "EngineStart") and cand in STATE_PT:
                    events.append({"t": cand_t, "label": STATE_PT[cand]})
                st0 = cand
        act = x["act"]
        if act and act != getattr(import_log, "_a", None) and act in ACTION_PT and sec >= 0: ev(ACTION_PT[act])
        import_log._a = act
        bat = x["bat"]
        for th in (20, 10):
            if bat is not None and bat <= th and th not in flags: flags.add(th); ev(f"Bateria em {th}%")
        vw = str(x["vw"])
        if vw in ("1", "2") and vw not in flags:
            flags.add(vw); ev("Aviso de bateria baixa (nível 1)" if vw == "1" else "Aviso de bateria crítica (nível 2)")
        if x["sats"] is not None and x["sats"] < 8 and "gps" not in flags:
            flags.add("gps"); ev(f"Sinal GPS fraco ({x['sats']} satélites)")
        if use_rc and isinstance(x["rc"], (int, float)):
            if x["rc"] <= 20:
                if low0 is None: low0 = sec
                if sec - low0 >= 3 and low0 >= 10 and not sig_low and nsig < 5:
                    sig_low = True; nsig += 1; events.append({"t": low0, "label": f"Sinal do controle fraco ({x['rc']}%)"})
            else:
                low0 = None
                if x["rc"] > 50: sig_low = False
        if x["vid"] != vid0:
            ev("Início de gravação de vídeo" if x["vid"] else "Fim da gravação de vídeo"); vid0 = x["vid"]
        if x["pho"] and not pho0 and photos < 30: photos += 1; ev("Foto")
        pho0 = x["pho"]
        w = str(x["warn"] or "")
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
        if x["vib"] and "vib" not in flags: flags.add("vib"); ev("Alerta: Vibração nas hélices detectada")
        if x["noforce"] and "noforce" not in flags: flags.add("noforce"); ev("Alerta: Força de propulsão insuficiente")
        if x["comperr"] and "comperr" not in flags: flags.add("comperr"); ev("Alerta: Interferência na bússola")
        if x["motblk"] and "motblk" not in flags: flags.add("motblk"); ev("Alerta: Motor bloqueado")
        if x["gimbstk"] and "gimbstk" not in flags: flags.add("gimbstk"); ev("Alerta: Gimbal travado")
        if sec != last:                                  # 1 ponto por segundo no trajeto
            last = sec
            if prev: dist += dist_3d(prev[0], prev[1], prev[2], x["lat"], x["lon"], x["alt"])
            prev = (x["lat"], x["lon"], x["alt"])
            if x["vib"]: mech["vib_s"] += 1
            if x["noforce"]: mech["force_s"] += 1
            if x["comperr"]: mech["comp_s"] += 1
            if x["gimbstk"]: mech["gimb_s"] += 1
            # Estimativa de vento em altitude quando pairando (spd < 1.5 m/s e |zspd| < 0.8 m/s)
            pt, rl, yw = x.get("pitch"), x.get("roll"), x.get("yaw")
            if x["alt"] >= 5 and x["spd"] < 1.5 and abs(x["zspd"]) < 0.8 and all(isinstance(v, (int, float)) for v in (pt, rl, yw)):
                tilt = degrees(acos(max(-1.0, min(1.0, cos(radians(pt)) * cos(radians(rl))))))
                if 1.0 <= tilt <= 35.0:
                    w_spd = round(12.0 * sqrt(tan(radians(tilt))), 1)
                    w_dir = (yw + degrees(atan2(rl, -pt))) % 360
                    wind_samples.append((w_spd, w_dir))
            # Telemetria elétrica (V, A, W e células)
            bv, bcur = x.get("bv"), x.get("bcur")
            vv = (bv / 1000.0 if bv > 100 else float(bv)) if isinstance(bv, (int, float)) and bv > 0 else None
            aa = (abs(bcur) / 1000.0 if abs(bcur) > 100 else abs(float(bcur))) if isinstance(bcur, (int, float)) else None
            ww = round(vv * aa, 1) if (vv is not None and aa is not None) else None
            if ww is not None and ww > 0: watts_list.append(ww)
            if aa is not None and aa > 0: amps_list.append(aa)
            cells = [round(c / 1000.0 if c > 20 else float(c), 3) for c in (x.get("bcells") or [])[:4] if isinstance(c, (int, float)) and c > 0]
            pwr = [round(vv, 2) if vv else None, round(aa, 1) if aa is not None else None, ww, *cells] if (vv or aa or cells) else None
            stk = [x["thr"], x["rud"], x["ele"], x["ail"]] if any(x[k] is not None for k in ("thr", "rud", "ele", "ail")) else None
            track.append([sec, x["lat"], x["lon"], round(x["alt"], 1), round(x["spd"], 1), bat, x["sats"],
                          round(yw, 1) if isinstance(yw, (int, float)) else None,
                          round(x["gp"], 1) if isinstance(x["gp"], (int, float)) else None,
                          round(pt, 1) if isinstance(pt, (int, float)) else None,
                          round(rl, 1) if isinstance(rl, (int, float)) else None,
                          round(x["zspd"], 1) if isinstance(x["zspd"], (int, float)) else 0.0,
                          stk,
                          int(x["rc"]) if isinstance(x["rc"], (int, float)) else None,
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
        "drone": {"sn": dsn or "", "name": dname or "", "type": str(dtype or "DJI Mini 3"), "rc_sn": rcsn or "", "cam_sn": camsn or ""}
    }
    im = max(range(len(track)), key=lambda i: track[i][3])
    events = ([{"t": 0, "label": "Decolagem"}] + events +
              [{"t": track[-1][0], "label": "Pouso"}, {"t": track[im][0], "label": f"Altura máxima ({track[im][3]} m)"}])
    events.sort(key=lambda e: e["t"])
    start = datetime.fromtimestamp(t0, timezone.utc).isoformat() if real_time else None
    if not start:                                        # plano B: data e hora do nome do arquivo
        m = re.search(r"(\d{4}-\d\d-\d\d)_\[(\d\d)-(\d\d)-(\d\d)\]", p.name)
        if m:
            lt = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}")
            start = (lt - timedelta(hours=TZ_OFFSET)).isoformat() + "Z"
    with db() as c:
        old = c.execute("SELECT place, weather FROM flights WHERE file=?", (str(p),)).fetchone()
    place = (old["place"] if old and old["place"] else None) or geocode(track[0][1], track[0][2])
    if not (old and old["place"]): time.sleep(1)
    wx = (json.loads(old["weather"]) if old and old["weather"] and old["weather"] != "null" else None) or weather(track[0][1], track[0][2], start)
    with db() as c:
        c.execute("INSERT INTO flights(file,start,duration,distance,max_alt,max_speed,battery_sn,lat,lon,place,weather,track,events,bat,meta)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(file) DO UPDATE SET start=excluded.start,"
                  "duration=excluded.duration,distance=excluded.distance,max_alt=excluded.max_alt,max_speed=excluded.max_speed,"
                  "battery_sn=excluded.battery_sn,lat=excluded.lat,lon=excluded.lon,place=excluded.place,"
                  "weather=excluded.weather,track=excluded.track,events=excluded.events,bat=excluded.bat,meta=excluded.meta",
                  (str(p), start, track[-1][0], dist, maxalt, maxspd, bsn, track[0][1], track[0][2], place,
                   json.dumps(wx), json.dumps(track), json.dumps(events), json.dumps(binfo), json.dumps(fmeta)))

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
                        "-GPSAltitude", "-GPSAltitudeRef", "-ImageWidth", "-ImageHeight", "-Make", "-Model", "-ISO", "-ExposureTime",
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
    return {k: v for k, v in m.items() if v not in (None, "", False, 0)}

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
                if s: lat, lon, alt, taken = s["lat"] or lat, s["lon"] or lon, s["alt"] or alt, s["taken"] or taken
                elif taken: taken += timedelta(hours=TZ_OFFSET)   # CreateDate de MP4 costuma ser UTC
            if not valid_gps(lat, lon): lat, lon = None, None
            taken = taken or datetime.fromtimestamp(p.stat().st_mtime)
            meta = _build_media_meta(x, kind, w, h, s)
            c.execute("INSERT OR IGNORE INTO media(path,kind,taken,lat,lon,alt,w,h,size,sha1,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (str(p), kind, taken.isoformat(), lat, lon, alt, w, h, p.stat().st_size, sha1(p), json.dumps(meta)))
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
    if not BACKUP_DIR: return
    have = {(p.name, p.stat().st_size) for p in Path(BACKUP_DIR).rglob("*") if p.is_file()}
    with db() as c:
        for m in c.execute("SELECT id,path,size FROM media").fetchall():
            c.execute("UPDATE media SET backed=? WHERE id=?", (int((Path(m["path"]).name, m["size"]) in have), m["id"]))

# ---------- API ----------
app = FastAPI(title="Drone Manager")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

@app.middleware("http")
async def security_headers_and_csrf(request: Request, call_next):
    if request.method in ("POST", "PATCH", "DELETE"):
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if origin and not re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", origin):
            return Response("Origem não autorizada", status_code=403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp

def fl_out(r, full=False):
    d = dict(r)
    d["weather"] = json.loads(d["weather"]) if d["weather"] else None
    d["meta"] = json.loads(d["meta"]) if d.get("meta") else {}
    d["start_local"] = local(d["start"]).isoformat() if d["start"] else None
    if full:
        tr = json.loads(d["track"])
        d["track"], d["events"] = tr, json.loads(d["events"])
        # Fallback automático para Ponto Home (H) e distância máxima VLOS caso o log ainda não tenha sido reimportado
        if "home" not in d["meta"] and tr:
            h0 = (tr[0][1], tr[0][2])
            md = max((hav(h0[0], h0[1], p[1], p[2]) for p in tr), default=0.0)
            d["meta"]["home"] = {"lat": h0[0], "lon": h0[1], "rth_alt": None, "max_dist": round(md, 1)}
    else:
        d.pop("track", None); d.pop("events", None)
    return d

def get_flight(fid):
    with db() as c: r = c.execute("SELECT * FROM flights WHERE id=?", (fid,)).fetchone()
    if not r: raise HTTPException(404, "voo não encontrado")
    return r

def _enrich_media(d):
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
    for p in sorted(LOGS_DIR.rglob("*FlightRecord*.txt")):
        if str(p) in known: continue
        try: import_log(p); res["logs"] += 1
        except Exception as e: res["errors"].append(f"{p.name}: {e}")
    res["media"] = index_media(res["errors"]); link_media(); res["reconstruidos"] = reconstruct_flights(); check_backup()
    return res

@app.post("/api/logs/upload")
async def upload_log(request: Request, name: str = ""):
    """Recebe um arquivo de log (.txt) via streaming pelo navegador, salva em data/logs/ e o importa."""
    raw_name = Path(name or "").name
    safe_name = re.sub(r"[^A-Za-z0-9_.\-\[\]]", "_", raw_name)
    if not safe_name.lower().endswith(".txt") or len(safe_name) < 5:
        raise HTTPException(400, "Nome de arquivo inválido. Envie um arquivo de log .txt da DJI.")
    if "FlightRecord" not in safe_name:
        safe_name = "DJIFlightRecord_" + safe_name
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    dest = LOGS_DIR / safe_name
    size = 0
    max_bytes = 25 * 1024 * 1024  # limite de 25 MB por arquivo de log
    try:
        with open(dest, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_bytes:
                    f.close()
                    _unlink_quiet(dest)
                    raise HTTPException(413, "Arquivo excede o limite máximo de 25 MB.")
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        _unlink_quiet(dest)
        raise HTTPException(400, f"Erro ao salvar arquivo de log: {e}")
    if size < 64:
        _unlink_quiet(dest)
        raise HTTPException(400, "Arquivo de log vazio ou inválido.")
    try:
        import_log(dest)
        link_media()
        return {"ok": True, "file": safe_name, "bytes": size}
    except Exception as e:
        return {"ok": False, "file": safe_name, "error": str(e)}

@app.get("/api/config")
def config(): return {"airspace": AIRSPACE_TILES, "cesium": os.getenv("CESIUM_TOKEN", "")}

@app.get("/api/flights")
def flights(q: str = "", min_alt: float = 0, min_dur: float = 0, date_from: str = "", date_to: str = "", battery: str = "",
            drone: str = "", fav: int = 0, source: str = "", sort: str = "start", order: str = "desc"):
    with db() as c: rows = c.execute("SELECT * FROM flights").fetchall()
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
    key = sort if sort in ("start", "duration", "distance", "max_alt", "max_speed", "place", "name", "fav") else "start"
    def val(f):
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
def tracks():
    with db() as c: rows = c.execute("SELECT id,start,place,distance,max_alt,track,source,name,fav FROM flights").fetchall()
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

@app.get("/api/flights/{fid}/report", response_class=HTMLResponse)
def report(fid: int):
    f = fl_out(get_flight(fid), True); w = f["weather"]
    with db() as c: ms = c.execute("SELECT id FROM media WHERE flight_id=? AND kind='photo' LIMIT 12", (fid,)).fetchall()
    ev = "".join(f"<li>{e['t']//60}m{e['t']%60:02d}s — {html.escape(e['label'])}</li>" for e in f["events"])
    wx = f"{w['temperature_2m']} °C, vento {w['wind_speed_10m']} m/s (rajadas {w['wind_gusts_10m']}), chuva {w['precipitation']} mm" if w else "sem dados"
    th = "".join(f'<img src="/api/media/{m["id"]}/thumb" width=170>' for m in ms)
    return f"""<meta charset=utf-8><title>Voo {fid}</title><body style="font:15px system-ui;max-width:760px;margin:30px auto">
<button onclick=print() style="float:right">Imprimir / Salvar PDF</button>
<h2>{html.escape(f['name'] or 'Voo')} — {f['start_local'][:16].replace('T', ' ')} — {html.escape(f['place'] or '')}</h2>
<p>Duração {int(f['duration'])//60} min {int(f['duration'])%60} s · Distância {f['distance']/1000:.2f} km · Altura máx. {f['max_alt']:.0f} m ·
Vel. máx. {f['max_speed']*3.6:.0f} km/h · Bateria {html.escape(f['battery_sn'] or '—')}</p>
<p>Clima: {wx}</p><h3>Eventos</h3><ul>{ev}</ul><h3>Anotações</h3><p>{html.escape(f['notes'] or '—')}</p><div>{th}</div>"""

def _csv_safe(v):
    s = str(v if v is not None else "")
    return ("'" + s) if s.startswith(("=", "+", "-", "@", "\t", "\r")) else s

@app.get("/api/logbook.csv")
def logbook():
    with db() as c: rows = c.execute("SELECT * FROM flights WHERE source='log' ORDER BY start").fetchall()
    o = io.StringIO(); w = csv.writer(o)
    w.writerow(["data_hora_local", "local", "duracao_min", "distancia_km", "altura_max_m", "bateria_sn", "lat", "lon", "notas"])
    for r in rows:
        w.writerow([local(r["start"]).isoformat(" "), _csv_safe(r["place"]), round(r["duration"] / 60, 1), round(r["distance"] / 1000, 2),
                    round(r["max_alt"], 1), _csv_safe(r["battery_sn"]), r["lat"], r["lon"], _csv_safe(r["notes"])])
    return attach(o.getvalue(), "diario_de_voo.csv", "text/csv")

@app.get("/api/media")
def media(kind: str = "", q: str = "", fav: int = 0, flight_id: int = 0, geo: int = 0, unlinked: int = 0,
          orient: str = "", date_from: str = "", date_to: str = "", near: str = ""):
    sql, a = "SELECT m.*, f.place AS place FROM media m LEFT JOIN flights f ON f.id=m.flight_id WHERE 1=1", []
    for cond, val, arg in ((kind, " AND m.kind=?", kind), (fav, " AND m.fav=1", None), (flight_id, " AND m.flight_id=?", flight_id),
                           (unlinked, " AND m.flight_id IS NULL", None), (geo, " AND m.lat IS NOT NULL", None),
                           (date_from, " AND m.taken>=?", date_from), (date_to, " AND m.taken<=?", date_to + "~")):
        if cond:
            sql += val
            if arg is not None: a.append(arg)
    with db() as c: rows = [_enrich_media(dict(r)) for r in c.execute(sql + " ORDER BY m.taken DESC", a)]
    if orient:
        if orient == "4k": rows = [r for r in rows if r["is_4k"]]
        else: rows = [r for r in rows if r["orient"] == orient]
    if q: rows = [r for r in rows if q.lower() in f'{r["tags"]} {r["path"]} {r["place"] or ""}'.lower()]
    if near:   # "lat,lon,km"
        la, lo, km = map(float, near.split(","))
        rows = [r for r in rows if r["lat"] is not None and hav(la, lo, r["lat"], r["lon"]) <= km * 1000]
    with db() as c: offp = [r["path"].rstrip("/") + "/" for r in c.execute("SELECT path FROM media_roots") if not Path(r["path"]).is_dir()]
    for r in rows: r["offline"] = bool(offp) and any(r["path"].startswith(o) for o in offp)
    return rows

@app.get("/api/media/{mid}")
def media_one(mid: int): return get_media(mid)

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

class MediaPatch(BaseModel):
    tags: Optional[str] = None
    fav: Optional[int] = None
    flight_id: Optional[int] = None     # 0 = nenhum voo; -1 = automático (o app decide)
    sync_adj: Optional[float] = None    # ajuste fino da sincronia do vídeo, em segundos

@app.patch("/api/media/{mid}")
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
        if b.ids:
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
def detections_report(flight_id: int = 0, cls: str = "", format: str = "html"):
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
                    for k, v in sorted(totals.items(), key=lambda x: -x[1])) or "<i>Nenhum objeto detectado ainda.</i>"
    cards = []
    for e in entries:
        m = e["m"]
        objs = ", ".join(f"{html.escape(k)} ({v})" for k, v in e["summary"].items())
        gps_link = (f'<a href="https://www.google.com/maps?q={e["lat"]},{e["lon"]}" target="_blank">🗺 {e["lat"]:.5f}, {e["lon"]:.5f} ↗</a>'
                    if valid_gps(e["lat"], e["lon"]) else "Sem GPS")
        t_info = f" · Instante destaque: {int(e['best_t'])//60}:{int(e['best_t'])%60:02d}" if e["best_t"] is not None else ""
        cards.append(f"""<div style="display:flex;gap:14px;border:1px solid #cbd5e1;border-radius:8px;padding:10px;margin:10px 0;align-items:center">
          <img src="/api/media/{m['id']}/thumb" width="150" style="border-radius:6px;object-fit:cover">
          <div style="font-size:13.5px;line-height:1.5">
            <b>{'🎬 Vídeo' if m['kind']=='video' else '📷 Foto'} #{m['id']}</b> — {html.escape(str(m.get('taken') or '')[:16].replace('T',' '))} {html.escape(m.get('place') or '')}{t_info}<br>
            <b>Objetos:</b> {objs}<br>
            <b>Localização:</b> {gps_link}<br>
            <small style="color:#64748b">{html.escape(Path(m['path']).name)} · Tags: {html.escape(m.get('tags') or '')}</small>
          </div></div>""")
    body = f"""<!doctype html><meta charset="utf-8"><title>Relatório de Detecções (IA) — Drone Manager</title>
    <body style="font:15px system-ui;max-width:860px;margin:28px auto;color:#0f172a">
      <div style="float:right;display:flex;gap:8px">
        <a href="/api/detections/report?flight_id={flight_id}&cls={urllib.parse.quote(cls_filter)}&format=csv" style="padding:6px 12px;border:1px solid #94a3b8;border-radius:6px;text-decoration:none;color:#0f172a">⬇ Exportar CSV</a>
        <button onclick="print()" style="padding:6px 12px;cursor:pointer">🖨 Imprimir / Salvar PDF</button>
      </div>
      <h2>🤖 Relatório de Detecção de Objetos (IA)</h2>
      <p style="color:#475569">Mídias analisadas com detecções: <b>{len(entries)}</b>{f' · Voo #{flight_id}' if flight_id else ''}{f' · Filtro: {html.escape(cls_filter)}' if cls_filter else ''}</p>
      <div style="margin:12px 0">{chips}</div><hr>
      {''.join(cards) or '<p>Nenhuma detecção encontrada para os filtros informados.</p>'}
    </body>"""
    return HTMLResponse(body)

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

@app.get("/api/stats")
def stats():
    with db() as c:
        fl = [dict(r) for r in c.execute("SELECT id,start,duration,distance,max_alt,battery_sn,place,meta FROM flights WHERE source='log'")]
        m = [dict(r) for r in c.execute("SELECT kind,path,sha1,backed,flight_id FROM media")]
        row = c.execute("SELECT v FROM kv WHERE k='prop_base'").fetchone()
    hours = sum(f["duration"] for f in fl) / 3600; base = float(row[0]) if row else 0.0
    bats, drones, mech_alerts = {}, {}, []
    for f in fl:
        b = bats.setdefault(f["battery_sn"] or "desconhecida", {"sn": f["battery_sn"] or "desconhecida", "flights": 0, "hours": 0})
        b["flights"] += 1; b["hours"] += f["duration"] / 3600
        mt = json.loads(f["meta"]) if f.get("meta") else {}
        dr = mt.get("drone") or {}
        key = dr.get("sn") or dr.get("type") or "DJI Mini 3"
        dn = drones.setdefault(key, {"sn": dr.get("sn") or "—", "name": dr.get("name") or "", "type": dr.get("type") or "DJI Mini 3", "flights": 0, "hours": 0.0, "km": 0.0})
        dn["flights"] += 1; dn["hours"] += f["duration"] / 3600; dn["km"] += (f["distance"] or 0) / 1000
        mc = mt.get("mech") or {}
        if any(mc.get(k, 0) > 0 for k in ("vib_s", "force_s", "comp_s", "gimb_s")):
            mech_alerts.append({"id": f["id"], "start": local(f["start"]).isoformat()[:16].replace("T", " "), "place": f["place"] or "", **mc})
    dups = {}
    for x in m: dups.setdefault(x["sha1"], []).append(x["path"])
    return {"flights": len(fl), "hours": hours, "km": sum(f["distance"] for f in fl) / 1000,
            "photos": sum(x["kind"] == "photo" for x in m), "videos": sum(x["kind"] == "video" for x in m),
            "max_alt": max((f["max_alt"] for f in fl), default=0),
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

class Spot(BaseModel):
    name: str; lat: float; lon: float; notes: str = ""

@app.get("/api/spots")
def spots_list():
    with db() as c: return [dict(r) for r in c.execute("SELECT * FROM spots")]

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
    lats, lons = [p[1] for p in pts], [p[2] for p in pts]; pad = 0.15
    bb = (min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad)
    res = {"checked": datetime.now().isoformat(timespec="seconds"), "hits": [], "near": [], "errors": [], "sev": 0}; ok = 0
    for layer, label, sev in AIR_CHECK:
        try: feats = _wfs(layer, bb); ok += 1
        except Exception as e: res["errors"].append(layer + ": " + str(e)[:80]); continue
        for f in feats:
            props = f.get("properties") or {}; ts = []
            for poly in _polys(f.get("geometry")):
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

def _mission_out(r):
    d = dict(r)
    wps = json.loads(d["waypoints"] or "[]")
    d["waypoints"] = wps
    d["air"] = json.loads(d["air"]) if d.get("air") else None
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
    dur_s = round(dist / spd + len(wps) * 3.0 + climb_s)
    # Estimativa conservadora para DJI Mini 3 (~25 min de voo efetivo seguro com margem de vento)
    est_bat = min(100, max(3, round((dur_s / (25.0 * 60.0)) * 100)))
    d["distance"] = round(dist, 1)
    d["max_vlos"] = round(max_vlos, 1)
    d["duration"] = dur_s
    d["est_battery_pct"] = est_bat
    return d

def _get_mission(mid: int):
    with db() as c: r = c.execute("SELECT * FROM missions WHERE id=?", (mid,)).fetchone()
    if not r: raise HTTPException(404, "missão não encontrada")
    return _mission_out(r)

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
    with db() as c:
        cur = c.execute("INSERT INTO missions(name,waypoints,alt,speed,notes,created) VALUES(?,?,?,?,?,?)",
                        (b.name.strip() or "Missão planejada", json.dumps(wps), alt, spd, b.notes.strip(), now))
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
    feat = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"id": m["id"], "name": m["name"], "distance_m": m["distance"], "duration_s": m["duration"],
                           "est_battery_pct": m["est_battery_pct"], "max_vlos_m": m["max_vlos"], "crs": "EPSG:4326"},
            "geometry": {"type": "LineString", "coordinates": coords}
        }]
    }
    return attach(json.dumps(feat, ensure_ascii=False, indent=2), f"missao_{mid}.geojson", "application/geo+json")

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

def _is_allowed_fs_path(p: str) -> bool:
    rp = os.path.realpath(p)
    if rp.startswith(("/mnt/c/Windows", "/mnt/c/Users", "/mnt/c/Program Files", "/mnt/c/ProgramData")):
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

app.mount("/", StaticFiles(directory="static", html=True), name="static")
