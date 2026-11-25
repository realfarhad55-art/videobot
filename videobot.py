#!/usr/bin/env python3
"""videobot.py - WizardScan standalone video-generation bot.

Takes ALL ffmpeg video rendering off the overloaded main bot (/opt/bot).
The main bot runs at 95-156% CPU and 6.8 GB RAM; its video work is a
major part of that load:
  (a) champion X-alert base pre-render job, every 60s (ffmpeg, ~8-10s each)
  (b) the daily Most Shared video render (ffmpeg, timed out on 2026-11-25
      while the main bot was at 156% CPU, so the post went out text-only)
  (c) per-alert champion final videos (warm overlay compose, ~12-16s each)
  (d) rank-up videos (champion_rankup_video renderer)

This process does ONLY rendering. It never posts to Telegram, owns no
bot token and no userbot session, and never writes the main bot's data
files (tracked_calls.json, sent_milestones.json, bot_config.json, ...).

HOW IT WORKS (same file/queue pattern as monbot + dropbot):
- The main bot writes a small JSON request into /opt/videobot/queue/
  (one file per render) and polls for /opt/videobot/out/<id>.done.
- Videobot reads requests oldest-first (mostshared + champ_final first,
  they are time-sensitive; champ_base last, it is speculative), renders
  with the SAME renderer modules the main bot uses today (copied from
  live /opt/bot), and writes the finished .mp4 + a .done status JSON
  into /opt/videobot/out/. Then it deletes the request file.
- If videobot is down, the main bot does NOT wait: it checks the
  heartbeat file first and falls back immediately (text-only post /
  normal X media). A dead videobot can never wedge an alert.
- Champion bases go to the SAME dir the main bot has always used
  ($CHAMP_VIDEO_DIR/champ_bases, default /tmp/champ_bases), so a base
  rendered here is byte-compatible with the main bot's read path and
  with a rollback (the main bot's own prerender would find them).

REQUEST KINDS (JSON in queue/, one file per request):
- champ_base:  {"kind": "champ_base", "id": "base-gauge-red-10",
                "renderer": "gauge", "tier": "red", "x_val": 10,
                "base_path": "/tmp/champ_bases/champ-base-red-10x.mp4",
                "channel": "pumpwatch1", "points": 142}
  -> renders the channel-independent base (no overlay) to base_path.
- champ_final: {"kind": "champ_final", "id": "champ-pumpwatch1-10x-12345",
                "renderer": "gauge", "tier": "red", "x_val": 10,
                "channel": "pumpwatch1", "symbol": "PI", "chain": "SOL",
                "entry_fmt": "16.3K", "cur_fmt": "163K", "ca": "...",
                "stats": {"rank": 1, "points": 142, "calls": 117,
                          "win_rate": "42%"},
                "avatar_file": "pumpwatch1.jpg"}
  -> renders the final alert video (warm from the base when fresh,
     else a cold render, exactly like the main bot did) to
     /opt/videobot/out/<id>.mp4.
- mostshared: {"kind": "mostshared", "id": "ms-2026-11-26-020000",
               "tokens": [{"symbol": "PI", "chain": "SOL", "count": 5,
                           "ath_mc": 100000, "from_mc": 10000, "x": 10.0,
                           "logo_path": "/opt/videobot/shared/ms-logos/abc.bin"}]}
  -> calls most_shared_video.build_most_shared_video(tokens) with the
     logo bytes read from logo_path. Output to /opt/videobot/out/<id>.mp4.
- rankup:    {"kind": "rankup", "id": "rankup-pumpwatch1-1-12345",
              "channel": "pumpwatch1", "rank": 1, "points": 142,
              "calls": [{"symbol": "PI", "x": 10.0}], "title": "...",
              "avatar_file": "pumpwatch1.jpg"}
  -> calls champion_rankup_video.build_rankup_video(...).
     Output to /opt/videobot/out/<id>.mp4.

RENDERER MODULES (copied from live /opt/bot into /opt/videobot):
  champion_video.py, champion_gauge_neon.py, champion_green_trio.py,
  champion_rocket_neon.py, champion_cyberhud_orange.py,
  most_shared_video.py, champion_rankup_video.py (last two are VPS-only,
  they were never synced to GitHub; copy them from live /opt/bot).
  Fonts: Poppins-Bold.ttf / Poppins-BoldItalic.ttf if present in
  /opt/bot or /opt/bot/assets/fonts (the renderers fall back to
  DejaVuSans if the Poppins files are missing).
  most_shared_video and champion_rankup_video are OPTIONAL: if they
  are not installed, those kinds fail fast with a clear error and the
  main bot falls back (text-only Most Shared / no rank-up post, same
  as a failed build today). The champion renderers are REQUIRED.

CONFIG (.env in /opt/videobot, KEY=VALUE lines):
  CHAMP_VIDEO_DIR=/tmp        (MUST match the main bot's value;
                               check: grep CHAMP_VIDEO_DIR /opt/bot/.env)
  BOT_DATA_DIR=/opt/bot/data  (avatars live here, read-only)
  VIDEOBOT_DIR=/opt/videobot  (queue/out/shared/heartbeat live here)

USAGE:
  python3 videobot.py            (normal: queue loop + prune + heartbeat)
  python3 videobot.py --check    (self-test: render one gauge-red-2x base
                                  to a temp file and report timing, then exit)
  python3 videobot.py --once     (process a single queued request, then exit)

LOGS: stdout (journalctl -u videobot.service). No secrets are logged.
"""

