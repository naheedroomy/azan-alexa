# Ulm Azan → Echo Show

A Python 3.9+ standard-library worker, SQLite state, AlAdhan prayer times, and
Voice Monkey announcements. Automatic playback needs no Python dependencies or
custom Alexa skill. An [optional English-US voice-query skill](alexa/README.md)
adds next-prayer times and countdowns. Caddy serves HTTPS; RustDesk is independent.

## Safety and current timetable

**Local commands default to dry-run.** Both `AZAN_LIVE=1` and
`"timetable_confirmed": true` are required for automatic playback. A one-shot
test is separate. The deployed VPS service is **enabled and LIVE** with the
confirmed recurring calculation below; physical Echo audio was user-confirmed.

Location: Ulm city centre, 48.4011 / 9.9876; timezone **Europe/Berlin**. The VPS's
own timezone is irrelevant and is not changed. DST is handled by `zoneinfo`.

## Confirmed recurring calculation

The user explicitly confirmed **Muslim World League (`method=3`)**, with these
minute adjustments, as their consistent local-mosque reference:

| Timing | Offset |
| --- | ---: |
| Fajr | +1 |
| Sunrise | 0 (not an audio event) |
| Dhuhr | +5 |
| Asr | +4 |
| Maghrib | -1 |
| Isha | -20 |

Standard Asr (`school=0`) and angle-based high-latitude adjustment (`3`) are used.
Offsets are applied locally **once**, after fetching the base API values.
Maghrib means **astronomical sunset minus one minute**, per the latest explicit
user recipe. Any API-method padding on `Sunset` is removed first. This supersedes
the earlier exact-sunset preference and the initial one-day sample; there are
no active date overrides. On 2026-11-18 this provider produces 05:43 / 12:10 /
14:23 / 16:37 / 18:02. Asr is 14:23, not the earlier 14:21 sample.

All six offsets are preserved in config; only the five prayers trigger audio.
Optional date-specific `overrides` supersede offsets for that date only.
The same MP3 is used at Fajr unless `fajr_audio_url` is supplied.
See [audio notes](audio/ATTRIBUTION.md).

## Local checks (never send audio by default)

```sh
python3 -m unittest -v test_azan
python3 azan.py schedule --date 2026-11-18
python3 azan.py test
python3 azan.py test --audio
python3 azan.py run --once
```

To send a deliberate test, privately export `VOICE_MONKEY_TOKEN` and
`VOICE_MONKEY_DEVICE`, then run:

```sh
python3 azan.py test --send          # short speech
python3 azan.py test --audio --send  # full supplied recording; plays immediately
```

A successful API response confirms acceptance, **not audible playback**.
For a new recording/device, confirm the full recording on the physical Echo,
including its end. These tests
consume API requests and do not claim any scheduled prayer in SQLite.

## VPS layout

- `/opt/azan-alexa/azan.py`, tests and documentation: root-owned, read-only to worker.
- `/etc/azan-alexa/config.json`: non-secret configuration.
- `/etc/azan-alexa/credentials`: root-only mode 0600, read by systemd.
- `/var/lib/azan-alexa/azan.sqlite3`: service-user-only persistent cache/history.
- `/srv/azan-audio/azan.mp3`: public recording only.
- `/etc/caddy/Caddyfile`: dedicated HTTPS file endpoint.
- `/etc/systemd/system/azan-alexa.service`: unprivileged, sandboxed worker.

The public URL is `https://178-83-33-107.sslip.io/azan.mp3`. sslip.io is a
third-party DNS dependency pointing at the VPS; Caddy obtains/renews the TLS
certificate. Replace it with your own domain later if desired. Incoming TCP
80/443 must remain reachable for HTTPS and certificate renewal. No scheduler
port is exposed. No firewall rules or RustDesk settings need to be changed.

Credentials file format for a disabled installation (never paste its contents
into logs). The deployed production file uses `AZAN_LIVE=1`:

```sh
VOICE_MONKEY_TOKEN=your-private-token
VOICE_MONKEY_DEVICE=your-speaker-id
AZAN_LIVE=0
```

### Operate the service

```sh
sudo systemctl status azan-alexa --no-pager
sudo journalctl -u azan-alexa -n 50 --no-pager
sudo -u azan-alexa env STATE_DIRECTORY=/var/lib/azan-alexa \
  python3 /opt/azan-alexa/azan.py --config /etc/azan-alexa/config.json schedule
sudo -u azan-alexa env STATE_DIRECTORY=/var/lib/azan-alexa \
  python3 /opt/azan-alexa/azan.py --config /etc/azan-alexa/config.json status
```

After the timetable and physical playback are verified, edit the non-secret
config to set `timetable_confirmed` to true, edit the protected credentials file
to set `AZAN_LIVE=1`, and restart `azan-alexa`. To disarm, set `AZAN_LIVE=0` and
restart. To stop all scheduled activity: `sudo systemctl stop azan-alexa`.
Config changes require a restart. Do not run a second live worker with a
different database: duplicate prevention depends on using the same state file.

When rotating the token, edit `/etc/azan-alexa/credentials` privately and restart
the service. Never put credentials/private keys in Git or command arguments.

## Reliability contract

- Fetches yesterday/today/tomorrow, caches valid daily responses on disk, and
  retries read failures with backoff. Yesterday covers Isha crossing midnight;
  tomorrow's prefetched cache covers a temporary network outage at midnight.
- Polls every 5 seconds; uses timezone-aware timestamps and UTC comparisons.
  Playback up to 90 seconds late is allowed; older events are recorded as skipped.
- A SQLite primary key on `(prayer date, prayer)` and a durable **claim before
  send** prevent automatic duplicate attempts across restarts/concurrent workers.
- Playback POSTs are **not automatically retried**: a timeout can mean the Echo
  already received the request, and Voice Monkey provides no idempotency key.
  `claimed` and `failed_or_unknown` events require inspection of Voice Monkey's
  execution log. This deliberately favours no duplicate Azan over guaranteed
  delivery. GET retries do not spend playback quota.
- `submitted` means API-accepted, not sound-confirmed. No exactly-once delivery
  guarantee, device-volume override, DND override or cloud-delay guarantee is made.
- Caddy, the worker and RustDesk are separate services. The worker restarts on
  failure; reboot restores the enabled service. Back up the SQLite database
  consistently (SQLite backup API) to preserve duplicate history.

## Optional: ask Alexa for prayer times

Say **“Alexa, open Prayer Times”** to hear the next prayer, local time,
countdown, and remaining times today. **“Alexa, ask Prayer Times for today's
prayer times”** also lists the full day. After Isha it looks ahead to tomorrow.
On Echo Show, you can [enable the on-screen timetable](alexa/README.md#show-the-timetable-on-echo-show)
without changing the voice commands or automatic playback.

See [the skill setup guide](alexa/README.md) for Amazon Developer registration,
model import and HTTPS endpoint configuration. It remains a private development
skill on the Echo's Amazon account. The backend needs Python 3.10+ and
`requirements-alexa.txt`; it runs as a separate read-only service and spends
no Voice Monkey requests. Automatic Azan playback is unchanged.

With the optional dependencies installed, run the full test suite:

```sh
.venv/bin/python -m unittest -v
```

## Sources

- https://aladhan.com/prayer-times-api
- https://aladhan.com/calculation-methods
- https://voicemonkey.io/docs/getting-started/add-device
- https://voicemonkey.io/docs/api/announcement
- https://voicemonkey.io/docs/media
