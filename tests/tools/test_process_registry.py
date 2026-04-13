"""Tests for tools/process_registry.py — ProcessRegistry query methods, pruning, checkpoint."""

import json
import os
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from hermes_constants import get_current_tenant, tenant_context
from tools.environments.local import _HERMES_PROVIDER_ENV_FORCE_PREFIX
from tools.process_registry import (
    ProcessRegistry,
    ProcessSession,
    MAX_OUTPUT_CHARS,
    FINISHED_TTL_SECONDS,
    MAX_PROCESSES,
)


@pytest.fixture()
def registry():
    """Create a fresh ProcessRegistry."""
    return ProcessRegistry()


def _make_session(
    sid="proc_test123",
    command="echo hello",
    task_id="t1",
    exited=False,
    exit_code=None,
    output="",
    started_at=None,
    user_id="default",
) -> ProcessSession:
    """Helper to create a ProcessSession for testing."""
    s = ProcessSession(
        id=sid,
        command=command,
        task_id=task_id,
        started_at=started_at or time.time(),
        exited=exited,
        exit_code=exit_code,
        output_buffer=output,
        user_id=user_id,
    )
    return s


class TestTenantResolution:
    def test_current_tenant_prefers_context(self, monkeypatch, registry):
        monkeypatch.setenv("HERMES_USER_ID", "envuser")
        with tenant_context("alice"):
            assert registry._current_tenant() == "alice"
        assert registry._current_tenant() == "envuser"


# =========================================================================
# Get / Poll
# =========================================================================

class TestGetAndPoll:
    def test_get_not_found(self, registry):
        assert registry.get("nonexistent") is None

    def test_get_running(self, registry):
        s = _make_session()
        registry._running[s.id] = s
        assert registry.get(s.id) is s

    def test_get_finished(self, registry):
        s = _make_session(exited=True, exit_code=0)
        registry._finished[s.id] = s
        assert registry.get(s.id) is s

    def test_poll_not_found(self, registry):
        result = registry.poll("nonexistent")
        assert result["status"] == "not_found"

    def test_poll_running(self, registry):
        s = _make_session(output="some output here")
        registry._running[s.id] = s
        result = registry.poll(s.id)
        assert result["status"] == "running"
        assert "some output" in result["output_preview"]
        assert result["command"] == "echo hello"

    def test_poll_exited(self, registry):
        s = _make_session(exited=True, exit_code=0, output="done")
        registry._finished[s.id] = s
        result = registry.poll(s.id)
        assert result["status"] == "exited"
        assert result["exit_code"] == 0


# =========================================================================
# Read log
# =========================================================================

class TestReadLog:
    def test_not_found(self, registry):
        result = registry.read_log("nonexistent")
        assert result["status"] == "not_found"

    def test_read_full_log(self, registry):
        lines = "\n".join([f"line {i}" for i in range(50)])
        s = _make_session(output=lines)
        registry._running[s.id] = s
        result = registry.read_log(s.id)
        assert result["total_lines"] == 50

    def test_read_with_limit(self, registry):
        lines = "\n".join([f"line {i}" for i in range(100)])
        s = _make_session(output=lines)
        registry._running[s.id] = s
        result = registry.read_log(s.id, limit=10)
        # Default: last 10 lines
        assert "10 lines" in result["showing"]

    def test_read_with_offset(self, registry):
        lines = "\n".join([f"line {i}" for i in range(100)])
        s = _make_session(output=lines)
        registry._running[s.id] = s
        result = registry.read_log(s.id, offset=10, limit=5)
        assert "5 lines" in result["showing"]


# =========================================================================
# List sessions
# =========================================================================

