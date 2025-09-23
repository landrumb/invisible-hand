import importlib.util
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_module_path = Path(__file__).resolve().parents[1] / "app" / "casino.py"
_spec = importlib.util.spec_from_file_location("app.casino", _module_path)
casino = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("app.casino", casino)
assert _spec and _spec.loader
_spec.loader.exec_module(casino)
CasinoManager = casino.CasinoManager


def make_manager(config_path):
    manager = object.__new__(CasinoManager)
    manager.app = None
    manager.config_path = config_path
    manager._lock = threading.Lock()
    manager._thread = None
    manager._stop = threading.Event()
    manager.slots = {}
    manager.blackjack_min_bet = 5.0
    manager.blackjack_max_bet = 250.0
    manager.blackjack_payout = 1.5
    manager._pending_profit = 0.0
    manager._last_publish = None
    manager.reload_config()
    return manager


def test_reload_config_applies_overrides(tmp_path):
    config_path = tmp_path / "casino.toml"
    config_path.write_text(
        """
[payouts]
default_slot = 0.85

[slots.nova]
payout = 5.0
name = "Custom Nova"
theme = "Custom Theme"
symbols = ["A", "B", "C", "D", "E", "F"]

[[slots.nova.prizes]]
symbol = "X"
label = "Custom Jackpot"
multiplier = 3.0
image = "jackpot.png"

[[slots.nova.prizes]]
symbol = "Y"
multiplier = 1.5

[blackjack]
min_bet = 2.5
max_bet = 150.0
blackjack_payout = 2.0
"""
    )

    manager = make_manager(config_path)

    nova = manager.get_slot("nova")
    assert nova.name == "Custom Nova"
    assert nova.theme == "Custom Theme"
    assert nova.payout_rate == pytest.approx(0.999)
    assert [prize.symbol for prize in nova.prizes] == ["X", "Y"]
    assert nova.symbols == ["X", "Y"]

    neon = manager.get_slot("neon")
    assert neon.payout_rate == pytest.approx(0.85)

    assert manager.blackjack_min_bet == pytest.approx(2.5)
    assert manager.blackjack_max_bet == pytest.approx(150.0)
    assert manager.blackjack_payout == pytest.approx(2.0)


def test_get_slot_invalid_key_raises(tmp_path):
    config_path = tmp_path / "casino.toml"
    config_path.write_text("")

    manager = make_manager(config_path)

    with pytest.raises(ValueError):
        manager.get_slot("missing")


def test_evaluate_slot_grid_multiple_wins(tmp_path):
    config_path = tmp_path / "casino.toml"
    config_path.write_text("")

    manager = make_manager(config_path)
    slot = manager.get_slot("nova")
    prize_lookup = {prize.symbol: prize for prize in slot.prizes}

    winning_symbol = slot.prizes[0].symbol
    reels = [[winning_symbol for _ in range(3)] for _ in range(3)]

    wins = manager._evaluate_slot_grid(reels, prize_lookup, wager=10.0)

    assert len(wins) == 8
    assert all(win.prize.symbol == winning_symbol for win in wins)
    expected_payout = pytest.approx(10.0 * slot.prizes[0].multiplier)
    assert all(win.payout == expected_payout for win in wins)


def test_should_publish_based_on_interval(tmp_path):
    config_path = tmp_path / "casino.toml"
    config_path.write_text("")

    manager = make_manager(config_path)

    now = datetime.utcnow()
    manager._last_publish = None
    assert manager._should_publish(now) is True

    manager._last_publish = now - manager.publish_interval + timedelta(minutes=1)
    assert manager._should_publish(now) is False

    manager._last_publish = now - manager.publish_interval
    assert manager._should_publish(now) is True
