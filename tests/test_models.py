import importlib.util
import sys
from pathlib import Path

_module_path = Path(__file__).resolve().parents[1] / "app" / "models.py"
_spec = importlib.util.spec_from_file_location("app.models", _module_path)
models = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("app.models", models)
assert _spec and _spec.loader
_spec.loader.exec_module(models)
Role = models.Role


def test_role_privilege_hierarchy():
    assert Role.ADMIN.at_least(Role.MERCHANT)
    assert Role.ADMIN.at_least(Role.PLAYER)
    assert Role.MERCHANT.at_least(Role.PLAYER)
    assert Role.MERCHANT.at_least("player")
    assert not Role.PLAYER.at_least(Role.ADMIN)
    assert not Role.PLAYER.at_least("merchant")