class TestListSessions:
    def test_empty(self, registry):
        assert registry.list_sessions() == []

    def test_lists_running_and_finished(self, registry):
        s1 = _make_session(sid="proc_1", task_id="t1")
        s2 = _make_session(sid="proc_2", task_id="t1", exited=True, exit_code=0)
        registry._running[s1.id] = s1
        registry._finished[s2.id] = s2
        result = registry.list_sessions()
        assert len(result) == 2

    def test_filter_by_task_id(self, registry):
        s1 = _make_session(sid="proc_1", task_id="t1")
        s2 = _make_session(sid="proc_2", task_id="t2")
        registry._running[s1.id] = s1
        registry._running[s2.id] = s2
        result = registry.list_sessions(task_id="t1")
        assert len(result) == 1
        assert result[0]["session_id"] == "proc_1"

    def test_list_entry_fields(self, registry):
        s = _make_session(output="preview text")
        registry._running[s.id] = s
        entry = registry.list_sessions()[0]
        assert "session_id" in entry
        assert "command" in entry
        assert "status" in entry
        assert "pid" in entry
        assert "output_preview" in entry


# =========================================================================
# Active process queries
# =========================================================================

class TestActiveQueries:
    def test_has_active_processes(self, registry):
        s = _make_session(task_id="t1")
        registry._running[s.id] = s
        assert registry.has_active_processes("t1") is True
        assert registry.has_active_processes("t2") is False

    def test_has_active_for_session(self, registry):
        s = _make_session()
        s.session_key = "gw_session_1"
        registry._running[s.id] = s
        assert registry.has_active_for_session("gw_session_1") is True
        assert registry.has_active_for_session("other") is False

    def test_exited_not_active(self, registry):
        s = _make_session(task_id="t1", exited=True, exit_code=0)
        registry._finished[s.id] = s
        assert registry.has_active_processes("t1") is False


# =========================================================================
# Pruning
# =========================================================================

class TestPruning:
    def test_prune_expired_finished(self, registry):
        old_session = _make_session(
            sid="proc_old",
            exited=True,
            started_at=time.time() - FINISHED_TTL_SECONDS - 100,
        )
        registry._finished[old_session.id] = old_session
        registry._prune_if_needed()
        assert "proc_old" not in registry._finished

    def test_prune_keeps_recent(self, registry):
        recent = _make_session(sid="proc_recent", exited=True)
        registry._finished[recent.id] = recent
        registry._prune_if_needed()
        assert "proc_recent" in registry._finished

    def test_prune_over_max_removes_oldest(self, registry):
        # Fill up to MAX_PROCESSES
        for i in range(MAX_PROCESSES):
            s = _make_session(
                sid=f"proc_{i}",
                exited=True,
                started_at=time.time() - i,  # older as i increases
            )
            registry._finished[s.id] = s

        # Add one more running to trigger prune
        s = _make_session(sid="proc_new")
        registry._running[s.id] = s
        registry._prune_if_needed()

        total = len(registry._running) + len(registry._finished)
        assert total <= MAX_PROCESSES


# =========================================================================
# Spawn env sanitization
# =========================================================================

class TestSpawnEnvSanitization:
    def test_spawn_local_strips_blocked_vars_from_background_env(self, registry):
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["env"] = kwargs["env"]
            proc = MagicMock()
            proc.pid = 4321
            proc.stdout = iter([])
            proc.stdin = MagicMock()
            proc.poll.return_value = None
            return proc

        fake_thread = MagicMock()

        with patch.dict(os.environ, {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/user",
            "USER": "tester",
            "TELEGRAM_BOT_TOKEN": "bot-secret",
            "FIRECRAWL_API_KEY": "fc-secret",
        }, clear=True), \
            patch("tools.process_registry._find_shell", return_value="/bin/bash"), \
            patch("subprocess.Popen", side_effect=fake_popen), \
            patch("threading.Thread", return_value=fake_thread), \
            patch.object(registry, "_write_checkpoint"):
            registry.spawn_local(
                "echo hello",
                cwd="/tmp",
                env_vars={
                    "MY_CUSTOM_VAR": "keep-me",
                    "TELEGRAM_BOT_TOKEN": "drop-me",
                    f"{_HERMES_PROVIDER_ENV_FORCE_PREFIX}TELEGRAM_BOT_TOKEN": "forced-bot-token",
                },
            )

        env = captured["env"]
        assert env["MY_CUSTOM_VAR"] == "keep-me"
        assert env["TELEGRAM_BOT_TOKEN"] == "forced-bot-token"
        assert "FIRECRAWL_API_KEY" not in env
        assert f"{_HERMES_PROVIDER_ENV_FORCE_PREFIX}TELEGRAM_BOT_TOKEN" not in env
        assert env["PYTHONUNBUFFERED"] == "1"


