"""Read-only Alexa custom skill; optional dependencies in requirements-alexa.txt."""
import json
import logging
import math
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.utils.predicate import is_intent_name, is_request_type
from ask_sdk_model.interfaces.alexa.presentation.apl.render_document_directive import (
    RenderDocumentDirective,
)
from ask_sdk_model.ui.simple_card import SimpleCard
from ask_sdk_runtime.exceptions import AskSdkException
from ask_sdk_webservice_support.webservice_handler import WebserviceSkillHandler
from flask import Flask, jsonify, request

from azan import UTC, get_schedule, load_config

LOG = logging.getLogger("azan.alexa")
HELP = ("Ask me for the next prayer, how long until the next prayer, "
        "or today's prayer times.")
UNAVAILABLE = "I couldn't load the prayer times. Please try again in a minute."
PRAYER_PHONEMES = {"Asr": "ˈɑ.sɚ", "Isha": "ˈi.ʃɑ"}


def prayer_ssml(speech: str) -> str:
    """Give Alexa pronunciation hints without changing card text."""
    return re.sub(
        r"\b(Asr|Isha)\b",
        lambda match: f'<phoneme alphabet="ipa" ph="{PRAYER_PHONEMES[match[0]]}">{match[0]}</phoneme>',
        escape(speech, quote=False),
    )


def clock_text(stamp, today):
    suffix = " tomorrow" if stamp.date() == today + timedelta(days=1) else ""
    return stamp.strftime("%I:%M %p").lstrip("0") + suffix


def countdown_text(seconds):
    if seconds <= 0:
        return "now"
    if seconds < 60:
        return "in less than a minute"
    hours, minutes = divmod(math.ceil(seconds / 60), 60)
    parts = []
    for value, unit in ((hours, "hour"), (minutes, "minute")):
        if value:
            parts.append(f"{value} {unit}" + ("s" if value != 1 else ""))
    return "in " + " and ".join(parts)


def prayer_reply(config, database, now, all_today=False):
    """Build speech, card, and screen data from the same validated cache."""
    if now.tzinfo is None:
        raise ValueError("The query clock must include a timezone")
    now = now.astimezone(ZoneInfo(config["timezone"]))
    today = now.date()
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as connection:
        schedule = get_schedule(config, today, connection, cached_only=True)
        candidates = list(schedule.items())
        # A previous prayer day's Isha can still be ahead just after midnight.
        try:
            previous = get_schedule(config, today - timedelta(days=1), connection,
                                    cached_only=True)
            candidates.append(("Isha", previous["Isha"]))
        except RuntimeError:
            pass
        upcoming = [(name, stamp) for name, stamp in candidates
                    if stamp.astimezone(UTC) >= now.astimezone(UTC)]
        if not upcoming:
            tomorrow = get_schedule(config, today + timedelta(days=1), connection,
                                    cached_only=True)
            upcoming = list(tomorrow.items())
    upcoming.sort(key=lambda item: item[1].astimezone(UTC))
    name, stamp = upcoming[0]
    seconds = (stamp.astimezone(UTC) - now.astimezone(UTC)).total_seconds()
    next_time = clock_text(stamp, today)
    countdown = countdown_text(seconds)
    introduction = f"The next prayer is {name} at {next_time}, {countdown}."
    later = [(prayer, time) for prayer, time in upcoming[1:] if time.date() == today]
    entries = [(prayer, clock_text(time, today))
               for prayer, time in (schedule.items() if all_today else later)]
    if entries:
        lead = "Today's prayer times are " if all_today else "Later today, "
        detail = lead + ", ".join(f"{prayer} at {time}" for prayer, time in entries) + "."
    elif stamp.date() > today:
        detail = "There are no more prayers today."
    else:
        detail = "That is the last prayer today."
    screen = {
        "title": f"Next: {name}",
        "subtitle": f"{next_time} · {countdown}",
        "rows": ([{"primaryText": f"{prayer} · {time}"} for prayer, time in entries]
                 or [{"primaryText": detail}]),
    }
    speech = introduction + " " + detail
    card = f"{today.isoformat()} | {config['timezone']}\n{introduction}\n{detail}"
    return speech, card, screen


