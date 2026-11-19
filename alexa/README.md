# Optional Alexa prayer-time skill (English US)

The separate **My Azan** custom skill answers queries. Automatic Azan playback
continues through Voice Monkey exactly as before. No public skill publication,
AWS account, OAuth account-linking flow, or Voice Monkey request is required.
You do need a free Amazon Developer account on the **same Amazon account as the
Echo**, with the Echo set to **English (United States)**.

## What to say

- “Alexa, open Prayer Times.”
- “Alexa, ask Prayer Times for the next prayer.”
- “Alexa, ask Prayer Times how long until the next prayer.”
- “Alexa, ask Prayer Times for the remaining prayer times.”
- “Alexa, ask Prayer Times for today's prayer times.”

The first four report the next prayer, local start time,
countdown, and later prayers today. The full-day query also lists past times.
For example, at 13:00 on 2026-11-18 in Ulm:

> The next prayer is Asr at 2:23 PM, in 1 hour and 23 minutes. Later today,
> Maghrib at 4:37 PM, Isha at 6:02 PM.

After Isha it reports tomorrow's Fajr. Countdown uses elapsed UTC time, including
DST transitions; displayed times use Europe/Berlin. It also handles a prayer
day's Isha occurring after midnight. A Simple Card accompanies the spoken reply.
The phrase “Alexa, what is the next prayer?” alone is not guaranteed to invoke
this skill: include **ask Prayer Times**.

## 1. Create the development skill

1. Sign in at https://developer.amazon.com/alexa/console/ask with the Echo's Amazon
   account. Complete the developer registration if prompted.
2. **Create Skill**: name **My Azan**, primary locale **English (US)**.
3. Experience **Other**, model **Custom**, hosting **Provision your own**.
4. Select **Start from Scratch**, then create the skill. Do not publish it.
5. Copy the **Skill ID** (`amzn1.ask.skill.…`) from the console. This identifier
   is not a password. Set it on the VPS as described below, or provide it to the
   deployment operator. Never share your Amazon password or session cookies.
6. Under **Build → Interaction Model → JSON Editor**, replace the model with
   [`interaction-model-en-US.json`](interaction-model-en-US.json). **Save Model**
   and **Build Model**. Wait for a successful build. Invocation name: `prayer times`.
7. Under **Build → Endpoint**, select **HTTPS**, and set **Default Region** to:

   ```text
   https://178-83-33-107.sslip.io/alexa
   ```

   Select **My development endpoint has a certificate from a trusted certificate
   authority**. Save the endpoint. Use the same hostname for the certificate;
   do not use the raw IP address.
8. Under **Test**, set skill testing to **Development**. In the simulator, use
   locale **English (US)** and enter `open prayer times`.
9. Ask the physical Echo the same question. Confirm the spoken prayer, time,
   countdown, and remaining times. A successful synthetic backend test alone
   does not verify Amazon's routing or physical-device playback.

Development testing on your own account is sufficient; leave certification and
public distribution alone. Locale/account mismatches are common reasons for
“skill not found.” This skill does not request personal information or device
permissions.

If the skill is already set up, go to **Build → Invocation** and set **Skill
Invocation Name** to `prayer times`, then save and build the model. Keep the
existing skill ID and HTTPS endpoint. The console can still list it as **My Azan**.

## 2. Backend deployment

The CLI scheduler remains Python 3.9+ with no dependencies. The optional web
backend needs **Python 3.10+**, the dependencies in `requirements-alexa.txt`, and
system `libcrypto` (already present on the Ubuntu 22.04 VPS).

For a new installation, install the checked-out code in `/opt/azan-alexa`, then:

```sh
sudo apt-get install python3-venv
sudo python3 -m venv /opt/azan-alexa/.venv
sudo /opt/azan-alexa/.venv/bin/python -m pip install \
  -r /opt/azan-alexa/requirements-alexa.txt
sudo install -o root -g root -m 600 deploy/alexa.env.example /etc/azan-alexa/alexa.env
sudoedit /etc/azan-alexa/alexa.env
```

Set `ALEXA_SKILL_ID=amzn1.ask.skill.your-actual-id`. Do not overwrite an existing
`alexa.env` when updating code. It is separate from the Voice Monkey credentials;
the query service does **not** load `/etc/azan-alexa/credentials`.

```sh
sudo install -m 644 deploy/azan-alexa-query.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now azan-alexa-query
```

Add the `/alexa` and `/alexa/health` reverse-proxy block from
[`deploy/Caddyfile`](../deploy/Caddyfile) to the existing HTTPS site, preserving
the audio route. Validate before reloading:

```sh
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
curl --fail https://178-83-33-107.sslip.io/alexa/health
```

With no skill ID, health and POST return **503 setup required**; this is
intentional. After setting the real ID, restart only `azan-alexa-query`; health
returns `{"status":"ready"}`. An unsigned POST must return **400**, not prayer
data. Gunicorn listens on **127.0.0.1:8008 only**; do not expose that port.
Its local control socket is disabled; the service needs no writable state.

```sh
sudo systemctl restart azan-alexa-query
sudo journalctl -u azan-alexa-query -n 40 --no-pager
```

The query process reads `/etc/azan-alexa/config.json` and opens
`/var/lib/azan-alexa/azan.sqlite3` with SQLite `mode=ro`. It reuses the scheduler's
validation/offset code and requires the same fingerprinted, dated cache. It
never fetches prayer times, claims events, changes history, or calls Voice
Monkey. The scheduler prefetches yesterday, today, and tomorrow; missing or
invalid required data produces a spoken temporary-unavailability message rather
than stale or invented times. Config changes require restarting both services.

The two services are independent. Deploying the skill does not require stopping
or restarting the live scheduler. The shared `get_schedule(..., cached_only=True)`
addition is backwards-compatible; restart the scheduler on a future code update
only when needed, preserving its database and credentials.

## Security and checks

The official Amazon SDK verifies the certificate chain, certificate URL/domain,
SHA-256 request signature, timestamp (150-second tolerance), and exact skill ID.
Verification cannot be disabled by an environment variable. Request bodies and
Alexa API access tokens are not logged. Bodies are limited to 64 KiB. TLS is
terminated by Caddy's existing trusted certificate.

Local full-suite setup, using any Python 3.10+ interpreter:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-alexa.txt
.venv/bin/python -m unittest -v
```

Tests exercise synthetic signed HTTP requests, invalid signatures/timestamps/IDs,
read-only cache failures, remaining/full-day replies, after-Isha rollover and DST.
The synthetic signing certificate replaces certificate fetch/trust **only in
tests**; production retains all SDK verification. Test speech/signature fixtures
contain no real Amazon credentials. An actual Amazon/Echo test is still required.

For a local development server (not public production):

```sh
export ALEXA_SKILL_ID=amzn1.ask.skill.your-actual-id
export AZAN_CONFIG=config.json
export AZAN_DATABASE=data/azan.sqlite3
.venv/bin/gunicorn --bind 127.0.0.1:8008 'alexa_skill:create_app()'
```

Use a real ID from your console, not the explanatory placeholder above.
The public query skill intentionally serves one household timetable. Do not
publish it as a general location-aware skill without redesigning configuration.

## References

- https://developer.amazon.com/en-US/docs/alexa/alexa-skills-kit-sdk-for-python/host-web-service.html
- https://developer.amazon.com/en-US/docs/alexa/custom-skills/host-a-custom-skill-as-a-web-service.html
- https://developer.amazon.com/en-US/docs/alexa/custom-skills/test-and-debug-a-custom-skill.html
