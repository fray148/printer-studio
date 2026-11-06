# Самостоятельный форк Printer Studio

Развёртывается отдельным Docker Compose-проектом на порту **5012**.
Таймлапсы, управление файлами и Telegram добавлены к текущему интерфейсу.
Настройка и описание форка: [FORK.md](FORK.md).

---

# Creator 5 Pro Dashboard

A self-hosted web dashboard for the FlashForge **Creator 5 Pro** 3D printer. Live status, temperatures, material station, chamber camera, a working chamber-light toggle, per-spool filament tracking, and a remote dismiss for the on-screen finish dialog — served from a small Flask app in Docker, reachable from anywhere you can reach the container.

Built against firmware **1.9.4**. It talks to the printer's local HTTP API on port 8898 — no cloud account, no vendor app.

![status](https://img.shields.io/badge/firmware-1.9.4-blue) ![license](https://img.shields.io/badge/license-MIT-green) ![release](https://img.shields.io/badge/release-v1.4.0-blue)

---

## Features

- **Live job** — filename, thumbnail, percent, layer *n*/*N*, elapsed, remaining, speed, infill
- **Temperatures** — bed, chamber, and all four nozzles, with the active tool highlighted; toggle °C/°F with the header button (choice persists in the browser)
- **Material station** — all 4 slots with their real filament colours, material names, and which is loaded
- **Chamber camera** — MJPEG stream, off by default, toggled on demand
- **Chamber light** — on/off, with the result verified against the printer (see [why that matters](#the-api-lies-about-success))
- **Clear finish dialog** — remotely dismiss the on-screen "print complete" prompt (appears only while the printer reports `completed`)
- **Lifetime totals** — print hours, filament used, free storage, firmware, door, fans, TVOC
- **Job history** — the firmware keeps no per-job log, so the app records completed jobs itself
- **Per-job filament** — metres used and approximate grams for every job, measured from the machine's own counter (see [Filament tracking](#filament-tracking))
- **Material tracking** — a `/materials` page tallying usage per filament *type and colour*, with a resettable per-spool counter, a lifetime counter that never resets, and a remaining-weight estimate (see [Material tracking](#material-tracking))

---

## Quick start

```bash
git clone https://github.com/afeind/Creator-5-Pro-Dashboard.git
cd Creator-5-Pro-Dashboard

cp .env.example .env
$EDITOR .env          # <-- put your printer IP, serial, and access code here

docker compose up -d
```

Then open **http://localhost:5011**.

> **Where does the code go?** Everything you need to configure lives in **`.env`**, created by copying `.env.example`. That file is gitignored and is the only place credentials belong — nothing is hard-coded in the source. See [Configuration](#configuration).

---

## Finding your serial and access code

Two values are required, and they come from different places.

### Serial number — discoverable over the network

The printer answers a UDP discovery probe on port **48899** (or 19000) with **no authentication at all**. Send it any payload and it replies with its model name and serial:

```bash
python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
s.sendto(b'x', ('192.168.1.100', 48899))     # <-- your printer's IP
print(s.recvfrom(2048)[0])
"
```

You'll get back a fixed-width blob containing something like `Creator 5 Pro` and `SNXXXXXXXXXXX`. The `SN...` string is your serial.

### Access code — only on the printer's screen

The access code (the API calls it `checkCode`) is shown **only on the printer's own touchscreen**, under its network/connection settings. There is no way to retrieve it over the network — that's the point of it.

Sanity-check both values before starting the app:

```bash
curl -s -X POST http://192.168.1.100:8898/detail \
  -H 'Content-Type: application/json' \
  -d '{"serialNumber":"SNXXXXXXXXXXX","checkCode":"xxxxxxxx"}'
```

| Response | Meaning |
|---|---|
| `{"code":0,...}` plus a big JSON blob | Both correct |
| `{"code":1,"message":"Access code is different"}` | Serial fine, access code wrong |
| `{"code":-1,"message":"Parameters is error"}` | Malformed request / wrong serial |

---

## Configuration

| Variable | Required | Default | Notes |
|---|---|---|---|
| `PRINTER_HOST` | ✅ | `192.168.1.100` | Printer LAN IP |
| `PRINTER_SERIAL` | ✅ | — | `SN...`, see above |
| `PRINTER_CHECK_CODE` | ✅ | — | From the printer's touchscreen |
| `PRINTER_API_PORT` | | `8898` | HTTP API port |
| `PRINTER_CAM_PORT` | | `8080` | MJPEG camera port |
| `PRINTER_CAM_PATH` | | `/?action=stream` | Camera path |
| `DATA_DIR` | | `/data` | Where job history and the material database are written |
| `FILAMENT_DIAMETER_MM` | | `1.75` | Filament diameter, used to convert length to grams |
| `DEFAULT_SPOOL_G` | | `1000` | Assumed spool weight for the "remaining" estimate, per filament until you set a real one |

The container publishes port **5011**; change the mapping in `docker-compose.yml` if that clashes.

---

## Reverse proxy

To serve it behind a hostname, proxy to port 5011. **Disable response buffering**, or the camera stream will buffer forever and never render.

**Caddy**

```caddyfile
printer.example.com {
    reverse_proxy 127.0.0.1:5011 {
        flush_interval -1          # required for MJPEG
    }
}
```

**nginx**

```nginx
location / {
    proxy_pass http://127.0.0.1:5011;
    proxy_buffering off;           # required for MJPEG
    proxy_read_timeout 3600s;
}
```

This app has **no authentication of its own**. Anyone who can reach it can control your printer. Keep it on your LAN or behind your proxy's auth — don't expose it to the open internet.

---

## Notes on the printer's API

Undocumented behaviour discovered while building this. Recorded here because it cost real time and may save yours.

### The API lies about success

`POST /control` returns `{"code":0,"message":"Success"}` for **any** command — including ones that don't exist. Verified by sending `{"cmd":"totalNonsenseCmd"}` and getting `Success` back.

Consequence: **you cannot discover command names by trial and error.** Every wrong guess looks exactly like a right one. This app never trusts the reply — after sending a light command it re-reads `/detail` and reports what the printer actually says.

### The light command has a `_cmd` suffix

```jsonc
// works
{"cmd": "lightControl_cmd", "args": {"status": "open"}}   // or "close"

// silently does nothing, still returns Success
{"cmd": "lightControl",     "args": {"status": "open"}}
```

Meanwhile `streamCtrl` has **no** suffix. The naming is inconsistent between commands, so there's no rule to infer — these strings were recovered by packet-capturing the official desktop app.

### Dismissing the finish dialog is `stateCtrl_cmd` / `setClearPlatform`

```jsonc
{"cmd": "stateCtrl_cmd", "args": {"action": "setClearPlatform"}}
```

This is the same call the vendor app's "Clear Platform" button sends — corroborated across three independent third-party FlashForge clients. It's a no-op outside the `completed` state, so the dashboard only shows the button while `status == "completed"`.

### The camera is a one-shot

The MJPEG server at `:8080` refuses connections until you enable it:

```jsonc
{"cmd": "streamCtrl", "args": {"action": "open"}}
```

It then serves **exactly one client per enable** — when that client disconnects, the stream stops again. Re-enable before *every* connection and allow ~2–3 s for the encoder to come up. A browser holding one long-lived connection is fine; rapid connect/disconnect testing looks like random failure.

### `/detail` field quirks

| Field | Gotcha |
|---|---|
| `nozzleTemps` / `nozzleTargetTemps` | Arrays of 4 — there is no single `nozzleTemp`. Active tool = the one with target > 0 |
| `estimatedTime` | Time **remaining**, not job total. There is no `estimatedLeftTime` |
| `printProgress` | 0..1, not a percentage |
| `remainingDiskSpace` | **GB**, not MB |
| `cumulativePrintTime` | Minutes |
| `cumulativeFilament` | Lifetime **metres**, all tools combined. The only filament figure the firmware gives — there is no per-job length |
| `estimatedRightLen` / `estimatedRightWeight` | Always `0` on 1.9.4; do not rely on them for job filament |
| `matlStationInfo.slotInfos[]` | Per-slot `materialName`, `materialColor` (hex), `hasFilament`; `currentSlot` marks the feeding slot — and reads `0` when the machine is idle, so treat it as "none selected", not "slot 0" |

---

## Filament tracking

The firmware reports filament as a **length only**, and only as a lifetime running
total (`cumulativeFilament`, in metres). There is no per-job figure —
`estimatedRightLen` / `estimatedRightWeight` read `0` on firmware 1.9.4.

So the app measures each job as a **delta on that counter**: it latches the value
when a job starts, follows it while the job runs, and subtracts when the job ends.
The number is a measurement from the machine, not a slicer estimate, and it covers
all four tools together — usage is not broken out per head.

Two cases report **unknown** rather than a wrong number:

- A job already running when the dashboard first sees it (`printDuration` > 120 s on
  the first poll) has no baseline to subtract from, so its length would only cover
  the tail. Restarting the container mid-job is fine — the baseline is persisted to
  `history.json` and survives.
- Jobs finished before this feature existed. They keep their old rows and sit out of
  the filament totals; the counter fills in going forward.

### Grams

Mass is derived from length. A metre of round filament is
`π·(d/2)²·1000 mm³`, so at the stock 1.75 mm that is **2.405 cm³/m**; multiply by the
polymer density for grams per metre:

| Material | Density (g/cm³) | Grams per metre |
|---|---|---|
| PLA | 1.24 | 2.98 |
| PETG | 1.27 | 3.05 |
| ABS | 1.04 | 2.50 |
| ASA | 1.07 | 2.57 |
| TPU | 1.21 | 2.91 |

The material is taken from the filename first (`Tags_PETG_37m50s.gcode.3mf` → PETG),
since that is what the plate was actually labelled with, then from the loaded
material-station slot, then from the extruder's reported type. When none of those
identify it, the figure falls back to the shop average of **0.33 m per gram**
(≈3.03 g/m, between PLA and PETG) and the row is marked with an asterisk.

Set `FILAMENT_DIAMETER_MM` if you run 2.85 mm.

Because these come from a nominal diameter and a nominal density, treat the grams as
**approximate** — good enough for spool budgeting, not for cost accounting to the gram.

### Full API surface

`POST /detail`, `POST /control`, `POST /product`, `POST /uploadGcode`, `GET /getThum`. Everything else 404s. There is **no G-code passthrough**. All calls except `/getThum` require `{"serialNumber","checkCode"}` in the body.

---

## Material tracking

[Filament tracking](#filament-tracking) answers *how much did this job use*. The
**Materials** page (`/materials`, linked from the dashboard header) answers the other
question: *how much of each colour do we actually go through, and how much is left on
the spool in slot 2?*

Each row is one **material + colour** pair, taken straight from the material station —
that pair is what identifies a spool. Rows create themselves the moment a filament is
loaded, so a new spool appears in the table before it has printed anything.

### How usage is attributed

Usage is metered **live**, not at the end of a job. On every poll, the rise in
`cumulativeFilament` is charged to whichever slot is feeding at that moment. Two
consequences worth knowing:

- A **multi-colour job splits across rows** — a two-tone print charges each colour
  separately, which a job-level total cannot do.
- A **cancelled job still counts**, because the filament really was extruded.

Readings that go backwards (a firmware counter reset) or leap more than 10 m between
polls are discarded as noise rather than banked against a spool.

When the station does not name a feeding slot, the app falls back to the only loaded
slot whose material matches the job, and failing that to the job's material with an
unknown colour — so a filament is still tracked by type even when the colour can't be
pinned down.

### Two counters per filament

| Counter | Resets? | What it's for |
|---|---|---|
| **This spool** | Yes — the **New spool** button | What the spool currently loaded has printed, and what's left of it |
| **Lifetime** | Never | Total metres, grams and jobs for that colour across every spool you've ever run |

**New spool** files the outgoing spool's final total under *Finished spools* (with an
optional note — "ran out mid-print", "colour shift", whatever) and zeroes the spool
counter. The lifetime counter keeps counting straight through the swap. That split is
the point: swapping spools shouldn't cost you the long-run picture of what the shop
prints in.

**Edit** sets a friendly name and the spool's weight in grams, which drives the
remaining-weight bar — amber past 80 % used, red at empty. Set it to `0` to hide the
estimate for that filament. **✕** deletes a row outright, lifetime total and all; a
filament that is still loaded reappears on the next poll with its counters at zero.

### Scope

Tracking is **forward-only**. Jobs finished before this release have no per-colour
attribution to recover, so the table starts empty and fills in from the next print
onward. Nothing is backfilled and nothing existing is rewritten.

Data lives in `materials.db` (SQLite) alongside `history.json` in `DATA_DIR` — three
tables: `filaments`, `usage_events` (one row per job per filament) and `spool_history`.
Grams are derived from length exactly as described [above](#grams), so the same
"approximate, not accounting-grade" caveat applies.

---

## Endpoints

| Route | Purpose |
|---|---|
| `GET /` | Dashboard UI |
| `GET /materials` | Material tracking UI |
| `GET /api/status` | Normalised printer state |
| `GET /api/raw` | Raw `/detail` passthrough, for debugging |
| `POST /api/light` | `{"on": true\|false}` — verifies against the printer |
| `POST /api/camera` | `{"on": true\|false}` — enables/disables the stream |
| `POST /api/clear_job` | Dismiss the on-screen "print complete" dialog (same as the printer's own Clear Platform button); verifies status leaves `completed` |
| `GET /camera/stream` | Proxied MJPEG |
| `GET /camera/thumb` | Current job thumbnail |
| `GET /api/history` | Completed jobs recorded locally, with per-job filament metres and grams |
| `GET /api/materials` | Per-filament spool and lifetime totals, recent per-job usage, retired spools |
| `POST /api/materials/<id>/new_spool` | `{"note": "..."}` — archive this spool's total and zero it; lifetime is kept |
| `POST /api/materials/<id>` | `{"label": "...", "capacity_g": 1000}` — name a filament and set its spool weight |
| `DELETE /api/materials/<id>` | Remove a filament row, including its lifetime total and history |
| `GET /healthz` | Health check |

---

## Compatibility

Developed and tested against a **Creator 5 Pro on firmware 1.9.4**. Other FlashForge models using the same port-8898 API (Adventurer 5M / 5M Pro and relatives) may work, but field names differ between single- and multi-nozzle firmware — the normaliser in `app.py` handles both shapes where it can. Reports welcome.

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with, endorsed by, or supported by FlashForge.
