# Snapshots

Snapshots is a self-hosted photo and video library for Windows. Your originals stay on your own drive, and the app indexes them locally for browsing, search, people and places. You can reach your library from your phone over Tailscale, with no cloud account or subscription required.

> **Status:** early development (v0.7). Browsing, search, accounts, albums and smart albums, tags, favourites, archive, trash, people, places, the map, memories, events, clean-up, editing, comments, the locked folder, exports, imports from Immich and Google Photos, offsite copies and webhooks all work. Automatic backup from a phone is not built yet, see [Roadmap](#roadmap).

## What works today

- Runs natively on Windows, with no Docker or WSL, packaged as a single `Snapshots.exe` that opens in its own app window (no browser tab, no black console window)
- **Keeps itself up to date:** rescans the library every 15 minutes (adjustable), and photos you move or rename keep their albums, favourites and faces
- **Accounts:** everyone logs in. The first account is the administrator, who can add accounts for the family
- **Albums** and **tags** shared by everyone, **favourites** for each person, and a **trash** you can restore from
- **Smart albums** that fill themselves from rules: people (all of them in the photo), words, what's in the photo, a tag, dates, a place, photos or videos, favourites. New photos that match join by themselves. Save any search as one with **Save as smart album**
- **Import from Immich or Google Photos** (your name, **Import**, administrators): albums, favourites, archived and trashed photos, tags, dates and places changed in the other app, descriptions and people's names come across. Photos are matched by their content wherever they are, and the ones Snapshots doesn't have can be copied into the library. **Check first** shows what would happen without changing anything
- **Export** an album or a selection as a zip (files named by date, edited photos edited) or as a **PDF photo book** (Letter or A4, 1 to 6 photos a page, with dates and places)
- **Trim videos** and **save a moment of a video as a photo**: both are saved as new files next to the video, with its date, place and albums
- **Offsite copy** of the photos themselves (Settings): to another drive, a NAS or a cloud-synced folder, or with rclone or restic to cloud storage (encrypted if you like). It is kept up to date every day, and files deleted from the library stay in the copy
- **Webhooks** tell other programs (Home Assistant, a script) about new photos, album additions, comments, named people, finished imports and offsite copies, and failed backups
- **Archive:** hide screenshots, receipts and the like from the timeline without deleting them. They still show in albums, people, places, folders and search
- **Folders:** browse the library by its real folders
- **Several library folders:** add photo folders on other drives (Settings, as an administrator). Each shows up as its own folder, and an unplugged drive loses nothing
- **Edit photos:** turn, mirror, straighten and crop. Only the recipe is saved: your original file is never changed, and **Download edited** gives you an edited copy
- **Change details:** fix the date (or move a whole batch by some hours, for a camera with a wrong clock), set or remove the location by town name, coordinates or a click on the map, and add a description. Descriptions are searched too
- **Slideshow** of any page, with crossfades, a choice of speed and shuffle
- **360° photos and panoramas:** drag to look around a 360° photo, or scroll along a wide panorama
- **Locked folder:** move private photos behind a PIN. They are hidden from everyone everywhere else in Snapshots (the files stay in your folder)
- **Comments** on photos and albums, and an **Activity** page with what others commented and added to albums
- **Videos that don't play** in the app (HEVC from phones, AVI, MKV) are converted once when you open them, with the graphics card (NVIDIA, AMD or Intel) when ffmpeg can use it
- **Database backups** every day, kept in the data folder or on another drive
- **API keys** for scripts and other apps
- **On this day:** photos from today's date in earlier years, at the top of All
- **Map:** every photo with a location on a world map, grouped into pins with a photo on each. Press **Show these photos** for everything in view
- **Stacks:** several photos shown as one in the timeline. RAW + JPEG pairs from a camera are stacked automatically, and you can stack bursts from the Clean up page or any photos you select
- **Live Photos and motion photos:** iPhone Live Photos (a photo and a short clip with the same name) and the motion photos Google Pixel and Samsung phones save are shown as one photo. Press **Live** in the viewer to play the moment
- **Camera RAW files** (DNG, CR2, CR3, NEF, ARW, ORF, RW2, RAF, PEF and more) are shown like any photo, with their date, camera and position
- **Keywords** that Lightroom, digiKam or Windows Photo Gallery saved in your photos become tags
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
| | `map_tiles` | OpenStreetMap | Where the map's pictures come from; `""` shows country outlines only, with nothing fetched |
| | `map_attribution` | `© OpenStreetMap contributors` | Credit shown on the map for `map_tiles` |
| | `extra_libraries` | none | More photo folders, like `["E:\\Phone backup"]` (also set in the app: Settings) |
| | `backup_hours` | `24` | Copy the database this often; `0` turns backups off |
| | `backup_keep` | `14` | How many backups to keep |
| | `backup_dir` | `backups` in the data folder | Where backups go (another drive is safer) |
| | `video_encoder` | `auto` | How videos are converted: `auto` tries the graphics card first, or `libx264`, `h264_amf` (AMD), `h264_nvenc` (NVIDIA), `h264_qsv` (Intel) |
| | `video_cache_gb` | `20` | Converted videos kept for playing, at most this many GB |
| | `offsite_method` | `""` (off) | Copies of the photos themselves: `folder`, `rclone` or `restic` (also set in the app: Settings) |
| | `offsite_target` | | A folder, an rclone `remote:path`, or a restic repository |
| | `offsite_hours` | `24` | Bring the offsite copy up to date this often |
| | `offsite_password_file` | | restic only: a file holding the repository's password |
| `--restore-backup FILE` | | | Put a database backup back in place (close Snapshots first), then exit |
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
- **RAW files** are read with LibRaw. Thumbnails and the viewer use the full-size JPEG preview that cameras store inside every RAW file, so they are quick and look like what the camera showed; a RAW file without one is developed at half size. The original RAW file is what **Download** gives you.
- **Live Photos** are recognised by name: a photo and a `.mov` or `.mp4` clip (up to 30 MB) with the same name in the same folder. **Motion photos** are recognised from the information the phone writes into the photo, and the video is copied out of it the first time you play it. iPhone clips are usually HEVC, which the Windows app window can't play without an extra codec, so when ffmpeg is installed they are converted to H.264 once.
- **Edits** are a small recipe kept in the database: a quarter turn, mirror, a straightening angle (the picture is enlarged just enough to leave no empty corners) and a crop. The editor in the app draws the same steps the server uses for thumbnails and the viewer, so what you see while editing is what you get.
- **360° photos** are recognised from the panorama information phones and 360° cameras write into the photo. The viewer draws them on the inside of a sphere with WebGL. Photos at least 2.4 times wider than tall get a **Pan** button.
- **Video conversion** happens the first time a video that can't play is opened. A video that is already H.264 but in a file type the app can't open (MKV, AVI) is only repackaged, which takes seconds. Anything else is converted with the graphics card's encoder when ffmpeg can use it (each one is tested once with a tiny clip), otherwise with the processor. Copies are kept in the data folder, up to `video_cache_gb`.
- **Smart albums** keep their rules, not a list of photos, so they are worked out each time they are opened. A rule with **What's in the photo** uses search by content and stays empty until that is ready.
- **Imports** match each photo by its SHA-1, which Immich reports for every photo and Snapshots works out for every file in a Google export, so it doesn't matter where either app keeps its files or what they are called. Immich is read through its API with an API key you make in Immich; nothing is changed in Immich. Google Takeout's `.json` files are matched to their photos despite Takeout's naming quirks (`.supplemental-metadata.json`, names cut short, `(1)` copies, `-edited` versions). Google's dates are in UTC with no time zone, so a Google date only replaces the file's own when the two differ by more than a day. Names of people from Immich come with where each face is, and go to the face Snapshots finds there; Google only says who is in a photo, so its names are used for photos with exactly one face.
- **The photo book** is a PDF written by Snapshots itself, one picture per page at 200 dots per inch, so it is sharp on screen and good for printing at home. Videos are left out.
- **Webhooks** are kept in the database before they are sent, so none are lost if Snapshots closes. A delivery that fails is tried again after 30 seconds, 2 and 10 minutes, 1 and 6 hours. Each one carries `X-Snapshots-Signature: sha256=<HMAC of the body with the webhook's secret>`. Items in a locked folder never appear in one.
- **The map** draws pictures from OpenStreetMap while you look at it, so your computer asks OpenStreetMap's servers for the areas you view (not for your photos' locations). Set `map_tiles = ""` to use only the country outlines that come with Snapshots, with nothing fetched, or point it at your own tile server.

