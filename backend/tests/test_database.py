from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from gate.config import load_settings
from gate.database import Database


@pytest.mark.asyncio
async def test_initialize_migrates_legacy_region_columns(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE regions (
            id VARCHAR(16) PRIMARY KEY,
            name VARCHAR(80) NOT NULL,
            countries JSON NOT NULL,
            socks_port INTEGER NOT NULL UNIQUE,
            enabled BOOLEAN NOT NULL,
            mode VARCHAR(16) NOT NULL,
            status VARCHAR(24) NOT NULL,
            active_node_id INTEGER,
            updated_at DATETIME NOT NULL
        );
        INSERT INTO regions VALUES (
            'jp', 'Japan', '["JP"]', 11081, 1, 'auto', 'unavailable', NULL,
            '2026-09-01 00:00:00'
        );
        """
    )
    connection.close()

    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    await database.initialize(load_settings().regions)
    japan = await database.get_region("jp")
    await database.close()

    assert japan is not None
    assert japan.group_id == "jp"
    assert japan.network_index == 1
    assert japan.active_egress_ip is None


@pytest.mark.asyncio
async def test_automation_state_persists_and_records_only_changes(tmp_path: Path) -> None:
    path = tmp_path / "automation.db"
    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    await database.initialize(load_settings().regions)

    assert await database.get_automation_enabled() is None
    await database.set_automation_enabled(False)
    first_event_id = await database.latest_event_id()
    await database.set_automation_enabled(False)

    assert await database.get_automation_enabled() is False
    assert await database.latest_event_id() == first_event_id
    events = await database.list_events()
    assert events[0].code == "AUTOMATION_ENABLED_CHANGED"
    assert events[0].message == "自动检查已关闭"
    await database.close()

    reopened = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    await reopened.initialize(load_settings().regions)
    assert await reopened.get_automation_enabled() is False
    await reopened.close()
