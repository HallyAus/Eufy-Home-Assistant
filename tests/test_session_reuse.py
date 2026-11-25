"""Offline tests for NVR session reuse (parked engine + supervisor handoff)."""
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name == "nt", reason="unix sockets and flock")


def load_run(monkeypatch, tmp_path, reuse="30"):
    monkeypatch.setenv("EUFY_SESSION_REUSE_SECONDS", reuse)
    monkeypatch.setenv("EUFY_SESSION_SOCKET", str(tmp_path / "s.sock"))
    monkeypatch.setenv("EUFY_SESSION_LOCK", str(tmp_path / "s.lock"))
    monkeypatch.setenv("EUFY_SESSION_PREEMPT", str(tmp_path / "s.preempt"))
    spec = importlib.util.spec_from_file_location("eufy_run_reuse", ROOT / "bridge/eufy_run.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module          # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


async def fake_engine(path, reply, seen):
    async def handle(reader, writer):
        seen.append(json.loads(await reader.readline()))
        writer.write((json.dumps(reply) + "\n").encode())
        await writer.drain()
        await reader.read()
        seen.append("released")
        writer.close()
    return await asyncio.start_unix_server(handle, path=str(path))


def test_handoff_reuses_a_parked_session_and_releases_it(monkeypatch, tmp_path):
    run = load_run(monkeypatch, tmp_path)

    async def scenario():
        seen = []
        server = await fake_engine(tmp_path / "s.sock", {"ok": True}, seen)
        args = ["6", "--rtsp", "rtsp://127.0.0.1:8556/x"]
        handoff = await run.try_handoff(args)
        assert handoff is not None
        stop = asyncio.Event()
        task = asyncio.create_task(run.run_handoff(handoff, stop))
        await asyncio.sleep(0.05)
        stop.set()
        assert await task == "shutdown"
        await asyncio.sleep(0.05)
        server.close()
        return seen

    seen = asyncio.run(scenario())
    assert seen == [{"argv": ["6", "--rtsp", "rtsp://127.0.0.1:8556/x"]}, "released"]


def test_handoff_reports_a_lost_session(monkeypatch, tmp_path):
    run = load_run(monkeypatch, tmp_path)

    async def scenario():
        async def handle(reader, writer):
            await reader.readline()
            writer.write(b'{"ok": true}\n')
            await writer.drain()
            writer.close()          # engine died after taking the camera
        server = await asyncio.start_unix_server(handle, path=str(tmp_path / "s.sock"))
        handoff = await run.try_handoff(["1", "--rtsp", "u"])
        result = await run.run_handoff(handoff, asyncio.Event())
        server.close()
        return result

    assert asyncio.run(scenario()) == "lost"


@pytest.mark.parametrize("reply", [{"ok": False, "reason": "busy"}, {"ok": False, "reason": "no video"}])
def test_declined_handoff_falls_back(monkeypatch, tmp_path, reply):
    run = load_run(monkeypatch, tmp_path)

    async def scenario():
        server = await fake_engine(tmp_path / "s.sock", reply, [])
        result = await run.try_handoff(["1", "--rtsp", "u"])
        server.close()
        return result

    assert asyncio.run(scenario()) is None


def test_no_parked_engine_or_reuse_off_means_no_handoff(monkeypatch, tmp_path):
    run = load_run(monkeypatch, tmp_path)
    assert asyncio.run(run.try_handoff(["1", "--rtsp", "u"])) is None
    off = load_run(monkeypatch, tmp_path, reuse="0")
    assert asyncio.run(off.try_handoff(["1", "--rtsp", "u"])) is None


def test_waiting_viewer_takes_the_parked_session(monkeypatch, tmp_path):
    """The lock is held by a parked engine, so a waiter must get the handoff."""
    import fcntl
    run = load_run(monkeypatch, tmp_path)
    holder = open(tmp_path / "s.lock", "a+b")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)

    async def scenario():
        gate = run.SessionGate(path=tmp_path / "s.lock", preempt_path=tmp_path / "s.preempt")
        calls = []

        async def alternative():
            calls.append(1)
            return "handoff" if len(calls) >= 2 else None
        result = await asyncio.wait_for(gate.acquire(asyncio.Event(), alternative=alternative), 5)
        return result, gate, list(gate.queue_dir.glob("*.ticket"))

    result, gate, tickets = asyncio.run(scenario())
    holder.close()
    assert result == "handoff"
    assert tickets == []            # the waiter left the queue cleanly


def test_detach_keeps_the_lock_for_the_engine(monkeypatch, tmp_path):
    import fcntl
    run = load_run(monkeypatch, tmp_path)

    async def take():
        gate = run.SessionGate(path=tmp_path / "s.lock", preempt_path=tmp_path / "s.preempt")
        assert await gate.acquire(asyncio.Event()) is True
        return gate
    gate = asyncio.run(take())
    engine_fd = os.dup(gate.fileno())     # what pass_fds gives the engine
    gate.detach()
    other = open(tmp_path / "s.lock", "a+b")
    with pytest.raises(BlockingIOError):
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(engine_fd)                   # the engine exits
    fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    other.close()


def test_engine_wiring():
    stream = (ROOT / "bridge/eufy_stream.py").read_text()
    supervisor = (ROOT / "bridge/eufy_run.py").read_text()
    assert "signal.SIGUSR1" in stream and "start_unix_server(handle_take" in stream
    assert 'build_cmd(USER_ID, 1004, {})' in stream
    assert "os.kill(proc.pid, signal.SIGUSR1)" in supervisor
    assert "pass_fds=(lock_fd,) if reusable else ()" in supervisor
    assert "if not parked:" in supervisor


def test_addon_exposes_session_reuse_off_by_default():
    config = yaml.safe_load((ROOT / "eufy_nvr/config.yaml").read_text())
    run_script = (ROOT / "eufy_nvr/run.sh").read_text()
    assert config["options"]["session_reuse_seconds"] == 0
    assert config["schema"]["session_reuse_seconds"] == "int(0,300)?"
    assert "export EUFY_SESSION_REUSE_SECONDS=\"$(bashio::config 'session_reuse_seconds' '0')\"" in run_script


def test_selection_parsers_match_previous_behaviour():
    sys.argv = ["eufy_stream.py"]
    source = (ROOT / "bridge/eufy_stream.py").read_text()
    start = source.index("def parse_selection(argv):")
    end = source.index("CHANNELS, GROUP = parse_selection(sys.argv)")
    namespace = {}
    exec(source[start:end], namespace)
    sel, sensor, rtsp = namespace["parse_selection"], namespace["parse_sensor"], namespace["parse_rtsp"]
    assert sel(["x", "6", "--rtsp", "u"]) == ([6], None)
    assert sel(["x", "all"]) == ([0, 1, 2, 3], None)
    assert sel(["x", "g:0-1-0,1-0-0"]) == ([1, 0], [(0, 1, 0), (1, 0, 0)])
    assert sensor(["x", "0", "--sensor", "0", "--rtsp", "u"]) == 0
    assert sensor(["x", "0", "--rtsp", "u"]) is None
    assert rtsp(["x", "0", "--sensor", "0", "--rtsp", "rtsp://h/p"]) == "rtsp://h/p"
    with pytest.raises(SystemExit):
        sensor(["x", "0", "--sensor", "9"])
