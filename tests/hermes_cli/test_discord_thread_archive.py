"""Tests for Discord thread archive on session prune."""

import json
import time
from unittest.mock import patch

from hermes_cli.discord_thread_archive import (
    archive_discord_thread,
    archive_threads_for_removed_sessions,
    collect_discord_threads_for_sessions,
    discord_thread_id_from_origin,
    remove_sessions_json_entries,
)


class TestDiscordThreadIdFromOrigin:
    def test_thread_origin(self):
        origin = {
            "platform": "discord",
            "chat_type": "thread",
            "chat_id": "123",
            "thread_id": "456",
        }
        assert discord_thread_id_from_origin(origin) == "456"

    def test_thread_falls_back_to_chat_id(self):
        origin = {
            "platform": "discord",
            "chat_type": "thread",
            "chat_id": "789",
        }
        assert discord_thread_id_from_origin(origin) == "789"

    def test_non_thread_returns_none(self):
        assert discord_thread_id_from_origin({"platform": "discord", "chat_type": "dm"}) is None
        assert discord_thread_id_from_origin({"platform": "telegram", "chat_type": "thread"}) is None


class TestCollectDiscordThreadsForSessions:
    def test_collects_matching_sessions(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        data = {
            "discord:thread:111": {
                "session_id": "sess-old",
                "display_name": "codexbar",
                "origin": {
                    "platform": "discord",
                    "chat_type": "thread",
                    "thread_id": "999",
                    "chat_id": "999",
                },
            },
            "discord:thread:222": {
                "session_id": "sess-active",
                "origin": {
                    "platform": "discord",
                    "chat_type": "thread",
                    "thread_id": "888",
                },
            },
        }
        (sessions_dir / "sessions.json").write_text(json.dumps(data))

        threads = collect_discord_threads_for_sessions(
            sessions_dir, ["sess-old"], exclude_thread_ids=["777"]
        )
        assert threads == {"999": "codexbar"}

    def test_respects_exclude_thread_ids(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        data = {
            "k": {
                "session_id": "sess-old",
                "origin": {
                    "platform": "discord",
                    "chat_type": "thread",
                    "thread_id": "999",
                },
            }
        }
        (sessions_dir / "sessions.json").write_text(json.dumps(data))

        assert collect_discord_threads_for_sessions(
            sessions_dir, ["sess-old"], exclude_thread_ids=["999"]
        ) == {}


class TestRemoveSessionsJsonEntries:
    def test_removes_pruned_entries(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        data = {
            "keep": {"session_id": "active"},
            "drop": {"session_id": "old"},
        }
        (sessions_dir / "sessions.json").write_text(json.dumps(data, indent=2))

        removed = remove_sessions_json_entries(sessions_dir, ["old"])
        assert removed == 1

        saved = json.loads((sessions_dir / "sessions.json").read_text())
        assert saved == {"keep": {"session_id": "active"}}


class TestArchiveDiscordThread:
    def test_archives_unarchived_thread(self):
        responses = [
            {"thread_metadata": {"archived": False}},
            {"thread_metadata": {"archived": True}},
        ]

        def fake_request(method, path, token, body=None, timeout=15):
            if method == "GET":
                return responses[0]
            assert method == "PATCH"
            assert body == {"archived": True}
            return responses[1]

        with patch(
            "hermes_cli.discord_thread_archive._discord_request",
            side_effect=fake_request,
        ):
            assert archive_discord_thread("token", "123") == "archived"

    def test_skips_already_archived(self):
        with patch(
            "hermes_cli.discord_thread_archive._discord_request",
            return_value={"thread_metadata": {"archived": True}},
        ):
            assert archive_discord_thread("token", "123") == "skipped"


class TestArchiveThreadsForRemovedSessions:
    def test_noop_without_token(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "sessions.json").write_text(
            json.dumps(
                {
                    "k": {
                        "session_id": "old",
                        "origin": {
                            "platform": "discord",
                            "chat_type": "thread",
                            "thread_id": "111",
                        },
                    }
                }
            )
        )

        with patch.dict("os.environ", {}, clear=True):
            with patch("hermes_cli.discord_thread_archive._get_bot_token", return_value=None):
                result = archive_threads_for_removed_sessions(
                    ["old"], sessions_dir, enabled=True
                )

        assert result["archived"] == 0
        assert result["threads_found"] == 0
        assert json.loads((sessions_dir / "sessions.json").read_text())["k"]["session_id"] == "old"

    def test_archives_and_cleans_index(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "sessions.json").write_text(
            json.dumps(
                {
                    "k": {
                        "session_id": "old",
                        "display_name": "test-thread",
                        "origin": {
                            "platform": "discord",
                            "chat_type": "thread",
                            "thread_id": "111",
                        },
                    }
                }
            )
        )

        with patch("hermes_cli.discord_thread_archive._get_bot_token", return_value="tok"):
            with patch(
                "hermes_cli.discord_thread_archive.archive_discord_thread",
                return_value="archived",
            ) as archive_mock:
                result = archive_threads_for_removed_sessions(
                    ["old"], sessions_dir, enabled=True
                )

        archive_mock.assert_called_once_with("tok", "111")
        assert result["archived"] == 1
        assert result["sessions_json_removed"] == 1
        assert json.loads((sessions_dir / "sessions.json").read_text()) == {}


class TestPruneSessionsIntegration:
    def test_prune_sessions_archives_discord_threads(self, tmp_path):
        from hermes_state import SessionDB

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "sessions.json").write_text(
            json.dumps(
                {
                    "k": {
                        "session_id": "old",
                        "origin": {
                            "platform": "discord",
                            "chat_type": "thread",
                            "thread_id": "555",
                        },
                    }
                }
            )
        )

        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(session_id="old", source="discord")
        db.end_session("old", end_reason="done")
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - 100 * 86400, "old"),
        )
        db._conn.commit()

        with patch(
            "hermes_cli.discord_thread_archive._get_bot_token",
            return_value="tok",
        ):
            with patch(
                "hermes_cli.discord_thread_archive.archive_discord_thread",
                return_value="archived",
            ):
                count = db.prune_sessions(
                    older_than_days=90,
                    sessions_dir=sessions_dir,
                    archive_discord_threads=True,
                )

        assert count == 1
        assert db.last_prune_discord_archive["archived"] == 1
        assert db.last_prune_discord_archive["sessions_json_removed"] == 1
        assert json.loads((sessions_dir / "sessions.json").read_text()) == {}
        db.close()
