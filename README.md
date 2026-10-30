# Snapshots

Snapshots is a self-hosted photo and video library for Windows. Your originals stay on your own drive, and the app indexes them locally for browsing, search, people and places. You can reach your library from your phone over Tailscale, with no cloud account or subscription required.

> **Status:** early development (v0.4). Browsing, search, accounts, albums, favourites, trash, people, places, events and clean-up all work. Automatic backup from a phone is not built yet, see [Roadmap](#roadmap).

## What works today

- Runs natively on Windows, with no Docker or WSL, packaged as a single `Snapshots.exe` that opens in its own app window (no browser tab, no black console window)
- **Keeps itself up to date:** rescans the library every 15 minutes (adjustable), and photos you move or rename keep their albums, favourites and faces
- **Accounts:** everyone logs in. The first account is the administrator, who can add accounts for the family
- **Albums** shared by everyone, **favourites** for each person, and a **trash** you can restore from
- **People:** faces are found and grouped automatically, on your own computer. Name someone, merge two groups of the same person, or hide someone
- **Places:** photos with a GPS position are labelled with the nearest town, worked out offline
- **Events:** days out and trips are found automatically from when and where photos were taken, and can be saved as albums
- **Clean up:** finds copies of the same photo, bursts of near-identical shots, and blurry or too-dark photos, and suggests which to keep. Nothing is deleted: the rest go to the trash
- **Search by what's in a photo:** type `dog on the beach` or `birthday cake` and the best matches come first
- **Text in photos:** words on signs, screenshots and receipts are read and can be searched
- One search box for everything: people, places, things, text in photos, file names, cameras, years and months (`grandma`, `sedona`, `snow`, `pixel 2024`, `december`)
- Timeline grouped by month, with fast infinite scrolling
- Select many photos at once (Shift-click for a range) to favourite, add to an album or move to the trash
- Full-screen viewer with arrow-key navigation, details (who is in it, where it was taken, which albums) and download
- HEIC, HEIF and AVIF photos from phones are shown as normal images
- Dates come from EXIF, then from names like `IMG_20241005_142233.jpg`, then from the file's modified time
- Upload from the browser by button or drag and drop, with duplicate detection
- Video playback, plus video thumbnails, dates and positions when ffmpeg is installed
- Works on a phone-sized screen and follows your light or dark setting

## Requirements

- Windows 10 or 11
- To run the exe: nothing else. The app window uses Microsoft Edge WebView2, which comes with Windows 11 and current Windows 10. If a PC doesn't have it, Snapshots opens in your browser instead
- To run from source or build the exe yourself: [Python 3.11 or newer](https://www.python.org/downloads/)
- Optional: [ffmpeg](https://ffmpeg.org/download.html) on your `PATH`, for video thumbnails, dates and positions

## Quick start with the exe

1. Get `Snapshots.exe` (see [Building the exe](#building-the-exe)) and put it anywhere, for example `C:\Snapshots`.
2. Double-click it. The first time, it asks which folder holds your photos.
3. Snapshots opens in its own window. Create the first account: it becomes the administrator.
4. **Closing the window stops Snapshots.** You stay logged in next time you open it. What Snapshots has been doing is written to `snapshots.log` in the data folder (`%LOCALAPPDATA%\Snapshots`), which helps if something goes wrong.

Next time you double-click, it goes straight in. Double-clicking while it is already running just shows the running copy. To pick a different photo folder, run `Snapshots.exe --change-library` from a terminal. Your choice is remembered in `%LOCALAPPDATA%\Snapshots\config.toml`.

Windows may warn "Windows protected your PC" the first time, because the exe isn't code-signed. Choose **More info**, then **Run anyway**.

**Forgot the administrator password?** Run `Snapshots.exe --reset-password <name>` in a terminal on the computer that runs Snapshots.

## Run from source

```powershell
git clone https://github.com/TheDutchMan13/Snapshots.git
cd Snapshots
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m snapshots --library "D:\Photos" --window
```

Use `--open` instead of `--window` to open your web browser.

The first scan starts automatically, and photos appear as they are found.

## Settings

Copy `config.example.toml` to `config.toml` and edit it, or use command-line options. Snapshots reads `config.toml` from the folder you run it in, or else from `%LOCALAPPDATA%\Snapshots\config.toml`. Command-line options win over the file.

| Option | Config key | Default | Description |
| --- | --- | --- | --- |
| `--library` | `library_path` | asked on first run | Folder that holds your photos and videos |
| `--change-library` | | | Pick the photo folder again |
| `--data` | `data_dir` | `%LOCALAPPDATA%\Snapshots` | Where the database, thumbnails and downloaded models live |
| `--config` | | | Use this config file |
| `--host` | `host` | `127.0.0.1` | Address to listen on |
| `--port` | `port` | `8080` | Port for the web interface |
| `--no-index` | `index_on_start` | scan on start | Skip the scan when starting |
| | `scan_interval_minutes` | `15` | Rescan this often; `0` turns it off |
| | `faces` | `true` | Find faces and group them into people |
| | `face_threads` | `2` | CPU threads face recognition may use |
| | `face_match_threshold` | `0.50` | How alike faces must be to be grouped (higher is stricter) |
| | `min_faces_per_person` | `3` | Smaller unnamed groups are not listed on the People page |
| | `places` | `true` | Turn GPS positions into town names |
| | `face_merge_threshold` | `0.42` | Merge two groups of faces this alike on average; `0` turns it off |
| | `photo_checks` | `true` | Look for copies, bursts, blurry and dark photos |
| | `duplicate_distance` | `8` | How different (out of 64) copies' fingerprints may be |
| | `burst_seconds`, `burst_distance` | `30`, `14` | Photos this close in time and this alike are a burst |
| | `blur_threshold`, `dark_threshold` | `50`, `30` | Lower them to flag fewer photos as blurry or dark |
| | `event_gap_hours`, `event_min_items` | `4`, `8` | A pause this long starts a new event; smaller events are not listed |
| | `smart_search` | `true` | Search by what's in a photo (downloads 154 MB once) |
| | `text_search` | `true` | Read text in photos (downloads 11 MB once) |
| | `search_threads` | `2` | CPU threads the two above may use |
| `--reset-password NAME` | | | Set a new password for an account, then exit |
| `--window` | | exe: on, source: off | Show Snapshots in its own window |
| `--open` / `--browser` | | off | Use your web browser instead of the app window |
| `--no-browser` | | | Open nothing: just run the server |

## How the clever parts work

Everything runs on your own computer, in the background after a scan, a few photos at a time, so the app stays responsive. The status bar shows how many photos are still to go. Nothing about your photos is sent anywhere; the only network use is downloading each model once.

- **Faces** are found with the YuNet detector and compared with the SFace recogniser, both run by OpenCV on your CPU. The two models (about 39 MB) are downloaded once from Hugging Face and checked against known SHA-256 hashes. Faces smaller than 40 pixels are ignored, because they are too blurry to recognise reliably. In a test on 1,200 photos of 598 people from the LFW face set, no group ever held two different people, and 98.8% of same-person photos were grouped together. After new photos are added, groups of the same person are merged automatically when, on average, their faces are clearly alike (never two differently named people, and never hidden people). In a test on 1,862 LFW faces this removed some splits without a single wrong merge. When one person still ends up in two groups, open one and choose **Same person as…**. If a face is in the wrong group, open the photo, press **Info**, and remove it.
- **Places** come from the GeoNames list of towns (about 14 MB), downloaded once. Lookups happen on your computer: your photo locations are never sent anywhere. Neighbourhoods are left out, so photos are labelled with the town.
- **Events** are worked out from dates and places. Photos belong together unless more than 4 hours pass or the location jumps more than 30 km. Home is the town most of your photos were taken in: a day's photos at home make one event, and days away that follow each other make one trip.
- **Copies and bursts** are found with a 64-bit "difference hash" of each thumbnail. In a test on 400 real photos, resized and recompressed copies were always found, and no two different photos were ever mistaken for copies. Photos taken within 30 seconds of each other that look alike are a burst. The suggested photo to keep has the most pixels, then is the sharpest.
- **Blurry and dark photos** are found by measuring fine detail in the sharpest part of each photo, in the direction camera shake would blur, and the light level. A photo only counts as too dark if even its brightest parts are dim, so night and concert photos with lights are left alone. In the test, none of the 400 normal photos was flagged.
- **Search by what's in a photo** uses OpenAI's CLIP model (ViT-B/32, 8-bit, about 154 MB). Each photo is learned once from its thumbnail, at about 60 ms per photo. Searches show the best matches first. It is good at things, scenes and activities, but it is approximate: for a description of one particular photo, the right one was the first result 57% of the time and in the first five 84% of the time.
- **Text in photos** is read with PaddleOCR's English models (about 11 MB). In a test, 97% of words on signs in photos were read exactly. Text-heavy photos take about a quarter of a second each, and this runs last.

## Your files and safety

- Snapshots reads your library and writes thumbnails, its database and downloaded models to the **data folder**, which is kept outside your photos.
- Uploads are saved to `Uploads\<year>\<month>` inside your library.
- **Moving a photo to the trash doesn't touch the file.** It is only hidden, and can be restored. **Emptying the trash deletes the files from your disk**, and only an administrator can do that, after a confirmation.
- If the library folder is missing (an unplugged drive) or can't be read completely, Snapshots does **not** remove anything from its index, and the trash can't be emptied.
- Deleting a photo from the library folder yourself removes it from Snapshots on the next scan.

## Accounts and remote access

Everyone logs in, and everyone sees the same library and albums. Favourites are personal. Administrators manage accounts (sidebar, your name, **Accounts**) and are the only ones who can empty the trash.

Passwords are stored as salted scrypt hashes. Sessions last 30 days and end when a password is changed. Repeated wrong passwords are slowed down, and the app is protected against cross-site request forgery. A future phone app can log in through the same API and send its session as `Authorization: Bearer <token>`.

The safe way to reach Snapshots from your phone is [Tailscale](https://tailscale.com/). Keep Snapshots on its default `127.0.0.1` address and let Tailscale publish it to your own devices only, for example with `tailscale serve --bg 8080` (see the Tailscale documentation for the current syntax). Even with login, don't port-forward Snapshots to the open internet.

## Building the exe

`Snapshots.exe` has to be built on Windows. There are two ways.

**Let GitHub build it (no setup on your PC).** Push the project to GitHub, open the repository's **Actions** tab, choose **Build Windows exe**, and press **Run workflow**. When it finishes, download `Snapshots-windows` from the run's page. To publish it on the **Releases** page instead, push a tag such as `v0.2.0`. The workflow runs the tests and then starts the finished exe to check it works before handing it over.

**Build it on your own PC.** Install Python 3.11 or newer, then double-click `build.bat`. When it finishes, the exe is at `dist\Snapshots.exe`. The script keeps its tools in `%LOCALAPPDATA%\Snapshots-build`, repairs them if an earlier run was interrupted, and is safe to run again.

If Windows Security removes the exe after a build (programs made with PyInstaller are sometimes flagged by mistake), restore it from Protection history.

## Upgrading

Replace the exe (or pull the new code) and start it again. Your database is upgraded automatically the first time. Version 0.2 added accounts, so after upgrading from 0.1 you'll be asked to create the administrator account. Version 0.3 only changes how Snapshots opens (its own window instead of a browser tab); your accounts, albums and photos are untouched. You will need to log in once more, because the app window keeps its own login separate from your browser. Version 0.4 then checks every photo once in the background (copies, blur, content and text), which takes a while for a big library; everything keeps working meanwhile.

## Roadmap

- [x] Login and accounts
- [x] Albums, favourites and trash
- [x] Face detection and grouping
- [x] Search by place
- [x] Automatic rescans
- [x] Its own app window
- [x] Events and trips
- [x] Duplicate and burst finder, blurry and dark photos
- [x] Search by what is in a photo, and by text in photos
- [ ] Android app with automatic backup
- [ ] Map view (could use offline map tiles)
- [ ] Run at startup as a Windows service

## Development

```powershell
pip install -r requirements-dev.txt
python -m pytest
```

The tests build small libraries of generated images, so they don't touch your photos, and they need no internet. Some extra tests use the real models: point `SNAPSHOTS_TEST_MODELS` at a Snapshots `models` folder (the face models, plus `clip` and `ocr` folders), and for faces also `SNAPSHOTS_TEST_FACES` at a folder of photos named `<person>_<anything>.jpg`.

| File | Job |
| --- | --- |
| `snapshots/scanner.py` | Walks the library and keeps the database in step |
| `snapshots/worker.py` | Background thread: timed rescans, then places, then faces |
| `snapshots/faces.py` | Finds faces and groups them into people |
| `snapshots/places.py` | Offline nearest-town lookup from GeoNames |
| `snapshots/analysis.py` | Copies, bursts, blurry and dark photos, and events |
| `snapshots/clipsearch.py` | Search by what is in a photo (CLIP) |
| `snapshots/ocr.py` | Reading text in photos |
| `snapshots/downloads.py` | Downloading models once and checking their SHA-256 |
| `snapshots/auth.py` | Passwords, sessions and login throttling |
| `snapshots/shell.py` | The app window, and logging and message boxes for the exe (which has no console) |
| `snapshots/media.py` | Reads dates, positions and camera info, makes thumbnails |
| `snapshots/db.py` | SQLite schema, upgrades, search and paging |
| `snapshots/app.py`, `routes_*.py` | Web server and JSON API |
| `snapshots/static/` | The web interface (plain HTML, CSS and JavaScript, no build step) |
| `snapshots.spec`, `build.bat` | Build recipe for `Snapshots.exe` |
| `.github/workflows/build-exe.yml` | Builds the exe on GitHub, then starts it and checks the window opens and closes properly |

Contributions are welcome. Please open an issue before starting a large change so we can discuss the approach.

## Credits

- Face detection: [YuNet](https://huggingface.co/opencv/face_detection_yunet), MIT licence
- Face recognition: [SFace](https://huggingface.co/opencv/face_recognition_sface), Apache 2.0 licence
- Place names: [GeoNames](https://www.geonames.org/), CC BY 4.0
- Search by content: [CLIP](https://github.com/openai/CLIP) by OpenAI, MIT licence, in the ONNX version by [Xenova](https://huggingface.co/Xenova/clip-vit-base-patch32)
- Text reading: [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR), Apache 2.0 licence, in the ONNX version by [RapidOCR](https://huggingface.co/SWHL/RapidOCR)
- [ONNX Runtime](https://onnxruntime.ai/), MIT licence
- [OpenCV](https://opencv.org/), [Pillow](https://python-pillow.org/), [pillow-heif](https://github.com/bigcat88/pillow_heif), [FastAPI](https://fastapi.tiangolo.com/)

The models and place data are downloaded by each installation; they are not part of this repository.

## License

Snapshots is free software, licensed under the [GNU General Public License v3.0](LICENSE).

Copyright (C) 2026 Hans De Kryger

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY. See the license for details.
