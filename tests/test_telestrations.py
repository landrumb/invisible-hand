import base64
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


def test_image_data_url_encoding():
    payload = b"binary-data"
    expected = base64.b64encode(payload).decode("ascii")
    entry = TelestrationEntry()
    entry.entry_type = "image"
    entry.image_data = payload
    entry.image_mime_type = "image/jpeg"
    assert entry.image_data_url() == f"data:image/jpeg;base64,{expected}"

    empty_entry = TelestrationEntry()
    empty_entry.entry_type = "description"
    assert empty_entry.image_data_url() is None