# =========================================================================
# Checkpoint
# =========================================================================

class TestCheckpoint:
    def test_write_checkpoint(self, registry, tmp_path):
        with patch("tools.process_registry.CHECKPOINT_PATH", tmp_path / "procs.json"):
            s = _make_session()
            registry._running[s.id] = s
            registry._write_checkpoint()

            data = json.loads((tmp_path / "procs.json").read_text())
            assert len(data) == 1
            assert data[0]["session_id"] == s.id

    def test_recover_no_file(self, registry, tmp_path):
        with patch("tools.process_registry.CHECKPOINT_PATH", tmp_path / "missing.json"):
            assert registry.recover_from_checkpoint() == 0

    def test_recover_dead_pid(self, registry, tmp_path):
        checkpoint = tmp_path / "procs.json"
        checkpoint.write_text(json.dumps([{
            "session_id": "proc_dead",
            "command": "sleep 999",
            "pid": 999999999,  # almost certainly not running
            "task_id": "t1",
        }]))
        with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint):
            recovered = registry.recover_from_checkpoint()
            assert recovered == 0

    def test_write_checkpoint_includes_watcher_metadata(self, registry, tmp_path):
        with patch("tools.process_registry.CHECKPOINT_PATH", tmp_path / "procs.json"):
            s = _make_session()
            s.watcher_platform = "telegram"
            s.watcher_chat_id = "999"
            s.watcher_thread_id = "42"
            s.watcher_interval = 60
            registry._running[s.id] = s
            registry._write_checkpoint()

            data = json.loads((tmp_path / "procs.json").read_text())
            assert len(data) == 1
            assert data[0]["watcher_platform"] == "telegram"
            assert data[0]["watcher_chat_id"] == "999"
            assert data[0]["watcher_thread_id"] == "42"
            assert data[0]["watcher_interval"] == 60

    def test_recover_enqueues_watchers(self, registry, tmp_path):
        checkpoint = tmp_path / "procs.json"
        checkpoint.write_text(json.dumps([{
            "session_id": "proc_live",
            "command": "sleep 999",
            "pid": os.getpid(),  # current process — guaranteed alive
            "task_id": "t1",
            "session_key": "sk1",
            "watcher_platform": "telegram",
            "watcher_chat_id": "123",
            "watcher_thread_id": "42",
            "watcher_interval": 60,
        }]))
        with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint):
            recovered = registry.recover_from_checkpoint()
            assert recovered == 1
            assert len(registry.pending_watchers) == 1
            w = registry.pending_watchers[0]
            assert w["session_id"] == "proc_live"
            assert w["platform"] == "telegram"
            assert w["chat_id"] == "123"
            assert w["thread_id"] == "42"
            assert w["check_interval"] == 60

    def test_recover_skips_watcher_when_no_interval(self, registry, tmp_path):
        checkpoint = tmp_path / "procs.json"
        checkpoint.write_text(json.dumps([{
            "session_id": "proc_live",
            "command": "sleep 999",
            "pid": os.getpid(),
            "task_id": "t1",
            "watcher_interval": 0,
        }]))
        with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint):
            recovered = registry.recover_from_checkpoint()
            assert recovered == 1
            assert len(registry.pending_watchers) == 0


# =========================================================================
# Kill process
# =========================================================================

