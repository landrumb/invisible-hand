import os
import time
import types
from pathlib import Path

from app.games import GamesManager


_MTIME_OFFSET = 0.0


class DummyLogger:
    def __init__(self):
        self.messages: list[tuple[tuple, dict]] = []

    def warning(self, *args, **kwargs):  # pragma: no cover - logging side effect
        self.messages.append((args, kwargs))


def _write_with_new_mtime(path: Path, content: str) -> None:
    global _MTIME_OFFSET
    path.write_text(content)
    _MTIME_OFFSET += 1.0
    new_time = time.time() + _MTIME_OFFSET
    os.utime(path, (new_time, new_time))


def _build_manager(
    tmp_path: Path, games_content: str, trivia_content: str, submitted_content: str = ""
) -> GamesManager:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    games_path = config_dir / "games.toml"
    trivia_path = config_dir / "trivia.toml"
    submitted_path = config_dir / "submitted_trivia.toml"
    _write_with_new_mtime(games_path, games_content)
    _write_with_new_mtime(trivia_path, trivia_content)
    _write_with_new_mtime(submitted_path, submitted_content)
    app = types.SimpleNamespace(
        config={"SECRET_KEY": "test"},
        root_path=str(tmp_path),
        extensions={},
        logger=DummyLogger(),
    )
    return GamesManager(
        app,
        games_path=games_path,
        trivia_path=trivia_path,
        submitted_trivia_path=submitted_path,
    )


def test_games_manager_hides_disabled_entries(tmp_path):
    manager = _build_manager(
        tmp_path,
        """
[[games]]
key = "visible"
name = "Visible"
type = "reaction"
description = "Visible"
enabled = true
base_reward = 1.0
max_time = 0.5

[[games]]
key = "hidden"
name = "Hidden"
type = "reaction"
description = "Hidden"
enabled = false
base_reward = 1.0
max_time = 0.5
""",
        """
[[sets]]
key = "quiz"
title = "Quiz"
reward = 2.0

  [[sets.questions]]
  id = "q1"
  prompt = "Question?"
  choices = ["a", "b"]
  answer = 0
""",
    )

    games = manager.list_games()
    assert [game.key for game in games] == ["visible"]
    assert manager.get_game("hidden") is None


def test_games_manager_live_reload_and_invalid_files(tmp_path):
    initial_games = """
[[games]]
key = "alpha"
name = "Alpha"
type = "reaction"
description = "First"
enabled = true
base_reward = 1.0
max_time = 1.0
"""
    initial_trivia = """
[[sets]]
key = "quiz"
title = "Quiz"
reward = 3.0

  [[sets.questions]]
  id = "q1"
  prompt = "Q1?"
  choices = ["a", "b"]
  answer = 0
"""

    manager = _build_manager(tmp_path, initial_games, initial_trivia)
    games_path = Path(manager.games_path)
    trivia_path = Path(manager.trivia_path)
    submitted_path = Path(manager.submitted_trivia_path)

    assert [game.name for game in manager.list_games()] == ["Alpha"]
    assert manager.get_trivia_set("quiz").reward == 3.0

    updated_games = """
[[games]]
key = "alpha"
name = "Alpha Updated"
type = "reaction"
description = "Updated"
enabled = true
base_reward = 2.0
max_time = 1.0
"""
    previous_mtime = manager._games_mtime
    _write_with_new_mtime(games_path, updated_games)
    updated_mtime = games_path.stat().st_mtime
    assert updated_mtime != previous_mtime
    assert [game.name for game in manager.list_games()] == ["Alpha Updated"]
    assert manager._games_mtime == updated_mtime

    invalid_games = """
[[games]
key = "broken"
"""
    _write_with_new_mtime(games_path, invalid_games)
    # Invalid file should preserve the previously loaded definition
    assert [game.name for game in manager.list_games()] == ["Alpha Updated"]

    repaired_games = """
[[games]]
key = "beta"
name = "Beta"
type = "reaction"
description = "Second"
enabled = true
base_reward = 3.0
max_time = 0.5
"""
    _write_with_new_mtime(games_path, repaired_games)
    repaired_mtime = games_path.stat().st_mtime
    assert repaired_mtime != manager._games_mtime
    assert [game.key for game in manager.list_games()] == ["beta"]
    assert manager._games_mtime == repaired_mtime

    updated_trivia = """
[[sets]]
key = "quiz"
title = "Quiz"
reward = 4.5

  [[sets.questions]]
  id = "q1"
  prompt = "Q1?"
  choices = ["a", "b"]
  answer = 1
"""
    _write_with_new_mtime(trivia_path, updated_trivia)
    assert manager.get_trivia_set("quiz").reward == 4.5

    submitted_questions = """
[[sets]]
key = "quiz"
title = "Quiz"
reward = 1.0

  [[sets.questions]]
  id = "s1"
  prompt = "Submitted?"
  choices = ["a", "b", "c"]
  answer = 1
  submitted_by = "user@example.com"
"""
    _write_with_new_mtime(submitted_path, submitted_questions)
    questions = manager.get_trivia_set("quiz").questions
    assert any(question.id == "s1" and question.submitted_by == "user@example.com" for question in questions)

    invalid_trivia = """
[[sets]
key = "quiz"
"""
    _write_with_new_mtime(trivia_path, invalid_trivia)
    assert manager.get_trivia_set("quiz").reward == 4.5

    repaired_trivia = """
[[sets]]
key = "quiz"
title = "Repaired"
reward = 6.0

  [[sets.questions]]
  id = "q1"
  prompt = "Q1?"
  choices = ["a", "b"]
  answer = 0
"""
    _write_with_new_mtime(trivia_path, repaired_trivia)
    assert manager.get_trivia_set("quiz").reward == 6.0

    manager.append_submitted_question(
        "quiz",
        {
            "prompt": "Another?",
            "choices": ["one", "two"],
            "answer": 1,
            "submitted_by": "author@example.com",
        },
    )
    merged_questions = manager.get_trivia_set("quiz").questions
    assert any(question.prompt == "Another?" for question in merged_questions)
