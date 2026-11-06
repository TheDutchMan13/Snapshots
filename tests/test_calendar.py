"""The calendar: photos counted per day, as the timeline shows them."""

from helpers import ids_by_name, make_jpeg, new_client, setup_admin, wait_scan


def test_photos_are_counted_per_day(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:07:04 09:00:00")
    make_jpeg(root / "b.jpg", "2024:07:04 18:00:00", color=(10, 20, 30))
    make_jpeg(root / "c.jpg", "2024:12:31 23:59:00", color=(30, 20, 10))
    make_jpeg(root / "d.jpg", "2019:01:01 00:00:01", color=(90, 90, 10))
    make_jpeg(root / "e.jpg", "2024:07:05 10:00:00", color=(5, 90, 10))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        cal = c.get("/api/calendar").json()
        assert cal["year"] == 2024                          # the newest year with photos
        assert cal["years"] == [{"year": 2024, "count": 4}, {"year": 2019, "count": 1}]
        assert cal["days"] == {"2024-07-04": 2, "2024-07-05": 1, "2024-12-31": 1}
        assert c.get("/api/calendar", params={"year": 2019}).json()["days"] == {"2019-01-01": 1}
        # Archived and trashed items are left out, as on the timeline.
        ids = ids_by_name(c)
        assert c.post("/api/archive", json={"ids": [ids["e.jpg"]], "value": True}).status_code == 200
        assert c.post("/api/trash", json={"ids": [ids["c.jpg"]]}).status_code == 200
        assert c.get("/api/calendar").json()["days"] == {"2024-07-04": 2}
        # A day of the calendar is the timeline between its first and last second.
        day = c.get("/api/assets", params={"start": "2024-07-04T00:00:00", "end": "2024-07-04T23:59:59"}).json()
        assert {i["filename"] for i in day["items"]} == {"a.jpg", "b.jpg"}
