"""apply_videobot_handoff_nov26.py - Main bot changes for the separate videobot.

Videobot (separate video-generation bot) takes over ALL ffmpeg rendering.
The main bot was running at 95-156% CPU / 6.8 GB RAM; its video work is a
major part of that load (champion base pre-render every 60s, Most Shared
video render, per-alert champion final videos, rank-up videos). On
2026-11-25 the Most Shared video build FAILED with TimeoutError while the
main bot was at 156% CPU, so the post went out text-only (video=no).

After this patch the main bot NEVER runs ffmpeg. It only writes a small
JSON request into /opt/videobot/queue/ and polls for
/opt/videobot/out/<id>.done. If videobot is down (stale heartbeat) the
caller falls back AT ONCE: normal X media for champion alerts, text-only
for Most Shared, no post for rank-up (same as a failed build today).
A dead videobot can never wedge an alert.

Nine edits, three files (run from /opt/bot):

E1 - x_alert.py: videobot helper block (write request / heartbeat check /
     wait for .done / champion routing / _vb_champ_final), inserted BEFORE
     the champion base pre-render section.
E2 - x_alert.py: champion_prerender_job drops in-flight keys whose base
     is now fresh, or whose request is older than 10 min.
E3 - x_alert.py: champion_prerender_job writes a champ_base request for
     videobot instead of rendering the base in-process. Scan + routing +
     80% threshold + 60s interval are UNCHANGED (only the render call
     is replaced).
E4a-d - x_alert.py: _champion_video_try_send calls _vb_champ_final
     instead of rendering the final video in-process (gauge / rocket /
     cyber / trio branches). The send + delete-after-post code is
     untouched. Fallback on failure: normal X media (existing path).
E5 - most_shared.py: videobot helpers (alive check + wait), inserted
     BEFORE _ms_make_video.
E6 - most_shared.py: _ms_make_video writes a mostshared request (token
     list + logo files) and waits up to 240s (same timeout as the old
     in-bot build). Returns None on failure -> text-only post, exactly
     like a failed build today.
E7 - champion_rankup.py: videobot helpers, inserted BEFORE
     _rankup_build_video.
E8 - champion_rankup.py: _rankup_build_video writes a rankup request
     and waits up to 120s. Returns None on failure -> no post, exactly
     like a failed build today.

The prerender interval stays 60s and the threshold stays 80% (user
decision Nov 23). Video format/size/themes are unchanged. The Most
Shared post time is unchanged (00:00 Turkey time).

Rollback: cp x_alert.py.bak_videobot_nov26 x_alert.py &&
cp most_shared.py.bak_videobot_nov26 most_shared.py &&
cp champion_rankup.py.bak_videobot_nov26 champion_rankup.py &&
systemctl restart bot.service && systemctl stop videobot.service
(The main bot's own prerender/render code comes back; videobot's bases
in $CHAMP_VIDEO_DIR/champ_bases are still usable by it.)

Idempotent. Backs up each file to <file>.bak_videobot_nov26 (first edit
only per file, so rollback restores a pristine pre-patch file).
Run: python3 apply_videobot_handoff_nov26.py [/opt/bot]
Then: systemctl restart bot.service

WARNING: live /opt/bot files have post-Nov-23 patches that are NOT in the
Nov 23 base copies this patch was written against. The touched regions
(champion_prerender_job, _champion_video_try_send, _ms_make_video,
_rankup_build_video) were not touched by the Nov 24/25 patches, but if
any anchor below FAILS on the live files, do NOT force it - report back
instead. Anchors were verified count==1 against the Nov 23 base copies
(x_alert-3aa26821.py, most_shared-b2355b23.py, champion_rankup.py).
"""

import os, sys, shutil

MARKER = "VIDEOBOT_HANDOFF_NOV26"

# ---------------------------------------------------------------- x_alert E1
# Videobot helpers, inserted before the champion base pre-render section.
OLD1 = '''# ---- champion base pre-render (CHAMPION_VIDEO_FINAL_NOV10) ----'''