def create_app():
    """Fail closed until a skill ID is configured; SDK verification is always on."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    config = load_config(os.environ.get("AZAN_CONFIG", "config.json"))
    database = os.environ.get("AZAN_DATABASE", "data/azan.sqlite3")
    skill_id = os.environ.get("ALEXA_SKILL_ID", "").strip()
    if skill_id and not re.fullmatch(r"amzn1\.ask\.skill\.[0-9a-f-]{36}", skill_id):
        raise ValueError("ALEXA_SKILL_ID must be the ID from the Alexa developer console")
    apl_path = Path(__file__).with_name("alexa") / "prayer-times-apl.json"
    try:
        apl_document = json.loads(apl_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        LOG.warning("Prayer timetable display template unavailable; using voice only")
        apl_document = None
    builder = SkillBuilder()

    def wants_prayer(handler_input: HandlerInput):
        return (is_request_type("LaunchRequest")(handler_input)
                or is_intent_name("NextPrayerIntent")(handler_input)
                or is_intent_name("TodaysPrayerTimesIntent")(handler_input))

    @builder.request_handler(can_handle_func=wants_prayer)
    def answer_prayer(handler_input: HandlerInput):
        try:
            speech, card, screen = prayer_reply(
                config, database, datetime.now(UTC),
                is_intent_name("TodaysPrayerTimesIntent")(handler_input),
            )
        except (RuntimeError, ValueError, KeyError, TypeError, OSError, sqlite3.Error):
            LOG.warning("Prayer cache unavailable for Alexa query")
            return handler_input.response_builder.speak(UNAVAILABLE).set_should_end_session(True).response
        response = (handler_input.response_builder.speak(prayer_ssml(speech))
                    .set_card(SimpleCard("Prayer times", card)))
        envelope = handler_input.request_envelope
        system = getattr(envelope.context, "system", None)
        interfaces = getattr(getattr(system, "device", None), "supported_interfaces", None)
        if apl_document and envelope.request and interfaces and interfaces.alexa_presentation_apl is not None:
            response.add_directive(RenderDocumentDirective(
                token=envelope.request.request_id,
                document=apl_document,
                datasources={"timetable": screen},
            ))
            # Omit shouldEndSession so the display can stay up without a voice prompt.
        else:
            response.set_should_end_session(True)
        return response.response

    def is_notification(handler_input: HandlerInput):
        return (is_request_type("SessionEndedRequest")(handler_input)
                or is_request_type("Alexa.Presentation.APL.RuntimeError")(handler_input))

    @builder.request_handler(can_handle_func=is_notification)
    def notification(handler_input: HandlerInput):
        return handler_input.response_builder.response

    def wants_stop(handler_input: HandlerInput):
        return (is_intent_name("AMAZON.StopIntent")(handler_input)
                or is_intent_name("AMAZON.CancelIntent")(handler_input)
                or is_intent_name("AMAZON.NavigateHomeIntent")(handler_input))

    @builder.request_handler(can_handle_func=wants_stop)
    def stop(handler_input: HandlerInput):
        return handler_input.response_builder.speak("Goodbye.").set_should_end_session(True).response

    @builder.request_handler(can_handle_func=lambda _: True)
    def help_or_fallback(handler_input: HandlerInput):
        return handler_input.response_builder.speak(HELP).ask(HELP).response

    skill = builder.create()
    skill.skill_id = skill_id
    handler = WebserviceSkillHandler(skill, verify_signature=True, verify_timestamp=True)

    @app.get("/alexa/health")
    def health():
        return jsonify(status="ready" if skill_id else "setup_required"), 200 if skill_id else 503

    @app.post("/alexa")
    def alexa():
        if not skill_id:
            return jsonify(error="Skill setup required"), 503
        if not request.is_json:
            return jsonify(error="JSON required"), 400
        try:
            body = request.get_data().decode("utf-8")
            envelope = json.loads(body)
            if not isinstance(envelope, dict) or not isinstance(envelope.get("request"), dict):
                raise TypeError("Invalid request envelope")
            result = handler.verify_request_and_dispatch(dict(request.headers), body)
        except (AskSdkException, ValueError, TypeError, KeyError, AttributeError, OSError):
            # Never log request bodies, signing headers, or Alexa API access tokens.
            LOG.warning("Rejected invalid Alexa request")
            return jsonify(error="Invalid Alexa request"), 400
        return jsonify(result)

    return app
