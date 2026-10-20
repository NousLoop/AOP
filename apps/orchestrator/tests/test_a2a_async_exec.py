"""Platform async A2A execute (submit + join) unit tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from a2a_sdk.models import Task, TaskStatus
from executor import A2AExecutor, ExecutionResult
from executor.engine import _a2a_exec_async_enabled


def test_a2a_exec_async_env(monkeypatch):
    monkeypatch.setenv("A2A_EXEC_ASYNC", "1")
    assert _a2a_exec_async_enabled() is True
    monkeypatch.setenv("A2A_EXEC_ASYNC", "0")
    assert _a2a_exec_async_enabled() is False


def test_execute_async_mode_passes_flag():
    exe = A2AExecutor(timeout=30.0)
    fake_task = Task(id="t1", status=TaskStatus.SUBMITTED, artifacts=[])
    fake_card = SimpleNamespace(name="Mock")

    with patch("executor.A2AClient") as Client:
        client = MagicMock()
        client.card = fake_card
        client.send_text.return_value = fake_task
        Client.return_value = client

        with patch("executor.guard_call", return_value=("hi", 30.0)):
            with patch("executor.clamp_output", side_effect=lambda x, p: x):
                with patch("executor.first_text_artifact", return_value=None):
                    with patch("executor.first_data_artifact", return_value=None):
                        result = exe.execute(
                            "http://agent",
                            "hi",
                            skill_id="claude-code",
                            async_mode=True,
                        )

        assert result.task.status == TaskStatus.SUBMITTED
        kwargs = client.send_text.call_args.kwargs
        assert kwargs.get("async_mode") is True


def test_join_task_polls_until_completed():
    exe = A2AExecutor(timeout=30.0)
    done = Task(
        id="t1",
        status=TaskStatus.COMPLETED,
        artifacts=[],
    )
    fake_card = SimpleNamespace(name="Mock")

    with patch("executor.A2AClient") as Client:
        client = MagicMock()
        client.card = fake_card
        client.poll_task.return_value = done
        Client.return_value = client

        with patch("executor.clamp_output", side_effect=lambda x, p: x):
            with patch("executor.first_text_artifact", return_value="ok"):
                with patch("executor.first_data_artifact", return_value=None):
                    result = exe.join_task(
                        "http://agent",
                        "t1",
                        timeout_s=5,
                        poll_interval_s=0.01,
                    )

    assert isinstance(result, ExecutionResult)
    assert result.ok
    assert result.task.status == TaskStatus.COMPLETED
    client.poll_task.assert_called_once()
    kwargs = client.poll_task.call_args.kwargs
    assert kwargs.get("timeout_s") == 5
    assert kwargs.get("poll_interval_s") == 0.01