NEW1 = '''# ---- videobot handoff (VIDEOBOT_HANDOFF_NOV26) ----
# All ffmpeg video rendering moved to the separate videobot process
# (/opt/videobot). The main bot only writes a small JSON request into
# /opt/videobot/queue/ and polls for /opt/videobot/out/<id>.done.
# If videobot is down (stale heartbeat) the caller falls back at once:
# normal X media for champion alerts, text-only for Most Shared.
# Nothing here ever raises: every helper returns None/False on failure.
import time as _vb_time
import json as _vb_json

_VB_DIR = os.environ.get("VIDEOBOT_DIR", "/opt/videobot")
_VB_QUEUE = os.path.join(_VB_DIR, "queue")
_VB_OUT = os.path.join(_VB_DIR, "out")
_VB_HEARTBEAT = os.path.join(_VB_DIR, "heartbeat.json")
_PRERENDER_REQUESTED = {}  # prerender key -> (base_path, request_time)


def _vb_write_request(kind, req_id, payload):
    """Atomic request write into the videobot queue. Returns the path or
    None on failure (never raises)."""
    try:
        os.makedirs(_VB_QUEUE, exist_ok=True)
        req = dict(payload or {})
        req["kind"] = kind
        req["id"] = req_id
        req["ts"] = _vb_time.time()
        path = os.path.join(_VB_QUEUE, req_id + ".json")
        tmp = path + ".tmp.%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            _vb_json.dump(req, f)
        os.replace(tmp, path)
        return path
    except Exception as e:
        logger.warning("videobot: request write failed %s: %s", req_id, e)
        return None


def _vb_alive(max_age=60):
    """True when videobot's heartbeat is fresh (written every 10s by a
    thread, even mid-render). A stale/missing heartbeat means videobot
    is down: callers must fall back at once, never wait."""
    try:
        return bool(os.path.exists(_VB_HEARTBEAT)
                    and (_vb_time.time() - os.path.getmtime(_VB_HEARTBEAT)) < max_age)
    except Exception:
        return False


async def _vb_wait(req_id, timeout):
    """Poll for /opt/videobot/out/<req_id>.done up to timeout seconds.
    Returns the finished .mp4 path, or None (timeout / render error /
    videobot down). Never raises. Consumes the .done file."""
    done_path = os.path.join(_VB_OUT, req_id + ".done")
    deadline = _vb_time.time() + float(timeout or 0)
    while _vb_time.time() < deadline:
        try:
            if os.path.exists(done_path):
                with open(done_path, encoding="utf-8") as f:
                    st = _vb_json.load(f) or {}
                try:
                    os.remove(done_path)
                except Exception:
                    pass
                if st.get("ok") and st.get("path") and os.path.exists(st["path"]):
                    return st["path"]
                logger.warning("videobot: render failed %s: %s", req_id, st.get("error"))
                return None
        except Exception as e:
            logger.warning("videobot: wait read failed %s: %s", req_id, e)
            return None
        await asyncio.sleep(2)
    logger.warning("videobot: wait timed out %s after %ss", req_id, timeout)
    return None


def _vb_base_req_id(key):
    return "base-" + "-".join(str(p) for p in key)


def _vb_route_for_level(x_val):
    """Same champion video routing the main bot used (mirror of
    _champion_video_try_send + champion_prerender_job). Returns
    (renderer, route-dict) or None when the level has no video."""
    try:
        x = int(x_val)
    except (TypeError, ValueError):
        return None
    if x in (2, 5, 10):
        return ("gauge", {"tier": "red"})
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
    if x >= 100:
        return ("trio", {"design": "area", "color": "bw"})
    return None


async def _vb_champ_final(channel, x_val, chain, entry_fmt, cur_fmt, ca, symbol, stats, avatar):
    """Ask videobot for the final champion alert video. Returns the .mp4
    path or None (videobot down / timeout / render error -> caller falls
    back to normal X media). Never raises."""
    try:
        routed = _vb_route_for_level(x_val)
        if not routed:
            return None
        renderer, route = routed
        if not _vb_alive():
            logger.warning("videobot: heartbeat stale, champion video skipped @%s %sx", channel, x_val)
            return None
        stats = stats or {}
        req_id = "champ-%s-%sx-%d" % (str(channel).lstrip("@").lower(), int(float(x_val)), int(_vb_time.time() * 1000) % 100000)
        avatar_file = None
        try:
            if avatar and os.path.exists(avatar):
                avatar_file = os.path.basename(avatar)
        except Exception:
            avatar_file = None
        payload = {"renderer": renderer, "tier": route.get("tier"),
                   "design": route.get("design"), "color": route.get("color"),
                   "x_val": int(float(x_val)), "channel": str(channel),
                   "symbol": str(symbol or "TOKEN"), "chain": str(chain or "SOL"),
                   "entry_fmt": entry_fmt, "cur_fmt": cur_fmt,
                   "ca": ca, "stats": {"rank": int(stats.get("rank", 1) or 1),
                                       "points": int(stats.get("points", 0) or 0),
                                       "calls": int(stats.get("calls", 0) or 0),
                                       "win_rate": str(stats.get("win_rate", "0%"))},
                   "avatar_file": avatar_file}
        if _vb_write_request("champ_final", req_id, payload) is None:
            return None
        return await _vb_wait(req_id, 120)
    except Exception as e:
        logger.warning("videobot: champ_final failed @%s %sx: %s", channel, x_val, e)
        return None


# ---- champion base pre-render (CHAMPION_VIDEO_FINAL_NOV10) ----'''

