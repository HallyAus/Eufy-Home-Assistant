"""The session gate serves waiting producers in arrival order (#25)."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("eufy_run_fifo", ROOT / "bridge/eufy_run.py")
supervisor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = supervisor
SPEC.loader.exec_module(supervisor)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="flock gate is POSIX only")


def _gate(tmp_path, label):
    return supervisor.SessionGate(tmp_path / "session.lock", tmp_path / "preempt", label)


def test_waiters_get_the_session_in_arrival_order(tmp_path):
    async def scenario():
        holder = _gate(tmp_path, "holder")
        assert await holder.acquire(asyncio.Event())
        order = []

        async def waiter(name):
            gate = _gate(tmp_path, name)
            assert await gate.acquire(asyncio.Event())
            order.append(name)
            await asyncio.sleep(0.05)
            gate.release()

        tasks = []
        for name in ("first", "second", "third"):
            tasks.append(asyncio.create_task(waiter(name)))
            await asyncio.sleep(0.03)  # distinct arrival times
        await asyncio.sleep(0.3)
        assert order == []
        holder.release()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        return order

    assert asyncio.run(scenario()) == ["first", "second", "third"]


def test_cancelled_or_abandoned_wait_leaves_the_queue(tmp_path):
    async def scenario():
        holder = _gate(tmp_path, "holder")
        assert await holder.acquire(asyncio.Event())
        queue_dir = tmp_path / "session.lock.queue"

        cancelled = asyncio.create_task(_gate(tmp_path, "cancelled").acquire(asyncio.Event()))
        viewer_gone = asyncio.Event()
        abandoned = asyncio.create_task(_gate(tmp_path, "abandoned").acquire(viewer_gone))
        await asyncio.sleep(0.2)
        assert len(list(queue_dir.glob("*.ticket"))) == 2

        cancelled.cancel()
        viewer_gone.set()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert await abandoned is False
        assert list(queue_dir.glob("*.ticket")) == []

        # A later arrival is not blocked by the departed waiters.
        holder.release()
        late = _gate(tmp_path, "late")
        assert await asyncio.wait_for(late.acquire(asyncio.Event()), timeout=2)
        late.release()

    asyncio.run(scenario())


def test_waiting_producer_logs_its_queue_position(tmp_path, capsys):
    async def scenario():
        holder = _gate(tmp_path, "holder")
        assert await holder.acquire(asyncio.Event())
        stop = asyncio.Event()
        task = asyncio.create_task(_gate(tmp_path, "channel 3").acquire(stop))
        await asyncio.sleep(0.2)
        stop.set()
        await task
        holder.release()

    asyncio.run(scenario())
    assert "channel 3 waiting for the NVR session, queue position 1/1" in capsys.readouterr().err
