"""Per-filament (material + colour) usage tracking.

The firmware has no per-spool accounting — the only filament figure it exposes
is `cumulativeFilament`, a lifetime metre counter for the whole machine. This
module turns that counter into per-filament numbers: app.py hands over each
poll-to-poll delta along with whichever material-station slot was feeding at
the time, and we bank it against that slot's (material, colour) pair.

Two counters are kept per filament:

  * the *spool* tally — what the current spool has printed. Zeroed when the
    user swaps spools ("New spool"), and archived to `spool_history` so the
    old spool's final figure is not lost.
  * the *lifetime* tally — every metre ever attributed to that material and
    colour. Never reset, so "how much red PLA do we go through" stays
    answerable across any number of spools.

Storage is SQLite in DATA_DIR rather than the JSON history file: the counters
are read-modify-write on every poll and want real transactions. The app runs a
single gunicorn worker, so the module-level write lock is sufficient.
"""
import os
import sqlite3
import threading
import time

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_FILE = os.path.join(DATA_DIR, "materials.db")

# Default spool size, in grams, used for the "remaining" estimate until the
# user sets a real one. 0 on a row means "don't estimate remaining at all".
DEFAULT_SPOOL_G = float(os.environ.get("DEFAULT_SPOOL_G", "1000"))

_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS filaments (
  id               INTEGER PRIMARY KEY,
  material         TEXT    NOT NULL,
  color            TEXT    NOT NULL,          -- '#RRGGBB', or '' if unknown
  label            TEXT    NOT NULL DEFAULT '',
  capacity_g       REAL    NOT NULL DEFAULT 0,
  first_seen       INTEGER NOT NULL,
  last_used        INTEGER,
  spool_started_at INTEGER NOT NULL,
  spool_m          REAL    NOT NULL DEFAULT 0,
  spool_g          REAL    NOT NULL DEFAULT 0,
  spool_jobs       INTEGER NOT NULL DEFAULT 0,
  life_m           REAL    NOT NULL DEFAULT 0,
  life_g           REAL    NOT NULL DEFAULT 0,
  life_jobs        INTEGER NOT NULL DEFAULT 0,
  spool_count      INTEGER NOT NULL DEFAULT 1, -- how many spools of this we've run
  UNIQUE(material, color)
);

