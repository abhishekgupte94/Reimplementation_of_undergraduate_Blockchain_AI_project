import re
from datetime import date, timedelta, timezone
from functools import cache
from zoneinfo import ZoneInfo

from chatterbot import ChatBot
from chatterbot.conversation import Statement
from chatterbot.logic import LogicAdapter
from chatterbot.storage import StorageAdapter
from chatterbot.tagging import NoOpTagger
from flask import current_app

from .emotion_observations import latest_emotion
from .reporting import day_window, last_known_room, public_report, room_report, selected_day

HELP = (
    "Try ‘How many times did Alice appear in each room?’, ‘Is Alice in Room 1?’, "
    "‘When did Alice arrive?’, ‘How many people are in the room?’, "
    "or ‘What was Alice’s latest emotion estimate?’ Use a participant’s name or @username. "
    "Arrival and emotion questions accept today, yesterday, or a date in YYYY-MM-DD format."
)

HEADCOUNT = re.compile(
    r"\b(?:how many (?:people|participants) (?:are )?(?:currently )?in (?:the )?rooms?"
    r"|room headcount)\b"
)
ROOM = re.compile(r"\broom\s+(\d+)\b")
DAY = re.compile(r"\b(today|yesterday|\d{4}-\d{2}-\d{2})\b")


def question_day(question, fallback_day):
    days = set(DAY.findall(question))
    if not days:
        return fallback_day
    if len(days) > 1:
        raise ValueError("Use one date per question.")
    day = days.pop()
    if day == "today":
        return selected_day()
    if day == "yesterday":
        return (date.fromisoformat(selected_day()) - timedelta(days=1)).isoformat()
    try:
        day_window(day)
    except (ValueError, OverflowError):
        raise ValueError("Use a valid date in YYYY-MM-DD format.") from None
    return day


def question_participant(question, participants, fallback_user):
    handles = re.findall(r"(?<!\w)@([a-z0-9_.-]+)", question)
    if handles:
        matches = [user for user in participants if user.username in handles]
        if not matches:
            stripped_handles = {handle.rstrip(".") for handle in handles}
            matches = [user for user in participants if user.username in stripped_handles]
    else:
        matches = [
            user
            for user in participants
            if any(
                re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", question)
                for alias in (user.username, " ".join(user.display_name.casefold().split()))
            )
        ]
        if not matches and fallback_user:
            return fallback_user
    if len(matches) > 1:
        raise ValueError("Please ask about one participant using their @username.")
    if not matches:
        raise ValueError("Include a registered participant’s name or @username in your question.")
    return matches[0]


def local_timestamp(at):
    zone = ZoneInfo(current_app.config["DISPLAY_TIMEZONE"])
    local = at.replace(tzinfo=timezone.utc).astimezone(zone)
    return f"{local:%H:%M:%S %Z} on {local:%Y-%m-%d}"


def report_period(report):
    return f"on {report['day']}" if report["day"] else "across all recorded activity"


def last_arrival(user, report, room):
    last = report["last_arrival"]
    if room is not None:
        last = next((item for item in reversed(report["arrivals"]) if item["room"] == room), None)
    if last is None:
        room_label = f" in Room {room}" if room is not None else ""
        return f"No confirmed arrivals for {user.display_name}{room_label} {report_period(report)}."
    return (
        f"{user.display_name} last arrived in Room {last['room']} at {local_timestamp(last['at'])}."
    )


def arrival_label(count):
    return "arrival" if count == 1 else "arrivals"


def visits(user, report, room):
    recorded = [item for item in report["arrivals"] if room is None or item["room"] == room]
    count = len(recorded)
    lines = [
        f"{user.display_name} has {count} confirmed {arrival_label(count)} {report_period(report)}."
    ]
    for arrival in recorded[:20]:
        lines.append(f"Arrived in Room {arrival['room']} at {local_timestamp(arrival['at'])}.")
    if count > 20:
        lines.append("Showing the first 20 arrivals. See Room analytics for the complete list.")
    return "\n".join(lines)


