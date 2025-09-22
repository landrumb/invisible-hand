from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from . import db, oauth
from .models import Role, User


bp = Blueprint("auth", __name__, url_prefix="/auth")


def require_oauth():
    provider = oauth.create_client("google") if "google" in oauth._clients else None
    if provider is None:
        flash(
            "Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
            "error",
        )
        return False
    return True


@bp.route("/login")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if not require_oauth():
        return render_template("login.html", allow_guest=True)
    redirect_uri = url_for("auth.authorize", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@bp.route("/authorize")
def authorize():
    if not require_oauth():
        return redirect(url_for("main.index"))
    token = oauth.google.authorize_access_token()
    user_info = token.get("userinfo")
    if not user_info:
        user_info = oauth.google.parse_id_token(token)
    if user_info is None:
        flash("Unable to fetch user information from Google.", "error")
        return redirect(url_for("main.index"))

    google_id = user_info["sub"]
    email = user_info.get("email")
    name = user_info.get("name") or email

    user = User.query.filter_by(google_id=google_id).first()
    if not user:
        user = User(google_id=google_id, email=email, name=name, role=Role.PLAYER)
        db.session.add(user)
        db.session.commit()

    login_user(user)
    session["token"] = token
    flash(f"Welcome, {user.name}!", "success")
    return redirect(url_for("main.dashboard"))


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    session.pop("token", None)
    flash("You have been signed out.", "info")
    return redirect(url_for("main.index"))


@bp.route("/guest-login", methods=["POST"])
def guest_login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    user = User.query.filter_by(google_id="guest").first()
    if not user:
        user = User(
            google_id="guest",
            email="guest@example.com",
            name="Guest",
            role=Role.PLAYER,
        )
        db.session.add(user)
        db.session.commit()

    login_user(user)
    session["token"] = {"userinfo": {"name": user.name}}
    flash("Signed in temporarily as Guest.", "info")
    return redirect(url_for("main.dashboard"))