-- One row per (job, filament): what a finished job took from each spool.
CREATE TABLE IF NOT EXISTS usage_events (
  id          INTEGER PRIMARY KEY,
  filament_id INTEGER NOT NULL,
  job_file    TEXT    NOT NULL DEFAULT '',
  slot_id     INTEGER,
  finished_at INTEGER NOT NULL,
  meters      REAL    NOT NULL DEFAULT 0,
  grams       REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_usage_finished ON usage_events(finished_at);

-- What each retired spool consumed, written when the user starts a new one.
CREATE TABLE IF NOT EXISTS spool_history (
  id          INTEGER PRIMARY KEY,
  filament_id INTEGER NOT NULL,
  started_at  INTEGER,
  ended_at    INTEGER NOT NULL,
  meters      REAL    NOT NULL DEFAULT 0,
  grams       REAL    NOT NULL DEFAULT 0,
  jobs        INTEGER NOT NULL DEFAULT 0,
  note        TEXT    NOT NULL DEFAULT ''
);
"""


def _conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_FILE, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=15000")
    return c


def init_db():
    with _write_lock, _conn() as c:
        c.executescript(SCHEMA)


# --- identity -------------------------------------------------------------

def norm_material(m):
    return str(m or "").strip().upper()


def norm_color(c):
    """Normalise a slot colour to '#RRGGBB'.

    The station reports colours as '#8C8C89'. Anything else — blank, a name,
    an odd length — is passed through uppercased so two spools that report the
    same odd string still collapse onto one row.
    """
    c = str(c or "").strip().upper()
    if not c:
        return ""
    if not c.startswith("#"):
        c = "#" + c
    return c


def key(material, color):
    return f"{norm_material(material)}|{norm_color(color)}"


def _get_id(c, material, color, now):
    """Row id for a (material, colour), creating the row on first sight."""
    material, color = norm_material(material), norm_color(color)
    row = c.execute("SELECT id FROM filaments WHERE material=? AND color=?",
                    (material, color)).fetchone()
    if row:
        return row["id"]
    cur = c.execute(
        "INSERT INTO filaments (material,color,capacity_g,first_seen,spool_started_at)"
        " VALUES (?,?,?,?,?)", (material, color, DEFAULT_SPOOL_G, now, now))
    return cur.lastrowid


def register_slots(slots):
    """Make sure every loaded material-station slot has a row.

    Called on each poll so a spool shows up in the table as soon as it is
    loaded, before it has printed anything.
    """
    seen = [(s.get("material"), s.get("color")) for s in (slots or [])
            if s.get("loaded") and s.get("material")]
    if not seen:
        return
    now = int(time.time())
    with _write_lock, _conn() as c:
        for material, color in seen:
            _get_id(c, material, color, now)


# --- metering -------------------------------------------------------------

def add_usage(material, color, meters, grams):
    """Bank a metered delta against a filament's spool and lifetime tallies.

    Called live during a print (not at job end) so the page reflects a running
    job, and so a job that is cancelled still counts the filament it really
    burned.
    """
    if not meters or meters <= 0:
        return
    now = int(time.time())
    with _write_lock, _conn() as c:
        fid = _get_id(c, material, color, now)
        c.execute(
            "UPDATE filaments SET spool_m=spool_m+?, spool_g=spool_g+?,"
            " life_m=life_m+?, life_g=life_g+?, last_used=? WHERE id=?",
            (meters, grams or 0, meters, grams or 0, now, fid))


def commit_job(job_file, finished_at, buckets):
    """Record a finished job's per-filament totals and bump the job counters.

    `buckets` is the tally app.py accumulated across the job, keyed by
    key(material, colour). The metres themselves are already banked by
    add_usage — this only writes the per-job event rows and the job counts.
    """
    rows = [b for b in (buckets or {}).values() if (b.get("m") or 0) > 0]
    if not rows:
        return
    with _write_lock, _conn() as c:
        for b in rows:
            fid = _get_id(c, b.get("material"), b.get("color"), finished_at)
            c.execute(
                "INSERT INTO usage_events (filament_id,job_file,slot_id,finished_at,meters,grams)"
                " VALUES (?,?,?,?,?,?)",
                (fid, job_file or "", b.get("slot"), finished_at,
                 round(b.get("m") or 0, 3), round(b.get("g") or 0, 2)))
            c.execute("UPDATE filaments SET spool_jobs=spool_jobs+1,"
                      " life_jobs=life_jobs+1, last_used=? WHERE id=?",
                      (finished_at, fid))


# --- reads ----------------------------------------------------------------

def _fil_dict(r):
    cap = r["capacity_g"] or 0
    used = r["spool_g"] or 0
    return {
        "id": r["id"],
        "material": r["material"],
        "color": r["color"],
        "label": r["label"],
        "capacity_g": round(cap, 1),
        "first_seen": r["first_seen"],
        "last_used": r["last_used"],
        "spool_count": r["spool_count"],
        "spool": {
            "started_at": r["spool_started_at"],
            "m": round(r["spool_m"] or 0, 2),
            "g": round(used, 1),
            "jobs": r["spool_jobs"],
            "remaining_g": round(max(0.0, cap - used), 1) if cap else None,
            "used_pct": round(min(100.0, used / cap * 100), 1) if cap else None,
        },
        "lifetime": {
            "m": round(r["life_m"] or 0, 2),
            "g": round(r["life_g"] or 0, 1),
            "jobs": r["life_jobs"],
        },
    }


def list_filaments():
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM filaments ORDER BY life_g DESC, material, color").fetchall()
    return [_fil_dict(r) for r in rows]


def recent_events(limit=40):
    with _conn() as c:
        rows = c.execute(
            "SELECT e.*, f.material, f.color, f.label FROM usage_events e"
            " JOIN filaments f ON f.id=e.filament_id"
            " ORDER BY e.finished_at DESC, e.id DESC LIMIT ?", (limit,)).fetchall()
    return [{
        "filament_id": r["filament_id"], "material": r["material"],
        "color": r["color"], "label": r["label"], "job_file": r["job_file"],
        "slot_id": r["slot_id"], "finished_at": r["finished_at"],
        "meters": round(r["meters"], 2), "grams": round(r["grams"], 1),
    } for r in rows]


def spool_log(limit=40):
    with _conn() as c:
        rows = c.execute(
            "SELECT h.*, f.material, f.color, f.label FROM spool_history h"
            " JOIN filaments f ON f.id=h.filament_id"
            " ORDER BY h.ended_at DESC LIMIT ?", (limit,)).fetchall()
    return [{
        "filament_id": r["filament_id"], "material": r["material"],
        "color": r["color"], "label": r["label"], "started_at": r["started_at"],
        "ended_at": r["ended_at"], "meters": round(r["meters"], 2),
        "grams": round(r["grams"], 1), "jobs": r["jobs"], "note": r["note"],
    } for r in rows]


def summary(filaments):
    by_mat = {}
    for f in filaments:
        m = by_mat.setdefault(f["material"] or "?",
                              {"material": f["material"] or "?", "m": 0.0, "g": 0.0, "colors": 0})
        m["m"] += f["lifetime"]["m"]
        m["g"] += f["lifetime"]["g"]
        m["colors"] += 1
    for m in by_mat.values():
        m["m"], m["g"] = round(m["m"], 2), round(m["g"], 1)
    return {
        "filaments": len(filaments),
        "lifetime_m": round(sum(f["lifetime"]["m"] for f in filaments), 2),
        "lifetime_g": round(sum(f["lifetime"]["g"] for f in filaments), 1),
        "spool_g": round(sum(f["spool"]["g"] for f in filaments), 1),
        "by_material": sorted(by_mat.values(), key=lambda x: -x["g"]),
    }


# --- mutations ------------------------------------------------------------

def reset_spool(fid, note=""):
    """Start a new spool: archive the current tally, zero it, keep lifetime."""
    now = int(time.time())
    with _write_lock, _conn() as c:
        r = c.execute("SELECT * FROM filaments WHERE id=?", (fid,)).fetchone()
        if not r:
            return None
        # Only archive a spool that actually printed something — otherwise a
        # double-click on "New spool" litters the log with empty rows.
        if (r["spool_m"] or 0) > 0 or (r["spool_jobs"] or 0) > 0:
            c.execute(
                "INSERT INTO spool_history (filament_id,started_at,ended_at,meters,grams,jobs,note)"
                " VALUES (?,?,?,?,?,?,?)",
                (fid, r["spool_started_at"], now, r["spool_m"], r["spool_g"],
                 r["spool_jobs"], str(note or "")[:200]))
        c.execute("UPDATE filaments SET spool_m=0, spool_g=0, spool_jobs=0,"
                  " spool_started_at=?, spool_count=spool_count+1 WHERE id=?", (now, fid))
        r = c.execute("SELECT * FROM filaments WHERE id=?", (fid,)).fetchone()
    return _fil_dict(r)


def update_filament(fid, label=None, capacity_g=None):
    sets, vals = [], []
    if label is not None:
        sets.append("label=?")
        vals.append(str(label)[:60])
    if capacity_g is not None:
        try:
            sets.append("capacity_g=?")
            vals.append(max(0.0, float(capacity_g)))
        except (TypeError, ValueError):
            return None
    if not sets:
        return None
    with _write_lock, _conn() as c:
        c.execute(f"UPDATE filaments SET {','.join(sets)} WHERE id=?", vals + [fid])
        r = c.execute("SELECT * FROM filaments WHERE id=?", (fid,)).fetchone()
    return _fil_dict(r) if r else None


def delete_filament(fid):
    """Drop a filament row outright, along with its events and spool log.

    For rows created by a mis-read slot. A filament that is still loaded will
    simply be re-created by the next poll — with its counters back at zero.
    """
    with _write_lock, _conn() as c:
        c.execute("DELETE FROM usage_events WHERE filament_id=?", (fid,))
        c.execute("DELETE FROM spool_history WHERE filament_id=?", (fid,))
        cur = c.execute("DELETE FROM filaments WHERE id=?", (fid,))
    return cur.rowcount > 0