import json
import os
import shutil
import sys
import threading
import time

# ---------------------------------------------------------------- config

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """Tiny .env loader (no python-dotenv dependency). Reads
    <videobot dir>/.env, sets os.environ defaults without overwriting
    anything already set. Missing file is fine."""
    try:
        path = os.path.join(_HERE, ".env")
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


_load_dotenv()

VIDEOBOT_DIR = os.environ.get("VIDEOBOT_DIR", "/opt/videobot")
CHAMP_VIDEO_DIR = os.environ.get("CHAMP_VIDEO_DIR", "/tmp")
BOT_DATA_DIR = os.environ.get("BOT_DATA_DIR", "/opt/bot/data")
QUEUE_DIR = os.path.join(VIDEOBOT_DIR, "queue")
OUT_DIR = os.path.join(VIDEOBOT_DIR, "out")
SHARED_DIR = os.path.join(VIDEOBOT_DIR, "shared")
MS_LOGOS_DIR = os.path.join(SHARED_DIR, "ms-logos")
HEARTBEAT_FILE = os.path.join(VIDEOBOT_DIR, "heartbeat.json")
BASES_DIR = os.path.join(CHAMP_VIDEO_DIR, "champ_bases")

# Same limits the main bot's prerender job used (do NOT change):
# max 6 bases kept, a base older than 30 min is stale.
PRERENDER_MAX_BASES = 6
PRERENDER_MAX_AGE = 1800  # 30 min

# Render timeouts (seconds). Same order as the old in-bot timeouts:
# Most Shared used 240s, champion final videos 120s.
TIMEOUTS = {"champ_base": 300, "champ_final": 180,
            "mostshared": 300, "rankup": 180}

# Video levels that get a video (CHAMPION_VIDEO_LEVELS, core.py).
CHAMPION_VIDEO_LEVELS = (2, 5, 10, 20, 30, 40, 50, 60, 80, 90, 100,
                         150, 200, 250, 300, 500, 600, 700, 800, 900, 1000)

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s videobot[%(process)d]: %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("videobot")

# ---------------------------------------------------------------- renderers
# Imported from this directory (copied from live /opt/bot). The champion
# modules are required; most_shared_video / champion_rankup_video are
# optional (those kinds fail fast with a clear error when missing).

sys.path.insert(0, _HERE)

champion_video = None
champion_green_trio = None
champion_rocket_neon = None
champion_cyberhud_orange = None
most_shared_video = None
champion_rankup_video = None
RENDERERS_OK = {}


