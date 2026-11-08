import re
from datetime import timezone
from functools import cache
from zoneinfo import ZoneInfo

from chatterbot import ChatBot
from chatterbot.conversation import Statement
from chatterbot.logic import LogicAdapter
from chatterbot.storage import StorageAdapter
from chatterbot.tagging import NoOpTagger
from flask import current_app

HELP = (
    "I can answer questions about the selected participant and date. "
    "Try ‘time in each room’, ‘compare rooms’, ‘show visits’, or ‘last seen’."
)


def last_seen(user, report):
    last = report["last_seen"]
    if last is None:
        return f"No sightings of {user.display_name} on {report['day']}."
    zone = ZoneInfo(current_app.config["DISPLAY_TIMEZONE"])
    local = last.observed_at.replace(tzinfo=timezone.utc).astimezone(zone)
    return (
        f"{user.display_name} was last seen in Room {last.room} at "
        f"{local:%H:%M:%S %Z} on {report['day']}."
    )


def visits(user, report):
    recorded = report["visits"]
    visit_label = "visit" if len(recorded) == 1 else "visits"
    summary = (
        f"{user.display_name} has {len(recorded)} confirmed {visit_label} "
        f"on {report['day']}. Total observed time: {report['total_seconds']:.1f} seconds."
    )
    zone = ZoneInfo(current_app.config["DISPLAY_TIMEZONE"])
    lines = [summary]
    for visit in recorded[:20]:
        first = visit["first_seen"].replace(tzinfo=timezone.utc).astimezone(zone)
        last = visit["last_seen"].replace(tzinfo=timezone.utc).astimezone(zone)
        lines.append(
            f"Room {visit['room']}: {first:%H:%M:%S %Z} to {last:%H:%M:%S %Z}, "
            f"{visit['seconds']:.1f}s."
        )
    if len(recorded) > 20:
        lines.append("Showing the first 20 visits. See Room analytics for the complete list.")
    return "\n".join(lines)


def room_times(user, report):
    lines = [f"{user.display_name} on {report['day']}"]
    lines.extend(
        f"Room {room['room']}: {room['seconds']:.1f}s ({room['percent']:.1f}%)"
        for room in report["rooms"]
    )
    lines.append("Percentages include observed room time only.")
    return "\n".join(lines)


RULES = (
    (re.compile(r"\b(?:last (?:seen|sighting|spotted)|most recent sighting)\b"), last_seen),
    (re.compile(r"\b(?:visits|visit history|sightings|entries and exits)\b"), visits),
    (
        re.compile(
            r"\b(?:time (?:spent|(?:in|per|by) (?:(?:each|both|the) )?rooms?)"
            r"|compare (?:the )?rooms|room (?:times?|duration)"
            r"|how long\b.*\b(?:stay|stayed|spend|spent|rooms?)|(?:more|most) time)\b"
        ),
        room_times,
    ),
)


class ActivityRules(LogicAdapter):
    def process(self, statement, additional_response_selection_parameters=None):
        question = " ".join(statement.text.casefold().split())
        context = additional_response_selection_parameters
        for pattern, answer in RULES:
            if pattern.search(question):
                reply = Statement(text=answer(context["user"], context["report"]))
                reply.confidence = 1
                return reply
        reply = Statement(text=HELP)
        reply.confidence = 0
        return reply


class NoConversationStorage(StorageAdapter):
    def get_statement_object(self):
        return Statement

    def filter(self, **kwargs):
        return ()


@cache
def activity_bot():
    return ChatBot(
        "Room activity",
        logic_adapters=[f"{__name__}.ActivityRules"],
        storage_adapter=f"{__name__}.NoConversationStorage",
        tagger=NoOpTagger,
        read_only=True,
    )


def chatbot_answer(question, user, report):
    if not question.strip() or len(question) > 500:
        return HELP
    return activity_bot().get_response(
        question, additional_response_selection_parameters={"user": user, "report": report}
    ).text
