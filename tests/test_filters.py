"""The Filter menu: size, camera, shape and black and white."""

from helpers import ids_by_name, make_jpeg, new_client, setup_admin, wait_for, wait_scan

from snapshots import db


def _names(c, **params):
    return {i["filename"] for i in c.get("/api/assets", params=params).json()["items"]}


def test_size_camera_shape_and_black_and_white(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "wide.jpg", "2024:01:01 10:00:00", size=(4000, 3000), camera=("Canon", "EOS R6"))
    make_jpeg(root / "tall.jpg", "2024:01:02 10:00:00", size=(600, 900), camera=("Google", "Pixel 8"))
    make_jpeg(root / "square.jpg", "2024:01:03 10:00:00", size=(800, 800), camera=("Google", "Pixel 8"))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        assert _names(c, megapixels=10) == {"wide.jpg"}
        assert _names(c, megapixels=0.5) == {"wide.jpg", "tall.jpg", "square.jpg"}
        assert _names(c, shape="landscape") == {"wide.jpg"}
        assert _names(c, shape="portrait") == {"tall.jpg"}
        assert _names(c, shape="square") == {"square.jpg"}
        cams = c.get("/api/cameras").json()["cameras"]
        assert cams[0]["count"] == 2 and len(cams) == 2
        pixel = cams[0]["name"]
        assert _names(c, camera=pixel) == {"tall.jpg", "square.jpg"}
        assert _names(c, camera=pixel, shape="square") == {"square.jpg"}
        ids = ids_by_name(c)

        def colours_named():
            with db.session(cfg.db_path) as conn:
                return conn.execute("SELECT COUNT(*) FROM assets WHERE colors_done = 0").fetchone()[0] == 0

        wait_for(colours_named)       # so the colour job doesn't name them again after this
        with db.session(cfg.db_path) as conn:
            conn.execute("UPDATE assets SET colors = 'black white gray' WHERE id = ?", (ids["wide.jpg"],))
            conn.execute("UPDATE assets SET colors = 'gray' WHERE id = ?", (ids["tall.jpg"],))
            conn.execute("UPDATE assets SET colors = 'blue white' WHERE id = ?", (ids["square.jpg"],))
        assert _names(c, mono="true") == {"wide.jpg", "tall.jpg"}
        assert c.get("/api/assets", params={"shape": "round"}).status_code == 422
