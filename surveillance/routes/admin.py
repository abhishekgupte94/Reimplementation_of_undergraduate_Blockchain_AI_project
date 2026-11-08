from flask import Blueprint, abort, jsonify, render_template, request

from ..extensions import db
from ..models import User
from ..security import json_object, role_required
from ..services.presence import reap_capture
from ..services.reporting import (
    day_window,
    public_report,
    room_report,
    selected_day,
)

bp = Blueprint("admin", __name__)


def filters(source, *, use_defaults=True):
    participants = list(
        db.session.scalars(
            db.select(User).where(User.role == "participant").order_by(User.display_name, User.id)
        )
    )
    raw = source.get("user_id")
    if raw is None or raw == "":
        user = participants[0] if participants and use_defaults else None
    else:
        try:
            user = next((u for u in participants if u.id == int(raw)), None)
        except (TypeError, ValueError):
            abort(400, "Invalid participant.")
        if user is None:
            abort(404, "Participant not found.")
    day = source.get("day") or (selected_day() if use_defaults else None)
    if day is not None:
        try:
            day_window(day)
        except (ValueError, TypeError, OverflowError):
            abort(400, "Use a valid date in YYYY-MM-DD format.")
    return participants, user, day


@bp.get("/admin/analytics")
@role_required("admin")
def analytics():
    reap_capture()
    participants, user, day = filters(request.args)
    report = room_report(user.id, day) if user else None
    return render_template(
        "analytics.html", participants=participants, selected_user=user, day=day, report=report
    )


@bp.get("/api/admin/report")
@role_required("admin")
def report():
    reap_capture()
    _, user, day = filters(request.args)
    return jsonify(public_report(room_report(user.id, day)) if user else None)


@bp.get("/admin/chatbot")
@role_required("admin")
def chatbot():
    return render_template("chatbot.html")


@bp.post("/api/admin/chat")
@role_required("admin")
def chat():
    data = json_object()
    question = data.get("question", "")
    if not isinstance(question, str):
        abort(400, "Question must be text.")

    from ..services.chatbot import chatbot_answer

    participants, user, day = filters(data, use_defaults=False)
    return jsonify(chatbot_answer(question, participants, fallback_user=user, fallback_day=day))