# ---------------------------------------------------------------- x_alert E2
# Prerender job: drop in-flight keys whose base is now fresh, or whose
# request is older than 10 min (videobot down / render failed).
OLD2 = '''    if _PRERENDER_LOCK.locked():
        return'''

NEW2 = '''    # VIDEOBOT_HANDOFF_NOV26: drop in-flight keys whose base is now fresh,
    # or whose request is older than 10 min (videobot down / render failed),
    # so the key can be requested again on a later tick.
    try:
        for _rk in list(_PRERENDER_RUNNING):
            _rbp, _rts = _PRERENDER_REQUESTED.get(_rk, (None, 0))
            _rfresh = bool(_rbp and os.path.exists(_rbp)
                           and (_time.time() - os.path.getmtime(_rbp)) < _PRERENDER_MAX_AGE)
            if _rfresh or (_time.time() - _rts) > 600:
                _PRERENDER_RUNNING.discard(_rk)
                _PRERENDER_REQUESTED.pop(_rk, None)
    except Exception:
        pass
    if _PRERENDER_LOCK.locked():
        return'''

# ---------------------------------------------------------------- x_alert E3
# Prerender job: write a champ_base request instead of rendering in-process.
OLD3 = '''    _cands.sort()
    _nv, _key, _bp = _cands[0]
    _PRERENDER_RUNNING.add(_key)
    try:
        async with _PRERENDER_LOCK:
            if _key[0] == "trio":
                # TRIO_PRERENDER_NOV12
                import champion_green_trio as _trio_mod2
                await asyncio.to_thread(
                    _trio_mod2.render_trio_base, design=_key[1], color=_key[2],
                    x_val=_key[3], out_path=_bp, crf=12, preset="veryfast")
                logger.info(f"🎬 Champion trio base pre-rendered: {_key[1]} {_key[2]} {_key[3]}x -> {_bp}")
            elif _key[0] == "rocket":
                # ROCKET_CYBER_PRERENDER_NOV12
                import champion_rocket_neon as _rk_mod2
                await asyncio.to_thread(
                    _rk_mod2.render_rocket_base, tier=_key[1], x_val=_key[2],
                    out_path=_bp, crf=12, preset="veryfast")
                logger.info(f"🎬 Champion rocket base pre-rendered: {_key[1]} {_key[2]}x -> {_bp}")
            elif _key[0] == "cyber":
                # ROCKET_CYBER_PRERENDER_NOV12
                import champion_cyberhud_orange as _cy_mod2
                await asyncio.to_thread(
                    _cy_mod2.render_cyber_base, tier=_key[1], x_val=_key[2],
                    out_path=_bp, crf=12, preset="veryfast")
                logger.info(f"🎬 Champion cyber base pre-rendered: {_key[1]} {_key[2]}x -> {_bp}")
            else:
                await asyncio.to_thread(
                    champion_video.render_gauge_base, tier=_key[1], x_val=_key[2],
                    out_path=_bp, crf=12, preset="veryfast")
                logger.info(f"🎬 Champion base pre-rendered: {_key[1]} {_key[2]}x -> {_bp}")
    except Exception as e:
        logger.warning(f"🎬 Champion base pre-render failed {_key}: {e}")
    finally:
        _PRERENDER_RUNNING.discard(_key)'''

