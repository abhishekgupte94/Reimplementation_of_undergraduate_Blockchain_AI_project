import re
from functools import wraps

from flask import abort, request
from flask_login import current_user, login_required


def role_required(role):
    def decorate(function):
        @wraps(function)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role != role:
                abort(403)
            return function(*args, **kwargs)

        return wrapped

    return decorate


def validate_account(username, display_name, password):
    if not re.fullmatch(r"[a-z0-9_.-]{3,64}", username):
        return "Username must contain 3–64 lowercase letters, numbers, dots, dashes or underscores."
    if not 1 <= len(display_name) <= 100:
        return "Enter a display name of 1–100 characters."
    if not 10 <= len(password) <= 128:
        return "Use a password of 10–128 characters."
    return None


def json_object():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "A JSON object is required.")
    return data
