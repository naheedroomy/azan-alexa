#!/usr/bin/env python3
"""Timezone-aware Azan scheduler. Python 3.9+, standard library only."""
import argparse
import hashlib
import json
import logging
import math
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

PRAYERS = ("Fajr", "Dhuhr", "Asr", "Maghrib", "Isha")
ADJUSTABLE = set(PRAYERS) | {"Sunrise"}
UTC = timezone.utc
LOG = logging.getLogger("azan")


class NoRedirect(HTTPRedirectHandler):
    """Never forward bearer credentials through an API redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


API_OPENER = build_opener(NoRedirect())


def local_time(day, clock, zone):
    naive = datetime.combine(day, datetime.strptime(clock, "%H:%M").time())
    value = naive.replace(tzinfo=zone)
    roundtrip = value.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if roundtrip != naive or value.utcoffset() != value.replace(fold=1).utcoffset():
        raise ValueError("Override falls in an ambiguous or nonexistent DST hour")
    return value


def load_config(path):
    try:
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError):
        raise ValueError("Cannot read config: provide a readable, valid JSON file") from None
    required = {"latitude", "longitude", "timezone", "method", "school",
                "latitude_adjustment_method", "audio_url", "fajr_audio_url",
                "offsets_minutes", "overrides", "timetable_confirmed",
                "max_late_seconds", "poll_seconds"}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError("Config has missing or unknown fields; compare config.json")
    for field, low, high in (("latitude", -90, 90), ("longitude", -180, 180)):
        number = config[field]
        if type(number) not in (int, float) or not math.isfinite(number) or not low <= number <= high:
            raise ValueError("Invalid " + field)
    ZoneInfo(config["timezone"])
    for field, low, high in (("method", 0, 23), ("school", 0, 1),
                             ("latitude_adjustment_method", 1, 3),
                             ("max_late_seconds", 0, 300), ("poll_seconds", 1, 30)):
        if type(config[field]) is not int or not low <= config[field] <= high:
            raise ValueError("Invalid " + field)
    if type(config["timetable_confirmed"]) is not bool:
        raise ValueError("timetable_confirmed must be true or false")
    for field in ("audio_url", "fajr_audio_url"):
        value = config[field]
        if not isinstance(value, str):
            raise ValueError("Invalid " + field)
        if value:
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError(field + " must be a public HTTPS URL without credentials")
    if not config["audio_url"]:
        raise ValueError("audio_url is required")
    offsets = config["offsets_minutes"]
    if not isinstance(offsets, dict) or not set(offsets) <= ADJUSTABLE:
        raise ValueError("Offsets may contain only the five prayers and Sunrise")
    if any(type(value) is not int or abs(value) > 120 for value in offsets.values()):
        raise ValueError("Offsets must be whole minutes between -120 and 120")
    if not isinstance(config["overrides"], dict):
        raise ValueError("overrides must be a date-to-prayer mapping")
    for day, values in config["overrides"].items():
        parsed_day = date.fromisoformat(day)
        if not isinstance(values, dict) or not set(values) <= set(PRAYERS):
            raise ValueError("Overrides may contain only the five prayers")
        for clock in values.values():
            if not isinstance(clock, str) or len(clock) != 5:
                raise ValueError("Override times must use HH:MM")
            local_time(parsed_day, clock, ZoneInfo(config["timezone"]))
    return config


def request_json(url, payload=None, token=None):
    """Retry reads only. Never automatically repeat a non-idempotent send."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {"api.aladhan.com", "api-v3.voicemonkey.io"}
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("Only trusted HTTPS API endpoints are allowed")
    headers = {"User-Agent": "AzanAlexa/1.0", "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    attempts = 3 if payload is None else 1
    for attempt in range(attempts):
        delay = 2 ** attempt
        try:
            with API_OPENER.open(Request(url, data=data, headers=headers), timeout=15) as response:
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("Response too large")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("Expected an object")
            return result
        except HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code < 600
            message = f"HTTP {error.code} from {urlsplit(url).hostname}"
            retry_after = error.headers.get("Retry-After", "") if error.headers else ""
            if retry_after.isdigit():
                delay = max(delay, min(int(retry_after), 60))
            error.close()
        except (URLError, TimeoutError, OSError, ValueError):
            retryable = True
            message = "Network or JSON failure from " + str(urlsplit(url).hostname)
        if not retryable or attempt + 1 == attempts:
            raise RuntimeError(message) from None
        time.sleep(delay)
    raise RuntimeError("Request failed")


def open_database(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    path.chmod(0o600)
    connection.execute("""CREATE TABLE IF NOT EXISTS cache (
        day TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(day, fingerprint))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS events (
        day TEXT NOT NULL, prayer TEXT NOT NULL, scheduled_at TEXT NOT NULL,
        status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(day, prayer))""")
    return connection


def make_schedule(config, day, response):
    if response.get("code") != 200:
        raise ValueError("Prayer API did not return success")
    data = response["data"]
    zone = ZoneInfo(config["timezone"])
    if data["meta"]["timezone"] != config["timezone"]:
        raise ValueError("Prayer API returned a different timezone")
    if data["date"]["gregorian"]["date"] != day.strftime("%d-%m-%Y"):
        raise ValueError("Prayer API returned a different date")
    result = {}
    overrides = config["overrides"].get(day.isoformat(), {})
    for prayer in PRAYERS:
        source = "Sunset" if prayer == "Maghrib" else prayer
        value = datetime.fromisoformat(data["timings"][source].replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError("Prayer API timestamps must include an offset")
        local = value.astimezone(zone)
        if value.utcoffset() != local.utcoffset():
            raise ValueError("Prayer API timestamp has an incorrect UTC offset")
        if prayer == "Maghrib":
            # Some calculation methods pad even the API's Sunset field.
            try:
                padding = float(data["meta"].get("offset", {}).get("Sunset", 0))
            except (ValueError, TypeError):
                raise ValueError("Invalid sunset adjustment") from None
            if not math.isfinite(padding) or abs(padding) > 120:
                raise ValueError("Invalid sunset adjustment")
            local -= timedelta(minutes=padding)
        if prayer in overrides:
            local = local_time(day, overrides[prayer], zone)
        else:
            local += timedelta(minutes=config["offsets_minutes"].get(prayer, 0))
        allowed_dates = (day, day + timedelta(days=1)) if prayer == "Isha" else (day,)
        if local.date() not in allowed_dates:
            raise ValueError("Prayer timestamp is outside the requested day")
        result[prayer] = local
    stamps = [result[prayer].timestamp() for prayer in PRAYERS]
    if stamps != sorted(set(stamps)):
        raise ValueError("Prayer times are not strictly ordered")
    return result


def get_schedule(config, day, connection, *, cached_only=False):
    query = {key: config[key] for key in ("latitude", "longitude", "method", "school",
                                         "latitude_adjustment_method")}
    query.update(timezonestring=config["timezone"], iso8601="true")
    fingerprint = hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()
    row = connection.execute("SELECT payload FROM cache WHERE day=? AND fingerprint=?",
                             (day.isoformat(), fingerprint)).fetchone()
    if row:
        try:
            return make_schedule(config, day, json.loads(row[0]))
        except (ValueError, KeyError, TypeError):
            if not cached_only:
                LOG.warning("Discarding invalid cached schedule for %s", day)
                connection.execute("DELETE FROM cache WHERE day=? AND fingerprint=?", (day.isoformat(), fingerprint))
    if cached_only:
        raise RuntimeError("Prayer times are not available in the cache")
    url = "https://api.aladhan.com/v1/timings/" + day.strftime("%d-%m-%Y")
    response = request_json(url + "?" + urlencode(query))
    schedule = make_schedule(config, day, response)
    connection.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                       (day.isoformat(), fingerprint, json.dumps(response)))
    return schedule


def credentials():
    token = os.environ.get("VOICE_MONKEY_TOKEN", "").strip()
    device = os.environ.get("VOICE_MONKEY_DEVICE", "").strip()
    if not token or not device:
        raise ValueError("Set VOICE_MONKEY_TOKEN and VOICE_MONKEY_DEVICE privately")
    if "," in device or any(char.isspace() for char in device):
        raise ValueError("Configure exactly one Speaker device ID")
    return token, device


def announcement(config, prayer=None):
    if prayer is None:
        return {"speech": "Your Azan connection is working."}
    audio = config["fajr_audio_url"] if prayer == "Fajr" and config["fajr_audio_url"] else config["audio_url"]
    return {"audio": audio, "text_display": "Time for " + prayer, "display_seconds": 180}


def send_announcement(config, prayer=None):
    token, device = credentials()
    payload = dict(announcement(config, prayer), device=device)
    response = request_json("https://api-v3.voicemonkey.io/announce", payload, token)
    success = response.get("success")
    if type(success) is not bool or not success or response.get("data") == "PARTIAL":
        raise RuntimeError("Voice Monkey did not confirm acceptance; inspect its execution log")
    if any(type(item.get("ok")) is not bool or not item["ok"] for item in response.get("devices", [])):
        raise RuntimeError("Voice Monkey reported a failed Speaker; inspect its execution log")


def dispatch(config, day, schedule, connection, live, seen, sender=send_announcement,
             clock=lambda: datetime.now(UTC)):
    for prayer, scheduled in schedule.items():
        age = (clock().astimezone(UTC) - scheduled.astimezone(UTC)).total_seconds()
        if age < 0:
            continue
        key = (day.isoformat(), prayer)
        late = age > config["max_late_seconds"]
        if not live:
            if key not in seen:
                LOG.info("DRY RUN %s %s %s", day, prayer, "too late; skip" if late else "would play")
                seen.add(key)
            continue
        status = "skipped" if late else "claimed"
        inserted = connection.execute(
            "INSERT OR IGNORE INTO events(day, prayer, scheduled_at, status) VALUES (?, ?, ?, ?)",
            (*key, scheduled.isoformat(), status)).rowcount
        if not inserted:
            continue
        if late:
            LOG.info("Skipped late prayer: %s %s", day, prayer)
            continue
        # Durable claim BEFORE sending: crashes/timeouts must never cause a repeat.
        try:
            sender(config, prayer)
        except (RuntimeError, ValueError, TypeError, KeyError) as error:
            connection.execute("UPDATE events SET status='failed_or_unknown', detail=? WHERE day=? AND prayer=?",
                               (str(error), *key))
            LOG.error("%s %s not confirmed; no automatic resend: %s", day, prayer, error)
        else:
            connection.execute("UPDATE events SET status='submitted' WHERE day=? AND prayer=?", key)
            LOG.info("Submitted %s %s to Voice Monkey (physical playback is not confirmed)", day, prayer)


def require_live_ready(config):
    if not config["timetable_confirmed"]:
        raise ValueError("Live scheduling is locked: confirm the future timetable first")
    credentials()


def run(config, connection, live=False, once=False):
    if live:
        require_live_ready(config)
    LOG.info("Scheduler mode: %s; timezone: %s", "LIVE" if live else "DRY RUN", config["timezone"])
    schedules, retry_after, seen = {}, {}, set()
    while True:
        now = datetime.now(UTC)
        today = now.astimezone(ZoneInfo(config["timezone"])).date()
        days = (today, today - timedelta(days=1), today + timedelta(days=1))
        schedules = {day: value for day, value in schedules.items() if day in days}
        retry_after = {day: value for day, value in retry_after.items() if day in days}
        seen = {key for key in seen if key[0] in {day.isoformat() for day in days}}
        # Dispatch cached events before any potentially slow network fetch.
        for day, schedule in schedules.items():
            dispatch(config, day, schedule, connection, live, seen)
        for day in days:
            if day in schedules or now.timestamp() < retry_after.get(day, 0):
                continue
            try:
                schedules[day] = get_schedule(config, day, connection)
                dispatch(config, day, schedules[day], connection, live, seen)
            except (RuntimeError, ValueError, KeyError, TypeError) as error:
                LOG.error("Schedule unavailable for %s: %s", day, error)
                retry_after[day] = time.time() + 60
        if once:
            return 0 if all(day in schedules for day in days) else 1
        time.sleep(config["poll_seconds"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    state_dir = os.environ.get("STATE_DIRECTORY", "data")
    parser.add_argument("--database", default=str(Path(state_dir) / "azan.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("schedule", help="Fetch and display prayer times; no playback")
    preview.add_argument("--date", type=date.fromisoformat)
    worker = commands.add_parser("run", help="Run the worker; dry-run unless explicitly armed")
    worker.add_argument("--live", action="store_true")
    worker.add_argument("--once", action="store_true")
    test = commands.add_parser("test", help="Preview or send a single connection test")
    test.add_argument("--audio", action="store_true")
    test.add_argument("--send", action="store_true", help="Actually send to the Echo")
    commands.add_parser("status", help="Show recent persistent playback attempts")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    connection = None
    try:
        config = load_config(args.config)
        if args.command == "test":
            prayer = "Dhuhr" if args.audio else None
            if args.send:
                send_announcement(config, prayer)
                print("Voice Monkey accepted the request. Confirm the sound on your Echo.")
            else:
                print("DRY RUN (no request sent): " + json.dumps(announcement(config, prayer)))
            return 0
        connection = open_database(args.database)
        if args.command == "schedule":
            day = args.date or datetime.now(ZoneInfo(config["timezone"])).date()
            schedule = get_schedule(config, day, connection)
            status = "CONFIRMED" if config["timetable_confirmed"] else "UNCONFIRMED; live scheduling locked"
            print(f"{day} | {config['timezone']} | timetable {status}")
            for prayer, value in schedule.items():
                print(f"{prayer:<8} {value.isoformat()}")
            return 0
        if args.command == "status":
            for row in connection.execute("SELECT day, prayer, status, detail FROM events ORDER BY day DESC, scheduled_at DESC LIMIT 20"):
                print(" | ".join(row))
            return 0
        flag = os.environ.get("AZAN_LIVE", "0")
        if flag not in ("0", "1"):
            raise ValueError("AZAN_LIVE must be 0 or 1")
        return run(config, connection, args.live or flag == "1", args.once)
    except (ValueError, KeyError, TypeError, RuntimeError, OSError, sqlite3.Error) as error:
        LOG.error("%s", error)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