NEW3 = '''    _cands.sort()
    _nv, _key, _bp = _cands[0]
    _PRERENDER_RUNNING.add(_key)
    try:
        # VIDEOBOT_HANDOFF_NOV26: no ffmpeg on the main bot. Write a base
        # request for videobot and let it render. The key stays in
        # _PRERENDER_RUNNING until the base file appears fresh (dropped
        # at the top of the next tick) or the request is older than
        # 10 min, so we do not spam one request per tick while videobot
        # renders. Scan + routing + 80% threshold + 60s interval are
        # unchanged; only the render call moved to videobot.
        _vb_write_request("champ_base", _vb_base_req_id(_key), {
            "renderer": _key[0],
            "tier": _key[1] if _key[0] in ("gauge", "rocket", "cyber") else None,
            "design": _key[1] if _key[0] == "trio" else None,
            "color": _key[2] if _key[0] == "trio" else None,
            "x_val": _key[-1],
            "base_path": _bp,
        })
        _PRERENDER_REQUESTED[_key] = (_bp, _time.time())
        logger.info("videobot: champion base requested %s -> %s", _key, _bp)
    except Exception as e:
        logger.warning("videobot: champion base request failed %s: %s", _key, e)
        _PRERENDER_RUNNING.discard(_key)
        _PRERENDER_REQUESTED.pop(_key, None)'''

# ---------------------------------------------------------------- x_alert E4a
# _champion_video_try_send, gauge branch (2/5/10): ask videobot.
OLD4A = '''            path = await champion_video.champion_alert_video(
                channel=channel, x_val=x_val, chain=chain, entry_fmt=entry_fmt,
                cur_fmt=current_fmt, ca=ca, symbol=symbol, stats=stats,
                avatar_path=avatar, base_path=_bp)'''

NEW4A = '''            # VIDEOBOT_HANDOFF_NOV26: final video renders in videobot.
            path = await _vb_champ_final(
                channel, x_val, chain, entry_fmt, cur_fmt, ca, symbol, stats, avatar)'''

# ---------------------------------------------------------------- x_alert E4b
# _champion_video_try_send, rocket branch (20x): ask videobot.
OLD4B = '''            path = await asyncio.to_thread(
                champion_rocket_neon.build_rocket_video, **_kw)'''

NEW4B = '''            # VIDEOBOT_HANDOFF_NOV26: final video renders in videobot.
            path = await _vb_champ_final(
                channel, x_val, chain, entry_fmt, cur_fmt, ca, symbol, stats, avatar)'''

# ---------------------------------------------------------------- x_alert E4c
# _champion_video_try_send, cyber branch (30x): ask videobot.
OLD4C = '''            path = await asyncio.to_thread(
                champion_cyberhud_orange.build_cyber_video, entry="slam", **_kw)'''

NEW4C = '''            # VIDEOBOT_HANDOFF_NOV26: final video renders in videobot.
            path = await _vb_champ_final(
                channel, x_val, chain, entry_fmt, cur_fmt, ca, symbol, stats, avatar)'''

# ---------------------------------------------------------------- x_alert E4d
# _champion_video_try_send, trio branch (40x+): ask videobot.
OLD4D = '''            path = await asyncio.to_thread(
                champion_green_trio.build_green_video, **_kw)'''

NEW4D = '''            # VIDEOBOT_HANDOFF_NOV26: final video renders in videobot.
            path = await _vb_champ_final(
                channel, x_val, chain, entry_fmt, cur_fmt, ca, symbol, stats, avatar)'''

# ---------------------------------------------------------------- most_shared E5
# Videobot helpers, inserted before _ms_make_video.
OLD5 = '''async def _ms_make_video(toks):'''

