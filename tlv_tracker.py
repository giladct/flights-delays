#!/usr/bin/env python3
"""
TLV Departure Delay Tracker  —  AeroDataBox edition (free, no credit card)
Polls AeroDataBox via RapidAPI every 2 hours and stores departure data to SQLite.

Free setup (no credit card required):
  1. Sign up at https://rapidapi.com  (email only)
  2. Subscribe to AeroDataBox free plan:
     https://rapidapi.com/aedbx-aedbx/api/aerodatabox
  3. Copy your RapidAPI key from the dashboard
  4. pip install requests
  5. set RAPIDAPI_KEY=your_key_here   (Windows)

Usage:
  python tlv_tracker.py            # start polling loop
  python tlv_tracker.py analyze    # print delay report
  python tlv_tracker.py export     # generate tlv_dashboard/data.js
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:
    sys.exit("ERROR: run:  pip install requests")

API_KEY       = os.environ.get("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = "aerodatabox.p.rapidapi.com"
BASE_URL      = f"https://{RAPIDAPI_HOST}"
POLL_HOURS    = 2
POLL_SECONDS  = POLL_HOURS * 3600

# ── Airport config (override with --airport YYZ) ──────────────────────────────

_AIRPORT_CONFIGS = {
    'TLV': {'icao': 'LLBG', 'tz': 'Asia/Jerusalem',  'name': 'Ben Gurion'},
    'YYZ': {'icao': 'CYYZ', 'tz': 'America/Toronto', 'name': 'Toronto Pearson'},
    'DXB': {'icao': 'OMDB', 'tz': 'Asia/Dubai',      'name': 'Dubai International'},
    'JFK': {'icao': 'KJFK', 'tz': 'America/New_York','name': 'John F. Kennedy International'},
}
AIRPORT_IATA = 'TLV'
if '--airport' in sys.argv:
    _i = sys.argv.index('--airport')
    if _i + 1 < len(sys.argv):
        AIRPORT_IATA = sys.argv[_i + 1].upper()

_cfg         = _AIRPORT_CONFIGS.get(AIRPORT_IATA, _AIRPORT_CONFIGS['TLV'])
AIRPORT_ICAO = _cfg['icao']
LOCAL_TZ     = ZoneInfo(_cfg['tz'])
DB_PATH      = Path(__file__).parent / f"{AIRPORT_IATA.lower()}_flights.db"


# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flights (
            flight_id     TEXT PRIMARY KEY,
            ident         TEXT,
            airline_iata  TEXT,
            airline_name  TEXT,
            dest_iata     TEXT,
            dest_name     TEXT,
            dest_country  TEXT,
            scheduled_out TEXT,
            actual_out    TEXT,
            delay_minutes INTEGER,
            status        TEXT,
            fetched_at    TEXT,
            aircraft_type TEXT,
            dep_terminal  TEXT,
            dep_gate      TEXT,
            scheduled_in  TEXT
        )
    """)
    # Migrate existing DB — silently skip if columns already exist
    for col_def in [
        "ADD COLUMN aircraft_type TEXT",
        "ADD COLUMN dep_terminal  TEXT",
        "ADD COLUMN dep_gate      TEXT",
        "ADD COLUMN scheduled_in  TEXT",
    ]:
        try:
            conn.execute(f"ALTER TABLE flights {col_def}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


# ── API ───────────────────────────────────────────────────────────────────────

def fetch_departures(start_local: datetime, end_local: datetime) -> list:
    if not API_KEY:
        sys.exit(
            "ERROR: RAPIDAPI_KEY not set.\n"
            "  1. Sign up free at https://rapidapi.com\n"
            "  2. Subscribe to AeroDataBox: https://rapidapi.com/aedbx-aedbx/api/aerodatabox\n"
            "  3. set RAPIDAPI_KEY=your_key_here"
        )

    from_str = start_local.strftime("%Y-%m-%dT%H:%M")
    to_str   = end_local.strftime("%Y-%m-%dT%H:%M")
    url = f"{BASE_URL}/flights/airports/icao/{AIRPORT_ICAO}/{from_str}/{to_str}"

    headers = {
        "X-RapidAPI-Key":  API_KEY,
        "X-RapidAPI-Host": RAPIDAPI_HOST,
    }
    params = {
        "direction":      "Departure",
        "withLeg":        "true",
        "withCancelled":  "true",
        "withCodeshared": "false",
        "withCargo":      "false",
    }

    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("departures", [])


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_utc_time(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip().rstrip("Z").replace(" ", "T"))
    except ValueError:
        return None


def parse_delay(sched_utc: str | None, actual_utc: str | None) -> int | None:
    s = parse_utc_time(sched_utc)
    a = parse_utc_time(actual_utc)
    if s and a:
        return int((a - s).total_seconds() / 60)
    return None


def local_str(time_block: dict | None) -> str | None:
    """Extract 'YYYY-MM-DD HH:MM' from a scheduledTime/revisedTime block."""
    if not time_block:
        return None
    loc = time_block.get("local") or ""
    return loc[:16] if len(loc) >= 16 else None


def parse_flight(f: dict, fetched_at: str) -> dict | None:
    dep      = f.get("departure") or {}
    arr      = f.get("arrival")   or {}
    airport  = arr.get("airport") or {}
    airline  = f.get("airline")   or {}
    aircraft = f.get("aircraft")  or {}

    sched_block   = dep.get("scheduledTime") or {}
    revised_block = dep.get("revisedTime")   or {}   # actual/estimated gate time

    number = f.get("number") or ""
    sched_utc  = sched_block.get("utc")
    actual_utc = revised_block.get("utc")
    delay  = parse_delay(sched_utc, actual_utc)

    sched_local  = local_str(sched_block)
    actual_local = local_str(revised_block)

    if not number or not sched_local:
        return None

    # Unique ID: flight number + scheduled UTC (survives re-polls)
    flight_id = f"{number}_{(sched_utc or sched_local).replace(' ', 'T')}"

    return {
        "flight_id":     flight_id,
        "ident":         number,
        "airline_iata":  airline.get("iata", ""),
        "airline_name":  airline.get("name", ""),
        "dest_iata":     airport.get("iata", ""),
        "dest_name":     airport.get("name", ""),
        "dest_country":  airport.get("countryCode", ""),
        "scheduled_out": sched_local,
        "actual_out":    actual_local,
        "delay_minutes": delay,
        "status":        f.get("status", ""),
        "fetched_at":    fetched_at,
        "aircraft_type": aircraft.get("model", "") or "",
        "dep_terminal":  dep.get("terminal", "") or "",
        "dep_gate":      dep.get("gate", "") or "",
        "scheduled_in":  local_str(arr.get("scheduledTime") or {}),
    }


def save_flights(conn: sqlite3.Connection, raw_flights: list, fetched_at: str) -> int:
    saved = 0
    for raw in raw_flights:
        flight = parse_flight(raw, fetched_at)
        if not flight:
            continue
        # COALESCE keeps existing actual_out/delay if the new fetch has no data
        conn.execute("""
            INSERT INTO flights
              (flight_id, ident, airline_iata, airline_name,
               dest_iata, dest_name, dest_country,
               scheduled_out, actual_out, delay_minutes, status, fetched_at,
               aircraft_type, dep_terminal, dep_gate, scheduled_in)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(flight_id) DO UPDATE SET
              actual_out    = COALESCE(excluded.actual_out,    actual_out),
              delay_minutes = COALESCE(excluded.delay_minutes, delay_minutes),
              status        = excluded.status,
              fetched_at    = excluded.fetched_at,
              aircraft_type = COALESCE(excluded.aircraft_type, aircraft_type),
              dep_terminal  = COALESCE(excluded.dep_terminal,  dep_terminal),
              dep_gate      = COALESCE(excluded.dep_gate,      dep_gate),
              scheduled_in  = COALESCE(excluded.scheduled_in,  scheduled_in)
        """, (
            flight["flight_id"],    flight["ident"],
            flight["airline_iata"], flight["airline_name"],
            flight["dest_iata"],    flight["dest_name"],    flight["dest_country"],
            flight["scheduled_out"], flight["actual_out"],  flight["delay_minutes"],
            flight["status"],       flight["fetched_at"],
            flight["aircraft_type"], flight["dep_terminal"], flight["dep_gate"],
            flight["scheduled_in"],
        ))
        saved += 1
    conn.commit()
    return saved


# ── Poll ──────────────────────────────────────────────────────────────────────

def poll():
    conn = None
    now_utc     = datetime.now(timezone.utc)
    now_local   = now_utc.astimezone(LOCAL_TZ)
    start_local = now_local - timedelta(hours=POLL_HOURS)

    print(
        f"[{now_utc.strftime('%Y-%m-%d %H:%M')} UTC] "
        f"Fetching {start_local.strftime('%H:%M')}-{now_local.strftime('%H:%M')} local...",
        end=" ", flush=True,
    )
    try:
        conn     = init_db()
        raw      = fetch_departures(start_local, now_local)
        new_rows = save_flights(conn, raw, now_utc.isoformat())
        print(f"{len(raw)} returned, {new_rows} new/updated -> {DB_PATH.name}")
        conn.close()
        conn = None
        sync_iaa()
    except requests.HTTPError as e:
        print(f"HTTP {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        print(f"ERROR: {e}")
    finally:
        if conn:
            conn.close()


# ── Backfill ──────────────────────────────────────────────────────────────────

def backfill(days: int = 7):
    """Fetch all departures for the past `days` complete days (never today)."""
    conn = init_db()
    now_local   = datetime.now(timezone.utc).astimezone(LOCAL_TZ)
    end_local   = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = (end_local - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    CHUNK_HOURS = 12
    total_raw = total_new = 0
    chunk_start = start_local

    print(f"Backfilling {days} days: {start_local.strftime('%Y-%m-%d')} -> {end_local.strftime('%Y-%m-%d')}\n")
    while chunk_start < end_local:
        chunk_end = min(chunk_start + timedelta(hours=CHUNK_HOURS), end_local)
        label = (f"{chunk_start.strftime('%Y-%m-%d %H:%M')} -> "
                 f"{chunk_end.strftime('%H:%M')}")
        # Skip chunks that already have data — saves API calls
        existing = conn.execute(
            "SELECT COUNT(*) FROM flights WHERE scheduled_out >= ? AND scheduled_out < ?",
            (chunk_start.strftime('%Y-%m-%d %H:%M'), chunk_end.strftime('%Y-%m-%d %H:%M'))
        ).fetchone()[0]
        if existing > 0:
            print(f"  {label} — {existing} rows already in DB, skipped")
            chunk_start = chunk_end
            continue
        print(f"  {label} ...", end=" ", flush=True)
        try:
            raw      = fetch_departures(chunk_start, chunk_end)
            new_rows = save_flights(conn, raw, datetime.now(timezone.utc).isoformat())
            total_raw += len(raw)
            total_new += new_rows
            print(f"{len(raw)} flights, {new_rows} new/updated")
            time.sleep(1)
        except requests.HTTPError as e:
            print(f"HTTP {e.response.status_code}: {e.response.text[:120]}")
            if e.response.status_code == 429:
                print("Rate limit hit — stopping early.")
                break
        except Exception as e:
            print(f"ERROR: {e}")
        chunk_start = chunk_end

    conn.close()
    print(f"\nDone: {total_raw} flights fetched, {total_new} new/updated rows.")
    if AIRPORT_IATA == 'TLV':
        sync_iaa()


# ── Analyze ───────────────────────────────────────────────────────────────────

def analyze():
    if not DB_PATH.exists():
        sys.exit("No database found — run the tracker first.")

    conn = sqlite3.connect(DB_PATH)

    total, avg_d, max_d, first, last = conn.execute("""
        SELECT COUNT(*), AVG(delay_minutes), MAX(delay_minutes),
               MIN(scheduled_out), MAX(scheduled_out)
        FROM flights
    """).fetchone()

    print("\n=== TLV Departure Delay Report ===")
    print(f"Data range : {first} -> {last} (local)")
    print(f"Flights    : {total:,}")
    print(f"Avg delay  : {avg_d:.1f} min" if avg_d else "Avg delay  : n/a")
    print(f"Max delay  : {max_d} min"     if max_d else "Max delay  : n/a")

    print("\n-- By airline (top 15 by avg delay) --")
    rows = conn.execute("""
        SELECT airline_iata, airline_name, COUNT(*), AVG(delay_minutes), MAX(delay_minutes)
        FROM flights
        WHERE delay_minutes IS NOT NULL AND airline_iata != ''
        GROUP BY airline_iata
        ORDER BY AVG(delay_minutes) DESC
        LIMIT 15
    """).fetchall()
    print(f"  {'Code':<6} {'Airline':<30} {'Flights':>7} {'Avg':>7} {'Max':>7}")
    for r in rows:
        print(f"  {r[0]:<6} {(r[1] or '')[:30]:<30} {r[2]:>7} {r[3]:>6.1f}m {r[4]:>6}m")

    print("\n-- By hour of day (local, scheduled departure) --")
    rows = conn.execute("""
        SELECT CAST(substr(scheduled_out, 12, 2) AS INTEGER) AS hr,
               COUNT(*), AVG(delay_minutes)
        FROM flights
        WHERE delay_minutes IS NOT NULL AND scheduled_out IS NOT NULL
        GROUP BY hr ORDER BY hr
    """).fetchall()
    for r in rows:
        bar = "#" * max(0, int((r[2] or 0) / 2))
        print(f"  {r[0]:02d}:00  {r[2]:5.1f}m  {bar}  ({r[1]} flights)")

    print("\n-- Top 10 most delayed flights --")
    rows = conn.execute("""
        SELECT ident, airline_iata, dest_iata, dest_name,
               scheduled_out, delay_minutes, status
        FROM flights WHERE delay_minutes IS NOT NULL
        ORDER BY delay_minutes DESC LIMIT 10
    """).fetchall()
    for r in rows:
        print(f"  {r[0]:<10} {r[1]:<5} -> {r[2]:<5} {(r[3] or '')[:25]:<25}  "
              f"{r[4][:16]}  +{r[5]}m  {r[6]}")

    conn.close()


# ── IAA Sync ─────────────────────────────────────────────────────────────────

IAA_URL = "https://www.iaa.gov.il/umbraco/surface/FlightBoardSurface/Search"


def _normalize_ident(s: str) -> str:
    """Remove spaces and leading zeros from numeric suffix: 'LY 007' -> 'LY7'"""
    s = s.replace(" ", "")
    i = len(s)
    while i > 0 and s[i - 1].isdigit():
        i -= 1
    prefix, number = s[:i], s[i:]
    return prefix + (str(int(number)) if number else "")


def _iaa_to_local(date_str: str, time_str: str, ms_str: str) -> str | None:
    """Build 'YYYY-MM-DD HH:MM' from IAA string fields (already local Israel time).
    Uses the ms timestamp only to determine the year."""
    import re
    if not date_str or not time_str:
        return None
    m = re.search(r"/Date\((\d+)\)/", ms_str or "")
    year = (datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).year
            if m else datetime.now(timezone.utc).year)
    try:
        day, month = date_str.strip().split("/")
        return f"{year}-{month.zfill(2)}-{day.zfill(2)} {time_str.strip()}"
    except Exception:
        return None


def sync_iaa():
    """Fetch IAA departure board and backfill actual_out + delay_minutes."""
    print("Fetching IAA departure board...", end=" ", flush=True)
    resp = requests.post(
        IAA_URL,
        data={"FlightType": "Outgoing", "AirportId": "LLBG", "UICulture": "en-US"},
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()
    flights = raw.get("Flights", raw) if isinstance(raw, dict) else raw
    print(f"{len(flights)} flights from IAA")

    conn = init_db()
    updated = 0
    for f in flights:
        ident_raw = (f.get("Flight") or "").strip()
        status    = (f.get("Status") or "").strip()

        if not ident_raw:
            continue

        # Normalize: IAA uses "LY 007"; DB stores "LY 7" — strip spaces + leading zeros
        ident_norm = _normalize_ident(ident_raw)

        sched_local  = _iaa_to_local(f.get("ScheduledDate", ""), f.get("ScheduledTime", ""), f.get("ScheduledDateTime", ""))
        actual_local = _iaa_to_local(f.get("UpdatedDate",   ""), f.get("UpdatedTime",   ""), f.get("UpdatedDateTime",   ""))
        if not sched_local:
            continue

        # Only use UpdatedTime as actual gate time once the flight has departed
        if status not in ("DEPARTED", "LANDED"):
            actual_local = None

        delay = None
        if actual_local and sched_local:
            try:
                fmt = "%Y-%m-%d %H:%M"
                delay = int((datetime.strptime(actual_local, fmt) - datetime.strptime(sched_local, fmt)).total_seconds() / 60)
            except Exception:
                pass

        sched_date = sched_local[:10]
        terminal = (f.get("Terminal") or "").strip()

        if actual_local:
            # We have actual departure data — update, but only when it's new
            cur = conn.execute("""
                UPDATE flights
                SET actual_out = ?, delay_minutes = ?, status = ?,
                    dep_terminal = COALESCE(dep_terminal, NULLIF(?, ''))
                WHERE REPLACE(ident, ' ', '') = ?
                  AND substr(scheduled_out, 1, 10) = ?
                  AND (actual_out IS NULL OR actual_out != ?)
            """, (actual_local, delay, status, terminal, ident_norm, sched_date, actual_local))
        else:
            # No actual yet — update status and terminal, never overwrite existing actuals
            cur = conn.execute("""
                UPDATE flights SET status = ?,
                    dep_terminal = COALESCE(dep_terminal, NULLIF(?, ''))
                WHERE REPLACE(ident, ' ', '') = ?
                  AND substr(scheduled_out, 1, 10) = ?
            """, (status, terminal, ident_norm, sched_date))

        updated += cur.rowcount

    conn.commit()
    conn.close()
    print(f"Updated {updated} rows with IAA actual departure times.")


# ── Arrivals backfill ────────────────────────────────────────────────────────

def _icao_for_iata(iata: str) -> str | None:
    try:
        r = requests.get(
            f"{BASE_URL}/airports/iata/{iata}",
            headers={"X-RapidAPI-Key": API_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("icao")
    except Exception:
        pass
    return None


def _fetch_arrivals(icao: str, start_local: datetime, end_local: datetime) -> list:
    from_str = start_local.strftime("%Y-%m-%dT%H:%M")
    to_str   = end_local.strftime("%Y-%m-%dT%H:%M")
    url = f"{BASE_URL}/flights/airports/icao/{icao}/{from_str}/{to_str}"
    headers = {"X-RapidAPI-Key": API_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST}
    params  = {"direction": "Arrival", "withLeg": "true",
               "withCancelled": "false", "withCodeshared": "false", "withCargo": "false"}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("arrivals", [])


def backfill_arrivals(days: int = 7):
    """Fill missing actual_out by querying arrival times at destination airports."""
    if not API_KEY:
        sys.exit("ERROR: RAPIDAPI_KEY not set.")

    conn = init_db()
    now_local = datetime.now(timezone.utc).astimezone(LOCAL_TZ)
    cutoff = (now_local - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = conn.execute("""
        SELECT flight_id, ident, dest_iata, scheduled_out
        FROM flights
        WHERE actual_out IS NULL
          AND dest_iata != ''
          AND scheduled_out IS NOT NULL
          AND scheduled_out < ?
          AND substr(scheduled_out, 1, 10) >= ?
        ORDER BY scheduled_out DESC
    """, (now_local.strftime("%Y-%m-%d %H:%M"), cutoff)).fetchall()

    if not rows:
        print("No past flights with missing actuals.")
        conn.close()
        return

    print(f"{len(rows)} flights missing actuals (last {days} days).\n")

    from collections import defaultdict
    groups = defaultdict(list)
    for row in rows:
        groups[(row[2], row[3][:10])].append(row)

    # Estimate API calls: 2 chunks per dest-date pair + 1 lookup per unique IATA
    unique_iatas = len({g[0] for g in groups})
    est_calls = unique_iatas + len(groups) * 2
    print(f"{len(groups)} destination-date pairs, ~{est_calls} API calls.\n")

    icao_cache = {}
    total_updated = 0

    for (dest_iata, date), flights in sorted(groups.items(), key=lambda x: x[0][1], reverse=True):
        # Resolve ICAO (one call per unique IATA)
        if dest_iata not in icao_cache:
            icao_cache[dest_iata] = _icao_for_iata(dest_iata)
            time.sleep(0.3)
        icao = icao_cache[dest_iata]
        if not icao:
            print(f"  {dest_iata} {date}: ICAO not found — skipping")
            continue

        # Query arrivals in two 12-hour chunks covering the departure date + next day
        date_dt = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
        print(f"  {dest_iata} ({icao}) {date} ({len(flights)} missing) ...", end=" ", flush=True)

        arrivals_by_ident = {}
        for offset_h in (0, 12):
            seg_start = date_dt + timedelta(hours=offset_h)
            seg_end   = date_dt + timedelta(hours=offset_h + 12)
            for attempt in range(3):
                try:
                    for arr in _fetch_arrivals(icao, seg_start, seg_end):
                        dep = arr.get("departure") or {}
                        if (dep.get("airport") or {}).get("icao") != "LLBG":
                            continue
                        num = arr.get("number", "")
                        if num:
                            arrivals_by_ident[num] = arr
                    time.sleep(2)
                    break
                except requests.HTTPError as e:
                    if e.response.status_code == 429:
                        wait = 15 * (attempt + 1)
                        print(f"(rate limit, waiting {wait}s)", end=" ", flush=True)
                        time.sleep(wait)
                    else:
                        break

        updated = 0
        for flight_id, ident, _, scheduled_out in flights:
            arr = arrivals_by_ident.get(ident)
            if not arr:
                continue
            dep_info = arr.get("departure") or {}
            arr_info = arr.get("arrival")   or {}

            # Reject if this arrival record belongs to a different day's flight
            sched_dep_s = local_str(dep_info.get("scheduledTime") or {})
            if sched_dep_s and sched_dep_s[:10] != scheduled_out[:10]:
                continue

            # Prefer direct actual departure from the arrival record
            actual_dep = local_str(dep_info.get("revisedTime") or {})

            # Fallback: estimate from actual_arrival − scheduled_block_time
            if not actual_dep:
                sched_arr_s  = local_str(arr_info.get("scheduledTime") or {})
                actual_arr_s = local_str(arr_info.get("revisedTime")   or {})
                if sched_dep_s and sched_arr_s and actual_arr_s:
                    fmt = "%Y-%m-%d %H:%M"
                    try:
                        block      = datetime.strptime(sched_arr_s, fmt) - datetime.strptime(sched_dep_s, fmt)
                        actual_dep = (datetime.strptime(actual_arr_s, fmt) - block).strftime(fmt)
                    except Exception:
                        pass

            if not actual_dep:
                continue

            delay = None
            fmt = "%Y-%m-%d %H:%M"
            try:
                delay = int((datetime.strptime(actual_dep, fmt)
                             - datetime.strptime(scheduled_out, fmt)).total_seconds() / 60)
            except Exception:
                pass

            # Sanity check: reject implausible delays (same-day flight can't be >10h late or >2h early)
            if delay is None or delay < -120 or delay > 600:
                continue

            conn.execute("""
                UPDATE flights SET actual_out = ?, delay_minutes = ?, status = ?
                WHERE flight_id = ? AND actual_out IS NULL
            """, (actual_dep, delay, arr.get("status", ""), flight_id))
            updated += 1

        conn.commit()
        total_updated += updated
        print(f"{len(arrivals_by_ident)} from TLV found, {updated} updated")
        time.sleep(1)

    conn.close()
    print(f"\nDone: {total_updated} actuals recovered from destination arrivals.")


# ── Export ────────────────────────────────────────────────────────────────────

CHARTJS_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"


def _fetch_chartjs() -> str:
    """Download Chart.js minified source for inline embedding."""
    try:
        import urllib.request
        with urllib.request.urlopen(CHARTJS_CDN, timeout=10) as r:
            return r.read().decode("utf-8")
    except Exception as e:
        print(f"  Warning: could not fetch Chart.js ({e}); charts may not render offline.")
        return ""


def export_js():
    dash_dir = Path(__file__).parent
    template = dash_dir / "index.html"
    out_file = dash_dir / "report.html"

    airports_data = {}
    total_flights = 0
    for db_file in sorted(dash_dir.glob("*_flights.db")):
        iata = db_file.stem.replace("_flights", "").upper()
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT flight_id AS fa_flight_id, ident, airline_iata, airline_name,
                   dest_iata, dest_name, dest_country,
                   scheduled_out, actual_out, delay_minutes, status,
                   aircraft_type, dep_terminal, dep_gate, scheduled_in
            FROM flights
            WHERE scheduled_out IS NOT NULL
            ORDER BY scheduled_out DESC
        """).fetchall()
        conn.close()
        airports_data[iata] = [dict(r) for r in rows]
        total_flights += len(airports_data[iata])
        print(f"  {iata}: {len(airports_data[iata])} flights")

    if not airports_data:
        sys.exit("No database files found — run backfill first.")

    now       = datetime.now(timezone.utc).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    data_json = json.dumps({"generated_at": now, "airports": airports_data}, ensure_ascii=False)

    chartjs_src = _fetch_chartjs()

    html = template.read_text(encoding="utf-8")
    if chartjs_src:
        html = html.replace(
            f'<script src="{CHARTJS_CDN}"></script>',
            f"<script>{chartjs_src}</script>",
        )
    html = html.replace(
        '<script src="data.js"></script>',
        f"<script>const TLV_DATA = {data_json};</script>",
    )
    out_file.write_text(html, encoding="utf-8")
    data_js = dash_dir / "data.js"
    data_js.write_text(f"const TLV_DATA = {data_json};", encoding="utf-8")
    print(f"Exported {total_flights} flights total -> {out_file}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "analyze":
            analyze()
        elif cmd == "export":
            export_js()
        elif cmd == "iaa":
            sync_iaa()
        elif cmd == "backfill":
            days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
            backfill(days)
        elif cmd == "arrivals":
            days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
            backfill_arrivals(days)
        else:
            print(f"Unknown command: {cmd}. Use 'analyze', 'export', 'iaa', 'backfill [days]', or 'arrivals [days]'.")
        sys.exit(0)

    print("TLV Tracker — usage:")
    print("  python tlv_tracker.py backfill [days]  fetch last N days (default 7)")
    print("  python tlv_tracker.py iaa              sync actual times from IAA")
    print("  python tlv_tracker.py arrivals [days]  fill missing actuals from destination arrivals")
    print("  python tlv_tracker.py export           generate report.html")
    print("  python tlv_tracker.py analyze          print delay report")
    print(f"\nDatabase: {DB_PATH}")
