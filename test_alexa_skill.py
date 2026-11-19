"""Full suite: .venv/bin/python -m unittest -v (install requirements-alexa.txt)."""
import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ask_sdk_webservice_support.verifier import RequestVerifier, VerificationException
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

import alexa_skill
import azan
from test_azan import DAY, ZONE, api_response

SKILL_ID = "amzn1.ask.skill.00000000-0000-0000-0000-000000000001"
CERT_URL = "https://s3.amazonaws.com/echo.api/test-signing-cert.pem"


class QueryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "state.sqlite3"
        self.db = azan.open_database(self.database)
        self.addCleanup(self.db.close)
        self.config = azan.load_config(Path(__file__).with_name("config.json"))
        for day in (DAY - timedelta(days=1), DAY, DAY + timedelta(days=1)):
            self.cache(day)
        self.network = patch("azan.request_json", side_effect=AssertionError("Query must never fetch or play"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def cache(self, day, response=None):
        with patch("azan.request_json", return_value=response or api_response(day)):
            self.db.execute("DELETE FROM cache WHERE day=?", (day.isoformat(),))
            azan.get_schedule(self.config, day, self.db)

    def reply(self, hour=13, minute=0, second=0, all_today=False):
        now = datetime.combine(DAY, datetime.min.time(), ZONE).replace(
            hour=hour, minute=minute, second=second)
        return alexa_skill.prayer_reply(self.config, self.database, now, all_today)[0]


class PrayerReplyTests(QueryFixture):
    def test_next_prayer_countdown_and_remaining_times(self):
        speech = self.reply()
        self.assertIn("next prayer is Asr at 2:23 PM, in 1 hour and 23 minutes", speech)
        self.assertIn("Later today, Maghrib at 4:37 PM, Isha at 6:02 PM", speech)
        self.assertNotIn("Fajr", speech)
        self.assertNotIn("Dhuhr", speech)

    def test_before_fajr_includes_all_later_prayers(self):
        speech = self.reply(0)
        self.assertIn("Fajr at 5:43 AM, in 5 hours and 43 minutes", speech)
        self.assertIn("Dhuhr at 12:10 PM", speech)

    def test_after_isha_looks_ahead_to_tomorrow(self):
        speech = self.reply(19)
        self.assertIn("Fajr at 5:43 AM tomorrow, in 10 hours and 43 minutes", speech)
        self.assertIn("no more prayers today", speech)
        self.assertNotIn("Later today", speech)

    def test_exact_due_time_says_now(self):
        self.assertIn("Asr at 2:23 PM, now.", self.reply(14, 23))

    def test_under_a_minute_does_not_say_zero_minutes(self):
        self.assertIn("in less than a minute", self.reply(14, 22, 30))

    def test_last_prayer_is_not_repeated_as_later_today(self):
        speech = self.reply(17)
        self.assertIn("next prayer is Isha", speech)
        self.assertIn("That is the last prayer today", speech)
        self.assertNotIn("Later today", speech)

    def test_full_timetable_includes_past_and_future_prayers(self):
        speech = self.reply(all_today=True)
        self.assertIn("next prayer is Asr", speech)
        self.assertIn("Today's prayer times are Fajr at 5:43 AM, Dhuhr at 12:10 PM", speech)
        self.assertIn("Isha at 6:02 PM", speech)
        self.assertNotIn("Sunrise", speech)

    def test_previous_prayer_days_isha_can_be_next_after_midnight(self):
        previous = DAY - timedelta(days=1)
        response = api_response(previous)
        response["data"]["timings"]["Isha"] = DAY.isoformat() + "T00:31:00+01:00"
        self.cache(previous, response)
        speech = self.reply(0, 5)
        self.assertIn("Isha at 12:11 AM, in 6 minutes", speech)
        self.assertIn("Later today, Fajr at 5:43 AM", speech)

    def test_current_prayer_days_isha_after_midnight_is_not_mislabeled_today(self):
        response = api_response()
        response["data"]["timings"]["Isha"] = (DAY + timedelta(days=1)).isoformat() + "T00:31:00+01:00"
        self.cache(DAY, response)
        speech = self.reply(23, 50)
        self.assertIn("Isha at 12:11 AM tomorrow, in 21 minutes", speech)
        self.assertIn("no more prayers today", speech)
        self.assertNotIn("Later today", speech)
        self.assertIn("Isha at 12:11 AM tomorrow", self.reply(23, 50, all_today=True))

    def test_dst_countdown_uses_elapsed_utc_time_not_wall_clock(self):
        for day, offset, expected in ((DAY.replace(month=3, day=29), "+02:00", "3 hours and 13 minutes"),
                                      (DAY.replace(month=10, day=25), "+01:00", "5 hours and 13 minutes")):
            with self.subTest(day=day):
                self.cache(day, api_response(day, offset))
                now = datetime.combine(day, datetime.min.time(), ZONE).replace(hour=1, minute=30)
                speech = alexa_skill.prayer_reply(self.config, self.database, now)[0]
                self.assertIn("Fajr at 5:43 AM, in " + expected, speech)

    def test_missing_today_cache_never_falls_back_to_yesterday(self):
        self.db.execute("DELETE FROM cache WHERE day=?", (DAY.isoformat(),))
        with self.assertRaisesRegex(RuntimeError, "cache"):
            self.reply()

    def test_missing_tomorrow_is_required_only_after_last_prayer(self):
        self.db.execute("DELETE FROM cache WHERE day=?", ((DAY + timedelta(days=1)).isoformat(),))
        self.assertIn("Asr", self.reply())
        with self.assertRaises(RuntimeError):
            self.reply(19)

    def test_corrupt_cache_is_not_deleted_or_refetched_by_query(self):
        self.db.execute("UPDATE cache SET payload='broken' WHERE day=?", (DAY.isoformat(),))
        with self.assertRaises(RuntimeError):
            self.reply()
        self.assertEqual(self.db.execute("SELECT payload FROM cache WHERE day=?", (DAY.isoformat(),)).fetchone()[0], "broken")

    def test_queries_do_not_claim_or_change_playback_events(self):
        self.db.execute("INSERT INTO events VALUES (?, 'Dhuhr', 'test', 'submitted', '')", (DAY.isoformat(),))
        before = self.db.execute("SELECT * FROM events").fetchall()
        self.reply()
        self.reply(19, all_today=True)
        self.assertEqual(self.db.execute("SELECT * FROM events").fetchall(), before)

    def test_new_offsets_apply_to_the_shared_cache_once(self):
        self.config["offsets_minutes"]["Asr"] = 6
        self.assertIn("Asr at 2:25 PM", self.reply())

    def test_naive_query_clock_is_rejected(self):
        with self.assertRaises(ValueError):
            alexa_skill.prayer_reply(self.config, self.database, datetime(2026, 11, 18))

    def test_countdown_pluralization(self):
        for seconds, expected in ((0, "now"), (1, "in less than a minute"), (60, "in 1 minute"),
                                  (3600, "in 1 hour"), (3660, "in 1 hour and 1 minute"),
                                  (7201, "in 2 hours and 1 minute")):
            with self.subTest(seconds=seconds):
                self.assertEqual(alexa_skill.countdown_text(seconds), expected)


class WebhookTests(QueryFixture):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.real_now = datetime.now(azan.UTC)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Alexa verifier unit test")])
        cls.cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                    .public_key(cls.key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(cls.real_now - timedelta(days=1))
                    .not_valid_after(cls.real_now + timedelta(days=1))
                    .add_extension(x509.SubjectAlternativeName([x509.DNSName("echo-api.amazon.com")]), critical=False)
                    .sign(cls.key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))

    def setUp(self):
        super().setUp()
        config_path = Path(self.temp.name) / "config.json"
        config_path.write_text(json.dumps(self.config))
        env = patch.dict(os.environ, {"ALEXA_SKILL_ID": SKILL_ID, "AZAN_CONFIG": str(config_path),
                                      "AZAN_DATABASE": str(self.database)})
        env.start()
        self.addCleanup(env.stop)
        self.app = alexa_skill.create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def envelope(self, intent="NextPrayerIntent", request_type="IntentRequest", *, screen=False):
        request: dict[str, object] = {"type": request_type, "requestId": "test-request", "locale": "en-US",
                   "timestamp": datetime.now(azan.UTC).isoformat()}
        if request_type == "IntentRequest":
            request["intent"] = {"name": intent, "slots": {}}
        interfaces = {"Alexa.Presentation.APL": {"runtime": {"maxVersion": "2024.3"}}} if screen else {}
        return {"version": "1.0", "request": request,
                "context": {"System": {"application": {"applicationId": SKILL_ID},
                                       "user": {"userId": "test-user"},
                                       "device": {"deviceId": "test-device", "supportedInterfaces": interfaces}}}}

    def signed_post(self, envelope=None, *, cert_url=CERT_URL, tamper=False, now=None):
        body = json.dumps(self.envelope() if envelope is None else envelope)
        signature = self.key.sign(body.encode(), padding.PKCS1v15(), hashes.SHA256())
        if tamper:
            body = body.replace("test-user", "different-user")
        headers = {"SignatureCertChainUrl": cert_url, "Signature-256": base64.b64encode(signature).decode()}
        # Synthetic test certificate: replace network fetch/trust only. SDK URL,
        # domain, expiry, SHA-256 signature, timestamp and skill-ID checks stay on.
        with patch.object(RequestVerifier, "_load_cert_chain", return_value=self.cert), \
             patch.object(RequestVerifier, "_validate_cert_chain"), \
             patch("alexa_skill.datetime") as clock:
            clock.now.return_value = now or datetime(2026, 11, 18, 13, 0, tzinfo=ZONE)
            return self.client.post("/alexa", data=body, headers=headers, content_type="application/json")

    def test_signed_request_returns_speech_card_and_ends_session(self):
        response = self.signed_post()
        self.assertEqual(response.status_code, 200)
        result = response.get_json()["response"]
        self.assertEqual(result["outputSpeech"]["type"], "SSML")
        speech = result["outputSpeech"]["ssml"]
        self.assertIn('<phoneme alphabet="ipa" ph="ˈɑ.sɚ">Asr</phoneme> at 2:23 PM', speech)
        self.assertIn('<phoneme alphabet="ipa" ph="ˈi.ʃɑ">Isha</phoneme> at 6:02 PM', speech)
        self.assertEqual(result["card"]["type"], "Simple")
        self.assertIn("Europe/Berlin", result["card"]["content"])
        self.assertIn("Asr at 2:23 PM", result["card"]["content"])
        self.assertIn("Isha at 6:02 PM", result["card"]["content"])
        self.assertNotIn("<phoneme", result["card"]["content"])
        self.assertTrue(result["shouldEndSession"])
        self.assertNotIn("directives", result)
        self.assertEqual(self.db.execute("SELECT count(*) FROM events").fetchone()[0], 0)

    def screen_reply(self, envelope, *, now=None):
        response = self.signed_post(envelope, now=now)
        self.assertEqual(response.status_code, 200)
        result = response.get_json()["response"]
        self.assertIn("outputSpeech", result)
        self.assertNotIn("shouldEndSession", result)  # Keep the screen visible without reprompting.
        self.assertNotIn("reprompt", result)
        directive, = result["directives"]
        self.assertEqual(directive["type"], "Alexa.Presentation.APL.RenderDocument")
        self.assertEqual(directive["document"]["type"], "APL")
        return directive["datasources"]["timetable"]

    def test_echo_show_next_prayer_and_later_times(self):
        screen = self.screen_reply(self.envelope(request_type="LaunchRequest", screen=True))
        self.assertEqual(screen["title"], "Next: Asr")
        self.assertEqual(screen["subtitle"], "2:23 PM · in 1 hour and 23 minutes")
        self.assertEqual([row["primaryText"] for row in screen["rows"]],
                         ["Maghrib · 4:37 PM", "Isha · 6:02 PM"])

    def test_echo_show_full_timetable(self):
        screen = self.screen_reply(self.envelope("TodaysPrayerTimesIntent", screen=True))
        self.assertEqual([row["primaryText"] for row in screen["rows"]],
                         ["Fajr · 5:43 AM", "Dhuhr · 12:10 PM", "Asr · 2:23 PM",
                          "Maghrib · 4:37 PM", "Isha · 6:02 PM"])

    def test_echo_show_after_isha(self):
        screen = self.screen_reply(self.envelope(screen=True), now=datetime(2026, 11, 18, 19, tzinfo=ZONE))
        self.assertEqual(screen["title"], "Next: Fajr")
        self.assertEqual(screen["subtitle"], "5:43 AM tomorrow · in 10 hours and 43 minutes")
        self.assertEqual(screen["rows"], [{"primaryText": "There are no more prayers today."}])

    def test_missing_device_metadata_keeps_voice_reply(self):
        envelope = self.envelope()
        del envelope["context"]["System"]["device"]
        response = self.signed_post(envelope)
        self.assertEqual(response.status_code, 200)
        result = response.get_json()["response"]
        self.assertIn("outputSpeech", result)
        self.assertNotIn("directives", result)

    def test_launch_returns_next_prayer_without_extra_prompt(self):
        response = self.signed_post(self.envelope(request_type="LaunchRequest"))
        self.assertEqual(response.status_code, 200)
        speech = response.get_json()["response"]["outputSpeech"]["ssml"]
        self.assertIn('next prayer is <phoneme alphabet="ipa" ph="ˈɑ.sɚ">Asr</phoneme>', speech)

    def test_full_day_intent_lists_all_five(self):
        response = self.signed_post(self.envelope("TodaysPrayerTimesIntent"))
        self.assertEqual(response.status_code, 200)
        speech = response.get_json()["response"]["outputSpeech"]["ssml"]
        self.assertIn("Today's prayer times are Fajr", speech)
        self.assertEqual(speech.count('ph="ˈɑ.sɚ">Asr</phoneme>'), 2)
        self.assertIn('ph="ˈi.ʃɑ">Isha</phoneme>', speech)

    def test_unsigned_request_is_rejected(self):
        response = self.client.post("/alexa", json=self.envelope())
        self.assertEqual(response.status_code, 400)

    def test_tampered_body_is_rejected(self):
        self.assertEqual(self.signed_post(tamper=True).status_code, 400)

    def test_wrong_skill_id_is_rejected_even_with_valid_signature(self):
        envelope = self.envelope()
        envelope["context"]["System"]["application"]["applicationId"] = SKILL_ID[:-1] + "2"
        self.assertEqual(self.signed_post(envelope).status_code, 400)

    def test_old_and_future_timestamps_are_rejected(self):
        for delta in (-600, 600):
            envelope = self.envelope()
            envelope["request"]["timestamp"] = (datetime.now(azan.UTC) + timedelta(seconds=delta)).isoformat()
            with self.subTest(delta=delta):
                self.assertEqual(self.signed_post(envelope).status_code, 400)

    def test_untrusted_certificate_urls_are_rejected_before_fetch(self):
        for url in ("http://s3.amazonaws.com/echo.api/test.pem", "https://example.com/echo.api/test.pem",
                    "https://s3.amazonaws.com/other/test.pem", "https://s3.amazonaws.com:8443/echo.api/test.pem"):
            with self.subTest(url=url):
                self.assertEqual(self.signed_post(cert_url=url).status_code, 400)

    def test_failed_certificate_trust_validation_is_not_bypassed(self):
        envelope = json.dumps(self.envelope())
        signature = self.key.sign(envelope.encode(), padding.PKCS1v15(), hashes.SHA256())
        with patch.object(RequestVerifier, "_load_cert_chain", return_value=self.cert), \
             patch.object(RequestVerifier, "_validate_cert_chain", side_effect=VerificationException("untrusted")):
            response = self.client.post("/alexa", data=envelope, content_type="application/json",
                                        headers={"SignatureCertChainUrl": CERT_URL,
                                                 "Signature-256": base64.b64encode(signature).decode()})
        self.assertEqual(response.status_code, 400)

    def test_malformed_requests_are_rejected(self):
        for body in (b"not json", b"[]", b"null", b'{"request": 1}', b'\xff'):
            with self.subTest(body=body):
                response = self.client.post("/alexa", data=body, content_type="application/json")
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post("/alexa", data="text").status_code, 400)
        self.assertEqual(self.client.get("/alexa").status_code, 405)

    def test_oversized_request_is_rejected(self):
        response = self.client.post("/alexa", data="x" * (64 * 1024 + 1), content_type="application/json")
        self.assertEqual(response.status_code, 413)

    def test_missing_skill_id_fails_closed(self):
        with patch.dict(os.environ, {"ALEXA_SKILL_ID": ""}):
            client = alexa_skill.create_app().test_client()
            self.assertEqual(client.get("/alexa/health").status_code, 503)
            self.assertEqual(client.post("/alexa", json=self.envelope()).status_code, 503)

    def test_invalid_skill_id_cannot_start_service(self):
        with patch.dict(os.environ, {"ALEXA_SKILL_ID": "not-a-skill-id"}), self.assertRaises(ValueError):
            alexa_skill.create_app()

    def test_health_is_ready_when_skill_id_is_set(self):
        self.assertEqual(self.client.get("/alexa/health").get_json(), {"status": "ready"})

    def test_unavailable_cache_is_spoken_truthfully(self):
        self.db.execute("DELETE FROM cache")
        response = self.signed_post(self.envelope(screen=True))
        self.assertEqual(response.status_code, 200)
        self.assertIn(alexa_skill.UNAVAILABLE, response.get_json()["response"]["outputSpeech"]["ssml"])
        self.assertNotIn("card", response.get_json()["response"])
        self.assertNotIn("directives", response.get_json()["response"])

    def test_help_fallback_stop_cancel_and_session_end(self):
        for intent in ("AMAZON.HelpIntent", "AMAZON.FallbackIntent", "UnexpectedIntent"):
            with self.subTest(intent=intent):
                response = self.signed_post(self.envelope(intent))
                self.assertEqual(response.status_code, 200)
                self.assertIn("Ask me for the next prayer", response.get_json()["response"]["outputSpeech"]["ssml"])
                self.assertFalse(response.get_json()["response"]["shouldEndSession"])
        for intent in ("AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.NavigateHomeIntent"):
            response = self.signed_post(self.envelope(intent))
            self.assertTrue(response.get_json()["response"]["shouldEndSession"])
        response = self.signed_post(self.envelope(request_type="SessionEndedRequest"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("outputSpeech", response.get_json()["response"])

    def test_missing_context_is_rejected_without_server_error(self):
        envelope = self.envelope()
        del envelope["context"]
        self.assertEqual(self.signed_post(envelope).status_code, 400)


if __name__ == "__main__":
    unittest.main()
