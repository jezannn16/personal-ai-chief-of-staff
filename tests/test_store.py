from datetime import UTC, datetime

import pytest

from chief_of_staff.store import Store


def test_user_isolation_and_idempotent_done(tmp_path):
    store=Store(tmp_path/"db.sqlite3")
    task=store.add_task(11,"Call",datetime(2030,1,1,tzinfo=UTC))
    assert store.tasks(22)==[]
    assert store.complete(22,task) is False
    assert store.complete(11,task) is True
    assert store.complete(11,task) is False
    assert store.tasks(11)==[]


def test_notes_search_and_restart(tmp_path):
    path=tmp_path/"db.sqlite3"
    Store(path).add_note(1,"Launch plan",["Work","work"])
    restarted=Store(path)
    tasks,notes=restarted.search(1,"work")
    assert tasks==[] and notes[0]["tags"]=="work"


def test_invalid_values(tmp_path):
    store=Store(tmp_path/"db.sqlite3")
    with pytest.raises(ValueError): store.add_task(1," ")
    with pytest.raises(ValueError): store.add_task(1,"x",datetime(2030,1,1))
    with pytest.raises(ValueError): store.set_timezone(1,"No/Such_Zone")
    with pytest.raises(ValueError): store.set_quiet_hours(1,25,8)


def test_user_settings_preserve_custom_values_on_restart(tmp_path):
    store=Store(tmp_path/"db.sqlite3")
    store.ensure_user_settings(1,"Europe/Moscow",22,7)
    store.set_timezone(1,"Asia/Tokyo")
    Store(tmp_path/"db.sqlite3").ensure_user_settings(1,"UTC",23,8)
    with store._connect() as db:
        row=db.execute("SELECT timezone,quiet_start,quiet_end FROM user_settings WHERE user_id=1").fetchone()
    assert tuple(row)==("Asia/Tokyo",22,7)


def test_reminder_claim_is_single_delivery(tmp_path):
    store=Store(tmp_path/"db.sqlite3")
    store.add_task(5,"Due",datetime(2020,1,1,tzinfo=UTC))
    reminder=store.due_reminders(datetime.now(UTC))[0]
    assert store.claim_reminder(reminder["id"],5)
    assert not store.claim_reminder(reminder["id"],5)
    store.finish_reminder(reminder["id"],True)
    assert store.due_reminders(datetime.now(UTC))==[]


def test_reminders_are_user_scoped_and_cancel_is_idempotent(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    task_id = store.add_task(5, "Synthetic reminder", datetime(2030, 1, 1, tzinfo=UTC))
    reminder_id = store.reminders(5)[0]["id"]
    assert store.reminders(6) == []
    assert store.cancel_reminder(6, reminder_id) is False
    assert store.cancel_reminder(5, reminder_id) is True
    assert store.cancel_reminder(5, reminder_id) is False
    assert store.reminders(5) == []
    with store._connect() as db:
        row = db.execute("SELECT state FROM reminders WHERE task_id=?", (task_id,)).fetchone()
    assert row["state"] == "cancelled"
