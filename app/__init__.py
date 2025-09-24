import os
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from authlib.integrations.flask_client import OAuth


db = SQLAlchemy()
login_manager = LoginManager()
oauth = OAuth()

# NYC timezone
NYC_TZ = ZoneInfo("America/New_York")


def get_nyc_now():
    """Get current time in NYC timezone."""
    return datetime.now(NYC_TZ)


def utc_to_nyc(utc_dt):
    """Convert UTC datetime to NYC timezone."""
    if utc_dt is None:
        return None
    if utc_dt.tzinfo is None:
        # Assume UTC if no timezone info
        utc_dt = utc_dt.replace(tzinfo=ZoneInfo("UTC"))
    return utc_dt.astimezone(NYC_TZ)


def nyc_to_utc(nyc_dt):
    """Convert NYC datetime to UTC timezone."""
    if nyc_dt is None:
        return None
    if nyc_dt.tzinfo is None:
        # Assume NYC timezone if no timezone info
        nyc_dt = nyc_dt.replace(tzinfo=NYC_TZ)
    return nyc_dt.astimezone(ZoneInfo("UTC"))


def format_nyc_datetime(dt, format_str="%Y-%m-%d %H:%M"):
    """Format datetime in NYC timezone."""
    if dt is None:
        return "—"
    nyc_dt = utc_to_nyc(dt)
    return nyc_dt.strftime(format_str)


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev"),
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL", f"sqlite:///{os.path.join(app.instance_path, 'app.sqlite')}"
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        GOOGLE_CLIENT_ID=os.environ.get("GOOGLE_CLIENT_ID", ""),
        GOOGLE_CLIENT_SECRET=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
    )

    if test_config is not None:
        app.config.update(test_config)

    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError:
        pass

    storage_path = app.config.get("TELESTRATIONS_STORAGE_PATH")
    if not storage_path:
        storage_path = os.path.join(app.instance_path, "telestrations")
        app.config["TELESTRATIONS_STORAGE_PATH"] = storage_path
    try:
        os.makedirs(storage_path, exist_ok=True)
    except OSError:
        pass

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    oauth.init_app(app)
    if app.config["GOOGLE_CLIENT_ID"] and app.config["GOOGLE_CLIENT_SECRET"]:
        oauth.register(
            name="google",
            client_id=app.config["GOOGLE_CLIENT_ID"],
            client_secret=app.config["GOOGLE_CLIENT_SECRET"],
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )

    from . import models  # noqa: F401
    from .auth import bp as auth_bp
    from .routes import bp as main_bp
    from .securities import init_market
    from .casino import init_casino
    from .games import init_games

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    register_cli_commands(app)
    init_market(app)
    init_casino(app)
    init_games(app)

    @app.context_processor
    def inject_now():
        return {"now": get_nyc_now()}

    @app.context_processor
    def inject_timezone_utils():
        return {
            "format_nyc_datetime": format_nyc_datetime,
            "utc_to_nyc": utc_to_nyc,
        }

    @app.context_processor
    def inject_request():
        from flask import request
        return {"request": request}

    return app


def register_cli_commands(app):
    @app.cli.command("init-db")
    def init_db_command():
        """Clear existing data and create new tables."""
        from .models import db

        db.drop_all()
        db.create_all()
        print("Initialized the database.")