NEW5 = '''# ---- videobot handoff (VIDEOBOT_HANDOFF_NOV26) ----
# The Most Shared video render moved to the separate videobot process.
# This file only writes the token list (+ logo files) as a request and
# waits for the finished video. Same 240s timeout as the old in-bot
# build; on timeout / videobot down it returns None and the post goes
# out text-only, exactly like a failed build today.
import json as _msvb_json

_MSVB_DIR = os.environ.get("VIDEOBOT_DIR", "/opt/videobot")
_MSVB_QUEUE = os.path.join(_MSVB_DIR, "queue")
_MSVB_OUT = os.path.join(_MSVB_DIR, "out")
_MSVB_LOGOS = os.path.join(_MSVB_DIR, "shared", "ms-logos")
_MSVB_HEARTBEAT = os.path.join(_MSVB_DIR, "heartbeat.json")


def _msvb_alive(max_age=60):
    """True when videobot's heartbeat is fresh (written every 10s by a
    thread, even mid-render). Stale/missing = videobot down: fall back
    at once, never wait."""
    try:
        return bool(os.path.exists(_MSVB_HEARTBEAT)
                    and (_ms_time.time() - os.path.getmtime(_MSVB_HEARTBEAT)) < max_age)
    except Exception:
        return False


async def _msvb_wait(req_id, timeout):
    """Poll for /opt/videobot/out/<req_id>.done up to timeout seconds.
    Returns the finished .mp4 path, or None. Never raises. Consumes
    the .done file."""
    done_path = os.path.join(_MSVB_OUT, req_id + ".done")
    deadline = _ms_time.time() + float(timeout or 0)
    while _ms_time.time() < deadline:
        try:
            if os.path.exists(done_path):
                with open(done_path, encoding="utf-8") as f:
                    st = _msvb_json.load(f) or {}
                try:
                    os.remove(done_path)
                except Exception:
                    pass
                if st.get("ok") and st.get("path") and os.path.exists(st["path"]):
                    return st["path"]
                logger.warning("videobot: mostshared render failed %s: %s", req_id, st.get("error"))
                return None
        except Exception as e:
            logger.warning("videobot: mostshared wait read failed %s: %s", req_id, e)
            return None
        await asyncio.sleep(2)
    logger.warning("videobot: mostshared wait timed out %s after %ss", req_id, timeout)
    return None


async def _ms_make_video(toks):'''

# ---------------------------------------------------------------- most_shared E6
# _ms_make_video: handoff version (request + wait instead of in-bot build).
OLD6 = '''async def _ms_make_video(toks):
    import most_shared_video as _msv
    return await _ms_run(_msv.build_most_shared_video, _ms_video_tokens(toks), timeout=240)'''

NEW6 = '''async def _ms_make_video(toks):
    # VIDEOBOT_HANDOFF_NOV26: no ffmpeg on the main bot. Write the token
    # list (+ logo files) as a request for videobot and wait up to 240s
    # (same timeout as the old in-bot build). Returns the video path or
    # None (videobot down / timeout / render error -> text-only post).
    # Never raises.
    try:
        if not _msvb_alive():
            logger.warning("videobot: heartbeat stale, mostshared video skipped (text-only post)")
            return None
        req_id = "ms-%d" % int(_ms_time.time())
        try:
            os.makedirs(_MSVB_LOGOS, exist_ok=True)
            os.makedirs(_MSVB_QUEUE, exist_ok=True)
        except Exception:
            pass
        tokens = []
        for i, t in enumerate(_ms_video_tokens(toks)):
            logo_path = None
            logo = t.get("logo")
            if isinstance(logo, (bytes, bytearray)) and len(logo) > 200:
                try:
                    sym = "".join(ch for ch in str(t.get("symbol", "tok")) if ch.isalnum()) or "tok"
                    logo_path = os.path.join(_MSVB_LOGOS, "%s-%s-%d.bin" % (req_id, sym, i))
                    with open(logo_path, "wb") as f:
                        f.write(bytes(logo))
                except Exception:
                    logo_path = None
            tokens.append({"symbol": t.get("symbol"), "chain": t.get("chain"),
                           "count": t.get("count"), "ath_mc": t.get("ath_mc"),
                           "from_mc": t.get("from_mc"), "x": t.get("x"),
                           "logo_path": logo_path})
        req = {"kind": "mostshared", "id": req_id, "tokens": tokens,
               "ts": _ms_time.time()}
        path = os.path.join(_MSVB_QUEUE, req_id + ".json")
        tmp = path + ".tmp.%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            _msvb_json.dump(req, f)
        os.replace(tmp, path)
        return await _msvb_wait(req_id, 240)
    except Exception as e:
        logger.warning("videobot: _ms_make_video handoff failed: %s: %s", type(e).__name__, e)
        return None'''