def room_counts(user, report, room):
    lines = [f"Confirmed arrivals for {user.display_name} {report_period(report)}."]
    for entry in report["rooms"]:
        if room is None or entry["room"] == room:
            count = entry["count"]
            lines.append(
                f"Room {entry['room']} has {count} {arrival_label(count)} "
                f"({entry['percent']:.1f}%)."
            )
    return "\n".join(lines)


def current_room(user, report, room):
    known = report["current_room"]
    if known is None:
        return f"{user.display_name} has no confirmed room yet."
    reply = f"{user.display_name}’s last known room is Room {known}."
    if room is not None:
        return f"{'Yes' if room == known else 'No'}. {reply}"
    return reply


def latest_expression(user, day, room):
    observation = latest_emotion(user.id, room=room, day=day)
    if observation is None:
        location = f" in Room {room}" if room is not None else ""
        period = f" on {day}" if day is not None else ""
        return f"I found no facial expression estimates for {user.display_name}{location}{period}."
    return (
        f"The latest recorded facial expression estimate for {user.display_name} was "
        f"{observation.emotion} in Room {observation.room} at "
        f"{local_timestamp(observation.observed_at)}."
    )


def headcount(participants, room):
    counts = {1: 0, 2: 0}
    for user in participants:
        known = last_known_room(user.id)
        if known is not None:
            counts[known] += 1
    lines = []
    for number, count in counts.items():
        if room is None or number == room:
            label = "person" if count == 1 else "people"
            lines.append(f"Room {number} is the last known room for {count} {label}.")
    return "\n".join(lines)


RULES = (
    (re.compile(r"\b(?:emotions?|expressions?|mood|feel|feeling)\b"), latest_expression),
    (
        re.compile(
            r"\b(?:last (?:seen|sighting|spotted|arrival)"
            r"|(?:latest|most recent) (?:arrival|sighting)"
            r"|(?:when|what time)\b.*\b(?:come in|arriv(?:e|ed)|enter(?:ed)?))\b"
        ),
        last_arrival,
    ),
    (
        re.compile(
            r"\b(?:how many (?:visits|arrivals|appearances)"
            r"|how (?:many times|often)\b.*\b(?:arriv\w*|enter\w*|appear\w*|visit\w*|rooms?)"
            r"|compare\b.*\brooms?"
            r"|arrival counts?|appearances)\b"
        ),
        room_counts,
    ),
    (re.compile(r"\b(?:visits|visit history|sightings|arrivals)\b"), visits),
    (
        re.compile(r"\b(?:where is|is\b.*\bin (?:the )?rooms?|current room|last known room)\b"),
        current_room,
    ),
)


class ActivityRules(LogicAdapter):
    def process(self, statement, additional_response_selection_parameters=None):
        question = " ".join(statement.text.casefold().split())
        answer = self.answer(question, additional_response_selection_parameters)
        reply = Statement(text=answer)
        reply.confidence = int(answer != HELP)
        return reply

    def answer(self, question, context):
        rooms = set(ROOM.findall(question))
        if rooms - {"1", "2"}:
            return "Use Room 1 or Room 2 in your question."
        room = int(rooms.pop()) if len(rooms) == 1 else None
        participants = context["participants"]
        if HEADCOUNT.search(question):
            return headcount(participants, room)
        for pattern, answer in RULES:
            if pattern.search(question):
                if not participants:
                    return "No participants have registered yet."
                try:
                    user = question_participant(question, participants, context["fallback_user"])
                    day = (
                        None
                        if answer is current_room
                        else question_day(question, context["fallback_day"])
                    )
                except ValueError as error:
                    return str(error)
                if answer is latest_expression:
                    return latest_expression(user, day, room)
                report = room_report(user.id, day)
                context["report"] = public_report(report)
                return answer(user, report, room)
        return HELP


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


def chatbot_answer(question, participants, *, fallback_user=None, fallback_day=None):
    if not question.strip() or len(question) > 500:
        return {"answer": HELP, "report": None}
    context = {
        "participants": participants,
        "fallback_user": fallback_user,
        "fallback_day": fallback_day,
        "report": None,
    }
    answer = activity_bot().get_response(question, additional_response_selection_parameters=context)
    return {"answer": answer.text, "report": context["report"]}
