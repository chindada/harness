"""Tests for PhaseBroker — per-job in-memory pub/sub for phase transitions."""

from __future__ import annotations

import anyio
import pytest

from harness_mcp.phase_broker import PhaseBroker


@pytest.mark.asyncio
async def test_subscribe_then_publish_delivers_event():
    broker = PhaseBroker()
    sub = broker.subscribe("J1")
    await broker.publish("J1", {"current_phase": "planning", "status": "running"})
    async with sub:
        event = await sub.receive()
    assert event == {"current_phase": "planning", "status": "running"}


@pytest.mark.asyncio
async def test_two_subscribers_both_receive():
    broker = PhaseBroker()
    sub_a = broker.subscribe("J1")
    sub_b = broker.subscribe("J1")
    await broker.publish("J1", {"current_phase": "planning", "status": "running"})
    async with sub_a, sub_b:
        evt_a = await sub_a.receive()
        evt_b = await sub_b.receive()
    assert evt_a == evt_b == {"current_phase": "planning", "status": "running"}


@pytest.mark.asyncio
async def test_close_ends_subscriber_loop():
    broker = PhaseBroker()
    sub = broker.subscribe("J1")
    received: list[dict] = []

    async def consume():
        async with sub:
            async for event in sub:
                received.append(event)

    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        await anyio.sleep(0)  # let consumer start
        await broker.publish("J1", {"current_phase": "planning", "status": "running"})
        await anyio.sleep(0)  # let consumer drain
        broker.close("J1")

    assert received == [{"current_phase": "planning", "status": "running"}]


@pytest.mark.asyncio
async def test_non_terminal_publish_drops_when_subscriber_full():
    broker = PhaseBroker()
    sub = broker.subscribe("J1")
    # Fill the buffer (32 slots) then publish one more — should silently drop.
    for i in range(32):
        await broker.publish("J1", {"current_phase": f"p{i}", "status": "running"})
    # 33rd publish must not raise and must not block.
    with anyio.fail_after(0.5):
        await broker.publish("J1", {"current_phase": "p32", "status": "running"})
    # Drain — we should see the first 32 (newest dropped == p32).
    drained: list[dict] = []
    async with sub:
        for _ in range(32):
            drained.append(await sub.receive())
    assert drained[-1]["current_phase"] == "p31"
    assert "p32" not in [e["current_phase"] for e in drained]


@pytest.mark.asyncio
async def test_terminal_publish_blocks_until_delivered():
    broker = PhaseBroker()
    sub = broker.subscribe("J1")
    # Fill the buffer with non-terminal events.
    for i in range(32):
        await broker.publish("J1", {"current_phase": f"p{i}", "status": "running"})

    delivered_at = anyio.Event()

    async def publish_terminal():
        await broker.publish("J1", {"current_phase": "done", "status": "completed"})
        delivered_at.set()

    async def drain_then_let_terminal_in():
        await anyio.sleep(0.05)  # let publish_terminal start and block
        async with sub:
            for _ in range(32):
                await sub.receive()
            terminal = await sub.receive()
        assert terminal == {"current_phase": "done", "status": "completed"}

    async with anyio.create_task_group() as tg:
        tg.start_soon(publish_terminal)
        tg.start_soon(drain_then_let_terminal_in)

    assert delivered_at.is_set()


@pytest.mark.asyncio
async def test_subscribe_after_terminal_returns_closed_stream(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path))
    from harness_mcp import state as state_mod

    state_mod.close_db()  # drop any cached writer from a prior test
    state_mod.init_db()
    try:
        # Insert a terminal job row.
        await state_mod.db_write(
            "INSERT INTO jobs (id, status, current_phase, design_path, options_json, "
            "started_at, updated_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("J-DONE", "completed", "done", "/tmp/x.md", "{}", 0, 0, 0),
        )
        broker = PhaseBroker()
        sub = broker.subscribe("J-DONE")
        # Stream should already be closed — receive raises EndOfStream immediately.
        with pytest.raises(anyio.EndOfStream):
            async with sub:
                await sub.receive()
    finally:
        state_mod.close_db()