# ---------------------------------------------------------------- rankup E7
# Videobot helpers, inserted before _rankup_build_video.
OLD7 = '''async def _rankup_build_video(channel, rank, points, alpha_calls, title):'''

NEW7 = '''# ---- videobot handoff (VIDEOBOT_HANDOFF_NOV26) ----
# Rank-up videos render in the separate videobot process now (ffmpeg
# off the main bot). Same 120s wait; on videobot down / timeout /
# render error this returns None and the caller posts nothing, exactly
# like a failed build today.
_RKVB_DIR = os.environ.get("VIDEOBOT_DIR", "/opt/videobot")
_RKVB_QUEUE = os.path.join(_RKVB_DIR, "queue")
_RKVB_OUT = os.path.join(_RKVB_DIR, "out")
_RKVB_HEARTBEAT = os.path.join(_RKVB_DIR, "heartbeat.json")


def _rkvb_alive(max_age=60):
    """True when videobot's heartbeat is fresh (written every 10s by a
    thread, even mid-render). Stale/missing = videobot down: fall back
    at once, never wait."""
    try:
        return bool(os.path.exists(_RKVB_HEARTBEAT)
                    and (time.time() - os.path.getmtime(_RKVB_HEARTBEAT)) < max_age)
    except Exception:
        return False


async def _rkvb_wait(req_id, timeout):
    """Poll for /opt/videobot/out/<req_id>.done up to timeout seconds.
    Returns the finished .mp4 path, or None. Never raises. Consumes
    the .done file."""
    done_path = os.path.join(_RKVB_OUT, req_id + ".done")
    deadline = time.time() + float(timeout or 0)
    while time.time() < deadline:
        try:
            if os.path.exists(done_path):
                with open(done_path, encoding="utf-8") as f:
                    st = json.load(f) or {}
                try:
                    os.remove(done_path)
                except Exception:
                    pass
                if st.get("ok") and st.get("path") and os.path.exists(st["path"]):
                    return st["path"]
                _rankup_log.warning("videobot: rankup render failed %s: %s", req_id, st.get("error"))
                return None
        except Exception as e:
            _rankup_log.warning("videobot: rankup wait read failed %s: %s", req_id, e)
            return None
        await asyncio.sleep(2)
    _rankup_log.warning("videobot: rankup wait timed out %s after %ss", req_id, timeout)
    return None


async def _rankup_build_video(channel, rank, points, alpha_calls, title):'''

# ---------------------------------------------------------------- rankup E8
# _rankup_build_video: handoff version (request + wait instead of in-bot build).
OLD8 = '''async def _rankup_build_video(channel, rank, points, alpha_calls, title):
    """Build the per-post video via the parallel renderer module.
    Returns the file path, or None on any failure (caller: no post)."""
    try:
        import champion_rankup_video
        kw = _rankup_video_kwargs(
            channel, rank, points, alpha_calls, title,
            avatar_path=_rankup_avatar_path(channel))
        path = await asyncio.to_thread(
            champion_rankup_video.build_rankup_video, **kw)
    except Exception as e:
        _rankup_log.warning(
            f"🏆 Rank-up video build failed @{channel} rank #{rank}: "
            f"{type(e).__name__}: {e}")
        return None
    if not path or not os.path.exists(path):
        _rankup_log.warning(
            f"🏆 Rank-up video missing @{channel} rank #{rank}: {path}")
        return None
    return path'''

NEW8 = '''async def _rankup_build_video(channel, rank, points, alpha_calls, title):
    """Ask videobot for the rank-up video. Returns the file path, or None
    on any failure (caller: no post)."""
    try:
        if not _rkvb_alive():
            _rankup_log.warning("videobot: heartbeat stale, rank-up video skipped @%s rank #%s", channel, rank)
            return None
        kw = _rankup_video_kwargs(
            channel, rank, points, alpha_calls, title,
            avatar_path=_rankup_avatar_path(channel))
        req_id = "rankup-%s-%s-%d" % (str(channel).lstrip("@").lower(), int(rank), int(time.time() * 1000) % 100000)
        avatar_file = None
        try:
            av = kw.get("avatar_path")
            if av and os.path.exists(av):
                avatar_file = os.path.basename(av)
        except Exception:
            avatar_file = None
        req = {"kind": "rankup", "id": req_id,
               "channel": str(channel).lstrip("@"),
               "rank": int(rank), "points": int(points),
               "calls": kw.get("calls") or [],
               "title": title, "avatar_file": avatar_file,
               "ts": time.time()}
        try:
            os.makedirs(_RKVB_QUEUE, exist_ok=True)
        except Exception:
            pass
        path = os.path.join(_RKVB_QUEUE, req_id + ".json")
        tmp = path + ".tmp.%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(req, f)
        os.replace(tmp, path)
        return await _rkvb_wait(req_id, 120)
    except Exception as e:
        _rankup_log.warning(
            "videobot: rank-up video request failed @%s rank #%s: %s: %s",
            channel, rank, type(e).__name__, e)
        return None'''


