import os
from datetime import datetime

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from authlib.integrations.flask_client import OAuth


db = SQLAlchemy()
login_manager = LoginManager()
oauth = OAuth()


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

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    register_cli_commands(app)
    init_market(app)

    @app.context_processor
    def inject_now():
        return {"now": datetime.utcnow()}

    return app


def register_cli_commands(app):
    @app.cli.command("init-db")
    def init_db_command():
        """Clear existing data and create new tables."""
        from .models import db

        db.drop_all()
        db.create_all()
        print("Initialized the database.")