def _try_import(name):
    global most_shared_video, champion_rankup_video
    global champion_video, champion_green_trio
    global champion_rocket_neon, champion_cyberhud_orange
    try:
        mod = __import__(name)
        RENDERERS_OK[name] = True
        log.info("renderer loaded: %s", name)
        return mod
    except Exception as e:
        RENDERERS_OK[name] = False
        log.warning("renderer NOT available: %s (%s: %s)",
                    name, type(e).__name__, e)
        return None


champion_video = _try_import("champion_video")
champion_green_trio = _try_import("champion_green_trio")
champion_rocket_neon = _try_import("champion_rocket_neon")
champion_cyberhud_orange = _try_import("champion_cyberhud_orange")
most_shared_video = _try_import("most_shared_video")
champion_rankup_video = _try_import("champion_rankup_video")

STATS = {"started": time.time(), "rendered": 0, "failed": 0,
         "last_kind": "", "last_ok": None}


# ---------------------------------------------------------------- helpers

def _ensure_dirs():
    for d in (QUEUE_DIR, OUT_DIR, SHARED_DIR, MS_LOGOS_DIR, BASES_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            log.error("cannot create %s: %s", d, e)


def _atomic_write_json(path, obj):
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _write_done(req_id, ok, path=None, error=None):
    _atomic_write_json(os.path.join(OUT_DIR, req_id + ".done"),
                       {"ok": bool(ok), "path": path, "error": error,
                        "ts": time.time()})


def _avatar_path(avatar_file):
    """Resolve a channel avatar from the main bot's data dir (read-only).
    Returns a path or None."""
    if not avatar_file:
        return None
    try:
        base = str(avatar_file).replace("/", "").replace("\\", "")
        p = os.path.join(BOT_DATA_DIR, "avatars", base)
        return p if os.path.exists(p) else None
    except Exception:
        return None


def _route_for_level(x_val):
    """Mirror of the main bot's champion video routing
    (_champion_video_try_send + champion_prerender_job, Nov 23 base).
    Returns (renderer, route-dict) or None when the level has no video."""
    try:
        x = int(x_val)
    except (TypeError, ValueError):
        return None
    if x in (2, 5, 10):
        tier = champion_video.tier_for(x) if champion_video else "red"
        return ("gauge", {"tier": tier})
    if x == 20:
        return ("rocket", {"tier": "blue"})
    if x == 30:
        return ("cyber", {"tier": "orange"})
    if x == 40:
        return ("trio", {"design": "matrix", "color": "green"})
    if x == 50:
        return ("trio", {"design": "orbit", "color": "green"})
    if x == 60:
        return ("trio", {"design": "area", "color": "purple"})
    if x in (80, 90):
        return ("trio", {"design": "area", "color": "green"})
    if x >= 100 and x in CHAMPION_VIDEO_LEVELS:
        return ("trio", {"design": "area", "color": "bw"})
    return None


def _base_path_for(renderer, route, x_val):
    """Base file path, using the renderer modules' own base_path_for
    (single source of truth, same filenames the main bot expects)."""
    try:
        if renderer == "gauge":
            return champion_video.base_path_for(route["tier"], x_val)
        if renderer == "rocket":
            return champion_rocket_neon.rocket_base_path_for(
                route["tier"], x_val)
        if renderer == "cyber":
            return champion_cyberhud_orange.cyber_base_path_for(
                route["tier"], x_val)
        if renderer == "trio":
            return champion_green_trio.trio_base_path_for(
                route["design"], route["color"], x_val)
    except Exception as e:
        log.warning("base_path_for failed (%s %s): %s", renderer, x_val, e)
    return None


def _fresh_base(path):
    try:
        return bool(path and os.path.exists(path)
                    and (time.time() - os.path.getmtime(path))
                    < PRERENDER_MAX_AGE)
    except Exception:
        return False


def _parse_mc(fmt):
    """Same helper the main bot uses (champion_video._parse_mc)."""
    try:
        if champion_video is not None:
            return float(champion_video._parse_mc(fmt) or 0)
    except Exception:
        pass
    try:
        s = str(fmt or "").strip().upper().replace("$", "").replace(",", "")
        if s.endswith("K"):
            return float(s[:-1]) * 1000.0
        if s.endswith("M"):
            return float(s[:-1]) * 1000000.0
        return float(s)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- renders
# Each render_* runs in a worker thread with a timeout (see _run_guarded).
# They raise on failure; the caller writes the .done status.

def _render_champ_base(req):
    renderer = req.get("renderer")
    x_val = int(req.get("x_val") or 0)
    base_path = req.get("base_path") or _base_path_for(
        renderer, req, x_val)
    if not base_path:
        raise RuntimeError("no base_path for %s %sx" % (renderer, x_val))
    if _fresh_base(base_path):
        return base_path  # already fresh, nothing to do
    if renderer == "gauge":
        if champion_video is None:
            raise RuntimeError("champion_video module not installed")
        champion_video.render_gauge_base(
            tier=req.get("tier"), x_val=x_val, out_path=base_path,
            crf=12, preset="veryfast")
    elif renderer == "rocket":
        if champion_rocket_neon is None:
            raise RuntimeError("champion_rocket_neon module not installed")
        champion_rocket_neon.render_rocket_base(
            tier=req.get("tier"), x_val=x_val, out_path=base_path,
            crf=12, preset="veryfast")
    elif renderer == "cyber":
        if champion_cyberhud_orange is None:
            raise RuntimeError(
                "champion_cyberhud_orange module not installed")
        champion_cyberhud_orange.render_cyber_base(
            tier=req.get("tier"), x_val=x_val, out_path=base_path,
            crf=12, preset="veryfast")
    elif renderer == "trio":
        if champion_green_trio is None:
            raise RuntimeError("champion_green_trio module not installed")
        champion_green_trio.render_trio_base(
            design=req.get("design"), color=req.get("color"), x_val=x_val,
            out_path=base_path, crf=12, preset="veryfast")
    else:
        raise RuntimeError("unknown renderer %r" % (renderer,))
    if not os.path.exists(base_path) or os.path.getsize(base_path) <= 0:
        raise RuntimeError("base render produced no file: %s" % base_path)
    return base_path


def _render_champ_final(req):
    """Final per-alert video: warm overlay compose from the pre-rendered
    base when fresh, else a cold render. Mirrors exactly what the main
    bot's _champion_video_try_send did, only in this process."""
    x_val = int(req.get("x_val") or 0)
    renderer = req.get("renderer")
    route = {"tier": req.get("tier"), "design": req.get("design"),
             "color": req.get("color")}
    if not renderer:
        routed = _route_for_level(x_val)
        if not routed:
            raise RuntimeError("no video for level %sx" % x_val)
        renderer, route = routed
    channel = str(req.get("channel") or "KOL").lstrip("@")
    symbol = str(req.get("symbol") or "TOKEN").lstrip("$")
    chain = str(req.get("chain") or "SOL")
    entry_mc = _parse_mc(req.get("entry_fmt"))
    now_mc = _parse_mc(req.get("cur_fmt"))
    try:
        xv = float(x_val) or 0
    except (TypeError, ValueError):
        xv = 0
    if entry_mc <= 0 and now_mc > 0 and xv > 0:
        entry_mc = now_mc / xv
    if now_mc <= 0 and entry_mc > 0 and xv > 0:
        now_mc = entry_mc * xv
    stats = req.get("stats") or {}
    rank = int(stats.get("rank", 1) or 1)
    points = int(stats.get("points", 0) or 0)
    calls = int(stats.get("calls", 0) or 0)
    win_rate = str(stats.get("win_rate", "0%"))
    avatar = _avatar_path(req.get("avatar_file"))
    out_path = os.path.join(OUT_DIR, req["id"] + ".mp4")
    base_path = _base_path_for(renderer, route, x_val)
    if not _fresh_base(base_path):
        base_path = None
    if renderer == "gauge":
        if champion_video is None:
            raise RuntimeError("champion_video module not installed")
        champion_video.build_gauge_video(
            kol_name=channel, x_val=float(x_val), symbol=symbol, chain=chain,
            entry_mc=entry_mc, now_mc=now_mc, rank=rank, points=points,
            calls=calls, win_rate=win_rate, avatar_path=avatar,
            logo_path=champion_video._golden_logo_path(),
            out_path=out_path, base_path=base_path,
            preset="veryfast", crf=18)
    elif renderer == "rocket":
        if champion_rocket_neon is None:
            raise RuntimeError("champion_rocket_neon module not installed")
        champion_rocket_neon.build_rocket_video(
            kol_name=channel, x_val=x_val, symbol=symbol, chain=chain,
            entry_mc=entry_mc, now_mc=now_mc, rank=rank, points=points,
            calls=calls, win_rate=win_rate, avatar_path=avatar,
            out_path=out_path, crf=18, preset="veryfast",
            base_path=base_path)
    elif renderer == "cyber":
        if champion_cyberhud_orange is None:
            raise RuntimeError(
                "champion_cyberhud_orange module not installed")
        champion_cyberhud_orange.build_cyber_video(
            entry="slam", kol_name=channel, x_val=x_val, symbol=symbol,
            chain=chain, entry_mc=entry_mc, now_mc=now_mc, rank=rank,
            points=points, calls=calls, win_rate=win_rate,
            avatar_path=avatar, out_path=out_path, crf=18,
            preset="veryfast", base_path=base_path)
    elif renderer == "trio":
        if champion_green_trio is None:
            raise RuntimeError("champion_green_trio module not installed")
        champion_green_trio.build_green_video(
            design=route["design"], x_val=x_val, color=route["color"],
            kol_name=channel, symbol=symbol, chain=chain,
            entry_mc=entry_mc, now_mc=now_mc, rank=rank, points=points,
            calls=calls, win_rate=win_rate, avatar_path=avatar,
            out_path=out_path, crf=18, preset="veryfast",
            base_path=base_path)
    else:
        raise RuntimeError("unknown renderer %r" % (renderer,))
    if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError("final render produced no file: %s" % out_path)
    return out_path


def _render_mostshared(req):
    if most_shared_video is None:
        raise RuntimeError(
            "most_shared_video module not installed (copy it from live "
            "/opt/bot/most_shared_video.py into /opt/videobot)")
    tokens = []
    for t in (req.get("tokens") or []):
        logo = None
        lp = t.get("logo_path")
        if lp:
            try:
                if os.path.exists(lp) and os.path.getsize(lp) > 200:
                    with open(lp, "rb") as f:
                        logo = f.read()
            except Exception:
                logo = None
        tokens.append({"symbol": t.get("symbol", "?"),
                       "chain": t.get("chain", "SOL"),
                       "count": int(t.get("count", 0) or 0),
                       "ath_mc": float(t.get("ath_mc", 0) or 0),
                       "from_mc": float(t.get("from_mc", 0) or 0),
                       "x": float(t.get("x", 0) or 0),
                       "logo": logo})
    if not tokens:
        raise RuntimeError("mostshared request has no tokens")
    out = most_shared_video.build_most_shared_video(tokens)
    out_path = os.path.join(OUT_DIR, req["id"] + ".mp4")
    if isinstance(out, (bytes, bytearray)):
        with open(out_path, "wb") as f:
            f.write(out)
    elif isinstance(out, str) and os.path.exists(out):
        if os.path.abspath(out) != os.path.abspath(out_path):
            shutil.copy2(out, out_path)
    else:
        raise RuntimeError(
            "build_most_shared_video returned unusable: %r" % (out,))
    if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError("mostshared render produced no file")
    return out_path


def _render_rankup(req):
    if champion_rankup_video is None:
        raise RuntimeError(
            "champion_rankup_video module not installed (copy it from "
            "live /opt/bot/champion_rankup_video.py into /opt/videobot)")
    out_path = os.path.join(OUT_DIR, req["id"] + ".mp4")
    calls = [{"symbol": str(c.get("symbol", "TOKEN")),
              "x": c.get("x")} for c in (req.get("calls") or [])]
    champion_rankup_video.build_rankup_video(
        channel=str(req.get("channel") or "").lstrip("@"),
        rank=int(req.get("rank", 1) or 1),
        points=int(req.get("points", 0) or 0),
        calls=calls, title=req.get("title", ""),
        avatar_path=_avatar_path(req.get("avatar_file")),
        out_path=out_path, crf=12, preset="veryfast")
    if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError("rankup render produced no file: %s" % out_path)
    return out_path


_RENDERERS = {"champ_base": _render_champ_base,
              "champ_final": _render_champ_final,
              "mostshared": _render_mostshared,
              "rankup": _render_rankup}


# ---------------------------------------------------------------- queue

# Time-sensitive kinds first (an alert video must not wait behind a
# speculative base render); oldest first inside a kind.
_KIND_PRIO = {"mostshared": 0, "champ_final": 1, "rankup": 2, "champ_base": 3}


def _list_requests():
    try:
        names = [f for f in os.listdir(QUEUE_DIR) if f.endswith(".json")]
    except Exception:
        return []
    reqs = []
    for n in names:
        p = os.path.join(QUEUE_DIR, n)
        try:
            with open(p, encoding="utf-8") as f:
                req = json.load(f)
            if not isinstance(req, dict) or "kind" not in req:
                continue
            req["_file"] = p
            reqs.append(req)
        except Exception as e:
            log.warning("bad request file %s (%s), deleting", n, e)
            try:
                os.remove(p)
            except Exception:
                pass
    reqs.sort(key=lambda r: (_KIND_PRIO.get(r.get("kind"), 9),
                             os.path.getmtime(r["_file"])))
    return reqs


def _run_guarded(fn, timeout):
    """Run fn() in a thread with a timeout. Returns (ok, result-or-error).
    A timeout abandons the thread (same as the old in-bot wait_for);
    the orphan ffmpeg finishes on its own, the request is marked failed
    so the main bot falls back instead of waiting forever."""
    box = {}

    def _target():
        try:
            box["res"] = fn()
        except Exception as e:
            box["err"] = "%s: %s" % (type(e).__name__, e)

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, "timeout after %ss (render abandoned)" % timeout
    if "err" in box:
        return False, box["err"]
    return True, box.get("res")


def _process_one(req):
    kind = req.get("kind")
    req_id = req.get("id") or os.path.splitext(
        os.path.basename(req.get("_file", "x.json")))[0]
    fn = _RENDERERS.get(kind)
    t0 = time.time()
    if fn is None:
        log.warning("unknown request kind %r (id %s), skipping", kind, req_id)
        _write_done(req_id, False, error="unknown kind %r" % (kind,))
        return False
    log.info("render start: %s %s", kind, req_id)
    ok, res = _run_guarded(lambda: fn(req), TIMEOUTS.get(kind, 180))
    dt = time.time() - t0
    if ok:
        STATS["rendered"] += 1
        STATS["last_kind"] = kind
        STATS["last_ok"] = True
        log.info("render done: %s %s -> %s (%.1fs)", kind, req_id, res, dt)
        _write_done(req_id, True, path=res)
    else:
        STATS["failed"] += 1
        STATS["last_kind"] = kind
        STATS["last_ok"] = False
        log.warning("render FAILED: %s %s: %s (%.1fs)", kind, req_id, res, dt)
        _write_done(req_id, False, error=str(res))
    try:
        os.remove(req["_file"])
    except Exception:
        pass
    return ok


def _prune_bases():
    """Same prune the main bot's prerender job did: max 6 bases, delete
    anything older than 30 min. Cheap file listing, runs every 60s."""
    try:
        if not os.path.isdir(BASES_DIR):
            return
        now = time.time()
        files = sorted(
            (os.path.join(BASES_DIR, f) for f in os.listdir(BASES_DIR)
             if f.startswith("champ-base-") and f.endswith(".mp4")),
            key=lambda p: os.path.getmtime(p))
        for old in files[:-PRERENDER_MAX_BASES]:
            try:
                os.remove(old)
            except Exception:
                pass
        for old in files:
            try:
                if now - os.path.getmtime(old) > PRERENDER_MAX_AGE:
                    os.remove(old)
            except Exception:
                pass
    except Exception as e:
        log.warning("prune_bases failed: %s", e)


def _heartbeat_loop():
    """Daemon thread: heartbeat every 10s even while a render is running,
    so a stale heartbeat really means videobot is dead (the main bot
    checks this before waiting for a render)."""
    while True:
        try:
            pending = 0
            try:
                pending = len([f for f in os.listdir(QUEUE_DIR)
                               if f.endswith(".json")])
            except Exception:
                pass
            _atomic_write_json(HEARTBEAT_FILE, {
                "ts": time.time(), "pid": os.getpid(),
                "uptime_s": round(time.time() - STATS["started"], 1),
                "rendered": STATS["rendered"], "failed": STATS["failed"],
                "last_kind": STATS["last_kind"],
                "last_ok": STATS["last_ok"],
                "queue_pending": pending,
                "renderers": dict(RENDERERS_OK),
                "bases_dir": BASES_DIR,
            })
        except Exception as e:
            log.warning("heartbeat write failed: %s", e)
        time.sleep(10)


def _self_check():
    """--check: prove the pipeline works. Renders one tiny gauge base
    (red 2x) to a temp file and reports timing. Exit 0 on success."""
    import tempfile
    if champion_video is None:
        print("CHECK FAIL: champion_video module not installed")
        return 1
    if shutil.which("ffmpeg") is None:
        print("CHECK FAIL: ffmpeg not on PATH")
        return 1
    tmp = tempfile.mkdtemp(prefix="videobot-check-")
    out = os.path.join(tmp, "champ-base-red-2x.mp4")
    t0 = time.time()
    try:
        champion_video.render_gauge_base(
            tier="red", x_val=2, out_path=out, crf=12, preset="veryfast")
    except Exception as e:
        print("CHECK FAIL: gauge base render failed: %s: %s"
              % (type(e).__name__, e))
        return 1
    dt = time.time() - t0
    if not os.path.exists(out) or os.path.getsize(out) <= 0:
        print("CHECK FAIL: render produced no file")
        return 1
    print("CHECK OK: gauge red 2x base rendered in %.1fs, %d bytes -> %s"
          % (dt, os.path.getsize(out), out))
    print("CHECK OK: renderers: %s"
          % ", ".join("%s=%s" % (k, "yes" if v else "no")
                      for k, v in sorted(RENDERERS_OK.items())))
    print("CHECK OK: bases dir %s, queue %s, out %s"
          % (BASES_DIR, QUEUE_DIR, OUT_DIR))
    return 0


def main():
    _ensure_dirs()
    if "--check" in sys.argv:
        return _self_check()
    once = "--once" in sys.argv
    log.info("videobot started (pid %d). bases=%s queue=%s out=%s",
             os.getpid(), BASES_DIR, QUEUE_DIR, OUT_DIR)
    log.info("renderers: %s", ", ".join(
        "%s=%s" % (k, "yes" if v else "no")
        for k, v in sorted(RENDERERS_OK.items())))
    if champion_video is None:
        log.error("champion_video module MISSING - champion renders will "
                  "fail. Copy champion_video.py + champion_gauge_neon.py + "
                  "champion_green_trio.py + champion_rocket_neon.py + "
                  "champion_cyberhud_orange.py from live /opt/bot.")
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    last_prune = 0.0
    while True:
        try:
            reqs = _list_requests()
            if reqs:
                _process_one(reqs[0])
            if once:
                return 0
            now = time.time()
            if now - last_prune >= 60:
                _prune_bases()
                last_prune = now
        except Exception as e:
            log.error("main loop error: %s: %s", type(e).__name__, e)
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
