"""Run with: python3 -m unittest -v"""
import copy
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

import azan

DAY = date(2026, 11, 18)
ZONE = ZoneInfo("Europe/Berlin")


def api_response(day=DAY, offset="+01:00", sunset_padding=0):
    clocks = {"Fajr": "05:42", "Dhuhr": "12:05", "Asr": "14:19",
              "Sunset": "16:38", "Maghrib": "16:45", "Isha": "18:22"}
    timings = {key: day.isoformat() + "T" + value + ":00" + offset
               for key, value in clocks.items()}
    timings["Sunset"] = (datetime.fromisoformat(timings["Sunset"]) + timedelta(minutes=sunset_padding)).isoformat()
    return {"code": 200, "data": {"timings": timings,
            "meta": {"timezone": "Europe/Berlin", "offset": {"Sunset": sunset_padding}},
            "date": {"gregorian": {"date": day.strftime("%d-%m-%Y")}}}}


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.db = azan.open_database(self.path)
        self.config = azan.load_config(Path(__file__).with_name("config.json"))
        self.when = datetime(2026, 11, 18, 12, 10, tzinfo=ZONE)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def dispatch(self, now=None, live=True, sender=None, seen=None):
        azan.dispatch(self.config, DAY, {"Dhuhr": self.when}, self.db, live,
                      set() if seen is None else seen, sender or (lambda *_: None),
                      lambda: now or self.when)

    def test_confirmed_recipe_uses_unadjusted_sunset_then_user_offset(self):
        self.assertEqual(self.config["offsets_minutes"],
                         {"Fajr": 1, "Sunrise": 0, "Dhuhr": 5,
                          "Asr": 4, "Maghrib": -1, "Isha": -20})
        result = azan.make_schedule(self.config, DAY, api_response(sunset_padding=7))
        self.assertEqual([value.strftime("%H:%M") for value in result.values()],
                         ["05:43", "12:10", "14:23", "16:37", "18:02"])
        self.assertEqual(tuple(result), azan.PRAYERS)
        self.assertNotIn("Sunrise", result)

    def test_one_day_overrides_do_not_repeat(self):
        self.config["overrides"] = {DAY.isoformat(): {"Isha": "18:30", "Dhuhr": "12:30"}}
        tomorrow = DAY + timedelta(days=1)
        result = azan.make_schedule(self.config, tomorrow, api_response(tomorrow))
        self.assertEqual(result["Isha"].strftime("%H:%M"), "18:02")
        self.assertEqual(result["Dhuhr"].strftime("%H:%M"), "12:10")

    def test_offsets_do_not_override_explicit_clock(self):
        self.config["overrides"] = {DAY.isoformat(): {"Dhuhr": "12:10"}}
        self.config["offsets_minutes"] = {"Dhuhr": 10, "Fajr": 2}
        result = azan.make_schedule(self.config, DAY, api_response())
        self.assertEqual(result["Dhuhr"], self.when)

    def test_new_offsets_apply_to_cached_raw_timings_once(self):
        with patch("azan.request_json", return_value=api_response()) as request:
            first = azan.get_schedule(self.config, DAY, self.db)
            self.config["offsets_minutes"]["Fajr"] = 2
            self.config["offsets_minutes"]["Maghrib"] = 0
            second = azan.get_schedule(self.config, DAY, self.db)
            self.assertEqual(second["Fajr"], first["Fajr"] + timedelta(minutes=1))
            self.assertEqual(second["Maghrib"], first["Maghrib"] + timedelta(minutes=1))
            self.assertEqual(azan.get_schedule(self.config, DAY, self.db), second)
            self.assertEqual(request.call_count, 1)

    def test_cache_survives_restart_without_network(self):
        with patch("azan.request_json", return_value=api_response()) as request:
            first = azan.get_schedule(self.config, DAY, self.db)
            self.assertEqual(request.call_count, 1)
        self.db.close()
        self.db = azan.open_database(self.path)
        with patch("azan.request_json", side_effect=AssertionError("offline")):
            self.assertEqual(azan.get_schedule(self.config, DAY, self.db), first)

    def test_cache_changes_with_calculation_settings(self):
        with patch("azan.request_json", return_value=api_response()) as request:
            azan.get_schedule(self.config, DAY, self.db)
            self.config["school"] = 1
            azan.get_schedule(self.config, DAY, self.db)
            self.assertEqual(request.call_count, 2)

    def test_rejects_wrong_date_timezone_offset_and_order(self):
        variants = []
        wrong = api_response()
        wrong["data"]["date"]["gregorian"]["date"] = "19-11-2026"
        variants.append(wrong)
        wrong = api_response()
        wrong["data"]["meta"]["timezone"] = "UTC"
        variants.append(wrong)
        variants.append(api_response(offset="+02:00"))
        wrong = api_response()
        wrong["data"]["timings"]["Sunset"] = "2026-11-18T13:00:00+01:00"
        variants.append(wrong)
        for response in variants:
            with self.subTest(response=response), self.assertRaises(ValueError):
                azan.make_schedule(self.config, DAY, response)

    def test_dst_dates_use_the_correct_offset(self):
        for day, offset in ((date(2026, 3, 29), "+02:00"),
                            (date(2026, 10, 25), "+01:00")):
            with self.subTest(day=day):
                result = azan.make_schedule(self.config, day, api_response(day, offset))
                self.assertEqual(result["Fajr"].utcoffset(), timedelta(hours=int(offset[1:3])))

    def test_nonexistent_and_ambiguous_override_hours_are_rejected(self):
        for day in (date(2026, 3, 29), date(2026, 10, 25)):
            with self.subTest(day=day), self.assertRaises(ValueError):
                azan.local_time(day, "02:30", ZONE)

    def test_future_prayer_does_not_send_or_claim(self):
        self.dispatch(self.when - timedelta(seconds=1), sender=lambda *_: self.fail("sent early"))
        self.assertEqual(self.db.execute("SELECT count(*) FROM events").fetchone()[0], 0)

    def test_late_prayer_is_persistently_skipped(self):
        self.dispatch(self.when + timedelta(seconds=91), sender=lambda *_: self.fail("sent late"))
        self.assertEqual(self.db.execute("SELECT status FROM events").fetchone()[0], "skipped")

    def test_on_time_prayer_sends_once_across_restarts(self):
        calls = []
        self.dispatch(sender=lambda *_: calls.append(1))
        self.db.close()
        self.db = azan.open_database(self.path)
        self.dispatch(sender=lambda *_: calls.append(1))
        self.assertEqual(calls, [1])
        self.assertEqual(self.db.execute("SELECT status FROM events").fetchone()[0], "submitted")

    def test_two_workers_cannot_claim_the_same_prayer(self):
        other = azan.open_database(self.path)
        calls = []
        try:
            self.dispatch(sender=lambda *_: calls.append(1))
            azan.dispatch(self.config, DAY, {"Dhuhr": self.when}, other, True, set(),
                          lambda *_: calls.append(1), lambda: self.when)
        finally:
            other.close()
        self.assertEqual(calls, [1])

    def test_ambiguous_failure_does_not_retry(self):
        calls = []
        def failing(*_):
            calls.append(1)
            raise RuntimeError("Network or JSON failure from api-v3.voicemonkey.io")
        self.dispatch(sender=failing)
        self.dispatch(sender=failing)
        self.assertEqual(calls, [1])
        self.assertEqual(self.db.execute("SELECT status FROM events").fetchone()[0], "failed_or_unknown")

    def test_crash_after_claim_does_not_resend(self):
        def crash(*_):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.dispatch(sender=crash)
        self.dispatch(sender=lambda *_: self.fail("resent after crash"))
        self.assertEqual(self.db.execute("SELECT status FROM events").fetchone()[0], "claimed")

    def test_dry_run_never_claims_or_sends(self):
        seen = set()
        for _ in range(2):
            self.dispatch(live=False, sender=lambda *_: self.fail("dry-run sent"), seen=seen)
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM events").fetchone()[0], 0)
        self.dispatch()
        self.assertEqual(self.db.execute("SELECT status FROM events").fetchone()[0], "submitted")

    def test_live_requires_timetable_and_credentials(self):
        self.config["timetable_confirmed"] = False
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "timetable"):
                azan.require_live_ready(self.config)
            self.config["timetable_confirmed"] = True
            with self.assertRaisesRegex(ValueError, "VOICE_MONKEY"):
                azan.require_live_ready(self.config)

    def test_api_payload_has_no_spoken_prefix_or_token(self):
        with patch.dict(os.environ, {"VOICE_MONKEY_TOKEN": "secret", "VOICE_MONKEY_DEVICE": "echo-id"}), \
             patch("azan.request_json", return_value={"success": True, "data": "OK"}) as request:
            azan.send_announcement(self.config, "Fajr")
            url, payload, token = request.call_args.args
            self.assertEqual(url, "https://api-v3.voicemonkey.io/announce")
            self.assertEqual(payload["device"], "echo-id")
            self.assertNotIn("speech", payload)
            self.assertNotIn("token", payload)
            self.assertEqual(token, "secret")

    def test_fajr_audio_can_be_configured_separately(self):
        self.config["fajr_audio_url"] = "https://example.org/fajr.mp3"
        self.assertEqual(azan.announcement(self.config, "Fajr")["audio"], self.config["fajr_audio_url"])
        self.assertEqual(azan.announcement(self.config, "Isha")["audio"], self.config["audio_url"])

    def test_rejected_response_is_not_success(self):
        with patch.dict(os.environ, {"VOICE_MONKEY_TOKEN": "secret", "VOICE_MONKEY_DEVICE": "echo-id"}), \
             patch("azan.request_json", return_value={"success": False, "error": "failure"}), \
             self.assertRaises(RuntimeError):
            azan.send_announcement(self.config)

    def test_http_reads_retry_with_backoff_but_sends_do_not(self):
        with patch("azan.API_OPENER.open", side_effect=URLError("secret details")) as request, \
             patch("azan.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "Network or JSON failure"):
                azan.request_json("https://api.aladhan.com/example")
            self.assertEqual(request.call_count, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        with patch("azan.API_OPENER.open", side_effect=URLError("secret details")) as request, \
             patch("azan.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "Network or JSON failure"):
                azan.request_json("https://api-v3.voicemonkey.io/announce", {}, "secret")
            self.assertEqual(request.call_count, 1)
            sleep.assert_not_called()

    def test_http_429_read_honors_retry_after(self):
        headers = Message()
        headers["Retry-After"] = "3"
        error = HTTPError("https://api.aladhan.com", 429, "limited", headers, io.BytesIO())
        with patch("azan.API_OPENER.open", side_effect=error) as request, patch("azan.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                azan.request_json("https://api.aladhan.com")
            self.assertEqual(request.call_count, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [3, 3])

    def test_config_rejects_unknown_adjustments_and_insecure_audio(self):
        for change in ({"offsets_minutes": {"Unknown": 7}},
                       {"audio_url": "http://example.org/audio.mp3"},
                       {"audio_url": "https://user:password@example.org/audio.mp3"},
                       {"max_late_seconds": 3600}):
            config = copy.deepcopy(self.config)
            config.update(change)
            path = Path(self.temp.name) / "invalid.json"
            path.write_text(json.dumps(config))
            with self.subTest(change=change), self.assertRaises(ValueError):
                azan.load_config(path)

    def test_corrupt_cache_is_refetched(self):
        with patch("azan.request_json", return_value=api_response()) as request:
            expected = azan.get_schedule(self.config, DAY, self.db)
            self.db.execute("UPDATE cache SET payload='broken JSON'")
            self.assertEqual(azan.get_schedule(self.config, DAY, self.db), expected)
            self.assertEqual(request.call_count, 2)

    def test_only_trusted_https_api_endpoints_are_allowed(self):
        urls = ("file:///etc/passwd", "http://api.aladhan.com/example",
                "https://example.org/", "https://api.aladhan.com:8443/",
                "https://user:password@api.aladhan.com/")
        with patch("azan.API_OPENER.open") as request:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    azan.request_json(url)
            request.assert_not_called()

    def test_redirect_is_rejected_without_retry(self):
        error = HTTPError("https://api-v3.voicemonkey.io/announce", 302, "redirect", Message(), io.BytesIO())
        with patch("azan.API_OPENER.open", side_effect=error) as request, patch("azan.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
                azan.request_json("https://api-v3.voicemonkey.io/announce", {}, "secret")
            self.assertEqual(request.call_count, 1)
            sleep.assert_not_called()

    def test_isha_after_midnight_keeps_original_prayer_date(self):
        self.config["overrides"] = {}
        response = api_response()
        response["data"]["timings"]["Isha"] = "2026-11-19T00:31:00+01:00"
        schedule = azan.make_schedule(self.config, DAY, response)
        self.assertEqual(schedule["Isha"].date(), DAY + timedelta(days=1))
        calls = []
        azan.dispatch(self.config, DAY, {"Isha": schedule["Isha"]}, self.db,
                      True, set(), lambda *_: calls.append(1), lambda: schedule["Isha"])
        self.assertEqual(calls, [1])
        self.assertEqual(self.db.execute("SELECT day FROM events").fetchone()[0], DAY.isoformat())

    def test_test_command_is_safe_by_default(self):
        with patch("azan.send_announcement") as send, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(azan.main(["--config", str(Path(__file__).with_name("config.json")), "test", "--audio"]), 0)
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