class TestKillProcess:
    def test_kill_not_found(self, registry):
        result = registry.kill_process("nonexistent")
        assert result["status"] == "not_found"

    def test_kill_already_exited(self, registry):
        s = _make_session(exited=True, exit_code=0)
        registry._finished[s.id] = s
        result = registry.kill_process(s.id)
        assert result["status"] == "already_exited"


# =========================================================================
# Tool handler
# =========================================================================

class TestProcessToolHandler:
    def test_list_action(self):
        from tools.process_registry import _handle_process
        result = json.loads(_handle_process({"action": "list"}))
        assert "processes" in result

    def test_poll_missing_session_id(self):
        from tools.process_registry import _handle_process
        result = json.loads(_handle_process({"action": "poll"}))
        assert "error" in result

    def test_unknown_action(self):
        from tools.process_registry import _handle_process
        result = json.loads(_handle_process({"action": "unknown_action"}))
        assert "error" in result


class TestTenantIsolation:
    def test_list_filters_by_tenant(self, registry, monkeypatch):
        monkeypatch.setenv("HERMES_USER_ID", "alice")
        s1 = _make_session(sid="proc_a", task_id="t1", user_id="alice")
        s2 = _make_session(sid="proc_b", task_id="t1", user_id="bob")
        registry._running[s1.id] = s1
        registry._running[s2.id] = s2
        result = registry.list_sessions()
        assert len(result) == 1
        assert result[0]["session_id"] == "proc_a"

    def test_poll_denies_foreign_tenant(self, registry, monkeypatch):
        monkeypatch.setenv("HERMES_USER_ID", "alice")
        s = _make_session(sid="proc_b", user_id="bob")
        registry._running[s.id] = s
        result = registry.poll(s.id)
        assert result["status"] == "not_found"

    def test_checkpoint_writes_per_tenant_path(self, registry, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        monkeypatch.setenv("HERMES_USER_ID", "alice")
        s = _make_session(sid="proc_tenant", user_id="alice")
        registry._running[s.id] = s
        registry._write_checkpoint()
        from hermes_constants import get_user_subpath
        checkpoint = get_user_subpath("alice", "processes.json")
        data = json.loads(checkpoint.read_text())
        assert data and data[0]["session_id"] == "proc_tenant"

    def test_recover_sets_recovered_session_tenant(self, registry, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        monkeypatch.setenv("HERMES_USER_ID", "alice")
        from hermes_constants import get_user_subpath

        checkpoint = get_user_subpath("alice", "processes.json")
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text(json.dumps([{
            "session_id": "proc_alive",
            "command": "sleep 1",
            "pid": os.getpid(),
            "task_id": "t1",
            "user_id": "alice",
        }]))

        recovered = registry.recover_from_checkpoint()
        assert recovered == 1
        session = registry.get("proc_alive")
        assert session is not None
        assert session.user_id == "alice"

    def test_recover_falls_back_to_legacy_checkpoint_file(self, registry, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        monkeypatch.setenv("HERMES_USER_ID", "alice")
        from hermes_cli.config import get_hermes_home

        legacy_checkpoint = get_hermes_home() / "processes.json"
        legacy_checkpoint.parent.mkdir(parents=True, exist_ok=True)
        legacy_checkpoint.write_text(json.dumps([{
            "session_id": "proc_legacy",
            "command": "sleep 1",
            "pid": os.getpid(),
            "task_id": "t1",
            "user_id": "alice",
        }]))

        recovered = registry.recover_from_checkpoint()
        assert recovered == 1
        assert registry.get("proc_legacy") is not None

    def test_same_task_id_isolated_per_tenant(self, registry, monkeypatch):
        shared_task = "shared"
        s_alice = _make_session(sid="proc_a", task_id=shared_task, user_id="alice", output="alice out")
        s_bob = _make_session(sid="proc_b", task_id=shared_task, user_id="bob", output="bob out")
        registry._running[s_alice.id] = s_alice
        registry._running[s_bob.id] = s_bob

        monkeypatch.setenv("HERMES_USER_ID", "alice")
        alice_list = registry.list_sessions(task_id=shared_task)
        assert [p["session_id"] for p in alice_list] == ["proc_a"]
        assert registry.poll("proc_b")["status"] == "not_found"
        assert registry.read_log("proc_b")["status"] == "not_found"

        monkeypatch.setenv("HERMES_USER_ID", "bob")
        bob_list = registry.list_sessions(task_id=shared_task)
        assert [p["session_id"] for p in bob_list] == ["proc_b"]
        assert "bob out" in registry.poll("proc_b")["output_preview"]

        monkeypatch.delenv("HERMES_USER_ID", raising=False)
        assert registry.list_sessions(task_id=shared_task) == []

    def test_kill_foreign_tenant_returns_not_found(self, registry, monkeypatch):
        s_bob = _make_session(sid="proc_b", task_id="t1", user_id="bob")
        registry._running[s_bob.id] = s_bob
        monkeypatch.setenv("HERMES_USER_ID", "alice")
        assert registry.kill_process("proc_b")["status"] == "not_found"

    def test_recover_shared_checkpoint_filters_foreign_entries(self, registry, tmp_path, monkeypatch):
        shared_checkpoint = tmp_path / "procs.json"
        shared_checkpoint.write_text(json.dumps([
            {
                "session_id": "proc_alice",
                "command": "sleep 1",
                "pid": os.getpid(),
                "task_id": "t1",
                "user_id": "alice",
            },
            {
                "session_id": "proc_bob",
                "command": "sleep 1",
                "pid": os.getpid(),
                "task_id": "t1",
                "user_id": "bob",
            },
            {
                "session_id": "proc_missing",
                "command": "sleep 1",
                "pid": os.getpid(),
                "task_id": "t1",
            },
        ]))

        monkeypatch.setenv("HERMES_USER_ID", "alice")
        with patch("tools.process_registry.CHECKPOINT_PATH", shared_checkpoint):
            recovered = registry.recover_from_checkpoint()

        assert recovered == 2  # alice + missing user_id normalized to alice
        assert registry.get("proc_bob") is None

        remaining = json.loads(shared_checkpoint.read_text())
        assert any(entry["session_id"] == "proc_bob" for entry in remaining)

    def test_default_and_named_tenants_isolate_same_task(self, registry, monkeypatch):
        shared = "shared-task"
        s_default = _make_session(sid="proc_default", task_id=shared, user_id="default", output="def")
        s_alice = _make_session(sid="proc_alice", task_id=shared, user_id="alice", output="alice")
        s_bob = _make_session(sid="proc_bob", task_id=shared, user_id="bob", output="bob")

        registry._running[s_default.id] = s_default
        registry._running[s_alice.id] = s_alice
        registry._finished[s_bob.id] = s_bob

        monkeypatch.setenv("HERMES_USER_ID", "alice")
        alice_list = registry.list_sessions(task_id=shared)
        assert [p["session_id"] for p in alice_list] == ["proc_alice"]
        assert registry.poll("proc_bob")["status"] == "not_found"
        assert registry.read_log("proc_default")["status"] == "not_found"
        assert registry.kill_process("proc_bob")["status"] == "not_found"

        monkeypatch.setenv("HERMES_USER_ID", "bob")
        bob_list = registry.list_sessions(task_id=shared)
        assert [p["session_id"] for p in bob_list] == ["proc_bob"]
        assert registry.poll("proc_alice")["status"] == "not_found"
        assert registry.read_log("proc_default")["status"] == "not_found"

        monkeypatch.delenv("HERMES_USER_ID", raising=False)
        default_list = registry.list_sessions(task_id=shared)
        assert [p["session_id"] for p in default_list] == ["proc_default"]
        assert registry.poll("proc_alice")["status"] == "not_found"
        assert registry.kill_process("proc_bob")["status"] == "not_found"

