"""Creator 5 Pro dashboard — status, light control, camera, job totals."""
import json
import math
import os
import re
import threading
import time

import requests
from flask import Flask, Response, jsonify, render_template, request

import materials

PRINTER_HOST = os.environ.get("PRINTER_HOST", "192.168.1.100")
API_PORT = int(os.environ.get("PRINTER_API_PORT", "8898"))
CAM_PORT = int(os.environ.get("PRINTER_CAM_PORT", "8080"))
CAM_PATH = os.environ.get("PRINTER_CAM_PATH", "/?action=stream")
SERIAL = os.environ.get("PRINTER_SERIAL", "")
CHECK_CODE = os.environ.get("PRINTER_CHECK_CODE", "")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
FILAMENT_DIAMETER_MM = float(os.environ.get("FILAMENT_DIAMETER_MM", "1.75"))

BASE = f"http://{PRINTER_HOST}:{API_PORT}"
CAM_URL = f"http://{PRINTER_HOST}:{CAM_PORT}{CAM_PATH}"
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")

app = Flask(__name__)

# Cache the last good poll so the UI degrades gracefully if the printer sleeps.
_cache = {"detail": None, "ts": 0, "error": None}
_lock = threading.Lock()

# The live job's filament tally, mirrored in memory so /api/status doesn't read
# the history file on every request. The on-disk copy is the durable one — it is
# what lets a container restart mid-job keep its baseline.
_current = None


# --- filament mass estimation ---------------------------------------------
#
# The firmware only ever reports filament as a LENGTH (cumulativeFilament, in
# metres). Mass is derived: a metre of round filament is
#   pi * (d/2)^2 mm^2 * 1000 mm  =  cm^3 per metre,  times the polymer density.
# For the stock 1.75 mm that is 2.405 cm^3/m, so PLA (1.24 g/cm^3) works out to
# ~2.98 g/m and PETG (1.27) to ~3.05 g/m.
#
# When the material is unknown we fall back to the shop average of 0.33 m per
# gram — i.e. ~3.03 g/m, which sits between PLA and PETG.
CM3_PER_M = math.pi * (FILAMENT_DIAMETER_MM / 2.0) ** 2 * 1000.0 / 1000.0

# g/cm^3, typical vendor figures for the spools this machine runs.
DENSITY = {
    "PLA": 1.24, "PETG": 1.27, "PCTG": 1.23, "ABS": 1.04, "ASA": 1.07,
    "TPU": 1.21, "PC": 1.20, "PA": 1.14, "PA12": 1.01, "PVA": 1.23,
    "HIPS": 1.04, "PP": 0.90, "PET": 1.38,
}
# Match longer names first so PETG never gets read as PET, PCTG as PC, etc.
MATERIALS = tuple(sorted(DENSITY, key=len, reverse=True))

UNKNOWN_M_PER_G = 0.33                      # average across PLA and PETG
UNKNOWN_G_PER_M = 1.0 / UNKNOWN_M_PER_G     # ~3.03 g/m

# A job whose printDuration already exceeds this on the first poll started
# before we were watching, so there is no counter baseline to subtract from.
MISSED_START_S = 120

# Biggest believable rise in cumulativeFilament between two polls. Flat out
# this machine extrudes well under a metre per poll interval; anything past
# this is a firmware counter jump, not filament, and must not be banked
# against a spool.
MAX_DELTA_M = 10.0


def grams_per_m(material):
    """Grams per metre for a material name, or the PLA/PETG average."""
    d = DENSITY.get((material or "").upper())
    return CM3_PER_M * d if d else UNKNOWN_G_PER_M


def filament_grams(length_m, material):
    if not length_m:
        return 0.0
    return round(length_m * grams_per_m(material), 1)


def material_from_name(name):
    """Pull a material out of a slicer filename like Tags_PETG_37m50s.gcode.3mf."""
    tokens = set(re.split(r"[^A-Za-z0-9]+", (name or "").upper()))
    for m in MATERIALS:
        if m in tokens:
            return m
    return ""


