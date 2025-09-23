import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Stub flask module when the real package is unavailable
if "flask" not in sys.modules:
    try:
        import flask  # type: ignore  # noqa: F401
    except ImportError:
        flask_stub = types.ModuleType("flask")
        flask_stub.current_app = types.SimpleNamespace()

        def has_app_context():
            return False

        flask_stub.has_app_context = has_app_context
        flask_stub.before_request = lambda func: func
        sys.modules["flask"] = flask_stub

# Stub flask_login module
if "flask_login" not in sys.modules:
    flask_login_stub = types.ModuleType("flask_login")
    flask_login_stub.UserMixin = type("UserMixin", (), {})
    flask_login_stub.LoginManager = type("LoginManager", (), {"user_loader": lambda self, func: func})
    flask_login_stub.login_required = lambda func: func
    flask_login_stub.current_user = object()
    flask_login_stub.user_loader = lambda func: func
    sys.modules["flask_login"] = flask_login_stub

# Stub itsdangerous
if "itsdangerous" not in sys.modules:
    itsdangerous_stub = types.ModuleType("itsdangerous")

    class DummyBadSignature(Exception):
        pass

    class DummySerializer:
        def __init__(self, *args, **kwargs):
            pass

        def dumps(self, payload):
            return "token"

        def loads(self, token):
            return {}

    itsdangerous_stub.BadSignature = DummyBadSignature
    itsdangerous_stub.URLSafeSerializer = DummySerializer
    sys.modules["itsdangerous"] = itsdangerous_stub

# Stub qrcode module
if "qrcode" not in sys.modules:
    qrcode_stub = types.ModuleType("qrcode")

    class DummyQR:
        def __init__(self, *args, **kwargs):
            pass

        def add_data(self, *args, **kwargs):
            pass

        def make(self, *args, **kwargs):
            pass

        def make_image(self, *args, **kwargs):
            class DummyImage:
                def save(self, *args, **kwargs):
                    pass

            return DummyImage()

    class DummyConstants:
        ERROR_CORRECT_L = 1

    def dummy_make(data, **kwargs):
        qr = DummyQR()
        qr.add_data(data)
        qr.make(**kwargs)
        return qr.make_image()

    qrcode_stub.QRCode = DummyQR
    qrcode_stub.constants = DummyConstants()
    qrcode_stub.make = dummy_make
    sys.modules["qrcode"] = qrcode_stub

# Stub sqlalchemy modules
if "sqlalchemy" not in sys.modules:
    sqlalchemy_stub = types.ModuleType("sqlalchemy")

    def check_constraint(*args, **kwargs):
        return None

    class DummyEnum:
        def __init__(self, *args, **kwargs):
            pass

    sqlalchemy_stub.CheckConstraint = check_constraint
    sqlalchemy_stub.Enum = DummyEnum
    sys.modules["sqlalchemy"] = sqlalchemy_stub

if "sqlalchemy.orm" not in sys.modules:
    orm_stub = types.ModuleType("sqlalchemy.orm")
    orm_stub.joinedload = lambda *args, **kwargs: None
    sys.modules["sqlalchemy.orm"] = orm_stub

# Stub application package with minimal db/login_manager
if "app" not in sys.modules:
    app_pkg = types.ModuleType("app")
    app_pkg.__path__ = [str(ROOT / "app")]

    class DummySession:
        def add(self, *args, **kwargs):
            pass

        def commit(self, *args, **kwargs):
            pass

        def rollback(self, *args, **kwargs):
            pass

        def flush(self, *args, **kwargs):
            pass

        def remove(self, *args, **kwargs):
            pass

        def delete(self, *args, **kwargs):
            pass

    class DummySQLAlchemy:
        session = DummySession()
        Model = type("Model", (), {})
        Integer = int
        Float = float
        String = str
        DateTime = object
        JSON = dict
        Text = str
        Boolean = bool
        LargeBinary = bytes

        @staticmethod
        def Column(*args, **kwargs):
            return None

        @staticmethod
        def ForeignKey(*args, **kwargs):
            return None

        @staticmethod
        def relationship(*args, **kwargs):
            return None

        @staticmethod
        def backref(*args, **kwargs):
            return None

        @staticmethod
        def UniqueConstraint(*args, **kwargs):
            return None

        @staticmethod
        def create_all(*args, **kwargs):
            pass

        @staticmethod
        def drop_all(*args, **kwargs):
            pass

    app_pkg.db = DummySQLAlchemy()

    class DummyLoginManager:
        def user_loader(self, func):
            return func

    app_pkg.login_manager = DummyLoginManager()
    app_pkg.oauth = object()

    sys.modules["app"] = app_pkg
