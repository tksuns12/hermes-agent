"""Tests for tools/process_registry.py — ProcessRegistry query methods, pruning, checkpoint."""

import json
import os
import signal
import subprocess
import sys
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



def _spawn_python_sleep(seconds: float) -> subprocess.Popen:
    """Spawn a portable short-lived Python sleep process."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
    )


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Poll a predicate until it returns truthy or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


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
# Orphaned-pipe reconciliation (issue #17327)
# =========================================================================

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: uses setsid/fcntl")
class TestOrphanedPipeReconciliation:
    """Regression tests for issue #17327.

    `hermes update` in Feishu spawned a background subprocess that restarted
    the gateway; the direct child exited quickly but a descendant daemon
    held the stdout pipe open. `_reader_loop.finally` never ran, so
    `session.exited` stayed False and the agent polled 74 times over 7
    minutes, all returning `status: running`.

    The fix is `_reconcile_local_exit()`: poll() and wait() now check the
    direct `Popen.poll()` before trusting `session.exited`.
    """

    def test_reconcile_flips_exited_when_direct_child_done(self, registry):
        """Direct child exited but reader thread is blocked on orphaned pipe."""
        # Simulate the orphaned-pipe scenario: direct child exited, but a
        # descendant holds stdout open so the reader never sees EOF.
        # Approach: spawn `sh -c 'sleep 10 &'` with setsid — sh forks the
        # sleep into a new session group, exits immediately, but sleep
        # inherits the stdout pipe and keeps it open.
        proc = subprocess.Popen(
            ["sh", "-c", "exec 1>&2; ( sleep 30 ) & disown; exit 0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
        )

        s = _make_session(sid="proc_orphan_test")
        s.process = proc
        s.pid = proc.pid
        registry._running[s.id] = s

        # Wait for the direct child to exit. We don't start a reader thread,
        # so session.exited stays False (mimicking the stuck-reader state).
        assert _wait_until(lambda: proc.poll() is not None, timeout=5.0), (
            "Direct child should exit quickly (sh exits, sleep descendant "
            "holds the pipe open)"
        )

        # Before the fix: poll would return "running" forever.
        # After the fix: poll reconciles against proc.poll() and flips.
        assert s.exited is False  # Precondition: reader hasn't updated it.
        result = registry.poll(s.id)
        assert result["status"] == "exited", (
            f"Expected reconciled 'exited' status; got {result!r}. "
            "This is issue #17327 — reader is blocked on orphaned pipe."
        )
        assert result["exit_code"] == 0
        assert s.exited is True
        assert s.id in registry._finished
        assert s.id not in registry._running

        # Clean up the orphaned descendant.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def test_reconcile_noop_when_child_still_running(self, registry):
        """Reconcile must NOT flip exited when the direct child is alive."""
        proc = _spawn_python_sleep(5.0)
        s = _make_session(sid="proc_running_test")
        s.process = proc
        s.pid = proc.pid
        registry._running[s.id] = s

        result = registry.poll(s.id)
        assert result["status"] == "running"
        assert s.exited is False

        proc.kill()
        proc.wait()

    def test_reconcile_noop_on_already_exited(self, registry):
        """Reconcile is a no-op when session.exited is already True."""
        s = _make_session(sid="proc_already_exited", exited=True, exit_code=7)
        s.process = MagicMock()
        s.process.poll = MagicMock(return_value=0)  # Would say exit 0
        registry._finished[s.id] = s

        registry._reconcile_local_exit(s)
        # Must not overwrite the existing exit_code with proc.poll()'s 0.
        assert s.exit_code == 7

    def test_reconcile_noop_on_no_process(self, registry):
        """Reconcile is a no-op for sessions without a local Popen (env/PTY)."""
        s = _make_session(sid="proc_no_popen")
        assert getattr(s, "process", None) is None
        # Must not raise.
        registry._reconcile_local_exit(s)
        assert s.exited is False

    def test_wait_returns_when_reader_blocked(self, registry):
        """wait() must also reconcile — not just poll()."""
        proc = subprocess.Popen(
            ["sh", "-c", "( sleep 30 ) & disown; exit 0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
        )

        s = _make_session(sid="proc_wait_orphan")
        s.process = proc
        s.pid = proc.pid
        registry._running[s.id] = s

        assert _wait_until(lambda: proc.poll() is not None, timeout=5.0)

        start = time.monotonic()
        result = registry.wait(s.id, timeout=10)
        elapsed = time.monotonic() - start

        assert result["status"] == "exited", result
        assert elapsed < 5.0, (
            f"wait() should return ~immediately via reconcile; took {elapsed:.1f}s"
        )

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


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
# Stdin helpers
# =========================================================================

class TestStdinHelpers:
    def test_close_stdin_not_found(self, registry):
        result = registry.close_stdin("nonexistent")
        assert result["status"] == "not_found"

    def test_close_stdin_pipe_mode(self, registry):
        proc = MagicMock()
        proc.stdin = MagicMock()
        s = _make_session()
        s.process = proc
        registry._running[s.id] = s

        result = registry.close_stdin(s.id)

        proc.stdin.close.assert_called_once()
        assert result["status"] == "ok"

    def test_close_stdin_pty_mode(self, registry):
        pty = MagicMock()
        s = _make_session()
        s._pty = pty
        registry._running[s.id] = s

        result = registry.close_stdin(s.id)

        pty.sendeof.assert_called_once()
        assert result["status"] == "ok"

    def test_close_stdin_allows_eof_driven_process_to_finish(self, registry, tmp_path):
        session = registry.spawn_local(
            'python3 -c "import sys; print(sys.stdin.read().strip())"',
            cwd=str(tmp_path),
            use_pty=False,
        )

        try:
            time.sleep(0.5)
            assert registry.submit_stdin(session.id, "hello")["status"] == "ok"
            assert registry.close_stdin(session.id)["status"] == "ok"

            deadline = time.time() + 5
            while time.time() < deadline:
                poll = registry.poll(session.id)
                if poll["status"] == "exited":
                    assert poll["exit_code"] == 0
                    assert "hello" in poll["output_preview"]
                    return
                time.sleep(0.2)

            pytest.fail("process did not exit after stdin was closed")
        finally:
            registry.kill_process(session.id)


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

    def test_spawn_via_env_uses_backend_temp_dir_for_artifacts(self, registry):
        class FakeEnv:
            def __init__(self):
                self.commands = []

            def get_temp_dir(self):
                return "/data/data/com.termux/files/usr/tmp"

            def execute(self, command, timeout=None):
                self.commands.append((command, timeout))
                return {"output": "4321\n"}

        env = FakeEnv()
        fake_thread = MagicMock()

        with patch("tools.process_registry.threading.Thread", return_value=fake_thread), \
            patch.object(registry, "_write_checkpoint"):
            session = registry.spawn_via_env(env, "echo hello")

        bg_command = env.commands[0][0]
        assert session.pid == 4321
        assert "/data/data/com.termux/files/usr/tmp/hermes_bg_" in bg_command
        assert ".exit" in bg_command
        assert "rc=$?;" in bg_command
        assert " > /tmp/hermes_bg_" not in bg_command
        assert "cat /tmp/hermes_bg_" not in bg_command
        fake_thread.start.assert_called_once()

    def test_env_poller_quotes_temp_paths_with_spaces(self, registry):
        session = _make_session(sid="proc_space")
        session.exited = False

        class FakeEnv:
            def __init__(self):
                self.commands = []
                self._responses = iter([
                    {"output": "hello\n"},
                    {"output": "1\n"},
                    {"output": "0\n"},
                ])

            def execute(self, command, timeout=None):
                self.commands.append((command, timeout))
                return next(self._responses)

        env = FakeEnv()

        with patch("tools.process_registry.time.sleep", return_value=None), \
            patch.object(registry, "_move_to_finished"):
            registry._env_poller_loop(
                session,
                env,
                "/path with spaces/hermes_bg.log",
                "/path with spaces/hermes_bg.pid",
                "/path with spaces/hermes_bg.exit",
            )

        assert env.commands[0][0] == "cat '/path with spaces/hermes_bg.log' 2>/dev/null"
        assert env.commands[1][0] == "kill -0 \"$(cat '/path with spaces/hermes_bg.pid' 2>/dev/null)\" 2>/dev/null; echo $?"
        assert env.commands[2][0] == "cat '/path with spaces/hermes_bg.exit' 2>/dev/null"


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
            s.watcher_user_id = "u123"
            s.watcher_user_name = "alice"
            s.watcher_thread_id = "42"
            s.watcher_interval = 60
            registry._running[s.id] = s
            registry._write_checkpoint()

            data = json.loads((tmp_path / "procs.json").read_text())
            assert len(data) == 1
            assert data[0]["watcher_platform"] == "telegram"
            assert data[0]["watcher_chat_id"] == "999"
            assert data[0]["watcher_user_id"] == "u123"
            assert data[0]["watcher_user_name"] == "alice"
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
            "watcher_user_id": "u123",
            "watcher_user_name": "alice",
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
            assert w["user_id"] == "u123"
            assert w["user_name"] == "alice"
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

    def test_recovery_keeps_live_checkpoint_entries(self, registry, tmp_path):
        checkpoint = tmp_path / "procs.json"
        checkpoint.write_text(json.dumps([{
            "session_id": "proc_live",
            "command": "sleep 999",
            "pid": os.getpid(),
            "task_id": "t1",
            "session_key": "sk1",
        }]))

        with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint):
            recovered = registry.recover_from_checkpoint()
            assert recovered == 1
            assert registry.get("proc_live") is not None

            data = json.loads(checkpoint.read_text())
            assert len(data) == 1
            assert data[0]["session_id"] == "proc_live"
            assert data[0]["pid"] == os.getpid()
            assert data != []

    def test_recovery_skips_explicit_sandbox_backed_entries(self, registry, tmp_path):
        checkpoint = tmp_path / "procs.json"
        original = [{
            "session_id": "proc_remote",
            "command": "sleep 999",
            "pid": os.getpid(),
            "task_id": "t1",
            "pid_scope": "sandbox",
        }]
        checkpoint.write_text(json.dumps(original))

        with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint):
            recovered = registry.recover_from_checkpoint()
            assert recovered == 0
            assert registry.get("proc_remote") is None

            data = json.loads(checkpoint.read_text())
            assert data == []

    def test_detached_recovered_process_eventually_exits(self, registry, tmp_path):
        proc = _spawn_python_sleep(0.4)
        checkpoint = tmp_path / "procs.json"
        checkpoint.write_text(json.dumps([{
            "session_id": "proc_live",
            "command": "python -c 'import time; time.sleep(0.4)'",
            "pid": proc.pid,
            "task_id": "t1",
            "session_key": "sk1",
        }]))

        try:
            with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint):
                recovered = registry.recover_from_checkpoint()
                assert recovered == 1

                session = registry.get("proc_live")
                assert session is not None
                assert session.detached is True

                proc.wait(timeout=5)

                assert _wait_until(
                    lambda: registry.get("proc_live") is not None
                    and registry.get("proc_live").exited,
                    timeout=5,
                )

                poll_result = registry.poll("proc_live")
                assert poll_result["status"] == "exited"

                wait_result = registry.wait("proc_live", timeout=1)
                assert wait_result["status"] == "exited"
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=5)


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

    def test_kill_detached_session_uses_host_pid(self, registry):
        s = _make_session(sid="proc_detached", command="sleep 999")
        s.pid = 424242
        s.detached = True
        registry._running[s.id] = s

        calls = []

        def fake_kill(pid, sig):
            calls.append((pid, sig))

        try:
            with patch("tools.process_registry.os.kill", side_effect=fake_kill):
                result = registry.kill_process(s.id)

            assert result["status"] == "killed"
            assert (424242, 0) in calls
            assert (424242, signal.SIGTERM) in calls
        finally:
            registry._running.pop(s.id, None)


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

