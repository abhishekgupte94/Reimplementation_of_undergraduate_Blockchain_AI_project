import re

from conftest import login, send_frame

from surveillance.extensions import db
from surveillance.models import RuntimeState
from surveillance.services.emotion_observations import persist_emotions


def ask(client, question):
    response = client.post("/api/admin/chat", json={"question": question})
    assert response.status_code == 200
    return response.json


def test_chatbot_form_has_only_a_question_and_submit(client):
    login(client, "admin")
    response = client.get("/admin/chatbot?user_id=invalid&day=invalid")
    assert response.status_code == 200
    form = re.search(r'<form id="chat-form"[^>]*>(.*?)</form>', response.text, re.S).group(1)
    assert re.findall(r'<input\b[^>]*\bname="([^"]+)"', form) == ["question"]
    assert "<select" not in form
    assert form.count("<button") == 1
    assert 'role="log"' in form


def test_chat_requires_administrator_and_csrf(client, app):
    payload = {"question": "What was Alice’s latest emotion estimate?"}
    assert client.post("/api/admin/chat", json=payload).status_code == 401
    login(client)
    assert client.post("/api/admin/chat", json=payload).status_code == 403
    login(client, "admin")
    app.config["WTF_CSRF_ENABLED"] = True
    page = client.get("/admin/chatbot")
    token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    assert client.post("/api/admin/chat", json=payload).status_code == 400
    response = client.post("/api/admin/chat", json=payload, headers={"X-CSRFToken": token})
    assert response.status_code == 200


def test_question_only_presence_headcount_and_date_queries(trained, clock):
    capture = trained.post("/api/camera/start", json={"room": 1}).json
    assert send_frame(trained, capture).status_code == 200
    clock.advance(0.5)
    assert send_frame(trained, capture, sequence=1).status_code == 200
    clock.advance(86400)
    trained.post("/logout")
    login(trained, "admin")

    latest = ask(trained, "At what time did Alice come in on 2026-11-04?")
    assert "Alice last arrived in Room 1 at 12:00:00" in latest["answer"]
    assert latest["report"]["total_arrivals"] == 1
    next_day = ask(trained, "When did Alice arrive on 2026-11-05?")
    assert "No confirmed arrivals for Alice" in next_day["answer"]
    assert next_day["report"]["current_room"] == 1
    assert "Yes." in ask(trained, "Is @alice in Room 1?")["answer"]
    assert "Bob has no confirmed room" in ask(trained, "Where is Bob?")["answer"]
    headcount = ask(trained, "How many people are in the room?")
    assert "Room 1 is the last known room for 1 person" in headcount["answer"]
    assert "Room 2 is the last known room for 0 people" in headcount["answer"]


def test_question_only_help_and_ambiguity(client):
    login(client, "admin")
    assert "Include a registered participant" in ask(client, "Show visits")["answer"]
    assert "one participant" in ask(client, "When did Alice or Bob arrive?")["answer"]
    assert "Use a valid date" in ask(client, "When did Alice arrive on 2026-02-30?")["answer"]
    assert "No confirmed arrivals" in ask(client, "When did @alice arrive?")["answer"]
    assert "Try" in ask(client, "Write a poem")["answer"]
    assert client.post("/api/admin/chat", json={"question": True}).status_code == 400


def test_question_only_emotion_queries(trained, app, clock):
    capture = trained.post("/api/camera/start", json={"room": 1}).json
    for room, emotion in ((1, "happy"), (2, "sad")):
        if room == 2:
            capture = trained.post("/api/camera/switch", json={**capture, "room": room}).json
        assert send_frame(trained, capture).status_code == 200
        clock.advance(0.5)
        assert send_frame(trained, capture, sequence=1).status_code == 200
        with app.app_context():
            model_id = db.session.get(RuntimeState, 1).active_model_id
            db.session.rollback()
            observation_ids = persist_emotions(
                {
                    "capture_id": capture["capture_id"],
                    "generation": capture["generation"],
                    "room": room,
                    "frame_sequence": 1,
                    "observed_at": clock(),
                    "model_id": model_id,
                },
                {1: {"emotion": emotion, "confidence": 0.95, "model_version": "test-model"}},
            )
            assert len(observation_ids) == 1
        clock.advance(0.5)

    trained.post("/logout")
    login(trained, "admin")
    latest = ask(trained, "What was Alice’s latest emotion estimate?")
    assert "facial expression estimate for Alice was sad in Room 2" in latest["answer"]
    assert latest["report"] is None
    room_one = ask(trained, "How was @alice feeling in Room 1 on 2026-11-04?")["answer"]
    assert "facial expression estimate for Alice was happy in Room 1" in room_one
    assert "12:00:00 GMT on 2026-11-04" in room_one
    assert (
        "no facial expression estimates for Alice in Room 1 on 2026-11-05"
        in ask(trained, "What emotion did Alice show in Room 1 on 2026-11-05?")["answer"]
    )
    assert (
        "no facial expression estimates for Bob"
        in ask(trained, "What was Bob’s facial expression?")["answer"]
    )
