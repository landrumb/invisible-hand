from datetime import UTC, datetime
from types import SimpleNamespace

from app.models import TelestrationEntry, TelestrationGame


def test_game_active_state_transitions():
    game = TelestrationGame()
    game.max_turns = 4
    game.turns_taken = 3
    game.completed_at = None
    assert game.is_active()
    game.turns_taken = 4
    assert not game.is_active()
    game.turns_taken = 2
    game.completed_at = datetime.now(UTC)
    assert not game.is_active()


def test_entry_contributor_prefix_and_upvotes():
    contributor = SimpleNamespace(email="artist@example.com")
    entry = TelestrationEntry()
    entry.entry_type = "description"
    entry.text_content = "A sketch"
    entry.upvotes = [1, 2]
    entry.contributor = contributor
    assert entry.contributor_prefix() == "artist"
    assert entry.upvote_count() == 2

    entry.contributor = None
    entry.upvotes = None
    assert entry.contributor_prefix() == "unknown"
    assert entry.upvote_count() == 0


def test_entry_image_availability():
    entry = TelestrationEntry()
    entry.entry_type = "image"
    entry.image_filename = "test.png"
    assert entry.image_available()

    entry.image_filename = ""
    assert not entry.image_available()

    entry.entry_type = "description"
    entry.image_filename = "test.png"
    assert not entry.image_available()
