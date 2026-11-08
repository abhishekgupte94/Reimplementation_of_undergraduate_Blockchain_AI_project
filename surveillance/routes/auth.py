from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import User
from ..security import validate_account

bp = Blueprint("auth", __name__)


@bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        name = request.form.get("display_name", "").strip()
        password = request.form.get("password", "")
        error = validate_account(username, name, password)
        if not error:
            user = User(username=username, display_name=name, role="participant")
            user.set_password(password)
            db.session.add(user)
            try:
                db.session.commit()
                flash("Account created. Please log in.")
                return redirect(url_for("auth.login"))
            except IntegrityError:
                db.session.rollback()
                error = "That username is already taken."
        flash(error, "error")
    return render_template("register.html")


@bp.route("/login", methods=["GET", "POST"])
@bp.route("/admin/login", methods=["GET", "POST"], endpoint="admin_login")
def login():
    admin = request.path.startswith("/admin/")
    if request.method == "POST":
        user = db.session.scalar(
            db.select(User).where(User.username == request.form.get("username", "").strip().lower())
        )
        if user and user.check_password(request.form.get("password", "")):
            if admin and user.role != "admin":
                flash("Use the participant login for this account.", "error")
            else:
                login_user(user)
                return redirect(
                    url_for("admin.analytics" if user.role == "admin" else "enrollment.index")
                )
        else:
            flash("Invalid username or password.", "error")
    return render_template("login.html", admin_login=admin)


@bp.post("/logout")
@login_required
def logout():
    from ..services.presence import stop_user_capture

    stop_user_capture(current_user.id)
    logout_user()
    return redirect(url_for("auth.login"))