def patch_one(path, old, new, tag):
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    if src.count(old) != 1:
        print("FAIL: %s anchor missing/dup in %s (found %dx) -- NOT writing "
              "anything." % (tag, path, src.count(old)))
        return False
    bak = path + ".bak_videobot_nov26"
    if not os.path.exists(bak):
        # First edit only: keep a PRISTINE pre-patch backup (later edits
        # must not overwrite it, or rollback would restore a half-patched file).
        shutil.copy2(path, bak)
    src = src.replace(old, new, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    return True


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else "."
    xa = os.path.join(d, "x_alert.py")
    ms = os.path.join(d, "most_shared.py")
    rk = os.path.join(d, "champion_rankup.py")
    for p in (xa, ms, rk):
        if not os.path.exists(p):
            print("FAIL: %s not found" % p)
            return 1
    with open(xa, encoding="utf-8") as f:
        xasrc = f.read()
    with open(ms, encoding="utf-8") as f:
        mssrc = f.read()
    with open(rk, encoding="utf-8") as f:
        rksrc = f.read()
    if MARKER in xasrc and MARKER in mssrc and MARKER in rksrc:
        print("OK: already has VIDEOBOT_HANDOFF_NOV26, skipping.")
        return 0
    if MARKER in xasrc or MARKER in mssrc or MARKER in rksrc:
        print("FAIL: partial state (marker in some files only) -- inspect "
              "x_alert.py / most_shared.py / champion_rankup.py before "
              "re-running.")
        return 1
    if not patch_one(xa, OLD1, NEW1, "E1"):
        return 1
    print("E1 OK (videobot helpers added to x_alert.py)")
    if not patch_one(xa, OLD2, NEW2, "E2"):
        return 1
    print("E2 OK (prerender in-flight key prune added)")
    if not patch_one(xa, OLD3, NEW3, "E3"):
        return 1
    print("E3 OK (prerender writes champ_base request, no in-bot ffmpeg)")
    if not patch_one(xa, OLD4A, NEW4A, "E4a"):
        return 1
    print("E4a OK (gauge final video -> videobot)")
    if not patch_one(xa, OLD4B, NEW4B, "E4b"):
        return 1
    print("E4b OK (rocket final video -> videobot)")
    if not patch_one(xa, OLD4C, NEW4C, "E4c"):
        return 1
    print("E4c OK (cyber final video -> videobot)")
    if not patch_one(xa, OLD4D, NEW4D, "E4d"):
        return 1
    print("E4d OK (trio final video -> videobot)")
    if not patch_one(ms, OLD5, NEW5, "E5"):
        return 1
    print("E5 OK (videobot helpers added to most_shared.py)")
    if not patch_one(ms, OLD6, NEW6, "E6"):
        return 1
    print("E6 OK (_ms_make_video writes mostshared request, no in-bot ffmpeg)")
    if not patch_one(rk, OLD7, NEW7, "E7"):
        return 1
    print("E7 OK (videobot helpers added to champion_rankup.py)")
    if not patch_one(rk, OLD8, NEW8, "E8"):
        return 1
    print("E8 OK (_rankup_build_video writes rankup request, no in-bot ffmpeg)")
    print("DONE. Backups: x_alert.py.bak_videobot_nov26, most_shared.py.bak_videobot_nov26, champion_rankup.py.bak_videobot_nov26")
    print("Next: systemctl restart bot.service")
    print("Note: start videobot.service FIRST and check its heartbeat, or the")
    print("main bot will fall back (text-only Most Shared / normal X media)")
    print("until videobot is up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
