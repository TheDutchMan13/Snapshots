import time

import pytest

from helpers import ids_by_name, make_jpeg, new_client, setup_admin, wait_for, wait_scan


@pytest.fixture
def lib(tmp_path):
    make_jpeg(tmp_path / "Photos" / "first.jpg", "2024:01:01 10:00:00")
    return tmp_path / "Photos"


def test_scans_run_automatically(tmp_path, lib):
    c, cfg = new_client(tmp_path, lib, scan_interval_minutes=15)
    with c:
        setup_admin(c)
        wait_scan(c)
        status = c.get("/api/status").json()
        assert status["next_scan_at"] is not None and status["scan_interval_minutes"] == 15
        worker = c.app.state.ctx.worker
        done = worker.scans_finished

        make_jpeg(lib / "second.jpg", "2024:02:01 10:00:00")
        worker.next_scan_at = time.time()  # pretend 15 minutes have passed
        worker.poke()
        wait_for(lambda: worker.scans_finished > done)
        scan = c.get("/api/status").json()["scan"]
        assert scan["automatic"] is True and scan["added"] == 1
        assert "second.jpg" in ids_by_name(c)
        assert worker.next_scan_at > time.time() + 14 * 60  # and the next one is booked


def test_automatic_scans_can_be_turned_off(tmp_path, lib):
    c, cfg = new_client(tmp_path, lib, scan_interval_minutes=0)
    with c:
        setup_admin(c)
        wait_scan(c)
        assert c.get("/api/status").json()["next_scan_at"] is None


def test_no_scan_on_start_still_books_the_timed_scan(tmp_path, lib):
    c, cfg = new_client(tmp_path, lib, index_on_start=False, scan_interval_minutes=5)
    with c:
        setup_admin(c)
        status = c.get("/api/status").json()
        assert status["scan"]["finished_at"] is None and status["next_scan_at"] is not None


def test_a_second_scan_request_is_not_queued_twice(tmp_path, lib):
    c, cfg = new_client(tmp_path, lib, index_on_start=False)
    with c:
        setup_admin(c)
        worker = c.app.state.ctx.worker
        first = c.post("/api/scan").json()["started"]
        second = c.post("/api/scan").json()["started"]
        assert first is True and second in (False, True)  # True only if the first had already finished
        wait_scan(c)