## Your files and safety

- Snapshots reads your library and writes thumbnails, its database and downloaded models to the **data folder**, which is kept outside your photos.
- Uploads are saved to `Uploads\<year>\<month>` inside your library.
- **Moving a photo to the trash doesn't touch the file.** It is only hidden, and can be restored. **Emptying the trash deletes the files from your disk**, and only an administrator can do that, after a confirmation.
- If the library folder is missing (an unplugged drive) or can't be read completely, Snapshots does **not** remove anything from its index, and the trash can't be emptied.
- Deleting a photo from the library folder yourself removes it from Snapshots on the next scan.
- A Live Photo's clip goes to the trash, comes back and is deleted together with its photo.
- Archiving, tagging, stacking, editing, changing details, comments and the locked folder change nothing in your files.
- **Extra library folders** are read the same way. Removing one in Settings takes its photos out of Snapshots and leaves the files alone.
- **The locked folder** hides photos inside Snapshots only. The files stay where they are, so anyone who can open your folders in Windows can still see them.
- **Trimmed videos and stills** are new files next to the video. **Imports** only add files to `From Immich` and `From Google Photos` in the library; nothing is changed in Immich or in the export.
- **The offsite copy** only reads your library. It never deletes anything from the copy either, so a photo deleted by mistake is still there.
- **Backups** contain the database (albums, tags, people's names, favourites, accounts, edits, comments), not your photos. To restore one, close Snapshots and run `Snapshots.exe --restore-backup "<backup file>"`; the database it replaces is kept as `snapshots.before-restore.db`.

## Accounts and remote access

Everyone logs in, and everyone sees the same library and albums. Favourites are personal. Administrators manage accounts (sidebar, your name, **Accounts**) and are the only ones who can empty the trash.

Passwords are stored as salted scrypt hashes. Sessions last 30 days and end when a password is changed. Repeated wrong passwords are slowed down, and the app is protected against cross-site request forgery. A future phone app can log in through the same API and send its session as `Authorization: Bearer <token>`. Scripts and other apps can use an **API key** instead (your name, **API keys**): it acts as you, except that it can't open your locked folder, change your password or make more keys. Only a hash of each key is stored.

**Imports, webhooks and the offsite copy** are for administrators, since they add albums and names for everyone, copy files, or send information out.

Each person's **locked folder** opens with their own PIN, for 10 minutes at a time and only in the window where it was typed. Wrong PINs are slowed down. If an administrator deletes an account, that person's locked photos become visible to everyone again.

The safe way to reach Snapshots from your phone is [Tailscale](https://tailscale.com/). Keep Snapshots on its default `127.0.0.1` address and let Tailscale publish it to your own devices only, for example with `tailscale serve --bg 8080` (see the Tailscale documentation for the current syntax). Even with login, don't port-forward Snapshots to the open internet.

## Building the exe

`Snapshots.exe` has to be built on Windows. There are two ways.

**Let GitHub build it (no setup on your PC).** Push the project to GitHub, open the repository's **Actions** tab, choose **Build Windows exe**, and press **Run workflow**. When it finishes, download `Snapshots-windows` from the run's page. To publish it on the **Releases** page instead, push a tag such as `v0.2.0`. The workflow runs the tests and then starts the finished exe to check it works before handing it over.

**Build it on your own PC.** Install Python 3.11 or newer, then double-click `build.bat`. When it finishes, the exe is at `dist\Snapshots.exe`. The script keeps its tools in `%LOCALAPPDATA%\Snapshots-build`, repairs them if an earlier run was interrupted, and is safe to run again.

If Windows Security removes the exe after a build (programs made with PyInstaller are sometimes flagged by mistake), restore it from Protection history.

## Upgrading

Replace the exe (or pull the new code) and start it again. Your database is upgraded automatically the first time. Version 0.2 added accounts, so after upgrading from 0.1 you'll be asked to create the administrator account. Version 0.3 only changes how Snapshots opens (its own window instead of a browser tab); your accounts, albums and photos are untouched. You will need to log in once more, because the app window keeps its own login separate from your browser. Version 0.4 then checks every photo once in the background (copies, blur, content and text), which takes a while for a big library; everything keeps working meanwhile. Version 0.5 looks once through your JPEG and HEIC photos for motion photos and keywords (quick: it reads only the start of each file), pairs Live Photos, stacks RAW + JPEG pairs, and picks up RAW files that earlier versions skipped. Version 0.6 looks once through your JPEG and HEIC photos for 360° panoramas (again reading only the start of each file) and makes its first database backup when it starts. Version 0.7 changes nothing on its own: imports, the offsite copy and webhooks only start once you set them up.

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
- [x] Archive, folders, tags, stacks and memories
- [x] Map view
- [x] RAW files, Live Photos and motion photos
- [x] Editing, changing dates and locations, descriptions
- [x] Slideshow, 360° photos and panoramas
- [x] Locked folder, comments and activity
- [x] Several library folders, database backups, API keys
- [x] Converting videos that don't play, with the graphics card
- [x] Smart albums, exports and photo books, trimming videos
- [x] Importing from Immich and Google Photos
- [x] Offsite copies of the photos, webhooks
- [ ] Android app with automatic backup
- [ ] Sharing links for people without an account
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
| `snapshots/linking.py` | Live Photos, motion photos, RAW + JPEG stacks, keywords as tags |
| `snapshots/libraries.py` | The main library and extra library folders, and where each item's file is |
| `snapshots/videos.py` | Converting videos that don't play, in the background |
| `snapshots/backup.py` | Database backups and restoring them |
| `snapshots/offsite.py` | Offsite copies of the photos: folder, rclone or restic |
| `snapshots/imports.py`, `immich.py`, `takeout.py` | Importing from Immich and Google Takeout |
| `snapshots/smart.py` | Smart albums' rules |
| `snapshots/exports.py` | Zip exports and the PDF photo book |
| `snapshots/webhooks.py` | Webhooks: queueing, signing and delivering events |
| `snapshots/clipsearch.py` | Search by what is in a photo (CLIP) |
| `snapshots/ocr.py` | Reading text in photos |
| `snapshots/downloads.py` | Downloading models once and checking their SHA-256 |
| `snapshots/auth.py` | Passwords, sessions, API keys, the locked folder PIN and login throttling |
| `snapshots/shell.py` | The app window, and logging and message boxes for the exe (which has no console) |
| `snapshots/media.py` | Reads dates, positions and camera info, makes thumbnails |
| `snapshots/db.py` | SQLite schema, upgrades, search and paging |
| `snapshots/app.py`, `routes_*.py` | Web server and JSON API |
| `snapshots/static/` | The web interface (plain HTML, CSS and JavaScript, no build step) |
| `snapshots/static/vendor/` | Leaflet and the country outlines for the map (licences in `LICENSES.txt`) |
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
- RAW files: [LibRaw](https://www.libraw.org/) (LGPL 2.1 or CDDL) through [rawpy](https://github.com/letmaik/rawpy) (MIT)
- Map: [Leaflet](https://leafletjs.com/) (BSD 2-clause) and [Leaflet.markercluster](https://github.com/Leaflet/Leaflet.markercluster) (MIT); map pictures © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors; country outlines from [Natural Earth](https://www.naturalearthdata.com/) (public domain) via [world-atlas](https://github.com/topojson/world-atlas) (ISC)
- [OpenCV](https://opencv.org/), [Pillow](https://python-pillow.org/), [pillow-heif](https://github.com/bigcat88/pillow_heif), [FastAPI](https://fastapi.tiangolo.com/)

The models and place data are downloaded by each installation; they are not part of this repository. The map's code and country outlines are included, in `snapshots/static/vendor`.

## AI disclaimer

Parts of this project were written with help from an AI assistant (Claude, made by Anthropic). The code and documentation may contain mistakes or behave in unexpected ways. Review and test it before relying on it, especially for anything important.

## License

Snapshots is free software, licensed under the [GNU General Public License v3.0](LICENSE).

Copyright (C) 2026 Hans De Kryger

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY. See the license for details.