def job_material(n):
    """Best guess at what a job is printing in.

    Filename first — it is what the user actually labelled the plate with —
    then the loaded material-station slot, then the extruder's reported type
    (which arrives as a ";"-joined pair like "PLA;PLA").
    """
    guess = material_from_name((n.get("job") or {}).get("file"))
    if guess:
        return guess
    for s in (n.get("material_station") or {}).get("slots") or []:
        if s.get("active") and s.get("material"):
            return str(s["material"]).upper()
    f = n.get("filament") or {}
    for raw in (f.get("right_type"), f.get("left_type")):
        part = str(raw or "").split(";")[0].strip().upper()
        if part:
            return part
    return ""


def ff_post(path, extra=None, timeout=8):
    body = {"serialNumber": SERIAL, "checkCode": CHECK_CODE}
    if extra:
        body.update(extra)
    r = requests.post(BASE + path, json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_detail():
    """Return (detail_dict, error_string)."""
    try:
        data = ff_post("/detail")
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if data.get("code") != 0:
        return None, data.get("message", "unknown printer error")
    # Firmware nests the payload under a couple of different keys.
    detail = data.get("detail") or data.get("data") or {}
    if isinstance(detail, dict) and "detail" in detail:
        detail = detail["detail"]
    return detail, None


def pick(d, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def to_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def normalize(d):
    """Map raw firmware fields onto a stable shape for the UI."""
    if not d:
        return {}

    progress = to_float(pick(d, "printProgress", "progress"), 0.0) or 0.0
    if progress <= 1.0:  # firmware reports 0..1
        progress *= 100.0

    status = str(pick(d, "status", "machineStatus", default="unknown")).lower()
    light = str(pick(d, "lightStatus", "led", default="")).lower()

    # This machine is a 4-nozzle tool-changer: temps arrive as parallel arrays,
    # not the single nozzleTemp/nozzleTargetTemp of single-extruder firmware.
    temps_arr = d.get("nozzleTemps") or []
    targs_arr = d.get("nozzleTargetTemps") or []
    if not temps_arr and pick(d, "nozzleTemp") is not None:
        temps_arr = [to_float(pick(d, "nozzleTemp"), 0)]
        targs_arr = [to_float(pick(d, "nozzleTargetTemp"), 0)]
    nozzles = []
    for i, t in enumerate(temps_arr):
        nozzles.append({
            "id": i + 1,
            "temp": to_float(t, 0) or 0,
            "target": to_float(targs_arr[i] if i < len(targs_arr) else 0, 0) or 0,
        })
    # The tool in use is the one with a live target; fall back to the hottest.
    active = None
    heated = [n for n in nozzles if n["target"] > 0]
    if heated:
        active = max(heated, key=lambda n: n["target"])
    elif nozzles:
        active = max(nozzles, key=lambda n: n["temp"])
    active = active or {"id": 0, "temp": 0, "target": 0}

    # Material station (4-slot AMS-style feeder).
    ms = d.get("matlStationInfo") or {}
    slots = []
    for s in (ms.get("slotInfos") or []):
        slots.append({
            "id": s.get("slotId"),
            "material": s.get("materialName") or "",
            "color": s.get("materialColor") or "",
            "loaded": bool(s.get("hasFilament")),
            "active": s.get("slotId") == ms.get("currentSlot"),
        })

    # estimatedTime is time REMAINING, not job total (verified against
    # printDuration vs printProgress on a live job).
    elapsed = to_float(pick(d, "printDuration"), 0) or 0
    remaining = to_float(pick(d, "estimatedTime"), 0) or 0

    return {
        "name": pick(d, "name", "machineName", default="Creator 5 Pro"),
        "model": pick(d, "model", default=""),
        "status": status,
        "printing": status in ("printing", "busy", "heating"),
        "error_code": pick(d, "errorCode", default=""),
        "firmware": pick(d, "firmwareVersion", default=""),
        "ip": pick(d, "ipAddr", default=PRINTER_HOST),
        "mac": pick(d, "macAddr", default=""),
        "location": pick(d, "location", default=""),
        "light_on": light in ("open", "on", "1", "true"),
        "has_camera": bool(d.get("camera")),
        "job": {
            "file": pick(d, "printFileName", "fileName", default=""),
            "has_thumb": bool(pick(d, "printFileThumbUrl", default="")),
            "progress": round(progress, 1),
            "layer": to_float(pick(d, "printLayer"), 0),
            "layer_total": to_float(pick(d, "targetPrintLayer"), 0),
            "elapsed_s": elapsed,
            "remaining_s": remaining,
            "total_s": elapsed + remaining,
            "speed_mm_s": to_float(pick(d, "currentPrintSpeed"), 0),
            "speed_pct": to_float(pick(d, "printSpeedAdjust"), 0),
            "fill": to_float(pick(d, "fillAmount"), 0),
        },
        "nozzles": nozzles,
        "temps": {
            "nozzle": active["temp"],
            "nozzle_target": active["target"],
            "nozzle_id": active["id"],
            "bed": to_float(pick(d, "platTemp"), 0),
            "bed_target": to_float(pick(d, "platTargetTemp"), 0),
            "chamber": to_float(pick(d, "chamberTemp"), 0),
            "chamber_target": to_float(pick(d, "chamberTargetTemp"), 0),
        },
        "material_station": {
            "slots": slots,
            "slot_count": ms.get("slotCnt") or len(slots),
            "current_slot": ms.get("currentSlot"),
        },
        "filament": {
            "left_type": pick(d, "leftFilamentType", default=""),
            "right_type": pick(d, "rightFilamentType", default=""),
            "est_len_m": to_float(pick(d, "estimatedRightLen"), 0),
            "est_weight_g": to_float(pick(d, "estimatedRightWeight"), 0),
            "cumulative_m": to_float(pick(d, "cumulativeFilament"), 0),
        },
        "fans": {
            "cooling": to_float(pick(d, "coolingFanSpeed"), 0),
            "chamber": to_float(pick(d, "chamberFanSpeed"), 0),
            "internal": pick(d, "internalFanStatus", default=""),
            "external": pick(d, "externalFanStatus", default=""),
        },
        "machine": {
            "nozzle_model": pick(d, "nozzleModel", default=""),
            "nozzle_count": pick(d, "nozzleCnt", default=""),
            "door": pick(d, "doorStatus", default=""),
            "tvoc": to_float(pick(d, "tvoc"), 0),
            "disk_free_gb": to_float(pick(d, "remainingDiskSpace"), 0),
            # cumulativePrintTime is reported in minutes.
            "cumulative_print_s": (to_float(pick(d, "cumulativePrintTime"), 0) or 0) * 60,
            "measure": pick(d, "measure", default=""),
            "lidar": bool(d.get("lidar")),
        },
    }


# --- local job history (the firmware exposes lifetime totals but no job log) ---

def load_history():
    try:
        with open(HISTORY_FILE) as fh:
            hist = json.load(fh)
    except Exception:
        hist = {}
    hist.setdefault("jobs", [])
    hist.setdefault("current", None)   # in-flight job's filament bookkeeping
    return hist


def save_history(hist):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(hist, fh)
        os.replace(tmp, HISTORY_FILE)
    except Exception:
        pass


def advance_counter(cur, cum):
    """Fold a fresh cumulativeFilament reading into the live job's tally.

    Returns the metres consumed since the previous reading, which is what gets
    attributed to a material-station slot. A counter that stands still, goes
    backwards (firmware reset / bad read) or leaps implausibly yields 0 — those
    are bookkeeping noise, not filament.
    """
    if not cum:
        return 0.0
    last = cur.get("last_cum_m")
    if last is None:
        cur["last_cum_m"] = cum
        return 0.0
    if cum < last:
        cur["last_cum_m"] = cum        # counter reset; re-baseline, bank nothing
        return 0.0
    cur["last_cum_m"] = cum
    delta = cum - last
    return delta if delta <= MAX_DELTA_M else 0.0


def track_filament(hist, n):
    """Maintain hist["current"] — the running filament tally for the live job.

    There is no per-job filament figure in the API (estimatedRightLen reads 0
    on this machine), but cumulativeFilament is a live, monotonically rising
    lifetime metre count. So a job's usage is simply the delta across it: latch
    the counter when the job starts and subtract when it finishes.

    A job already underway when we first see it has no baseline — the delta
    would only cover the tail of it — so its length is reported as unknown
    rather than as a number that is quietly too low. printDuration is the tell:
    a job we caught from the start is only seconds old on the first poll.
    """
    cur = hist.get("current") or {}
    cum = n["filament"]["cumulative_m"]
    fname = n["job"]["file"]

    if cur.get("file") != fname:
        missed_start = (n["job"]["elapsed_s"] or 0) > MISSED_START_S
        cur = {
            "file": fname,
            "started_at": int(time.time()),
            "start_cum_m": None if missed_start else cum,
            "last_cum_m": cum,
            "material": job_material(n),
            # Per-filament breakdown for this job, keyed material|colour. Unlike
            # start_cum_m this works even for a job we joined late: it only ever
            # counts deltas we actually saw.
            "by_filament": {},
        }
        delta = 0.0
    else:
        delta = advance_counter(cur, cum)
        if not cur.get("material"):
            cur["material"] = job_material(n)
        cur.setdefault("by_filament", {})
    hist["current"] = cur
    return cur, delta


def attribute_slot(n):
    """Which spool is feeding right now — (material, colour, slot_id).

    The station names the feeding slot in `currentSlot`, but that reads 0 when
    nothing is selected, and a tool-changer job can be mid-swap. So fall back
    to the one loaded slot whose material matches the job, and failing that to
    the job's material with an unknown colour — a filament is still tracked by
    type even when we cannot pin the colour down.
    """
    slots = (n.get("material_station") or {}).get("slots") or []
    active = next((s for s in slots if s.get("active") and s.get("material")), None)
    if active:
        return active.get("material"), active.get("color"), active.get("id")

    material = job_material(n)
    if material:
        matches = [s for s in slots if s.get("loaded")
                   and str(s.get("material") or "").upper() == material]
        if len(matches) == 1:
            return matches[0].get("material"), matches[0].get("color"), matches[0].get("id")
    return material, "", None


def attribute(cur, n, delta_m):
    """Bank `delta_m` against the feeding spool, live.

    Written straight through to the database rather than held until the job
    ends, so the page tracks a running print — and so a cancelled job still
    counts the filament it really burned.
    """
    material, color, slot_id = attribute_slot(n)
    if not material:
        return                      # nothing to attribute it to; drop it
    grams = delta_m * grams_per_m(material)
    materials.add_usage(material, color, delta_m, grams)

    b = cur.setdefault("by_filament", {}).setdefault(
        materials.key(material, color),
        {"material": materials.norm_material(material),
         "color": materials.norm_color(color), "slot": slot_id, "m": 0.0, "g": 0.0})
    b["m"] += delta_m
    b["g"] += grams
    if slot_id:
        b["slot"] = slot_id


def current_filament_m(cur):
    """Metres consumed so far by the tracked job, or None if unknowable."""
    if not cur:
        return None
    start, last = cur.get("start_cum_m"), cur.get("last_cum_m")
    if start is None or last is None:
        return None
    return round(max(0.0, last - start), 2)


def poller():
    """Background poll: keeps the cache warm and logs finished jobs."""
    global _current
    last_printing = False
    _current = load_history().get("current")   # resume a job we were mid-way through
    while True:
        detail, err = fetch_detail()
        with _lock:
            if detail is not None:
                _cache.update(detail=detail, ts=time.time(), error=None)
            else:
                _cache["error"] = err

        if detail:
            n = normalize(detail)
            printing = n["printing"]
            hist = load_history()
            cur = hist.get("current") or {}

            materials.register_slots((n.get("material_station") or {}).get("slots"))

            if printing:
                # One final counter read lands here before the job flips out of
                # "printing", so the tally is current when we close it out below.
                _current, delta = track_filament(hist, n)
                if delta:
                    attribute(_current, n, delta)
                save_history(hist)
            elif last_printing and cur.get("file"):
                # printing -> not printing == a finished job. Fold in the last
                # counter value first; it can still tick up on the final poll.
                delta = advance_counter(cur, n["filament"]["cumulative_m"])
                if delta:
                    attribute(cur, n, delta)
                length_m = current_filament_m(cur)
                material = cur.get("material") or ""
                materials.commit_job(cur["file"], int(time.time()),
                                     cur.get("by_filament"))
                hist["jobs"].append({
                    "file": cur["file"],
                    "finished_at": int(time.time()),
                    "duration_s": n["job"]["elapsed_s"],
                    "status": n["status"],
                    "material": material,
                    "filament_m": length_m,
                    "filament_g": filament_grams(length_m, material) if length_m else None,
                    "grams_estimated": bool(length_m) and not DENSITY.get(material.upper()),
                })
                hist["jobs"] = hist["jobs"][-500:]
                hist["current"] = _current = None
                save_history(hist)
            last_printing = printing
        time.sleep(10)


@app.route("/")
def index():
    return render_template("index.html", printer_host=PRINTER_HOST,
                           configured=bool(SERIAL and CHECK_CODE))


def attach_live_filament(out):
    """Add the running filament tally to the live job, from the poller's state."""
    material = job_material(out)
    cur = _current or {}
    mine = cur.get("file") == out["job"]["file"]
    length_m = current_filament_m(cur) if mine else None
    out["job"]["material"] = material
    out["job"]["filament_tracked"] = mine and cur.get("start_cum_m") is not None
    out["job"]["filament_m"] = length_m
    out["job"]["filament_g"] = filament_grams(length_m, material) if length_m else None
    out["job"]["grams_estimated"] = not DENSITY.get(material.upper())
    return out


@app.route("/api/status")
def api_status():
    detail, err = fetch_detail()
    if detail is None:
        with _lock:
            stale = _cache.get("detail")
            age = time.time() - _cache.get("ts", 0)
        if stale:
            out = attach_live_filament(normalize(stale))
            out["_stale"] = True
            out["_age_s"] = int(age)
            out["_error"] = err
            return jsonify(out)
        return jsonify({"_error": err or "no data"}), 502
    out = attach_live_filament(normalize(detail))
    out["_stale"] = False
    return jsonify(out)


@app.route("/api/raw")
def api_raw():
    detail, err = fetch_detail()
    return jsonify({"detail": detail, "error": err})


def ff_control(cmd, args):
    """Send a control command.

    Command names are exactly as the FlashForge desktop app sends them —
    captured off the wire. Note `lightControl_cmd` carries a `_cmd` suffix
    while `streamCtrl` does not; the firmware silently ignores anything it
    doesn't recognise (and still answers "Success"), so these strings are
    load-bearing. Don't "tidy" them.
    """
    try:
        res = ff_post("/control", {"payload": {"cmd": cmd, "args": args}})
        return res.get("code") == 0, {"res": res}
    except Exception as exc:
        return False, {"error": f"{type(exc).__name__}: {exc}"}


@app.route("/api/light", methods=["POST"])
def api_light():
    """NOTE: /control answers {"code":0,"message":"Success"} to *any* command,
    including nonsense ones — so the reply proves nothing. Confirm by re-reading
    lightStatus instead of trusting it."""
    want_on = bool((request.get_json(silent=True) or {}).get("on"))
    ff_control("lightControl_cmd", {"status": "open" if want_on else "close"})

    for _ in range(4):
        time.sleep(0.8)
        detail, _err = fetch_detail()
        if detail:
            now_on = str(detail.get("lightStatus", "")).lower() in ("open", "on", "1", "true")
            if now_on == want_on:
                return jsonify({"ok": True, "on": now_on, "confirmed": True})
    detail, _err = fetch_detail()
    now_on = str((detail or {}).get("lightStatus", "")).lower() in ("open", "on", "1", "true")
    printing = str((detail or {}).get("status", "")).lower() == "printing"
    return jsonify({
        "ok": False, "on": now_on, "confirmed": False,
        "reason": "The printer accepted the command but did not change lightStatus"
                  + (" — it appears to lock the light on during a print." if printing else "."),
    })


@app.route("/api/clear_job", methods=["POST"])
def api_clear_job():
    """Dismiss the printer's on-screen "print complete" dialog.

    This is the same stateCtrl_cmd/setClearPlatform call the vendor app sends
    for its "Clear Platform" button. It only does anything meaningful when the
    printer is in the "completed" state; sending it otherwise is harmless
    (silently ACKed like any command) but won't change anything. As with
    /api/light, the /control reply proves nothing — confirm by re-reading
    status.
    """
    ff_control("stateCtrl_cmd", {"action": "setClearPlatform"})

    for _ in range(4):
        time.sleep(0.8)
        detail, _err = fetch_detail()
        if detail:
            status = str(detail.get("status", "")).lower()
            if status != "completed":
                return jsonify({"ok": True, "status": status, "confirmed": True})
    detail, _err = fetch_detail()
    status = str((detail or {}).get("status", "")).lower()
    return jsonify({
        "ok": False, "status": status, "confirmed": False,
        "reason": "The printer accepted the command but status is still \"completed\".",
    })


@app.route("/camera/thumb")
def camera_thumb():
    """Current job thumbnail — plain GET on the printer, no auth body."""
    try:
        r = requests.get(f"{BASE}/getThum", timeout=8)
        r.raise_for_status()
    except Exception as exc:
        return Response(f"no thumbnail: {exc}", status=502)
    return Response(r.content,
                    content_type=r.headers.get("Content-Type", "image/bmp"),
                    headers={"Cache-Control": "no-store"})


@app.route("/api/camera", methods=["POST"])
def api_camera():
    """The printer keeps its MJPEG server gated until the stream is enabled."""
    want_on = bool((request.get_json(silent=True) or {}).get("on"))
    with _cam.cond:
        if not want_on and _cam.viewers > 0:
            return jsonify({"ok": True, "on": True, "shared": True})
    ok, info = ff_control("streamCtrl", {"action": "open" if want_on else "close"})
    return jsonify({"ok": ok, "on": want_on, "detail": info})


@app.route("/api/history")
def api_history():
    hist = load_history()
    jobs = hist.get("jobs", [])
    # Jobs logged before filament tracking existed carry no length; they are
    # left out of the totals rather than counted as zero.
    tracked = [j for j in jobs if j.get("filament_m")]
    return jsonify({
        "count": len(jobs),
        "total_duration_s": sum(j.get("duration_s") or 0 for j in jobs),
        "tracked_count": len(tracked),
        "total_filament_m": round(sum(j["filament_m"] for j in tracked), 2),
        "total_filament_g": round(sum(j.get("filament_g") or 0 for j in tracked), 1),
        "jobs": list(reversed(jobs[-50:])),
    })


@app.route("/materials")
def materials_page():
    return render_template("materials.html", printer_host=PRINTER_HOST)


def loaded_slot_map():
    """slot ids currently holding each (material, colour), from the last poll."""
    with _lock:
        detail = _cache.get("detail")
    out = {}
    if not detail:
        return out
    for s in (normalize(detail).get("material_station") or {}).get("slots") or []:
        if s.get("loaded") and s.get("material"):
            out.setdefault(materials.key(s["material"], s.get("color")), []).append(s.get("id"))
    return out


@app.route("/api/materials")
def api_materials():
    """The material tracking table: one row per material + colour.

    Spool figures zero out when the user starts a new spool; lifetime figures
    never do. `live` is the running job's attribution so far — already included
    in the spool numbers, and shown separately only so it is obvious which
    spool is currently ticking up.
    """
    fils = materials.list_filaments()
    loaded = loaded_slot_map()
    for f in fils:
        f["loaded_slots"] = sorted(loaded.get(materials.key(f["material"], f["color"]), []))

    cur = _current or {}
    live = [dict(b, m=round(b["m"], 2), g=round(b["g"], 1))
            for b in (cur.get("by_filament") or {}).values()]
    return jsonify({
        "filaments": fils,
        "summary": materials.summary(fils),
        "events": materials.recent_events(),
        "spools": materials.spool_log(),
        "live": {"file": cur.get("file") or "", "by_filament": live},
        "default_spool_g": materials.DEFAULT_SPOOL_G,
    })


@app.route("/api/materials/<int:fid>/new_spool", methods=["POST"])
def api_new_spool(fid):
    """Start a new spool: archive this spool's tally and zero it. Lifetime stays."""
    note = (request.get_json(silent=True) or {}).get("note", "")
    row = materials.reset_spool(fid, note)
    if not row:
        return jsonify({"ok": False, "error": "no such filament"}), 404
    return jsonify({"ok": True, "filament": row})


@app.route("/api/materials/<int:fid>", methods=["POST"])
def api_update_material(fid):
    body = request.get_json(silent=True) or {}
    row = materials.update_filament(fid, label=body.get("label"),
                                    capacity_g=body.get("capacity_g"))
    if not row:
        return jsonify({"ok": False, "error": "nothing to update"}), 400
    return jsonify({"ok": True, "filament": row})


@app.route("/api/materials/<int:fid>", methods=["DELETE"])
def api_delete_material(fid):
    """Remove a row entirely — including its lifetime total and history."""
    return jsonify({"ok": materials.delete_filament(fid)})


# ── Camera: one upstream connection shared by all viewers ─────────────────
import threading


class _CamHub:
    """Printer serves one MJPEG client: keep one upstream, fan frames out."""

    def __init__(self):
        self.cond = threading.Condition()
        self.frame = None
        self.seq = 0
        self.updated_at = 0
        self.viewers = 0
        self.thread = None

    def join(self):
        with self.cond:
            self.viewers += 1
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()

    def leave(self):
        with self.cond:
            self.viewers = max(0, self.viewers - 1)

    def snapshot(self, timeout=20):
        with self.cond:
            ok = self.cond.wait_for(
                lambda: self.frame is not None and time.monotonic() - self.updated_at < 15,
                timeout=timeout)
            if not ok:
                from automation import AutomationError
                raise AutomationError("Свежий кадр камеры недоступен.")
            return self.frame

    def _alive(self):
        with self.cond:
            if self.viewers <= 0:
                self.thread = None
                self.frame = None
                return False
            return True

    def _run(self):
        while self._alive():
            up = None
            try:
                ff_control("streamCtrl", {"action": "open"})
                time.sleep(2.0)
                up = requests.get(CAM_URL, stream=True, timeout=10)
                buf = b""
                for chunk in up.iter_content(chunk_size=4096):
                    if not self._alive():
                        return
                    buf += chunk
                    if len(buf) > 8 * 1024 * 1024:
                        buf = b""
                        continue
                    while True:
                        start = buf.find(b"\xff\xd8")
                        if start < 0:
                            buf = buf[-1:]
                            break
                        end = buf.find(b"\xff\xd9", start + 2)
                        if end < 0:
                            buf = buf[start:]
                            break
                        with self.cond:
                            self.frame = buf[start:end + 2]
                            self.updated_at = time.monotonic()
                            self.seq += 1
                            self.cond.notify_all()
                        buf = buf[end + 2:]
            except Exception:
                pass
            finally:
                if up is not None:
                    up.close()
            time.sleep(3.0)


_cam = _CamHub()


@app.route("/camera/stream")
def camera_stream():
    """Proxy the printer MJPEG; any number of viewers share one upstream."""
    _cam.join()

    def gen():
        last = 0
        first = True
        try:
            while True:
                with _cam.cond:
                    _cam.cond.wait_for(lambda: _cam.seq != last and _cam.frame is not None,
                                       timeout=30 if first else 60)
                    if _cam.seq == last or _cam.frame is None:
                        return
                    frame, last = _cam.frame, _cam.seq
                first = False
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
        finally:
            _cam.leave()

    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


def cached_status():
    with _lock:
        detail = _cache.get("detail")
        timestamp = _cache.get("ts", 0)
        error = _cache.get("error")
    if not detail:
        return {"_stale": True}
    out = normalize(detail)
    out["_stale"] = bool(error) or time.time() - timestamp > 30
    return out


materials.init_db()
from automation import Automation
from automation_web import register_automation
_background = os.environ.get("DASHBOARD_BACKGROUND", "1") == "1"
automation = Automation(DATA_DIR, cached_status, _cam, start=_background)
register_automation(app, automation)
if _background:
    threading.Thread(target=poller, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
