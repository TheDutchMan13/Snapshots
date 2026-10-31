"use strict";

const $ = (id) => document.getElementById(id);
const PAGE = 120;

const S = {
  me: null,
  status: null,
  route: null,
  q: "",
  gen: 0,            // bumps whenever the grid is reloaded, so stale responses are dropped
  cursor: null, done: false, loading: false, loaded: false,
  items: [],
  counts: {},        // "YYYY-MM" -> number of items in the current view
  lastMonth: null,
  selecting: false,
  selected: new Set(),
  marks: { rating: 0, label: "", flag: "" },   // the star / label / pick filter, kept from page to page
  lastClicked: -1,
  viewing: -1,
  detail: null,      // full details of the photo open in the viewer
  look: null,        // searching by what is in the photo (true), by words (false), or not chosen yet (null)
  search: null,      // the last search summary: how many matches each way
};

// ------------------------------------------------------------------ helpers

const fmtInt = (n) => Number(n || 0).toLocaleString();
const plural = (n, one, many) => `${fmtInt(n)} ${n === 1 ? one : (many || one + "s")}`;

function monthLabel(key) {
  const [y, m] = key.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleString(undefined, { month: "long", year: "numeric" });
}
function fmtDateTime(iso) {
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(undefined, { dateStyle: "long", timeStyle: "short" });
}
function fmtMonth(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleString(undefined, { month: "short", year: "numeric" });
}
function fmtSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = bytes, i = -1;
  do { v /= 1024; i++; } while (v >= 1024 && i < units.length - 1);
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}
function ext(name) {
  const i = name.lastIndexOf(".");
  return i < 0 ? "" : name.slice(i + 1).toUpperCase();
}
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function button(text, cls, onClick) {
  const b = el("button", cls || "btn", text);
  b.type = "button";
  if (onClick) b.addEventListener("click", onClick);
  return b;
}
function link(text, href, cls) {
  const a = el("a", cls, text);
  a.href = href;
  return a;
}
function img(src, alt, cls) {
  const i = new Image();
  i.src = src;
  i.alt = alt || "";
  i.loading = "lazy";
  i.decoding = "async";
  if (cls) i.className = cls;
  return i;
}
const thumbUrl = (id, v) => `/api/assets/${id}/thumb?v=${v || ""}`;
const faceUrl = (id) => `/api/faces/${id}/thumb`;
function placeHash(country, state, city) {
  const part = (x) => encodeURIComponent(x == null || x === "" ? "~" : x);
  let h = `#/places/${part(country)}`;
  if (state !== undefined) h += `/${part(state)}`;
  if (city !== undefined) h += `/${part(city)}`;
  return h;
}
function placeName(p) {
  return [p.city, p.state, p.country].filter(Boolean).join(", ");
}

function detailText(data) {
  if (!data) return "";
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) return data.detail.map((d) => d.msg).join(" ");
  return "";
}

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (init.method !== "GET") init.headers["X-Snapshots-Request"] = "1";
  if (opts.json !== undefined) {
    init.body = JSON.stringify(opts.json);
    init.headers["Content-Type"] = "application/json";
  } else if (opts.body !== undefined) {
    init.body = opts.body;
  }
  const res = await fetch(path, init);
  if (res.status === 401) {
    location.replace("/login");
    throw new Error("Please log in.");
  }
  let data = null;
  try { data = await res.json(); } catch (_) { /* not JSON */ }
  if (!res.ok) throw new Error(detailText(data) || res.statusText || "Something went wrong.");
  return data;
}
const post = (path, json) => api(path, { method: "POST", json });
const patch = (path, json) => api(path, { method: "PATCH", json });
const del = (path) => api(path, { method: "DELETE" });

// ------------------------------------------------------------------ dialogs

/** A dialog box. build(body, ctl) fills it and may return {submit} (async; return a value to close). */
function modal({ title, build, ok = "OK", cancel = "Cancel", danger = false, wide = false }) {
  return new Promise((resolve) => {
    const dlg = $("modal");
    dlg.className = wide ? "modal wide" : "modal";
    const form = el("form");
    form.noValidate = true;
    const h = el("h2", "modal-title", title);
    h.id = "modal-title";
    const body = el("div", "modal-body");
    const err = el("p", "form-error");
    err.setAttribute("role", "alert");
    let finished = false;
    const finish = (value) => {
      if (finished) return;
      finished = true;
      if (dlg.open) dlg.close();
      resolve(value);
    };
    const ctl = build(body, { close: finish, error: (m) => { err.textContent = m || ""; } }) || {};
    const foot = el("div", "modal-foot");
    if (ok) {
      const okBtn = el("button", danger ? "btn danger" : "btn primary", ok);
      okBtn.type = "submit";
      foot.append(okBtn);
    }
    if (cancel) foot.append(button(cancel, "btn", () => finish(null)));
    form.append(h, body, err, foot);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      err.textContent = "";
      if (!ctl.submit) { finish(true); return; }
      try {
        const v = await ctl.submit();
        if (v !== undefined) finish(v);
      } catch (x) {
        err.textContent = x.message;
      }
    });
    dlg.replaceChildren(form);
    // A dialog closed just before this one opened sends its "close" late: only a real close counts.
    dlg.onclose = () => { if (!dlg.open) finish(null); };
    dlg.showModal();
    const first = body.querySelector("input, button, [tabindex]");
    if (first) first.focus();
  });
}

function field(label, attrs = {}) {
  const wrap = el("label", "field");
  wrap.append(el("span", null, label));
  const input = el("input");
  Object.assign(input, attrs);
  wrap.append(input);
  return [wrap, input];
}

function confirmBox(title, text, ok = "OK", danger = false) {
  return modal({ title, ok, danger, build: (body) => { body.append(el("p", null, text)); } });
}

function askText(title, label, value = "", ok = "Save") {
  return modal({
    title, ok,
    build: (body) => {
      const [wrap, input] = field(label, { value, maxLength: 200 });
      body.append(wrap);
      return { submit: async () => {
        const v = input.value.trim();
        if (!v) throw new Error("Please type a name.");
        return v;
      } };
    },
  });
}

let flash = null;
let toastTimer = null;
function flashNote(text) {
  flash = { text, until: Date.now() + 8000 };
  $("st-note").textContent = text;
  // On phones there is no status bar: the note shows for a moment above the tab bar.
  const toast = $("toast");
  toast.textContent = text;
  toast.hidden = !text;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 4500);
  setTimeout(() => { if (flash && Date.now() >= flash.until) { flash = null; renderStatus(); } }, 8100);
}

// ------------------------------------------------------------------ routing

function parseRoute() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, query] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map((p) => decodeURIComponent(p));
  const params = new URLSearchParams(query || "");
  const un = (x) => (x === "~" ? "" : x);
  // The timelines hide archived items and show each stack as one photo.
  const TIMELINE = { archived: "hide", stacks: "true" };
  switch (parts[0] || "") {
    case "photos": return { name: "photos", view: "grid", filter: { ...TIMELINE, kind: "photo" } };
    case "videos": return { name: "videos", view: "grid", filter: { ...TIMELINE, kind: "video" } };
    case "favorites": return { name: "favorites", view: "grid", filter: { favorite: "true" }, title: "Favourites" };
    case "trash": return { name: "trash", view: "grid", filter: { trashed: "true" }, title: "Trash" };
    case "archive": return { name: "archive", view: "grid", filter: { archived: "only" }, archive: true };
    case "locked":
      if (S.status && S.status.locked && S.status.locked.open) return { name: "locked", view: "grid", filter: { locked: "true" }, locked: true };
      return { name: "locked", view: "locked" };
    case "activity": return { name: "activity", view: "activity" };
    case "review": return { name: "review", view: "review", year: /^\d{4}$/.test(parts[1] || "") ? +parts[1] : null };
    case "events":
      if (parts[1] && parts[2]) return { name: "events", view: "grid", filter: { start: parts[1], end: parts[2], archived: "hide" }, event: { start: parts[1], end: parts[2] } };
      return { name: "events", view: "events" };
    case "memories":
      if (parts[1] && /^\d{4}-\d{2}-\d{2}$/.test(parts[1])) {
        return { name: "all", view: "grid", filter: { start: `${parts[1]}T00:00:00`, end: `${parts[1]}T23:59:59`, archived: "hide", stacks: "true" }, memory: parts[1] };
      }
      return { name: "all", view: "grid", filter: TIMELINE };
    case "tags":
      if (parts[1] && /^\d+$/.test(parts[1])) return { name: "tags", view: "grid", filter: { tag: parts[1] }, tag: +parts[1] };
      return { name: "tags", view: "tags" };
    case "folders": {
      const folder = parts.slice(1).join("/");
      return { name: "folders", view: "grid", filter: { folder }, folder };
    }
    case "map":
      if (parts[1] === "area" && parts[2] && /^-?[\d.]+(,-?[\d.]+){3}$/.test(parts[2])) {
        return { name: "map", view: "grid", filter: { bbox: parts[2], archived: "hide", stacks: "true" }, area: parts[2] };
      }
      return { name: "map", view: "map" };
    case "cleanup":
      if (parts[1] === "quality") return { name: "cleanup", view: "grid", filter: { quality: "true" }, quality: true };
      if (parts[1] === "storage") return { name: "storage", view: "storage" };   // older links
      if (parts[1] === "health") return { name: "health", view: "health" };
      return { name: "cleanup", view: "cleanup" };
    case "storage": return { name: "storage", view: "storage" };
    case "health": return { name: "health", view: "health" };
    case "settings": return { name: "settings", view: "settings", tab: parts[1] || "library" };
    case "albums":
      if (parts[1] && /^\d+$/.test(parts[1])) return { name: "albums", view: "grid", filter: { album: parts[1] }, album: +parts[1] };
      return { name: "albums", view: "albums" };
    case "people":
      if (parts[1] && /^\d+$/.test(parts[1])) return { name: "people", view: "grid", filter: { person: parts[1] }, person: +parts[1] };
      return { name: "people", view: "people", hidden: params.get("hidden") === "1" };
    case "places":
      if (parts.length > 1) {
        const filter = { country: un(parts[1]) };
        if (parts.length > 2) filter.state = un(parts[2]);
        if (parts.length > 3) filter.city = un(parts[3]);
        return { name: "places", view: "grid", filter, place: filter };
      }
      return { name: "places", view: "places" };
    default: return { name: "all", view: "grid", filter: TIMELINE };
  }
}

let keepSearch = false;   // true while the search box itself caused a change of page

const ROUTE_TITLES = {
  all: "Snapshots", photos: "Photos", videos: "Videos", favorites: "Favourites", activity: "Activity", people: "People",
  places: "Places", map: "Map", events: "Events", review: "Year in review", albums: "Albums", tags: "Tags", folders: "Folders",
  cleanup: "Clean up", storage: "Storage", health: "Library health", archive: "Archive", locked: "Locked", trash: "Trash",
  settings: "Settings",
};

async function render() {
  S.route = parseRoute();
  document.querySelectorAll(".side .nav[data-route]").forEach((a) => {
    if (a.dataset.route === S.route.name) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  $("top-title").textContent = ROUTE_TITLES[S.route.name] || "Snapshots";
  if (S.route.name === "settings") $("settings-gear").setAttribute("aria-current", "page");
  else $("settings-gear").removeAttribute("aria-current");
  closeSheet();
  markTab();
  hideScrubber();
  S.jumpedTo = null;
  $("jumped").hidden = true;
  if (!keepSearch) { S.q = ""; $("search").value = ""; }
  S.look = null;
  keepSearch = false;
  if (!S.q) topBar.classList.remove("searching");
  markTab();
  stopSelecting();
  $("new-items").hidden = true;
  $("scroller").scrollTop = 0;

  const grid = S.route.view === "grid";
  $("grid").hidden = !grid;
  $("sentinel").hidden = !grid;
  $("select-btn").hidden = !grid;
  $("layout-btn").hidden = !grid;
  $("show-btn").hidden = !grid || S.route.name === "trash";
  $("marks-btn").hidden = !grid || S.route.name === "trash" || !!S.route.locked;
  $("marks-pop").hidden = true;
  renderMarksFilter();
  $("collection").hidden = grid;
  $("empty").hidden = true;
  $("page-head").replaceChildren();
  $("page-head").hidden = true;
  $("collection").replaceChildren();
  $("collection").classList.toggle("full", S.route.view === "map");
  S.folderInfo = null;
  S.album = null;

  try {
    if (grid) {
      await renderHead();
      await reload();
    } else if (S.route.view === "albums") {
      await renderAlbums();
    } else if (S.route.view === "people") {
      await renderPeople();
    } else if (S.route.view === "places") {
      await renderPlaces();
    } else if (S.route.view === "events") {
      await renderEvents();
    } else if (S.route.view === "cleanup") {
      await renderCleanup();
    } else if (S.route.view === "storage") {
      await renderStorage();
    } else if (S.route.view === "health") {
      await renderHealth();
    } else if (S.route.view === "review") {
      await renderReview();
    } else if (S.route.view === "tags") {
      await renderTags();
    } else if (S.route.view === "map") {
      await renderMap();
    } else if (S.route.view === "locked") {
      await renderLockedGate();
    } else if (S.route.view === "activity") {
      await renderActivity();
    } else if (S.route.view === "settings") {
      await renderSettings();
    }
  } catch (err) {
    showEmpty("Something went wrong", err.message);
  }
}

window.addEventListener("hashchange", render);

// ------------------------------------------------------------------ page headings

function headRow(title, metaText, actions, note) {
  const head = $("page-head");
  head.replaceChildren();
  if (title instanceof Node) head.append(title); else head.append(el("h1", null, title));
  if (metaText) head.append(el("span", "meta", metaText));
  if (actions && actions.length) {
    const box = el("div", "actions");
    box.append(...actions);
    head.append(box);
  }
  if (note) head.append(el("p", "note", note));
  head.hidden = false;
}

const SEARCHABLE = ["all", "photos", "videos", "favorites"];

async function renderHead() {
  const r = S.route;
  if (S.q && SEARCHABLE.includes(r.name)) {
    await renderSearchHead();
  } else if (r.memory) {
    renderMemoryHead(r.memory);
  } else if (r.name === "all" && !r.memory) {
    await renderMemoriesStrip();
  } else if (r.tag) {
    await renderTagHead(r.tag);
  } else if (r.folder !== undefined) {
    await renderFolderHead(r.folder);
  } else if (r.area) {
    headRow("Photos in this area", null, [link("Back to the map", "#/map", "btn small")]);
  } else if (r.locked) {
    headRow("Locked", null, [button("Lock now", "btn small", lockNow)],
      "Only you can see these, and only after typing your PIN. They are hidden everywhere else in Snapshots. The files stay in your library folder.");
  } else if (r.archive) {
    headRow("Archive", null, [], "Archived photos are hidden from All, Photos, Videos, memories and the map. They still show in albums, people, places, folders and search.");
  } else if (r.event) {
    await renderEventHead(r.event);
  } else if (r.quality) {
    renderQualityHead();
  } else if (r.album) {
    const album = await api(`/api/albums/${r.album}`);
    S.album = album;
    const range = album.first ? `${fmtMonth(album.first)}${fmtMonth(album.last) !== fmtMonth(album.first) ? " – " + fmtMonth(album.last) : ""}` : "";
    let note = null;
    if (album.smart) {
      note = `Smart album: ${album.rule_text}. Photos that match join it by themselves.`;
      if (album.waiting) note += " Search by what's in the photo isn't ready yet, so it is empty for now.";
    }
    headRow(album.name, [plural(album.count, "item"), range].filter(Boolean).join(" · "), [
      ...(album.smart ? [button("Edit rules", "btn small", () => smartAlbumDialog(album, album.rule))] : []),
      button("Export", "btn small", () => exportDialog({ album: album.id, title: album.name, count: album.count })),
      button(album.comments ? `Comments (${album.comments})` : "Comments", "btn small", () => albumComments(album)),
      button("Rename", "btn small", async () => {
        const name = await askText("Rename album", "Album name", album.name);
        if (!name) return;
        try { await patch(`/api/albums/${album.id}`, { name }); renderHead(); } catch (err) { flashNote(err.message); }
      }),
      button("Delete album", "btn small", async () => {
        const ok = await confirmBox("Delete this album?",
          `“${album.name}” will be removed. The photos in it stay in your library.`, "Delete album", true);
        if (!ok) return;
        try { await del(`/api/albums/${album.id}`); location.hash = "#/albums"; } catch (err) { flashNote(err.message); }
      }),
    ], note);
  } else if (r.person) {
    await renderPersonHead(r.person);
  } else if (r.place) {
    headRow(placeName(r.place) || "Unknown place", null, [link("All places", "#/places", "btn small")]);
  } else if (r.name === "trash") {
    const actions = [];
    if (S.me && S.me.is_admin) actions.push(button("Empty trash", "btn small", () => deleteForever(null)));
    headRow("Trash", null, actions,
      "Items in the trash are hidden everywhere else and can be restored. Emptying the trash deletes the files from your disk.");
  } else if (r.title) {
    headRow(r.title);
  }
}

/** Search results: by words, names, places and dates, or by what is in the photo. */
async function renderSearchHead() {
  const q = S.q;
  let sum;
  try { sum = await api(`/api/search?${filterParams()}`); } catch (_) { sum = null; }
  if (q !== S.q) return;
  S.search = sum;
  if (!sum) { $("page-head").hidden = true; S.look = false; return; }
  if (S.look === null) S.look = sum.words === 0 && sum.content > 0;
  const tabs = el("div", "tabs");
  tabs.setAttribute("role", "group");
  tabs.setAttribute("aria-label", "Search by");
  const tab = (label, count, look) => {
    const b = button("", "tab", () => {
      if (S.look === look) return;
      S.look = look;
      tabs.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-pressed", String(t === b)));
      stopSelecting();
      reload();
    });
    b.append(el("span", null, label), el("span", "count", fmtInt(count)));
    b.setAttribute("aria-pressed", String(S.look === look));
    return b;
  };
  tabs.append(tab("Words, names and dates", sum.words, false));
  let note = null;
  if (sum.content_enabled) {
    tabs.append(tab("What's in the photo", sum.content, true));
    if (sum.content_message) note = sum.content_message;
    else if (!sum.content_ready) note = "Search by what's in the photo starts once its model is downloaded.";
    else if (sum.content_pending) note = `Still learning what's in your photos: ${plural(sum.content_pending, "photo")} to go.`;
    else if (S.look) note = "Best matches first. Matching by content is approximate.";
  }
  const save = button("Save as smart album", "btn small", () =>
    smartAlbumDialog(null, S.look ? { look: q } : { words: q }, q));
  headRow(el("h1", null, `“${q}”`), null, [tabs, save], note);
}

async function renderEventHead(ev) {
  let info = null;
  try {
    const { events } = await api("/api/events");
    info = events.find((e) => e.start === ev.start && e.end === ev.end);
  } catch (_) { /* the photos still show */ }
  const title = info ? eventTitle(info) : "Event";
  headRow(title, info ? `${eventDates(info)} · ${plural(info.count, "item")}` : null, [
    button("Save as album", "btn small", async () => {
      const name = await askText("Save as album", "Album name", title, "Create album");
      if (!name) return;
      try {
        const ids = await allIds();
        const album = await post("/api/albums", { name, ids });
        flashNote(`Album “${album.name}” created with ${plural(ids.length, "item")}`);
      } catch (err) { flashNote(err.message); }
    }),
    link("All events", "#/events", "btn small"),
  ]);
}

function renderQualityHead() {
  headRow("Blurry or dark", null, [
    button("All look fine", "btn small", async () => {
      const ok = await confirmBox("Keep all of these?", "They won't be suggested here again. Nothing is moved or deleted.", "Keep them all");
      if (!ok) return;
      try { const ids = await allIds(); await post("/api/cleanup/fine", { ids }); await reload(); } catch (err) { flashNote(err.message); }
    }),
    button("Move all to trash", "btn small", async () => {
      const ids = await allIds();
      if (!ids.length) return;
      const ok = await confirmBox(`Move ${plural(ids.length, "photo")} to the trash?`,
        "They can be restored from the trash until it is emptied.", "Move to trash");
      if (ok) await moveToTrash(ids);
    }),
    link("Clean up", "#/cleanup", "btn small"),
  ], "Photos that look blurry or too dark. Select the ones to keep and press Looks fine, or move them to the trash.");
}

/** Every id in the current grid (not only the ones loaded so far). */
async function allIds() {
  const ids = [];
  let cursor = "";
  for (;;) {
    const p = filterParams({ limit: 500 });
    if (S.look) p.set("look", "true");
    if (cursor) p.set("cursor", cursor);
    const page = await api(`/api/assets?${p}`);
    page.items.forEach((it) => ids.push(it.id));
    if (!page.next) return ids;
    cursor = page.next;
  }
}

async function renderPersonHead(id) {
  const person = await api(`/api/people/${id}`);
  const wrap = el("div", "person-title");
  if (person.face_id) wrap.append(img(faceUrl(person.face_id), "", "avatar small"));
  const input = el("input", "name-input");
  input.value = person.name || "";
  input.placeholder = "Add a name";
  input.setAttribute("aria-label", "Name");
  input.maxLength = 200;
  const save = async () => {
    const name = input.value.trim();
    if ((person.name || "") === name) return;
    try {
      // Same name as someone already listed? They are probably the same person.
      if (name) {
        const all = (await api("/api/people")).people;
        const twin = all.find((p) => p.id !== person.id && (p.name || "").toLowerCase() === name.toLowerCase());
        if (twin) {
          const merge = await confirmBox(`Is this ${twin.name}?`,
            `There is already someone called ${twin.name}. Put these photos together with theirs?`, "Yes, merge");
          if (merge) {
            await post(`/api/people/${person.id}/merge`, { into: twin.id });
            location.hash = `#/people/${twin.id}`;
            return;
          }
        }
      }
      const updated = await patch(`/api/people/${person.id}`, name ? { name } : { clear_name: true });
      person.name = updated.name;
      flashNote(name ? `Named ${name}` : "Name removed");
    } catch (err) { flashNote(err.message); }
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); input.blur(); } });
  input.addEventListener("change", save);
  wrap.append(input);
  headRow(wrap, plural(person.photos, "photo"), [
    button("Same person as…", "btn small", () => mergePerson(person)),
    button("Smart album", "btn small", () => smartAlbumDialog(null, { people: [person.id] }, person.name || "")),
    button(person.hidden ? "Show in People" : "Hide", "btn small", async () => {
      try {
        await patch(`/api/people/${person.id}`, { hidden: !person.hidden });
        if (!person.hidden) location.hash = "#/people"; else renderHead();
      } catch (err) { flashNote(err.message); }
    }),
  ]);
  if (!person.hidden) {
    suggestionStrip(person.id).then((strip) => {
      if (strip && S.route.person === id) $("page-head").append(strip);
    });
  }
}

async function mergePerson(person) {
  const all = (await api("/api/people")).people.filter((p) => p.id !== person.id);
  if (!all.length) { flashNote("There is nobody else to merge with yet."); return; }
  const target = await modal({
    title: "Which person is this the same as?", ok: null, cancel: "Cancel",
    build: (body, ctl) => {
      body.append(el("p", "muted", "Their photos will be put together. Faces found later will join them too."));
      const list = el("div", "pick-list");
      for (const p of all) {
        const b = button("", "pick", () => ctl.close(p));
        if (p.face_id) b.append(img(faceUrl(p.face_id), "", "avatar small")); else b.append(el("span", "pick-ph"));
        const t = el("span", null, p.name || "Unnamed");
        t.append(el("small", null, plural(p.photos, "photo")));
        b.append(t);
        list.append(b);
      }
      body.append(list);
    },
  });
  if (!target) return;
  try {
    await post(`/api/people/${person.id}/merge`, { into: target.id });
    location.hash = `#/people/${target.id}`;
  } catch (err) { flashNote(err.message); }
}

// ------------------------------------------------------------------ the photo grid

function filterParams(extra) {
  const p = new URLSearchParams(extra);
  for (const [k, v] of Object.entries(S.route.filter || {})) {
    // A search also finds archived photos, and each photo of a stack on its own.
    if (S.q && (k === "archived" || k === "stacks")) continue;
    p.set(k, v);
  }
  if (S.q) p.set("q", S.q);
  if (S.marks.rating) p.set("rating", S.marks.rating);
  if (S.marks.label) p.set("label", S.marks.label);
  if (S.marks.flag) p.set("flag", S.marks.flag);
  return p;
}

const HEART = '<svg class="heart" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20.5s-7.5-4.6-9.3-9.4C1.4 7.6 3.6 4 7.2 4c2 0 3.6 1.1 4.8 2.8C13.2 5.1 14.8 4 16.8 4c3.6 0 5.8 3.6 4.5 7.1C19.5 15.9 12 20.5 12 20.5z"/></svg>';

function tile(item, index) {
  const b = el("button", "tile");
  b.type = "button";
  b.dataset.index = index;
  b.dataset.id = item.id;
  b.setAttribute("aria-label", `${item.filename}, ${fmtDateTime(item.taken_at)}`);
  if (item.has_thumb) {
    const i = img(thumbUrl(item.id, item.v));
    i.addEventListener("error", () => { i.remove(); b.prepend(el("span", "ph", ext(item.filename))); });
    b.append(i);
  } else {
    b.append(el("span", "ph", ext(item.filename)));
  }
  const badges = [];
  if (item.kind === "video") badges.push("Video");
  if (item.live) badges.push("Live");
  if (item.raw && !item.stack) badges.push("RAW");
  if (item.projection === "sphere") badges.push("360°");
  else if (item.projection === "pano") badges.push("Panorama");
  if (item.stack && item.stack_count > 1) badges.push(`${item.stack_count} in stack`);
  if (badges.length) {
    const row = el("span", "badges");
    badges.forEach((t) => row.append(el("span", "badge", t)));
    b.append(row);
  }
  if (item.stack && item.stack_count > 1) b.classList.add("stacked");
  if (item.favorite) b.insertAdjacentHTML("beforeend", HEART);
  tileShape(b, item);
  tileMarks(b, item);
  b.append(el("span", "check"));
  if (S.selected.has(item.id)) { b.classList.add("selected"); b.setAttribute("aria-pressed", "true"); }
  return b;
}

function monthHeader(month) {
  const h = el("h2", "month", monthLabel(month));
  h.dataset.month = month;
  if (S.counts[month]) h.append(el("small", null, plural(S.counts[month], "item")));
  return h;
}

function appendItems(items) {
  const frag = document.createDocumentFragment();
  for (const item of items) {
    const month = item.taken_at.slice(0, 7);
    if (!S.look && month !== S.lastMonth) {
      // Ends the last row of the month before (for photos shown in their own shape).
      if (S.lastMonth) frag.append(el("span", "row-end"));
      S.lastMonth = month;
      frag.append(monthHeader(month));
    }
    frag.append(tile(item, S.items.length));
    S.items.push(item);
  }
  $("grid").append(frag);
}

async function loadMore() {
  if (S.loading || S.done || !S.route || S.route.view !== "grid") return;
  S.loading = true;
  const gen = S.gen;
  try {
    const params = filterParams({ limit: PAGE });
    if (S.look) params.set("look", "true");
    if (S.cursor) params.set("cursor", S.cursor);
    const page = await api(`/api/assets?${params}`);
    if (gen !== S.gen) return;
    appendItems(page.items);
    S.cursor = page.next;
    S.done = !page.next;
  } catch (err) {
    if (gen === S.gen) flashNote(`Could not load photos: ${err.message}`);
  } finally {
    if (gen === S.gen) { S.loading = false; S.loaded = true; updateEmpty(); }
  }
  if (gen === S.gen && !S.done && sentinelVisible()) loadMore();
}

function sentinelVisible() {
  const s = $("sentinel").getBoundingClientRect();
  const c = $("scroller").getBoundingClientRect();
  return s.top < c.bottom + 600;
}

async function reload() {
  S.gen++;
  S.cursor = null; S.done = false; S.loading = false; S.loaded = false;
  S.items = []; S.lastMonth = null;
  $("grid").replaceChildren();
  $("new-items").hidden = true;
  if (S.look) S.counts = {};
  else {
    try {
      const t = await api(`/api/timeline?${filterParams()}`);
      S.counts = Object.fromEntries(t.months.map((m) => [m.month, m.count]));
    } catch (_) { S.counts = {}; }
  }
  S.jumpedTo = null;
  $("jumped").hidden = true;
  buildScrubber();
  await loadMore();
  updateScrubPos();
}

/** Take items out of the grid in place (keeps the scroll position). */
function removeItems(ids) {
  const gone = new Set(ids);
  for (const item of S.items) {
    if (gone.has(item.id)) {
      const m = item.taken_at.slice(0, 7);
      if (S.counts[m]) S.counts[m] -= 1;
    }
  }
  S.items = S.items.filter((it) => !gone.has(it.id));
  $("grid").querySelectorAll(".tile").forEach((t) => { if (gone.has(+t.dataset.id)) t.remove(); });
  $("grid").querySelectorAll(".tile").forEach((t, i) => { t.dataset.index = i; });
  $("grid").querySelectorAll(".month").forEach((h) => {
    const between = [];
    for (let n = h.nextElementSibling; n && !n.classList.contains("month"); n = n.nextElementSibling) between.push(n);
    if (!between.some((n) => n.classList.contains("tile"))) { between.forEach((n) => n.remove()); h.remove(); return; }
    const small = h.querySelector("small");
    const n = S.counts[h.dataset.month];
    if (small && n) small.textContent = plural(n, "item");
  });
  S.lastMonth = S.items.length ? S.items[S.items.length - 1].taken_at.slice(0, 7) : null;
  for (const id of ids) S.selected.delete(id);
  updateSelection();
  updateEmpty();
}

function setFavoriteInGrid(ids, value) {
  const set = new Set(ids);
  for (const item of S.items) if (set.has(item.id)) item.favorite = value;
  $("grid").querySelectorAll(".tile").forEach((t) => {
    if (!set.has(+t.dataset.id)) return;
    const h = t.querySelector(".heart");
    if (value && !h) t.insertAdjacentHTML("beforeend", HEART);
    if (!value && h) h.remove();
  });
}

function showEmpty(title, text) {
  $("empty-title").textContent = title;
  $("empty-text").textContent = text;
  $("empty").hidden = false;
}

function updateEmpty() {
  if (!S.route || S.route.view !== "grid") return;
  if (S.items.length > 0 || !S.loaded) { $("empty").hidden = true; return; }
  const st = S.status;
  const r = S.route;
  if (S.q && S.look) showEmpty("Nothing looks like that", S.search && !S.search.content_ready
    ? "Search by what's in the photo is not ready yet." : "Try other words, like “dog”, “snow” or “birthday cake”.");
  else if (S.q) showEmpty("No matches", S.search && S.search.content
    ? "No names, places or dates match. Try “What's in the photo” above." : "Try a different word, or clear the search.");
  else if (r.quality) showEmpty("Nothing to tidy up", "No blurry or dark photos were found.");
  else if (r.locked) showEmpty("Your locked folder is empty", "Select photos anywhere in the library and press Move to Locked.");
  else if (r.archive) showEmpty("Nothing archived", "Archive screenshots, receipts and other photos you want to keep but not see in the timeline. Select them and press Archive.");
  else if (r.tag) showEmpty("Nothing with this tag", "Select photos anywhere in the library and press Tag.");
  else if (r.folder !== undefined) {
    if (S.folderInfo && S.folderInfo.folders.length) { $("empty").hidden = true; return; }
    showEmpty("Nothing in this folder", "");
  }
  else if (r.memory) showEmpty("No photos", "They may have been archived or moved to the trash.");
  else if (r.area) showEmpty("No photos here", "Go back to the map and try a bigger area.");
  else if (r.event) showEmpty("No photos", "The photos of this event may all be in the trash.");
  else if (r.name === "trash") showEmpty("The trash is empty", "Photos you move to the trash appear here until the trash is emptied.");
  else if (r.name === "favorites") showEmpty("No favourites yet", "Open a photo and press Favourite, or select several and press Favourite.");
  else if (r.album) showEmpty("This album is empty", "Select photos anywhere in the library and choose Add to album.");
  else if (r.person) showEmpty("No photos", "This person's photos may all be in the trash.");
  else if (r.place) showEmpty("No photos here", "");
  else if (st && !st.library_ok) {
    showEmpty("Library folder not found", st.library_path
      ? `${st.library_path} is not available. Is the drive connected?`
      : "Start Snapshots with --library \"D:\\Photos\" to choose a folder.");
  } else if (st && st.scan.running) showEmpty("Indexing your library…", "Photos will appear here as they are found.");
  else if (r.kind === "video" || (r.filter && r.filter.kind === "video")) showEmpty("No videos", "");
  else showEmpty("No photos yet", "Put photos in your library folder and press Rescan, or drag files onto this window to upload them.");
}

new IntersectionObserver((entries) => {
  if (entries.some((e) => e.isIntersecting)) loadMore();
}, { root: $("scroller"), rootMargin: "600px" }).observe($("sentinel"));

// ------------------------------------------------------------------ selecting

function startSelecting() {
  S.selecting = true;
  document.body.classList.add("selecting-items");
  $("grid").classList.add("selecting");
  $("select-btn").setAttribute("aria-pressed", "true");
  updateSelection();
}

function stopSelecting() {
  S.selecting = false;
  document.body.classList.remove("selecting-items");
  S.selected.clear();
  S.lastClicked = -1;
  $("grid").classList.remove("selecting");
  $("select-btn").setAttribute("aria-pressed", "false");
  $("grid").querySelectorAll(".tile.selected").forEach((t) => { t.classList.remove("selected"); t.removeAttribute("aria-pressed"); });
  $("actionbar").hidden = true;
}

function toggleTile(index, range) {
  const ids = [];
  if (range && S.lastClicked >= 0 && S.lastClicked < S.items.length) {
    const [a, b] = [Math.min(S.lastClicked, index), Math.max(S.lastClicked, index)];
    for (let i = a; i <= b; i++) ids.push(S.items[i].id);
    ids.forEach((id) => S.selected.add(id));
  } else {
    const id = S.items[index].id;
    if (S.selected.has(id)) S.selected.delete(id); else S.selected.add(id);
  }
  S.lastClicked = index;
  $("grid").querySelectorAll(".tile").forEach((t) => {
    const on = S.selected.has(+t.dataset.id);
    t.classList.toggle("selected", on);
    if (on) t.setAttribute("aria-pressed", "true"); else t.removeAttribute("aria-pressed");
  });
  updateSelection();
}

function updateSelection() {
  if (!S.selecting) { $("actionbar").hidden = true; return; }
  const n = S.selected.size;
  $("actionbar").hidden = false;
  $("sel-count").textContent = n ? `${plural(n, "item")} selected` : "Click photos to select them (Shift-click for a range)";
  const actions = $("sel-actions");
  actions.replaceChildren();
  if (!n) return;
  const ids = () => [...S.selected];
  const r = S.route;
  if (r.locked) {
    actions.append(button("Move out of Locked", "btn", () => setLocked(ids(), false)));
    return;
  }
  if (r.name === "trash") {
    actions.append(button("Restore", "btn", () => restore(ids())));
    if (S.me && S.me.is_admin) actions.append(button("Delete permanently", "btn danger", () => deleteForever(ids())));
    return;
  }
  if (r.quality) {
    actions.append(button("Looks fine", "btn", async () => {
      const chosen = ids();
      try { await post("/api/cleanup/fine", { ids: chosen }); removeItems(chosen); flashNote(`${plural(chosen.length, "photo")} kept`); }
      catch (err) { flashNote(err.message); }
    }));
  }
  const chosen = () => ids().map((id) => S.items.find((it) => it.id === id) || {});
  const allFav = chosen().every((it) => it.favorite);
  actions.append(button(allFav ? "Unfavourite" : "Favourite", "btn", () => setFavorite(ids(), !allFav)));
  actions.append(button("Add to album", "btn", () => addToAlbum(ids())));
  if (r.album && !(S.album && S.album.smart)) actions.append(button("Remove from album", "btn", () => removeFromAlbum(ids())));
  actions.append(button("Tag", "btn", () => addTag(ids())));
  if (r.tag) actions.append(button("Remove tag", "btn", () => removeTag(ids())));
  const allArchived = chosen().every((it) => it.archived);
  actions.append(button(allArchived ? "Unarchive" : "Archive", "btn", () => setArchived(ids(), !allArchived)));
  if (chosen().some((it) => it.stack)) actions.append(button("Unstack", "btn", () => unstackPhotos(ids())));
  else if (n >= 2) actions.append(button("Stack", "btn", () => stackPhotos(ids())));
  actions.append(button("Stars & labels", "btn", () => marksDialog(ids())));
  const photosChosen = () => chosen().filter((it) => it.id && it.kind === "photo");
  if (n >= 2 && n <= 4) actions.append(button("Compare", "btn", () => {
    const p = photosChosen();
    if (p.length < 2) { flashNote("Compare works with photos: choose 2 to 4 photos."); return; }
    compareItems(p);
  }));
  if (n >= 2) actions.append(button("Cull", "btn", () => {
    const p = photosChosen();
    if (!p.length) { flashNote("Culling works with photos."); return; }
    if (p.length < chosen().length) flashNote("Videos are left out of culling.");
    cullItems(p);
  }));
  actions.append(button("Edit details", "btn", () => editDetails(ids())));
  actions.append(button("Export", "btn", () => exportDialog({ ids: ids(), count: n })));
  actions.append(button("Move to Locked", "btn", () => setLocked(ids(), true)));
  actions.append(button("Move to trash", "btn", () => moveToTrash(ids())));
}

$("select-btn").addEventListener("click", () => { if (S.selecting) stopSelecting(); else startSelecting(); });
$("sel-cancel").addEventListener("click", stopSelecting);


// ------------------------------------------------------------------ stars, colour labels, pick and reject

const LABELS = ["red", "yellow", "green", "blue", "purple"];
const LABEL_NAMES = { red: "Red", yellow: "Yellow", green: "Green", blue: "Blue", purple: "Purple" };

/** The small marks on a tile: stars, a colour dot, pick or reject. */
function tileMarks(b, item) {
  b.querySelectorAll(".marks").forEach((m) => m.remove());
  b.classList.toggle("rejected", item.flag === -1);
  if (!item.rating && !item.label && !item.flag) return;
  const m = el("span", "marks");
  if (item.flag === 1) m.append(el("span", "mark-pick", "✓"));
  if (item.flag === -1) m.append(el("span", "mark-reject", "✕"));
  if (item.rating) m.append(el("span", "mark-stars", "★".repeat(item.rating)));
  if (item.label) {
    const d = el("span", `mark-dot label-${item.label}`);
    d.title = `${LABEL_NAMES[item.label]} label`;
    m.append(d);
  }
  b.append(m);
}

function marksText(item) {
  const parts = [];
  if (item.rating) parts.push(plural(item.rating, "star"));
  if (item.label) parts.push(`${LABEL_NAMES[item.label]} label`);
  if (item.flag === 1) parts.push("Pick");
  if (item.flag === -1) parts.push("Reject");
  return parts.join(" · ");
}

/** Change stars (rating), label ("" removes it) or flag (1, -1, 0) of some items, everywhere they show. */
async function setMarks(ids, change, quiet = false) {
  try {
    await post("/api/marks", { ids, ...change });
  } catch (err) { flashNote(err.message); return false; }
  const set = new Set(ids);
  const apply = (it) => {
    if (change.rating !== undefined) it.rating = change.rating;
    if (change.label !== undefined) it.label = change.label || null;
    if (change.flag !== undefined) it.flag = change.flag;
  };
  for (const it of S.items) if (set.has(it.id)) apply(it);
  $("grid").querySelectorAll(".tile").forEach((t) => {
    const it = S.items.find((x) => x.id === +t.dataset.id);
    if (it && set.has(it.id)) tileMarks(t, it);
  });
  if (S.detail && set.has(S.detail.id)) { apply(S.detail); renderMarksBar(S.detail); }
  if (!quiet) {
    const what = change.rating !== undefined ? (change.rating ? plural(change.rating, "star") : "No stars")
      : change.label !== undefined ? (change.label ? `${LABEL_NAMES[change.label]} label` : "Label removed")
      : change.flag === 1 ? "Pick" : change.flag === -1 ? "Reject" : "Pick or reject removed";
    flashNote(ids.length > 1 ? `${what}: ${plural(ids.length, "item")}` : what);
  }
  return true;
}

/** The change a key stands for, Lightroom style: 0-5 stars, 6-9 labels, P pick, X reject, U neither. */
function markForKey(e, item) {
  if (e.ctrlKey || e.metaKey || e.altKey) return null;
  const k = e.key.toLowerCase();
  if (/^[0-5]$/.test(k)) return { rating: +k };
  if (/^[6-9]$/.test(k)) {
    const label = LABELS[+k - 6];
    return { label: item && item.label === label ? "" : label };   // the same key again removes it
  }
  if (k === "p") return { flag: 1 };
  if (k === "x") return { flag: -1 };
  if (k === "u") return { flag: 0 };
  return null;
}

/** Stars, labels and pick/reject under the photo in the viewer. */
function renderMarksBar(item, bar = $("v-marks"), after = null) {
  if (!item || item.locked || S.route.name === "trash") { bar.hidden = true; return; }
  const set = (change) => setMarks([item.id], change, true).then((ok) => { if (ok && after) after(); });
  bar.hidden = false;
  bar.replaceChildren();
  const stars = el("span", "mk-stars");
  stars.setAttribute("role", "group");
  stars.setAttribute("aria-label", "Stars");
  for (let n = 1; n <= 5; n++) {
    const b = button("★", "mk-star" + (n <= (item.rating || 0) ? " on" : ""), () => set({ rating: item.rating === n ? 0 : n }));
    b.title = `${plural(n, "star")} (key ${n}${item.rating === n ? "; again for none" : ""})`;
    b.setAttribute("aria-pressed", String(n <= (item.rating || 0)));
    stars.append(b);
  }
  const labels = el("span", "mk-labels");
  labels.setAttribute("role", "group");
  labels.setAttribute("aria-label", "Colour label");
  LABELS.forEach((l, i) => {
    const b = button("", `mk-label label-${l}` + (item.label === l ? " on" : ""), () => set({ label: item.label === l ? "" : l }));
    b.title = `${LABEL_NAMES[l]}${i < 4 ? ` (key ${i + 6})` : ""}`;
    b.setAttribute("aria-label", `${LABEL_NAMES[l]} label`);
    b.setAttribute("aria-pressed", String(item.label === l));
    labels.append(b);
  });
  const flags = el("span", "mk-flags");
  const pick = button("✓", "mk-flag pick" + (item.flag === 1 ? " on" : ""), () => set({ flag: item.flag === 1 ? 0 : 1 }));
  pick.append(el("span", "mk-word", " Pick"));
  pick.title = "Pick (key P)";
  pick.setAttribute("aria-label", "Pick");
  pick.setAttribute("aria-pressed", String(item.flag === 1));
  const rej = button("✕", "mk-flag reject" + (item.flag === -1 ? " on" : ""), () => set({ flag: item.flag === -1 ? 0 : -1 }));
  rej.append(el("span", "mk-word", " Reject"));
  rej.title = "Reject (key X)";
  rej.setAttribute("aria-label", "Reject");
  rej.setAttribute("aria-pressed", String(item.flag === -1));
  flags.append(pick, rej);
  bar.append(stars, labels, flags);
}

/** Choose stars, a label or pick/reject for the selected items. */
async function marksDialog(ids) {
  await modal({
    title: `Stars and labels for ${plural(ids.length, "item")}`, ok: null, cancel: "Close",
    build: (body, ctl) => {
      body.append(el("p", "muted small", "Tip: with items selected, press 0 to 5 for stars, 6 to 9 for a colour label, P to pick, X to reject."));
      const row = (label, buttons) => { const r = el("div", "mk-row"); r.append(el("span", "mk-name", label), ...buttons); body.append(r); };
      const done = async (change) => { if (await setMarks(ids, change)) ctl.close(true); };
      row("Stars", [0, 1, 2, 3, 4, 5].map((n) => button(n ? "★".repeat(n) : "None", "btn small", () => done({ rating: n }))));
      row("Label", [...LABELS.map((l) => {
        const b = button("", `mk-label label-${l}`, () => done({ label: l }));
        b.setAttribute("aria-label", `${LABEL_NAMES[l]} label`);
        b.title = LABEL_NAMES[l];
        return b;
      }), button("None", "btn small", () => done({ label: "" }))]);
      row("Flag", [button("✓ Pick", "btn small", () => done({ flag: 1 })), button("✕ Reject", "btn small", () => done({ flag: -1 })),
        button("Neither", "btn small", () => done({ flag: 0 }))]);
    },
  });
}

/** The filter by stars, label and pick/reject, in the header. */
function renderMarksFilter() {
  const btn = $("marks-btn");
  const f = S.marks;
  const parts = [];
  if (f.rating) parts.push(`★${f.rating}+`);
  if (f.label) parts.push(LABEL_NAMES[f.label]);
  if (f.flag) parts.push({ pick: "Picks", reject: "Rejects", unflagged: "Unflagged" }[f.flag]);
  btn.textContent = parts.length ? `Filter: ${parts.join(", ")}` : "Filter";
  btn.classList.toggle("active", parts.length > 0);
  btn.setAttribute("aria-expanded", String(!$("marks-pop").hidden));
  const pop = $("marks-pop");
  pop.replaceChildren();
  const group = (title, options, key) => {
    const g = el("div", "mk-row");
    g.append(el("span", "mk-name", title));
    for (const [value, text, cls] of options) {
      const b = button(text, cls || "chip", () => {
        S.marks[key] = S.marks[key] === value ? (key === "rating" ? 0 : "") : value;
        renderMarksFilter();
        if (S.route.view === "grid") { renderHead(); reload(); }
      });
      b.setAttribute("aria-pressed", String(S.marks[key] === value));
      if (cls) { b.setAttribute("aria-label", `${LABEL_NAMES[value]} label`); b.title = LABEL_NAMES[value]; }
      g.append(b);
    }
    pop.append(g);
  };
  group("Stars", [[1, "★1+"], [2, "★2+"], [3, "★3+"], [4, "★4+"], [5, "★5"]], "rating");
  group("Label", LABELS.map((l) => [l, "", `mk-label label-${l}`]), "label");
  group("Flag", [["pick", "Picks"], ["reject", "Rejects"], ["unflagged", "Neither"]], "flag");
  const clear = button("Show everything", "btn small", () => {
    S.marks = { rating: 0, label: "", flag: "" };
    renderMarksFilter();
    if (S.route.view === "grid") { renderHead(); reload(); }
  });
  clear.disabled = !parts.length;
  pop.append(clear);
}
$("marks-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  $("marks-pop").hidden = !$("marks-pop").hidden;
  renderMarksFilter();
});
document.addEventListener("click", (e) => {
  const pop = $("marks-pop");
  if (!pop.hidden && !pop.contains(e.target) && e.target !== $("marks-btn")) { pop.hidden = true; renderMarksFilter(); }
});

// With items selected in a grid, the same keys as in the viewer change all of them.
document.addEventListener("keydown", (e) => {
  if (!S.selecting || !S.selected.size || document.querySelector("dialog[open]")) return;
  if (e.target.closest && e.target.closest("input, textarea, select")) return;
  const ids = [...S.selected];
  const first = S.items.find((it) => it.id === ids[0]);
  const change = markForKey(e, first);
  if (!change) return;
  e.preventDefault();
  if (change.label !== undefined && ids.length > 1 && first && first.label === change.label) change.label = "";
  setMarks(ids, change);
});


// ------------------------------------------------------------------ compare and cull

const viewUrl = (it) => `/api/assets/${it.id}/view?v=${it.v || ""}`;

/** Two to four photos side by side, zoomed and moved together. */
function compareItems(items) {
  const dlg = $("compare");
  const panes = $("cmp-panes");
  panes.replaceChildren();
  panes.dataset.n = items.length;
  const view = { scale: 1, x: 0, y: 0 };
  let active = 0;
  const imgs = [];
  const apply = () => imgs.forEach((i) => { i.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`; });
  const paneOf = [];
  const drawInfo = (k) => {
    const it = items[k];
    const info = paneOf[k].querySelector(".cmp-info");
    info.replaceChildren();
    info.append(el("strong", null, it.filename),
      el("span", null, [it.width && it.height ? `${it.width} × ${it.height}` : null, fmtSize(it.size)].filter(Boolean).join(" · ")));
    const marks = marksText(it);
    if (marks) info.append(el("span", "cmp-marks", marks));
    const row = el("span", "cmp-actions");
    row.append(
      button("✓ Pick", "btn small" + (it.flag === 1 ? " primary" : ""), async (e) => { e.stopPropagation(); if (await setMarks([it.id], { flag: it.flag === 1 ? 0 : 1 }, true)) { it.flag = it.flag === 1 ? 0 : 1; drawInfo(k); } }),
      button("Keep only this", "btn small", async (e) => {
        e.stopPropagation();
        const others = items.filter((x) => x.id !== it.id).map((x) => x.id);
        if (!window.confirm(`Move the other ${plural(others.length, "photo")} to the trash?`)) return;
        await moveToTrash(others);
        dlg.close();
      }),
    );
    info.append(row);
  };
  const setActive = (k) => {
    active = k;
    paneOf.forEach((p, i) => p.classList.toggle("active", i === k));
  };
  items.forEach((it, k) => {
    const pane = el("div", "cmp-pane");
    const box = el("div", "cmp-img");
    const i = new Image();
    i.src = viewUrl(it);
    i.alt = it.filename;
    i.draggable = false;
    imgs.push(i);
    box.append(i);
    pane.append(box, el("div", "cmp-info"));
    pane.addEventListener("pointerdown", () => setActive(k));
    paneOf.push(pane);
    panes.append(pane);
    drawInfo(k);
  });
  setActive(0);
  // One zoom and one position for all panes.
  panes.onwheel = (e) => {
    e.preventDefault();
    const old = view.scale;
    view.scale = Math.min(8, Math.max(1, view.scale * (e.deltaY < 0 ? 1.2 : 1 / 1.2)));
    if (view.scale === 1) { view.x = 0; view.y = 0; }
    else { view.x *= view.scale / old; view.y *= view.scale / old; }
    apply();
  };
  let drag = null;
  panes.onpointerdown = (e) => { if (view.scale > 1) { drag = { x: e.clientX - view.x, y: e.clientY - view.y }; panes.setPointerCapture(e.pointerId); } };
  panes.onpointermove = (e) => { if (drag) { view.x = e.clientX - drag.x; view.y = e.clientY - drag.y; apply(); } };
  panes.onpointerup = () => { drag = null; };
  $("cmp-fit").onclick = () => { view.scale = 1; view.x = 0; view.y = 0; apply(); };
  $("cmp-close").onclick = () => dlg.close();
  dlg.onkeydown = async (e) => {
    if (/^[1-4]$/.test(e.key) && e.altKey) { setActive(Math.min(items.length - 1, +e.key - 1)); return; }
    const it = items[active];
    const change = markForKey(e, it);
    if (change) {
      e.preventDefault();
      if (await setMarks([it.id], change)) { Object.assign(it, change.label !== undefined ? { label: change.label || null } : change); drawInfo(active); }
    } else if (e.key === "ArrowRight" || e.key === "Tab") { e.preventDefault(); setActive((active + 1) % items.length); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); setActive((active + items.length - 1) % items.length); }
  };
  apply();
  dlg.showModal();
}

/** Go through photos one at a time, picking and rejecting with the keyboard. */
function cullItems(items) {
  if (!items.length) return;
  const dlg = $("cull");
  let at = 0;
  let auto = true;
  const strip = $("cull-strip");
  strip.replaceChildren();
  const thumbs = items.map((it, k) => {
    const b = el("button", "cull-thumb");
    b.type = "button";
    b.setAttribute("role", "option");
    b.append(img(thumbUrl(it.id, it.v), it.filename));
    b.addEventListener("click", () => show(k));
    strip.append(b);
    return b;
  });
  const paintThumb = (k) => {
    const it = items[k];
    const b = thumbs[k];
    b.classList.toggle("pick", it.flag === 1);
    b.classList.toggle("reject", it.flag === -1);
    b.setAttribute("aria-selected", String(k === at));
    tileMarks(b, it);
  };
  const tally = () => {
    const picks = items.filter((it) => it.flag === 1).length, rejects = items.filter((it) => it.flag === -1).length;
    $("cull-tally").textContent = `${at + 1} of ${items.length} · ${plural(picks, "pick")} · ${plural(rejects, "reject")}`;
  };
  const show = (k) => {
    at = Math.max(0, Math.min(items.length - 1, k));
    const it = items[at];
    const stage = $("cull-stage");
    const i = new Image();
    i.src = viewUrl(it);
    i.alt = it.filename;
    stage.replaceChildren(i);
    if (items[at + 1]) { const pre = new Image(); pre.src = viewUrl(items[at + 1]); }
    $("cull-title").textContent = it.filename;
    thumbs.forEach((_, n) => paintThumb(n));
    thumbs[at].scrollIntoView({ block: "nearest", inline: "center" });
    drawBar();
    tally();
  };
  const drawBar = () => renderMarksBar(items[at], $("cull-marks"), () => { paintThumb(at); drawBar(); tally(); });
  const mark = async (change) => {
    const it = items[at];
    if (!(await setMarks([it.id], change, true))) return;
    if (change.rating !== undefined) it.rating = change.rating;
    if (change.label !== undefined) it.label = change.label || null;
    if (change.flag !== undefined) it.flag = change.flag;
    paintThumb(at);
    drawBar();
    tally();
    if (auto && change.flag !== undefined && change.flag !== 0 && at < items.length - 1) show(at + 1);
  };
  dlg.onkeydown = (e) => {
    if (e.key === "ArrowRight") { e.preventDefault(); show(at + 1); return; }
    if (e.key === "ArrowLeft") { e.preventDefault(); show(at - 1); return; }
    const change = markForKey(e, items[at]);
    if (change) { e.preventDefault(); mark(change); }
  };
  $("cull-auto").onclick = () => { auto = !auto; $("cull-auto").setAttribute("aria-pressed", String(auto)); };
  $("cull-done").onclick = async () => {
    dlg.close();
    const rejects = items.filter((it) => it.flag === -1).map((it) => it.id);
    const picks = items.filter((it) => it.flag === 1).map((it) => it.id);
    if (!rejects.length && !picks.length) return;
    await modal({
      title: "Done culling", ok: null, cancel: "Close",
      build: (body, ctl) => {
        body.append(el("p", null, `${plural(picks.length, "pick")} and ${plural(rejects.length, "reject")}.`));
        const row = el("div", "row-actions");
        if (rejects.length) row.append(button(`Move the ${plural(rejects.length, "reject")} to the trash`, "btn danger", async () => {
          await moveToTrash(rejects); ctl.close(true);
        }));
        if (picks.length && S.route.view === "grid") row.append(button("Select the picks", "btn", () => {
          if (!S.selecting) startSelecting();
          S.selected = new Set(picks);
          $("grid").querySelectorAll(".tile").forEach((t) => {
            const on = S.selected.has(+t.dataset.id);
            t.classList.toggle("selected", on);
            if (on) t.setAttribute("aria-pressed", "true"); else t.removeAttribute("aria-pressed");
          });
          updateSelection();
          ctl.close(true);
        }));
        body.append(row, el("p", "muted small", "Rejects stay in the library until you move them to the trash. To see them again later, use Filter, Rejects."));
      },
    });
  };
  dlg.showModal();
  show(0);
}

// ------------------------------------------------------------------ actions on photos

async function setFavorite(ids, value) {
  try {
    await post("/api/favorites", { ids, value });
    setFavoriteInGrid(ids, value);
    if (S.route.name === "favorites" && !value) removeItems(ids);
    if (S.detail && ids.includes(S.detail.id)) { S.detail.favorite = value; showFavorite(value); }
    updateSelection();
  } catch (err) { flashNote(err.message); }
}

async function moveToTrash(ids) {
  try {
    await post("/api/trash", { ids });
    removeItems(ids);
    flashNote(`${plural(ids.length, "item")} moved to the trash`);
    refreshStatus();
  } catch (err) { flashNote(err.message); }
}

async function restore(ids) {
  try {
    await post("/api/trash/restore", { ids });
    removeItems(ids);
    flashNote(`${plural(ids.length, "item")} restored`);
    refreshStatus();
  } catch (err) { flashNote(err.message); }
}

async function deleteForever(ids) {
  const count = ids ? ids.length : (S.status ? S.status.trashed : 0);
  if (!count) { flashNote("The trash is already empty."); return; }
  const ok = await confirmBox(
    ids ? `Delete ${plural(count, "item")} permanently?` : "Empty the trash?",
    `${plural(count, "file")} will be deleted from your disk. This cannot be undone.`,
    "Delete permanently", true);
  if (!ok) return;
  try {
    const res = await post("/api/trash/empty", ids ? { ids } : {});
    if (ids) removeItems(ids.filter((id) => !res.failed.some((f) => f.id === id)));
    else await reload();
    let msg = `${plural(res.deleted, "file")} deleted`;
    if (res.failed.length) msg += `; ${res.failed.length} could not be deleted (${res.failed[0].reason})`;
    flashNote(msg);
    refreshStatus();
  } catch (err) { flashNote(err.message); }
}

async function pickAlbum(count) {
  const albums = (await api("/api/albums")).albums.filter((a) => !a.smart);
  return modal({
    title: `Add ${plural(count, "item")} to an album`, ok: "Create album", cancel: "Cancel",
    build: (body, ctl) => {
      if (albums.length) {
        const list = el("div", "pick-list");
        for (const a of albums) {
          const b = button("", "pick", () => ctl.close({ id: a.id, name: a.name }));
          if (a.cover) b.append(img(thumbUrl(a.cover, a.cover_v))); else b.append(el("span", "pick-ph"));
          const t = el("span", null, a.name);
          t.append(el("small", null, plural(a.count, "item")));
          b.append(t);
          list.append(b);
        }
        body.append(list);
      }
      const [wrap, input] = field(albums.length ? "Or make a new album" : "Name for the new album",
                                  { placeholder: "Album name", maxLength: 200 });
      body.append(wrap);
      return { submit: async () => {
        const name = input.value.trim();
        if (!name) throw new Error(albums.length ? "Pick an album, or type a name for a new one." : "Please type a name.");
        return { name, new: true };
      } };
    },
  });
}

async function addToAlbum(ids) {
  try {
    const choice = await pickAlbum(ids.length);
    if (!choice) return;
    let album;
    if (choice.new) album = await post("/api/albums", { name: choice.name, ids });
    else album = await post(`/api/albums/${choice.id}/add`, { ids });
    flashNote(`Added to ${album.name}`);
    if (S.selecting) stopSelecting();
    if (S.detail && ids.includes(S.detail.id)) loadDetail(S.detail.id);
  } catch (err) { flashNote(err.message); }
}

async function removeFromAlbum(ids) {
  try {
    await post(`/api/albums/${S.route.album}/remove`, { ids });
    removeItems(ids);
    renderHead();
  } catch (err) { flashNote(err.message); }
}

/** Archived photos leave the timelines. In the Archive, unarchiving takes them out of view. */
async function setArchived(ids, value) {
  try {
    await post("/api/archive", { ids, value });
    for (const it of S.items) if (ids.includes(it.id)) it.archived = value;
    const leaves = (S.route.filter || {}).archived === (value ? "hide" : "only");
    if (leaves) removeItems(ids);
    if (S.detail && ids.includes(S.detail.id)) { S.detail.archived = value; showArchived(value); }
    flashNote(value ? `${plural(ids.length, "item")} archived` : `${plural(ids.length, "item")} back in the timeline`);
    updateSelection();
  } catch (err) { flashNote(err.message); }
}

async function pickTag(count) {
  const { tags } = await api("/api/tags");
  return modal({
    title: `Tag ${plural(count, "item")}`, ok: "Add tag", cancel: "Cancel",
    build: (body, ctl) => {
      const [wrap, input] = field("Tag", { placeholder: "Type a tag, like “Birthday”", maxLength: 100 });
      input.setAttribute("list", "tag-names");
      const list = el("datalist");
      list.id = "tag-names";
      tags.forEach((t) => { const o = el("option"); o.value = t.name; list.append(o); });
      body.append(wrap, list);
      if (tags.length) {
        const chips = el("div", "tag-chips");
        tags.slice(0, 30).forEach((t) => chips.append(button(t.name, "tag-chip", () => ctl.close(t.name))));
        body.append(el("p", "muted", "Or pick one:"), chips);
      }
      return { submit: async () => {
        const v = input.value.trim();
        if (!v) throw new Error("Please type a tag.");
        return v;
      } };
    },
  });
}

async function addTag(ids) {
  try {
    const name = await pickTag(ids.length);
    if (!name) return;
    const tag = await post("/api/tags", { name, ids });
    flashNote(`Tagged ${tag.name}`);
    if (S.selecting) stopSelecting();
    if (S.detail && ids.includes(S.detail.id)) loadDetail(S.detail.id);
  } catch (err) { flashNote(err.message); }
}

async function removeTag(ids) {
  try {
    await post(`/api/tags/${S.route.tag}/remove`, { ids });
    removeItems(ids);
    renderHead();
  } catch (err) { flashNote(err.message); }
}

async function stackPhotos(ids) {
  try {
    await post("/api/stacks", { ids });
    flashNote(`${plural(ids.length, "photo")} stacked`);
    stopSelecting();
    await reload();
  } catch (err) { flashNote(err.message); }
}

async function unstackPhotos(ids) {
  try {
    const res = await post("/api/stacks/unstack", { ids });
    flashNote(`${plural(res.stacks, "stack")} taken apart`);
    stopSelecting();
    await reload();
  } catch (err) { flashNote(err.message); }
}

// ------------------------------------------------------------------ albums page

async function renderAlbums() {
  const { albums } = await api("/api/albums");
  headRow("Albums", albums.length ? plural(albums.length, "album") : null, [
    button("New smart album", "btn small", () => smartAlbumDialog(null, {})),
    button("New album", "btn small primary", async () => {
      const name = await askText("New album", "Album name", "", "Create");
      if (!name) return;
      try { const a = await post("/api/albums", { name }); location.hash = `#/albums/${a.id}`; } catch (err) { flashNote(err.message); }
    }),
  ]);
  const coll = $("collection");
  if (!albums.length) {
    showEmpty("No albums yet", "Press Select, pick some photos and choose Add to album. Albums are shared with everyone who uses Snapshots.");
    return;
  }
  const cards = el("div", "cards");
  for (const a of albums) {
    const card = link("", `#/albums/${a.id}`, "card");
    const box = el("div", "card-img");
    if (a.cover) box.append(img(thumbUrl(a.cover, a.cover_v))); else box.textContent = "Empty";
    if (a.smart) box.append(el("span", "smart-badge", "Smart"));
    card.append(box, el("div", "card-title", a.name));
    const range = a.first ? ` · ${fmtMonth(a.last)}` : "";
    card.append(el("div", "card-meta", plural(a.count, "item") + range));
    cards.append(card);
  }
  coll.replaceChildren(cards);
}

// ------------------------------------------------------------------ people page

function workLine(job, what) {
  if (!job || !job.enabled) return `${what} is turned off in the settings.`;
  if (job.message) return job.message;
  if (job.pending) return `Still looking: ${plural(job.pending, "photo")} to go.`;
  return "";
}

async function renderPeople() {
  const hidden = S.route.hidden;
  const data = await api(`/api/people${hidden ? "?hidden=true" : ""}`);
  const actions = [];
  if (hidden) actions.push(link("Back to people", "#/people", "btn small"));
  else if (data.hidden_count) actions.push(link(`Hidden (${data.hidden_count})`, "#/people?hidden=1", "btn small"));
  headRow(hidden ? "Hidden people" : "People", null, actions,
    workLine(data.faces, "Face recognition") || null);
  const coll = $("collection");
  if (!data.people.length) {
    if (hidden) showEmpty("Nobody is hidden", "");
    else showEmpty("No people yet", data.faces && data.faces.enabled
      ? "Faces are found in the background after photos are indexed. People appear here once they are in 3 or more photos."
      : "Face recognition is turned off.");
    return;
  }
  const card = (p) => {
    const cell = el("div", "person-cell");
    cell.dataset.name = (p.name || "").toLowerCase();
    const a = link("", `#/people/${p.id}`, "person");
    a.append(p.face_id ? img(faceUrl(p.face_id), "", "avatar") : el("span", "avatar"));
    a.append(p.name ? el("span", null, p.name) : el("span", "unnamed", "Add a name"));
    a.append(el("small", null, plural(p.photos, "photo")));
    const who = p.name || "this person";
    const toggle = button(hidden ? "Show" : "×", hidden ? "person-hide show" : "person-hide", async () => {
      try {
        await patch(`/api/people/${p.id}`, { hidden: !hidden });
        flashNote(hidden ? `${p.name || "They"} will show in People again` : `Hidden ${p.name || "this face"}. Find them under Hidden.`);
        render();
      } catch (err) { flashNote(err.message); }
    });
    toggle.title = hidden ? `Show ${who} in People` : `Hide ${who} from People`;
    toggle.setAttribute("aria-label", toggle.title);
    cell.append(a, toggle);
    return cell;
  };
  const named = data.people.filter((p) => p.name);
  const unnamed = data.people.filter((p) => !p.name);
  const parts = [];
  if (data.people.length > 12) {
    const tools = el("div", "people-tools");
    const find = el("input", "people-find");
    find.type = "search";
    find.placeholder = "Find a person";
    find.setAttribute("aria-label", "Find a person by name");
    find.addEventListener("input", () => {
      const q = find.value.trim().toLowerCase();
      coll.querySelectorAll(".person-cell").forEach((c) => { c.hidden = !!q && !c.dataset.name.includes(q); });
      coll.querySelectorAll(".people-sub").forEach((h) => {
        const g = h.nextElementSibling;
        h.hidden = !!q && !(g && g.querySelector(".person-cell:not([hidden])"));
      });
    });
    tools.append(find);
    parts.push(tools);
  }
  const group = (list) => { const g = el("div", "people"); list.forEach((p) => g.append(card(p))); return g; };
  if (named.length) parts.push(group(named));
  if (unnamed.length) {
    if (named.length) {
      const h = el("h2", "section-title people-sub", "Not named yet");
      h.append(el("span", "muted", hidden ? "" : "Add a name, or hide faces you don't want to see (×)"));
      parts.push(h);
    }
    parts.push(group(unnamed));
  }
  coll.replaceChildren(...parts);
  if (!hidden) suggestionStrip(null).then((strip) => { if (strip && coll.isConnected) coll.prepend(strip); });
}

/** "Same person?" questions: for everyone (person = null) or for one person. Null when there are none. */
async function suggestionStrip(person) {
  let data;
  try { data = await api(`/api/people/suggestions?limit=${person ? 6 : 12}${person ? `&person=${person}` : ""}`); }
  catch (_) { return null; }
  if (!data.suggestions.length) return null;
  const box = el("section", "suggest");
  const h = el("h2", "section-title", person ? "Might also be them" : "Same person?");
  h.append(el("span", "muted", person ? "Faces that look alike" : `${plural(data.total, "question")} to help group faces`));
  box.append(h);
  const row = el("div", "suggest-row");
  box.append(row);
  const nameOf = (p) => p.name || "Unnamed";
  for (const sg of data.suggestions) {
    // On a person's page, "them" is always on the left.
    let { a, b } = sg;
    if (person && b.id === person) [a, b] = [b, a];
    const card = el("div", "suggest-card");
    const faces = el("div", "suggest-faces");
    for (const p of [a, b]) {
      const f = link("", `#/people/${p.id}`, "suggest-face");
      f.title = `${nameOf(p)} · ${plural(p.photos, "photo")}`;
      f.append(p.face_id ? img(faceUrl(p.face_id), "", "avatar") : el("span", "avatar"));
      f.append(el("span", p.name ? null : "unnamed", nameOf(p)), el("small", null, plural(p.photos, "photo")));
      faces.append(f);
    }
    const named = a.name || b.name;
    const q = sg.kind === "is_this" || (named && !(a.name && b.name))
      ? `Is this ${named || "the same person"}?`
      : sg.same_name ? `Both are called ${a.name}. One person?` : "Same person?";
    card.append(faces, el("p", "suggest-q", q));
    if (!sg.likely) card.append(el("p", "muted small", "Not very alike"));
    const done = (text) => { card.remove(); if (text) flashNote(text); if (!row.children.length) box.remove(); };
    // The named (or bigger) person keeps their name: the other one joins them.
    const [keep, gone] = (b.name && !a.name) || (!a.name && !b.name && b.photos > a.photos) ? [b, a] : [a, b];
    const acts = el("div", "suggest-acts");
    acts.append(
      button("Yes", "btn small primary", async () => {
        try {
          await post(`/api/people/${gone.id}/merge`, { into: keep.id });
          done(`Put together${keep.name ? ` as ${keep.name}` : ""}`);
          if (person && gone.id === person) location.hash = `#/people/${keep.id}`;
          else if (person) render();
        } catch (err) { flashNote(err.message); }
      }),
      button("No", "btn small", async () => {
        try { await post("/api/people/not-same", { a: a.id, b: b.id }); done(); } catch (err) { flashNote(err.message); }
      }),
      button("Skip", "btn small", () => done()),
    );
    card.append(acts);
    row.append(card);
  }
  return box;
}

// ------------------------------------------------------------------ places page

async function renderPlaces() {
  const data = await api("/api/places");
  headRow("Places", data.located ? `${plural(data.located, "photo")} with a location` : null, [],
    workLine(data.status, "Place lookup") || null);
  const coll = $("collection");
  if (!data.places.length) {
    showEmpty("No places yet", data.located
      ? "Place names are being looked up in the background."
      : "Photos taken with location turned on show up here, grouped by town.");
    return;
  }
  const byCountry = new Map();
  for (const p of data.places) {
    if (!byCountry.has(p.country)) byCountry.set(p.country, []);
    byCountry.get(p.country).push(p);
  }
  const frag = document.createDocumentFragment();
  for (const [country, list] of byCountry) {
    const total = list.reduce((n, p) => n + p.count, 0);
    const h = el("h2", "section-title", country);
    h.append(el("span", "muted", plural(total, "photo")));
    if (list.length > 1) h.append(link("Show all", placeHash(country)));
    frag.append(h);
    const cards = el("div", "cards");
    for (const p of list) {
      const card = link("", placeHash(p.country, p.state, p.city), "card");
      const box = el("div", "card-img");
      box.append(img(thumbUrl(p.cover, p.cover_v)));
      card.append(box, el("div", "card-title", p.city || p.state || p.country));
      card.append(el("div", "card-meta", [p.city ? p.state : null, plural(p.count, "photo")].filter(Boolean).join(" · ")));
      cards.append(card);
    }
    frag.append(cards);
  }
  const credit = el("p", "attribution", "Place names from ");
  credit.append(link("GeoNames", "https://www.geonames.org/"), document.createTextNode(" (CC BY 4.0). Looked up on this computer; your locations are not sent anywhere."));
  frag.append(credit);
  coll.replaceChildren(frag);
}

// ------------------------------------------------------------------ events page

function fmtDay(iso, opts) {
  const d = new Date(iso);
  return isNaN(d) ? iso.slice(0, 10) : d.toLocaleDateString(undefined, opts);
}
function eventTitle(e) {
  if (e.kind === "trip") return e.place ? `Trip to ${e.place}` : "A trip";
  const day = fmtDay(e.start, { weekday: "long", day: "numeric", month: "long" });
  return e.place ? `${e.place}, ${day}` : day;
}
function eventDates(e) {
  const a = fmtDay(e.start, { day: "numeric", month: "short", year: "numeric" });
  if (e.days <= 1) return a;
  const b = fmtDay(e.end, { day: "numeric", month: "short", year: "numeric" });
  return `${a} – ${b} · ${e.days} days`;
}
const eventHash = (e) => `#/events/${encodeURIComponent(e.start)}/${encodeURIComponent(e.end)}`;

async function renderEvents() {
  const { events } = await api("/api/events");
  headRow("Events", events.length ? plural(events.length, "event") : null, [],
    "Days out and trips, found from when and where photos were taken.");
  if (!events.length) {
    showEmpty("No events yet", "Events appear when several photos were taken close together. Trips are found from photos with a location.");
    return;
  }
  const frag = document.createDocumentFragment();
  let year = null, cards = null;
  for (const e of events) {
    const y = e.start.slice(0, 4);
    if (y !== year) {
      year = y;
      frag.append(el("h2", "section-title", y));
      cards = el("div", "cards");
      frag.append(cards);
    }
    const card = link("", eventHash(e), "card");
    const box = el("div", "card-img");
    box.append(img(thumbUrl(e.cover, e.cover_v)));
    if (e.kind === "trip") box.append(el("span", "card-badge", "Trip"));
    card.append(box, el("div", "card-title", eventTitle(e)));
    card.append(el("div", "card-meta", `${eventDates(e)} · ${plural(e.count, "item")}`));
    cards.append(card);
  }
  $("collection").replaceChildren(frag);
}

// ------------------------------------------------------------------ memories

function localDate(d = new Date()) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
const yearsAgo = (n) => (n === 1 ? "1 year ago" : `${n} years ago`);

/** "On this day" above the timeline: photos taken on today's date in earlier years. */
async function renderMemoriesStrip() {
  let data;
  try { data = await api(`/api/memories?today=${localDate()}`); } catch (_) { return; }
  if (S.q || S.route.name !== "all" || S.route.memory || !data.memories.length) return;
  const head = $("page-head");
  head.replaceChildren();
  const box = el("section", "memories");
  box.setAttribute("aria-label", "On this day");
  box.append(el("h2", "memories-title", "On this day"));
  const row = el("div", "memory-row");
  for (const m of data.memories) {
    const card = link("", `#/memories/${m.date}`, "memory");
    card.append(img(thumbUrl(m.cover, m.cover_v)));
    const label = el("span", "memory-label");
    label.append(el("strong", null, yearsAgo(m.years_ago)), el("span", null, plural(m.count, "item")));
    card.append(label);
    row.append(card);
  }
  box.append(row);
  head.append(box);
  head.hidden = false;
}

function renderMemoryHead(day) {
  const d = new Date(`${day}T12:00:00`);
  const title = isNaN(d) ? day : d.toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" });
  const ago = new Date().getFullYear() - Number(day.slice(0, 4));
  headRow(title, ago > 0 ? yearsAgo(ago) : null, [
    button("Save as album", "btn small", async () => {
      const name = await askText("Save as album", "Album name", title, "Create album");
      if (!name) return;
      try {
        const ids = await allIds();
        const album = await post("/api/albums", { name, ids });
        flashNote(`Album “${album.name}” created with ${plural(ids.length, "item")}`);
      } catch (err) { flashNote(err.message); }
    }),
    link("Back to All", "#/", "btn small"),
  ]);
}

// ------------------------------------------------------------------ tags

async function renderTags() {
  const { tags } = await api("/api/tags");
  headRow("Tags", tags.length ? plural(tags.length, "tag") : null, [], tags.length
    ? "Tags are shared by everyone. Keywords that other programs (Lightroom, digiKam, Windows Photo Gallery) saved in your photos appear here too."
    : null);
  if (!tags.length) {
    showEmpty("No tags yet", "Press Select, pick some photos and press Tag. Keywords saved in your photos by other programs show up here too.");
    return;
  }
  const cards = el("div", "cards");
  for (const t of tags) {
    const card = link("", `#/tags/${t.id}`, "card");
    const box = el("div", "card-img");
    if (t.cover) box.append(img(thumbUrl(t.cover, t.cover_v))); else box.textContent = "Empty";
    card.append(box, el("div", "card-title", t.name), el("div", "card-meta", plural(t.count, "item")));
    cards.append(card);
  }
  $("collection").replaceChildren(cards);
}

async function renderTagHead(id) {
  const tag = await api(`/api/tags/${id}`);
  headRow(tag.name, plural(tag.count, "item"), [
    button("Rename", "btn small", async () => {
      const name = await askText("Rename tag", "Tag", tag.name);
      if (!name) return;
      try {
        const t = await patch(`/api/tags/${tag.id}`, { name });
        if (t.id !== tag.id) { flashNote(`Put together with ${t.name}`); location.hash = `#/tags/${t.id}`; } else renderHead();
      } catch (err) { flashNote(err.message); }
    }),
    button("Delete tag", "btn small", async () => {
      const ok = await confirmBox("Delete this tag?", `“${tag.name}” will be removed from every photo. The photos stay in your library.`, "Delete tag", true);
      if (!ok) return;
      try { await del(`/api/tags/${tag.id}`); location.hash = "#/tags"; } catch (err) { flashNote(err.message); }
    }),
    link("All tags", "#/tags", "btn small"),
  ]);
}

// ------------------------------------------------------------------ folders

// One encoded part, so paths of other library folders ("D:/Phone", "/media/phone") survive the trip.
const folderHash = (path) => "#/folders" + (path ? "/" + encodeURIComponent(path) : "");

async function renderFolderHead(path) {
  const info = await api(`/api/folders?path=${encodeURIComponent(path)}`);
  if (S.route.folder !== path) return;
  S.folderInfo = info;
  const crumbs = el("nav", "crumbs");
  crumbs.setAttribute("aria-label", "Folder");
  const items = (info.crumbs || [{ name: "Library", path: "" }]).map((c) => [c.name, c.path]);
  if (info.crumbs && info.crumbs[0] && info.crumbs[0].path) items.unshift([libraryName(), ""]);
  items.forEach(([name, at], i) => {
    if (i) crumbs.append(el("span", "crumb-sep", "/"));
    if (i === items.length - 1) { const h = el("h1", null, name); crumbs.append(h); }
    else crumbs.append(link(name, folderHash(at)));
  });
  headRow(crumbs, info.items ? plural(info.items, "item") + " here" : null);
  if (info.folders.length) {
    const list = el("div", "folders");
    for (const f of info.folders) {
      const a = link("", folderHash(f.path), "folder");
      const pic = el("span", "folder-pic");
      if (f.cover) pic.append(img(thumbUrl(f.cover, f.cover_v)));
      const text = el("span", "folder-text");
      let meta = plural(f.count, "item");
      if (f.library) { a.classList.add("library"); meta = (f.available === false ? "Not connected · " : "Library folder · ") + meta; a.title = f.library; }
      text.append(el("span", "folder-name", f.name), el("small", null, meta));
      a.append(pic, text);
      list.append(a);
    }
    $("page-head").append(list);
  }
}

function libraryName() {
  const p = S.status && S.status.library_path;
  return (p ? p.split(/[\\/]/).filter(Boolean).pop() : "") || "Library";
}

// ------------------------------------------------------------------ map

let mapLibs = null;
function loadMapLibs() {
  if (mapLibs) return mapLibs;
  const css = (href) => { const l = document.createElement("link"); l.rel = "stylesheet"; l.href = href; document.head.append(l); };
  const js = (src) => new Promise((ok, fail) => {
    const t = document.createElement("script");
    t.src = src; t.onload = ok; t.onerror = () => fail(new Error("The map could not be loaded."));
    document.head.append(t);
  });
  css("/static/vendor/leaflet.css");
  css("/static/vendor/MarkerCluster.css");
  mapLibs = js("/static/vendor/leaflet.js")
    .then(() => Promise.all([js("/static/vendor/leaflet.markercluster.js"), js("/static/vendor/topojson-client.min.js")]))
    .catch((err) => { mapLibs = null; throw err; });
  return mapLibs;
}

let worldShapes = null;
async function loadWorld() {
  if (!worldShapes) {
    const topo = await (await fetch("/static/vendor/countries-50m.json")).json();
    const shapes = topojson.feature(topo, topo.objects.countries);
    // The shapes are made for a round globe. On a flat map, a coastline that crosses the date line
    // (Fiji, the east tip of Russia) would be drawn the long way round, as a line across the world:
    // keep each outline's longitudes continuous instead. Antarctica circles the pole and is left out.
    const unwrap = (ring) => {
      let prev = null;
      return ring.map(([lon, lat]) => {
        if (prev !== null) { while (lon - prev > 180) lon -= 360; while (lon - prev < -180) lon += 360; }
        prev = lon;
        return [lon, lat];
      });
    };
    shapes.features = shapes.features.filter((f) => f.id !== "010" && f.geometry);
    for (const f of shapes.features) {
      const g = f.geometry;
      if (g.type === "Polygon") g.coordinates = g.coordinates.map(unwrap);
      else if (g.type === "MultiPolygon") g.coordinates = g.coordinates.map((poly) => poly.map(unwrap));
    }
    worldShapes = shapes;
  }
  return worldShapes;
}

const MAP_VIEW_KEY = "snapshots.mapView";
function savedMapView() {
  try { return JSON.parse(localStorage.getItem(MAP_VIEW_KEY) || "null"); } catch (_) { return null; }
}

async function renderMap() {
  const [data] = await Promise.all([api("/api/map"), loadMapLibs()]);
  if (S.route.view !== "map") return;
  headRow("Map", data.points.length ? plural(data.points.length, "item") + " with a location" : null, [],
    data.tiles ? null : "Showing country outlines only. Set map_tiles in config.toml for detailed maps.");
  if (!data.points.length) {
    showEmpty("Nothing on the map yet", "Photos taken with location turned on show up here.");
    return;
  }
  const box = el("div", "map-box");
  const mapEl = el("div", "map");
  mapEl.setAttribute("aria-label", "Map of your photos");
  const bar = el("div", "map-bar");
  const count = el("span", "map-count");
  const show = button("Show these photos", "btn small primary");
  bar.append(count, show);
  box.append(mapEl, bar);
  $("collection").replaceChildren(box);

  const map = L.map(mapEl, { worldCopyJump: true, minZoom: 2, maxZoom: 19, zoomControl: true });
  map.attributionControl.setPrefix(false);
  // Country outlines sit under the map pictures: they show when the pictures can't be fetched (offline).
  map.createPane("world").style.zIndex = 150;
  const world = await loadWorld();
  L.geoJSON(world, { pane: "world", interactive: false, style: { className: "map-country", weight: 1, fillOpacity: 1 } }).addTo(map);
  if (data.tiles) {
    L.tileLayer(data.tiles, { maxZoom: 19, attribution: data.attribution || "", className: "map-tiles", referrerPolicy: "no-referrer" }).addTo(map);
  } else {
    map.attributionControl.addAttribution("Country shapes: Natural Earth");
  }

  const pinIcon = (id, v, label) => L.divIcon({
    className: "pin", iconSize: [44, 44], iconAnchor: [22, 22],
    html: `<img class="pin-img" src="${thumbUrl(id, v)}" alt="" loading="lazy">${label ? `<span class="pin-count">${label}</span>` : ""}`,
  });
  const clusters = L.markerClusterGroup({
    showCoverageOnHover: false, maxClusterRadius: 60, spiderfyOnMaxZoom: true, chunkedLoading: true,
    iconCreateFunction: (c) => {
      const first = c.getAllChildMarkers()[0].options.photo;
      return pinIcon(first.id, first.v, fmtInt(c.getChildCount()));
    },
  });
  const markers = data.points.map(([id, lat, lon, v]) => {
    const m = L.marker([lat, lon], { icon: pinIcon(id, v), photo: { id, v }, keyboard: false });
    m.on("click", () => {
      const d = 0.0005;
      location.hash = `#/map/area/${[lat - d, lon - d, lat + d, lon + d].map((x) => x.toFixed(5)).join(",")}`;
    });
    return m;
  });
  clusters.addLayers(markers);
  map.addLayer(clusters);

  const saved = savedMapView();
  if (saved) map.setView(saved.center, saved.zoom);
  else map.fitBounds(L.latLngBounds(data.points.map((p) => [p[1], p[2]])).pad(0.1), { maxZoom: 12 });

  const update = () => {
    const b = map.getBounds();
    const n = data.points.reduce((k, p) => k + (b.contains([p[1], p[2]]) ? 1 : 0), 0);
    count.textContent = n ? `${plural(n, "item")} in view` : "Nothing in view";
    show.disabled = !n;
    try { localStorage.setItem(MAP_VIEW_KEY, JSON.stringify({ center: map.getCenter(), zoom: map.getZoom() })); } catch (_) { /* fine */ }
  };
  map.on("moveend", update);
  update();
  show.addEventListener("click", () => {
    const b = map.getBounds();
    const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
    const wrap = (x) => ((((x + 180) % 360) + 360) % 360) - 180;
    const span = b.getEast() - b.getWest();
    const west = span >= 360 ? -180 : wrap(b.getWest()), east = span >= 360 ? 180 : wrap(b.getEast());
    const area = [clamp(b.getSouth(), -90, 90), west, clamp(b.getNorth(), -90, 90), east].map((x) => x.toFixed(5)).join(",");
    location.hash = `#/map/area/${area}`;
  });
  // The map was drawn before its box had its final size.
  setTimeout(() => map.invalidateSize(), 0);
}

// ------------------------------------------------------------------ clean up page

const CLEANUP_KINDS = [
  ["", "All"],
  ["identical", "Identical files"],
  ["copies", "Look alike"],
  ["burst", "Bursts"],
];
const CLEANUP_HELP = {
  "": "Each group's best item is marked to keep. Click items to change what to keep. Nothing is deleted: the rest go to the trash, and the files are only removed from your disk when the trash is emptied.",
  identical: "The very same file, more than once (in two folders, say). Keeping one loses nothing.",
  copies: "Photos that look the same: a smaller or re-saved copy, or the same picture from another app. The one with the most pixels is kept.",
  burst: "Photos taken seconds apart. Keep your favourite, or stack them so they show as one.",
};

async function renderCleanup() {
  const kind = S.cleanupKind || "";
  const strictness = S.cleanupStrictness || "normal";
  const sum = await api(`/api/cleanup?strictness=${strictness}`);
  const st = sum.status || {};
  headRow("Clean up", null, [], !sum.enabled ? "Photo checks are turned off in the settings."
    : st.pending ? `Still checking photos: ${plural(st.pending, "photo")} to go.` : null);
  if (!sum.enabled) { showEmpty("Photo checks are off", "Turn on photo_checks in config.toml."); return; }
  const frag = document.createDocumentFragment();

  const q = el("a", "cleanup-link");
  q.href = "#/cleanup/quality";
  q.append(el("strong", null, "Blurry or dark photos"), el("span", "muted", sum.low_quality ? plural(sum.low_quality, "photo") : "None found"));
  frag.append(q);

  const h = el("h2", "section-title", "Duplicates");
  if (sum.groups) h.append(el("span", "muted", `${plural(sum.extra, "extra item")} · ${fmtSize(sum.space)} could be freed`));
  frag.append(h);

  const bar = el("div", "cleanup-bar");
  const tabs = el("div", "tabs");
  for (const [k, label] of CLEANUP_KINDS) {
    const n = k ? sum.kinds[k].groups : sum.groups;
    const t = button(n ? `${label} (${n})` : label, "tab", () => { S.cleanupKind = k; renderCleanup(); });
    t.setAttribute("aria-pressed", String(k === kind));
    tabs.append(t);
  }
  const strictWrap = el("label", "field inline compact");
  strictWrap.append(el("span", null, "Look-alikes"));
  const sel = el("select");
  for (const [v, label] of [["strict", "Nearly identical only"], ["normal", "Normal"], ["loose", "Also similar shots"]]) {
    const o = el("option", null, label);
    o.value = v;
    o.selected = v === strictness;
    sel.append(o);
  }
  sel.addEventListener("change", () => { S.cleanupStrictness = sel.value; renderCleanup(); });
  strictWrap.append(sel);
  bar.append(tabs, strictWrap);
  frag.append(bar);

  const info = kind ? sum.kinds[kind] : { groups: sum.groups, extra: sum.extra, space: sum.space };
  frag.append(el("p", "muted", CLEANUP_HELP[kind]));
  if (!info.groups) {
    frag.append(el("p", "muted", kind ? "None found." : "No duplicates found."));
    $("collection").replaceChildren(frag);
    return;
  }
  const params = `strictness=${strictness}${kind ? `&kind=${kind}` : ""}`;
  const bulk = button(`Keep the best of every group (frees ${fmtSize(info.space)})`, "btn", async () => {
    const ok = await confirmBox(`Move ${plural(info.extra, "item")} to the trash?`,
      "Each group keeps its best item. The others can be restored from the trash until it is emptied.", "Move to trash");
    if (!ok) return;
    try {
      let moved = 0;
      for (;;) {
        const page = await api(`/api/cleanup/groups?limit=100&${params}`);
        if (!page.groups.length) break;
        for (const g of page.groups) {
          const res = await post("/api/cleanup/resolve", { keep: [g.keep], trash: g.items.map((i) => i.id).filter((i) => i !== g.keep) });
          moved += res.trashed;
        }
        if (page.groups.length < 100 && !page.next) break;
      }
      flashNote(`${plural(moved, "item")} moved to the trash`);
      refreshStatus();
      render();
    } catch (err) { flashNote(err.message); }
  });
  frag.append(bulk);
  const list = el("div", "dup-groups");
  frag.append(list);
  const more = button("Show more", "btn", () => loadGroups());
  frag.append(more);
  $("collection").replaceChildren(frag);

  let offset = 0;
  async function loadGroups() {
    more.disabled = true;
    try {
      const page = await api(`/api/cleanup/groups?offset=${offset}&limit=20&${params}`);
      page.groups.forEach((g) => list.append(groupCard(g)));
      offset = page.next || offset + page.groups.length;
      more.hidden = !page.next;
    } catch (err) { flashNote(err.message); }
    more.disabled = false;
  }
  await loadGroups();
}

function groupCard(g) {
  const card = el("section", "dup-group");
  const keep = new Set([g.keep]);
  const head = el("div", "dup-head");
  const label = { identical: "Identical files", burst: "Burst", copies: "Look alike" }[g.kind] || "Copies";
  head.append(el("strong", null, label),
    el("span", "muted", `${fmtDateTime(g.items[0].taken_at)} · ${plural(g.items.length, "item")}` +
      (g.space ? ` · ${fmtSize(g.space)} to free` : "")));
  if (g.why) head.append(el("span", "dup-why", `Kept: ${g.why.toLowerCase()}`));
  const row = el("div", "dup-row");
  const draw = () => row.querySelectorAll(".dup-item").forEach((d) => {
    const on = keep.has(+d.dataset.id);
    d.classList.toggle("keep", on);
    d.setAttribute("aria-pressed", String(on));
    d.querySelector(".dup-flag").textContent = on ? "Keep" : "Trash";
  });
  for (const it of g.items) {
    const b = el("button", "dup-item");
    b.type = "button";
    b.dataset.id = it.id;
    b.title = it.path;
    b.append(img(thumbUrl(it.id, it.v), it.filename), el("span", "dup-flag"));
    const meta = el("span", "dup-meta");
    meta.append(el("span", null, it.width && it.height ? `${it.width} × ${it.height}` : it.filename), el("span", null, fmtSize(it.size)));
    const folder = (it.path || "").split("/").slice(0, -1).join("/");
    meta.append(el("span", "dup-folder", folder || libraryName()));
    if (it.id === g.keep) meta.append(el("span", "best", "Best"));
    b.append(meta);
    b.addEventListener("click", () => {
      if (keep.has(it.id)) { if (keep.size > 1) keep.delete(it.id); } else keep.add(it.id);
      draw();
    });
    row.append(b);
  }
  const actions = el("div", "dup-actions");
  actions.append(
    button("Keep marked, trash the rest", "btn small primary", async () => {
      const trash = g.items.map((i) => i.id).filter((i) => !keep.has(i));
      try {
        await post("/api/cleanup/resolve", { keep: [...keep], trash });
        card.remove();
        flashNote(trash.length ? `${plural(trash.length, "item")} moved to the trash` : "Kept them all");
        refreshStatus();
      } catch (err) { flashNote(err.message); }
    }),
    g.kind === "identical" ? "" : button(g.kind === "burst" ? "Stack them" : "Stack them instead", "btn small", async () => {
      const ids = g.items.map((i) => i.id);
      const first = [...keep][0];
      try {
        await post("/api/stacks", { ids: [first, ...ids.filter((i) => i !== first)], cover: first });
        card.remove();
        flashNote(`${plural(ids.length, "photo")} stacked: they show as one in the timeline`);
      } catch (err) { flashNote(err.message); }
    }),
    button("These are different", "btn small", async () => {
      try { await post("/api/cleanup/not-duplicates", { ids: g.items.map((i) => i.id) }); card.remove(); }
      catch (err) { flashNote(err.message); }
    }),
  );
  card.append(head, row, actions);
  draw();
  return card;
}


// ------------------------------------------------------------------ storage report

function bar(fraction, cls = "") {
  const b = el("span", "meter " + cls);
  const f = el("span", "meter-fill");
  f.style.width = `${Math.max(0.5, Math.min(100, fraction * 100)).toFixed(1)}%`;
  b.append(f);
  return b;
}

async function renderStorage() {
  headRow("Storage", null, []);
  $("collection").replaceChildren(el("p", "muted", "Adding it all up…"));
  let s;
  try { s = await api("/api/storage"); } catch (err) { showEmpty("Something went wrong", err.message); return; }
  const refresh = button("Count again", "btn small", async () => {
    refresh.disabled = true;
    try { await api("/api/storage?fresh=true"); renderStorage(); } catch (err) { flashNote(err.message); refresh.disabled = false; }
  });
  headRow("Storage", `${fmtSize(s.total)} of photos and videos`, [refresh],
    `Counted ${fmtDateTime(s.made_at)}. Locked folders are not included.`);
  const frag = document.createDocumentFragment();

  const kinds = el("div", "stat-tiles");
  for (const [key, label] of [["photos", "Photos"], ["raw", "RAW photos"], ["videos", "Videos"]]) {
    const k = s.kinds[key];
    if (!k.items && key === "raw") continue;
    const t = el("div", "stat-tile");
    t.append(el("span", "stat-label", label), el("strong", "stat-value", fmtSize(k.bytes)), el("span", "muted small", plural(k.items, "item")));
    t.append(bar(s.total ? k.bytes / s.total : 0));
    kinds.append(t);
  }
  frag.append(kinds);

  const save = el("div", "stat-tiles");
  const saving = (label, bytes, text, href) => {
    const t = el(href ? "a" : "div", "stat-tile" + (href ? " link" : ""));
    if (href) t.href = href;
    t.append(el("span", "stat-label", label), el("strong", "stat-value", fmtSize(bytes)), el("span", "muted small", text));
    save.append(t);
  };
  saving("In the trash", s.trash.bytes, s.trash.items ? `${plural(s.trash.items, "item")}. Freed when the trash is emptied.` : "The trash is empty.", s.trash.items ? "#/trash" : null);
  saving("Duplicates", s.duplicates, s.duplicates ? "Could be freed by keeping one of each." : "None found.", s.duplicates ? "#/cleanup" : null);
  saving("Snapshots' own files", s.data_total, "Thumbnails, converted videos, faces, backups.", null);
  frag.append(el("h2", "section-title", "Space you could get back"), save);

  // By year: photos and videos stacked in one bar each
  frag.append(el("h2", "section-title", "By year"));
  const maxYear = Math.max(1, ...s.years.map((y) => y.photos + y.videos));
  const years = el("div", "bars");
  for (const y of [...s.years].reverse()) {
    const row = el("a", "bar-row");
    row.href = `#/?q=${y.year}`;
    row.addEventListener("click", (e) => { e.preventDefault(); $("search").value = y.year; $("search").dispatchEvent(new Event("input")); });
    const b = el("span", "meter split");
    const p1 = el("span", "meter-fill");
    p1.style.width = `${(y.photos / maxYear * 100).toFixed(1)}%`;
    const p2 = el("span", "meter-fill video");
    p2.style.width = `${(y.videos / maxYear * 100).toFixed(1)}%`;
    b.append(p1, p2);
    row.append(el("span", "bar-name", y.year), b, el("span", "bar-value", `${fmtSize(y.photos + y.videos)} · ${plural(y.items, "item")}`));
    years.append(row);
  }
  const legend = el("p", "legend muted small");
  legend.append(el("span", "key photos"), " Photos ", el("span", "key videos"), " Videos");
  frag.append(legend, years);

  frag.append(el("h2", "section-title", "By folder"));
  const maxF = Math.max(1, ...s.folders.map((f) => f.bytes));
  const folders = el("div", "bars");
  for (const f of s.folders) {
    const row = el("div", "bar-row");
    row.append(el("span", "bar-name", f.folder), bar(f.bytes / maxF), el("span", "bar-value", `${fmtSize(f.bytes)} · ${plural(f.items, "item")}`));
    folders.append(row);
  }
  frag.append(folders);

  if (s.biggest.length) {
    frag.append(el("h2", "section-title", "Biggest files"));
    const list = el("div", "big-files");
    for (const it of s.biggest) {
      const a = el("a", "big-file");
      a.href = `#/folders/${it.path.split("/").slice(0, -1).map(encodeURIComponent).join("/")}`;
      a.title = it.path;
      a.append(img(thumbUrl(it.id, it.v), "", "big-thumb"));
      const t = el("span", "big-text");
      t.append(el("strong", null, it.filename), el("span", "muted small", `${fmtSize(it.size)} · ${fmtDay(it.taken_at, { day: "numeric", month: "short", year: "numeric" })}${it.kind === "video" ? " · video" : ""}`));
      a.append(t);
      list.append(a);
    }
    frag.append(list);
  }

  frag.append(el("h2", "section-title", "Snapshots' own files"));
  const data = el("div", "bars");
  const dataNames = { thumbnails: "Thumbnails", previews: "Previews", videos: "Converted videos", faces: "Face pictures",
    edited: "Edited copies", motion: "Live Photo clips", models: "Search and face models", places: "Place names",
    backups: "Database backups", exports: "Exports", database: "Database" };
  const maxD = Math.max(1, ...Object.values(s.data));
  for (const [k, v] of Object.entries(s.data).sort((a, b) => b[1] - a[1])) {
    if (!v) continue;
    const row = el("div", "bar-row");
    row.append(el("span", "bar-name", dataNames[k] || k), bar(v / maxD), el("span", "bar-value", fmtSize(v)));
    data.append(row);
  }
  frag.append(data, el("p", "muted small", `Converted videos are kept up to ${fmtSize(s.video_cache_limit)}; the oldest are removed after that. Thumbnails and previews are made again if deleted.`));

  frag.append(el("h2", "section-title", "Drives"));
  const disks = el("div", "bars");
  for (const d of s.disks) {
    const row = el("div", "bar-row");
    if (!d.available) { row.append(el("span", "bar-name", d.label), el("span", "muted small", "Not connected")); disks.append(row); continue; }
    const used = d.used / d.total;
    row.append(el("span", "bar-name", d.label), bar(used, used > 0.9 ? "warn" : ""), el("span", "bar-value", `${fmtSize(d.free)} free of ${fmtSize(d.total)}`));
    disks.append(row);
  }
  frag.append(disks);
  $("collection").replaceChildren(frag);
}

// ------------------------------------------------------------------ library health

const HEALTH_NAMES = {
  damaged: ["Damaged", "The file's content changed although nothing saved it: disk damage, or a bad copy."],
  unreadable: ["Can't be read", "Windows reported an error reading the file."],
  missing: ["Missing", "The file is gone from its folder. If the offsite copy has it, Snapshots keeps the item for a week so it can be put back."],
  changed: ["Saved again", "Another program changed the file since the last scan. Not a problem: Rescan reads it again."],
  unavailable: ["Drive not connected", "These files' drive wasn't connected, so they weren't checked."],
};

let healthTimer = null;
async function renderHealth() {
  clearTimeout(healthTimer);
  let st;
  try { st = await api("/api/library-health"); } catch (err) { showEmpty("Library health", err.message); return; }
  if (S.route.view !== "health") return;
  const last = st.last;
  const actions = [];
  if (st.running) actions.push(button("Stop", "btn small", async () => { try { await post("/api/library-health/stop"); } catch (_) { /* ignore */ } renderHealth(); }));
  else actions.push(button(last ? "Check again" : "Check now", "btn small primary", async () => {
    try { await post("/api/library-health/run"); renderHealth(); } catch (err) { flashNote(err.message); }
  }));
  headRow("Library health", null, actions, "Reads every photo and video again and compares it with the checksum Snapshots stored, to find files damaged on disk before your backups copy the damage. Nothing is changed.");
  const frag = document.createDocumentFragment();
  if (st.running) {
    const p = st.progress;
    const box = el("div", "health-run");
    box.append(el("strong", null, `Checking… ${fmtInt(p.done)} of ${fmtInt(p.total)}`), bar(p.total ? p.done / p.total : 0),
      el("span", "muted small", `${fmtSize(p.bytes)} read. You can keep using Snapshots meanwhile.`));
    frag.append(box);
    healthTimer = setTimeout(() => { if (S.route.view === "health") renderHealth(); }, 1500);
  }
  if (st.progress.message) frag.append(el("p", "form-error", st.progress.message));
  if (last && last.finished_at) {
    const verdict = el("div", "health-verdict " + (last.problems ? "bad" : "good"));
    verdict.append(el("strong", null, last.problems ? `${plural(last.problems, "problem")} found` : "Every file is as it should be"),
      el("span", "muted small", `${fmtInt(last.checked)} of ${fmtInt(last.total)} files checked (${fmtSize(last.bytes)}), ${fmtDateTime(last.finished_at)}${last.stopped ? ". Stopped before the end." : "."}`));
    if (last.locked_problems) verdict.append(el("span", "muted small", `And ${plural(last.locked_problems, "problem")} in someone's locked folder, not listed here.`));
    frag.append(verdict);
    const chips = el("div", "tabs");
    const counts = last.counts;
    for (const k of ["damaged", "unreadable", "missing", "changed", "unavailable"]) {
      if (!counts[k]) continue;
      const b = button(`${HEALTH_NAMES[k][0]} (${counts[k]})`, "tab", () => { S.healthStatus = S.healthStatus === k ? null : k; renderHealth(); });
      b.setAttribute("aria-pressed", String(S.healthStatus === k));
      chips.append(b);
    }
    if (chips.children.length) frag.append(chips);
    const list = el("div", "health-list");
    frag.append(list);
    const more = button("Show more", "btn", () => load());
    more.hidden = true;
    frag.append(more);
    let offset = 0;
    const load = async () => {
      const q = new URLSearchParams({ offset, limit: 50 });
      if (S.healthStatus) q.set("status", S.healthStatus);
      let page;
      try { page = await api(`/api/library-health/issues?${q}`); } catch (err) { flashNote(err.message); return; }
      for (const i of page.issues) list.append(healthRow(i));
      offset = page.next || offset;
      more.hidden = !page.next;
    };
    load();
  } else if (!st.running) {
    frag.append(el("p", "muted", "No check has been made yet. A check reads every file once, so it takes a while on a big library."));
  }
  const sched = el("div", "health-sched");
  const [w, days] = field("Check by itself every (days, 0 for only when you ask)", { type: "number", min: "0", max: "365", step: "1", value: String(st.days || 0) });
  sched.append(w, button("Save", "btn small", async () => {
    try { await api("/api/library-health/settings", { method: "PUT", json: { days: Number(days.value || 0) } }); flashNote("Saved"); }
    catch (err) { flashNote(err.message); }
  }));
  frag.append(el("h2", "section-title", "Schedule"), sched);
  if (!st.offsite_folder) frag.append(el("p", "muted small", "Tip: with an offsite copy to a folder (Settings), a damaged file can be put back from the copy here with one click."));
  $("collection").replaceChildren(frag);
}

function healthRow(i) {
  const row = el("div", "health-row " + i.status);
  const it = i.item;
  row.append(img(thumbUrl(it.id, it.v), "", "big-thumb"));
  const t = el("div", "big-text");
  t.append(el("strong", null, it.filename), el("span", "muted small", it.path),
    el("span", "health-status", `${HEALTH_NAMES[i.status][0]}: ${i.detail || HEALTH_NAMES[i.status][1]}`));
  row.append(t);
  if (i.restorable) {
    row.append(button("Put back the good copy", "btn small primary", async (e) => {
      e.target.disabled = true;
      try { const r = await post(`/api/library-health/restore/${it.id}`); flashNote(r.message); row.remove(); }
      catch (err) { flashNote(err.message); e.target.disabled = false; }
    }));
  }
  return row;
}

// ------------------------------------------------------------------ year in review

async function renderReview() {
  const info = await api("/api/review");
  if (!info.years.length) { headRow("Year in review"); showEmpty("No photos yet", "Your best photos of each year will show up here."); return; }
  const year = S.route.year || (info.years.find((y) => y.year < new Date().getFullYear()) || info.years[0]).year;
  const count = S.reviewCount || 60;
  const yearsRow = el("div", "tabs review-years");
  for (const y of info.years.slice(0, 12)) {
    const b = link(String(y.year), `#/review/${y.year}`, "tab");
    b.setAttribute("aria-pressed", String(y.year === year));
    yearsRow.append(b);
  }
  headRow(`${year} in review`, null, []);
  $("page-head").append(yearsRow);
  $("collection").replaceChildren(el("p", "muted", "Choosing the best photos…"));
  let pick;
  try { pick = await post(`/api/review/${year}`, { count }); } catch (err) { showEmpty("Something went wrong", err.message); return; }
  if (S.route.view !== "review") return;
  let items = pick.items;
  const facts = [plural(pick.photos, "photo") + ` from ${year}`];
  if (pick.places.length) facts.push(pick.places.slice(0, 3).join(", "));
  if (pick.people.length) facts.push("with " + pick.people.slice(0, 4).join(", "));
  const frag = document.createDocumentFragment();
  frag.append(el("p", "muted", `${facts.join(" · ")}. Favourites, stars, picks and named people count most; near-copies and bursts are left out, and the whole year gets a share. Click × to leave a photo out.`));
  const bar1 = el("div", "row-actions");
  const sizeSel = el("select");
  for (const n of [30, 60, 100]) { const o = el("option", null, `${n} photos`); o.value = n; o.selected = n === count; sizeSel.append(o); }
  sizeSel.addEventListener("change", () => { S.reviewCount = +sizeSel.value; renderReview(); });
  const play = button("Play", "btn primary", () => {
    S.items = items.slice(); S.done = true;
    startSlideshow(0);
  });
  const album = button("Save as album", "btn", async () => {
    const name = await askText("Save as album", "Album name", `${year} in review`, "Save");
    if (!name) return;
    try {
      const a = await post("/api/albums", { name });
      await post(`/api/albums/${a.id}/add`, { ids: items.map((x) => x.id) });
      flashNote(`Album “${name}” saved with ${plural(items.length, "photo")}`);
      location.hash = `#/albums/${a.id}`;
    } catch (err) { flashNote(err.message); }
  });
  const video = button("Make a video", "btn", () => reviewVideo(year, items, info));
  if (!info.ffmpeg) { video.disabled = true; video.title = "Making a video needs ffmpeg"; }
  bar1.append(sizeSel, play, album, video);
  frag.append(bar1);
  const grid = el("div", "review-grid");
  const draw = () => {
    grid.replaceChildren();
    items.forEach((it, k) => {
      const cell = el("div", "review-cell");
      const open = el("button", "review-img");
      open.type = "button";
      open.append(img(thumbUrl(it.id, it.v), it.filename));
      open.title = `${it.filename} · ${fmtDateTime(it.taken_at)}`;
      open.addEventListener("click", () => { S.items = items.slice(); S.done = true; S.viewing = k; viewer.showModal(); showItem(k); });
      const x = button("×", "review-x", () => { items = items.filter((y) => y.id !== it.id); draw(); });
      x.setAttribute("aria-label", `Leave out ${it.filename}`);
      cell.append(open, x);
      grid.append(cell);
    });
  };
  draw();
  frag.append(grid);
  $("collection").replaceChildren(frag);
}

async function reviewVideo(year, items, info) {
  const photos = items.filter((x) => x.kind === "photo");
  await modal({
    title: `${year} in review: a video`, ok: "Make the video",
    build: (body, ctl) => {
      body.append(el("p", "muted", `${plural(photos.length, "photo")}, each slowly zooming, with a title at the start and the end. Made on this computer; it can take a few minutes.`));
      const [tw, title] = field("Title", { value: String(year), maxLength: 100 });
      const grid = el("div", "rule-grid");
      const sWrap = el("label", "field");
      sWrap.append(el("span", null, "Size"));
      const size = el("select");
      [["720p", "720p (smaller file)"], ["1080p", "1080p (sharper)"]].forEach(([v, t]) => { const o = el("option", null, t); o.value = v; size.append(o); });
      sWrap.append(size);
      const [secW, secs] = field("Seconds for each photo", { type: "number", min: "2", max: "10", step: "0.5", value: String(info.seconds || 4) });
      grid.append(sWrap, secW);
      const saveWrap = el("label", "field inline");
      const save = el("input");
      save.type = "checkbox";
      saveWrap.append(save, el("span", null, "Also add it to the library (folder “Year in review”)"));
      const prog = el("div", "export-progress");
      prog.hidden = true;
      body.append(tw, grid, saveWrap, prog);
      const length = () => Math.round(6 + photos.length * Number(secs.value || 4));
      const est = el("p", "muted small");
      const upd = () => { est.textContent = `About ${Math.floor(length() / 60)}:${String(length() % 60).padStart(2, "0")} long.`; };
      secs.addEventListener("input", upd);
      upd();
      body.append(est);
      return { submit: async () => {
        const job = await post(`/api/review/${year}/video`, { ids: photos.map((x) => x.id), size: size.value, seconds: Number(secs.value || 4), title: title.value.trim(), save: save.checked });
        prog.hidden = false;
        const meter = bar(0);
        const text = el("span", "muted small", "Starting…");
        prog.replaceChildren(text, meter);
        for (;;) {
          await new Promise((r) => setTimeout(r, 1000));
          const t = await api(`/api/tasks/${job.id}`);
          meter.firstChild.style.width = `${(t.progress * 100).toFixed(1)}%`;
          text.textContent = t.done ? "Done" : `Making the video… ${Math.round(t.progress * 100)}%`;
          if (t.done) {
            if (t.error) throw new Error(t.error);
            const a = el("a", "btn primary", "Download the video");
            a.href = `/api/tasks/${job.id}/file`;
            a.download = t.result.name;
            prog.replaceChildren(el("p", null, `${t.result.seconds} seconds, ${t.result.photos} photos.${t.result.id ? " Added to the library too." : ""}`), a);
            ctl.error("");
            const form = body.closest("form");
            const again = form && form.querySelector("button[type=submit]");
            if (again) again.hidden = true;
            const cancel = form && [...form.querySelectorAll(".modal-foot .btn")].find((b) => b.type !== "submit");
            if (cancel) cancel.textContent = "Close";
            return undefined;   // stay open, so the download button can be used
          }
        }
      } };
    },
  });
}

// ------------------------------------------------------------------ status bar

let pollTimer = null;
let lastFinished = null;

async function refreshStatus() {
  let st;
  try { st = await api("/api/status"); } catch (_) { return; }
  S.status = st;
  S.me = st.user;
  renderStatus();

  const sc = st.scan;
  if (!sc.running && sc.finished_at && sc.finished_at !== lastFinished) {
    const first = lastFinished === null;
    lastFinished = sc.finished_at;
    if (!first && (sc.added || sc.removed || sc.moved || sc.updated) && S.route && S.route.view === "grid") {
      const quiet = $("scroller").scrollTop < 200 && !S.selecting && !$("viewer").open;
      if (quiet) await reload(); else $("new-items").hidden = false;
    }
  }
  updateEmpty();
  clearTimeout(pollTimer);
  const busy = sc.running || [st.faces, st.places, st.checks, st.content, st.text].some((j) => j && j.running);
  pollTimer = setTimeout(refreshStatus, busy ? 1500 : 30000);
}

function renderStatus() {
  const st = S.status;
  if (!st) return;
  $("account-name").textContent = st.user ? st.user.username : "Account";
  document.querySelectorAll(".admin-only").forEach((e) => { e.hidden = !(st.user && st.user.is_admin); });
  $("st-counts").textContent = `${plural(st.photos, "photo")} · ${plural(st.videos, "video")}`;
  $("st-library").textContent = st.library_path ? `Library: ${st.library_path}` : "";
  const sc = st.scan;
  $("st-scan").textContent = sc.running
    ? (sc.phase === "queued" || sc.phase === "listing" ? "Looking for files…" : `Indexing ${fmtInt(sc.done)} of ${fmtInt(sc.total)}`)
    : (sc.message || (sc.errors ? `Last scan skipped ${sc.errors} unreadable file(s)` : "Up to date"));
  const work = [];
  if (st.places.enabled && st.places.pending) work.push(`Places: ${fmtInt(st.places.pending)} to look up`);
  if (st.faces.enabled && st.faces.pending) work.push(`Faces: ${fmtInt(st.faces.pending)} photos to check`);
  if (st.checks && st.checks.enabled && st.checks.pending) work.push(`Checking: ${fmtInt(st.checks.pending)}`);
  if (st.content && st.content.enabled && st.content.pending) work.push(`Content search: ${fmtInt(st.content.pending)} to learn`);
  if (st.text && st.text.enabled && st.text.pending) work.push(`Reading text: ${fmtInt(st.text.pending)} to go`);
  $("st-work").textContent = work.join(" · ");
  const notes = [];
  if (!st.heif) notes.push("HEIC photos need pillow-heif");
  if (st.raw === false) notes.push("RAW files need rawpy");
  if (st.videos && !st.ffmpeg) notes.push("Video thumbnails need ffmpeg");
  if (st.faces.message && st.faces.enabled) notes.push(st.faces.message);
  if (st.places.message && st.places.enabled) notes.push(st.places.message);
  for (const job of [st.content, st.text]) if (job && job.enabled && job.message) notes.push(job.message);
  $("st-note").textContent = flash && Date.now() < flash.until ? flash.text : notes.join(" · ");
  $("rescan").disabled = sc.running || !st.library_ok;
  const unread = st.activity_unread || 0;
  $("activity-count").hidden = !unread;
  $("activity-count").textContent = unread > 99 ? "99+" : String(unread);
  $("more-dot").hidden = !unread;
  if (st.backup && st.backup.enabled && st.backup.message) $("st-note").textContent = st.backup.message;
}

$("rescan").addEventListener("click", async () => {
  const sc = S.status && S.status.scan;
  let body = {};
  // The last scan kept a lot of missing photos, in case of a wrong folder. An administrator can
  // confirm that they really are gone.
  if (sc && sc.skipped_removals && S.me && S.me.is_admin) {
    const ok = await confirmBox("Forget the missing photos?",
      `${plural(sc.skipped_removals, "photo")} were not found in the library folder last time. ` +
      "If you deleted or moved them out of the library on purpose, Snapshots can forget them, along with " +
      "their albums, favourites and faces. If the drive is just not connected, press Cancel. " +
      "Either way, no files are deleted.", "Forget them");
    body = { remove_missing: !!ok };
  }
  try { await post("/api/scan", body); } catch (err) { flashNote(err.message); }
  refreshStatus();
});
$("new-items").addEventListener("click", () => { $("scroller").scrollTop = 0; reload(); });

// ------------------------------------------------------------------ search

let searchTimer = null;
$("search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    S.q = e.target.value.trim();
    S.look = null;
    // Search looks through everything, except that All, Photos, Videos and Favourites keep their filter.
    if (S.q && !SEARCHABLE.includes(S.route.name)) {
      keepSearch = true;
      location.hash = "#/";
      return;
    }
    stopSelecting();
    $("page-head").hidden = true;
    renderHead().then(reload);
  }, 250);
});

// ------------------------------------------------------------------ upload

async function uploadFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  const tally = { added: 0, duplicate: 0, restored: 0, rejected: 0 };
  const BATCH = 8;
  let summary = "";
  $("upload-btn").disabled = true;
  try {
    for (let i = 0; i < files.length; i += BATCH) {
      $("st-note").textContent = `Uploading ${Math.min(i + BATCH, files.length)} of ${files.length}…`;
      const body = new FormData();
      files.slice(i, i + BATCH).forEach((f) => body.append("files", f, f.name));
      const res = await api("/api/upload", { method: "POST", body });
      res.results.forEach((r) => { tally[r.status] = (tally[r.status] || 0) + 1; });
    }
    const parts = [`${tally.added} added`];
    if (tally.duplicate) parts.push(`${tally.duplicate} already in library`);
    if (tally.restored) parts.push(`${tally.restored} brought back from the trash`);
    if (tally.rejected) parts.push(`${tally.rejected} not supported`);
    summary = parts.join(" · ");
  } catch (err) {
    summary = `Upload failed: ${err.message}`;
  } finally {
    $("upload-btn").disabled = false;
    await refreshStatus();
    if (S.route.view === "grid") await reload();
    flashNote(summary);
  }
}

$("upload-btn").addEventListener("click", () => $("upload-input").click());
$("upload-input").addEventListener("change", (e) => { uploadFiles(e.target.files); e.target.value = ""; });

let dragDepth = 0;
const hasFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
window.addEventListener("dragenter", (e) => { if (hasFiles(e)) { dragDepth++; $("drop").hidden = false; } });
window.addEventListener("dragleave", (e) => { if (hasFiles(e) && --dragDepth <= 0) { dragDepth = 0; $("drop").hidden = true; } });
window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth = 0; $("drop").hidden = true;
  uploadFiles(e.dataTransfer.files);
});

// ------------------------------------------------------------------ viewer

const viewer = $("viewer");

function showFavorite(on) {
  $("v-fav").setAttribute("aria-pressed", String(!!on));
  $("v-fav").querySelector(".v-label").textContent = on ? "Favourite" : "Favourite";
  $("v-fav").title = on ? "Remove from favourites" : "Add to favourites";
}

function showArchived(on) {
  $("v-archive").querySelector(".v-label").textContent = on ? "Unarchive" : "Archive";
  $("v-archive").title = on ? "Put back in the timeline" : "Hide from the timeline, keep everywhere else";
}

function showItem(index) {
  const item = S.items[index];
  if (!item) return;
  S.viewing = index;
  showPicture({ ...item });
  $("v-prev").disabled = index === 0;
  $("v-next").disabled = index === S.items.length - 1 && S.done;
  renderStackStrip(null);
  loadDetail(item.id);
  if (index >= S.items.length - 6) loadMore();
}

/** Show one photo or video in the viewer (a grid item, or another photo of its stack). */
function showPicture(item) {
  S.detail = item;
  $("v-title").textContent = item.filename;
  $("v-download").href = `/api/assets/${item.id}/original?download=true`;
  showFavorite(item.favorite);
  showArchived(item.archived);
  const trashed = S.route.name === "trash";
  $("v-trash").querySelector(".v-label").textContent = trashed ? "Restore" : "Move to trash";
  const locked = !!item.locked;
  $("v-fav").hidden = trashed || locked;
  $("v-album").hidden = trashed || locked;
  $("v-archive").hidden = trashed || locked;
  $("v-edit").hidden = trashed || locked || item.kind !== "photo";
  $("v-trim").hidden = trashed || item.kind !== "video" || !(S.status && S.status.ffmpeg);
  $("v-play").hidden = trashed;
  $("v-trash").hidden = locked;

  const stage = $("v-stage");
  stage.replaceChildren();
  const src = `/api/assets/${item.id}/view?v=${item.v}`;
  if (item.kind === "video") {
    playVideo(item, stage);
  } else {
    const i = new Image();
    i.alt = item.filename;
    i.src = src;
    i.addEventListener("error", () => stage.replaceChildren(el("p", "msg", "This file cannot be displayed. Use Download.")));
    stage.append(i);
    if (item.live) stage.append(liveButton(item));
    const wide = item.width && item.height && item.width / item.height >= 2.4;
    if (item.projection || wide) stage.append(panoButton(item));
  }
  renderDetails(item);
  renderMarksBar(item);
}

/** Live Photos and motion photos: play the moment around the photo. */
function liveButton(item) {
  const b = button("", "live-btn");
  b.append(el("span", "live-dot"), el("span", null, "Live"));
  b.title = "Play the moment around this photo";
  b.setAttribute("aria-pressed", "false");
  b.addEventListener("click", (e) => {
    e.stopPropagation();
    const stage = $("v-stage");
    const old = stage.querySelector("video.live");
    if (old) { old.remove(); b.setAttribute("aria-pressed", "false"); return; }
    const v = document.createElement("video");
    v.className = "live";
    v.src = `/api/assets/${item.id}/motion?v=${item.v}`;
    v.autoplay = true;
    v.playsInline = true;
    const done = () => { v.remove(); b.setAttribute("aria-pressed", "false"); };
    v.addEventListener("ended", done);
    v.addEventListener("error", () => { done(); flashNote("This clip cannot be played here. It may need a video codec Windows doesn't have."); });
    v.addEventListener("click", done);
    stage.insertBefore(v, b);
    b.setAttribute("aria-pressed", "true");
  });
  return b;
}

/** The other photos of a stack, under the picture. */
function renderStackStrip(detail) {
  const strip = $("v-stack");
  const items = detail && detail.stack_items && detail.stack_items.length > 1 ? detail.stack_items : null;
  strip.hidden = !items;
  strip.replaceChildren();
  if (!items) return;
  for (const it of items) {
    const b = button("", "stack-item");
    b.append(img(thumbUrl(it.id, it.v), it.filename));
    if (it.cover) b.append(el("span", "stack-cover", "Cover"));
    b.title = it.filename;
    b.setAttribute("aria-label", it.filename);
    b.setAttribute("aria-current", String(S.detail && S.detail.id === it.id));
    b.addEventListener("click", async () => {
      if (S.detail && S.detail.id === it.id) return;
      try {
        const full = await api(`/api/assets/${it.id}`);
        showPicture(full);
        renderStackStrip(full);
      } catch (err) { flashNote(err.message); }
    });
    strip.append(b);
  }
}

async function loadDetail(id) {
  try {
    const full = await api(`/api/assets/${id}`);
    if (!S.detail || S.detail.id !== id) return;
    S.detail = full;
    renderDetails(full);
    renderStackStrip(full);
  } catch (_) { /* the basic details are already shown */ }
}

function renderDetails(item) {
  const dl = $("v-details");
  dl.replaceChildren();
  const row = (k, v) => {
    dl.append(el("dt", null, k));
    const dd = el("dd");
    if (v instanceof Node) dd.append(v); else dd.textContent = v;
    dl.append(dd);
  };
  row("File", item.filename);
  if (item.description !== undefined && !item.locked) row("Description", descriptionBox(item));
  const when = el("span", "with-action");
  when.append(document.createTextNode(fmtDateTime(item.taken_at) + (item.date_edited ? " (changed)" : "")));
  if (!item.locked && item.id !== undefined && item.description !== undefined) when.append(button("Change", "linklike", () => editDetails([item.id], item)));
  row("Taken", when);
  if (item.place) {
    row("Place", link(placeName(item.place), placeHash(item.place.country, item.place.state, item.place.city)));
  } else if (item.position) {
    row("Place", `${item.position[0].toFixed(4)}, ${item.position[1].toFixed(4)}`);
  }
  if (item.edited && !item.locked) {
    const box = el("span", "with-action");
    box.append(document.createTextNode("Edited in Snapshots "));
    const dl = link("Download edited", `/api/assets/${item.id}/edited`, "linklike");
    dl.setAttribute("download", "");
    box.append(dl);
    row("Edits", box);
  }
  if (item.people && item.people.length) {
    const chips = el("div", "face-chips");
    for (const p of item.people) {
      const chip = el("span", "face-chip");
      chip.append(img(faceUrl(p.face_id)));
      chip.append(link(p.name || "Unnamed", `#/people/${p.person_id}`));
      const x = button("×", null, async () => {
        const who = p.name || "this person";
        const ok = await confirmBox("Not the right person?", `Take this face out of ${who}'s photos?`, "Take it out");
        if (!ok) return;
        try { await post(`/api/faces/${p.face_id}/unassign`); loadDetail(item.id); } catch (err) { flashNote(err.message); }
      });
      x.setAttribute("aria-label", `This is not ${p.name || "this person"}`);
      x.title = "Not this person";
      chip.append(x);
      chips.append(chip);
    }
    row("People", chips);
  }
  if (item.albums && item.albums.length) {
    const box = el("span");
    item.albums.forEach((a, i) => {
      if (i) box.append(document.createTextNode(", "));
      box.append(link(a.name, `#/albums/${a.id}`));
    });
    row("Albums", box);
  }
  if (item.tags) {
    const box = el("div", "tag-chips");
    for (const t of item.tags) {
      const chip = el("span", "tag-chip dark");
      chip.append(link(t.name, `#/tags/${t.id}`));
      const x = button("×", null, async () => {
        try { await post(`/api/tags/${t.id}/remove`, { ids: [item.id] }); loadDetail(item.id); } catch (err) { flashNote(err.message); }
      });
      x.setAttribute("aria-label", `Remove the tag ${t.name}`);
      chip.append(x);
      box.append(chip);
    }
    box.append(button("+ Tag", "tag-add", () => addTag([item.id])));
    row("Tags", box);
  }
  if (item.stack_items && item.stack_items.length > 1) {
    const box = el("div", "stack-actions");
    box.append(el("span", null, `${plural(item.stack_items.length, "photo")} in this stack `));
    const isCover = item.stack_items.some((s) => s.id === item.id && s.cover);
    if (!isCover) box.append(button("Make cover", "btn small", async () => {
      try { await post(`/api/stacks/${item.stack}/cover`, { cover: item.id }); flashNote("Cover changed"); loadDetail(item.id); }
      catch (err) { flashNote(err.message); }
    }));
    box.append(button("Unstack", "btn small", async () => {
      try { await post("/api/stacks/unstack", { ids: [item.id] }); viewer.close(); await reload(); } catch (err) { flashNote(err.message); }
    }));
    row("Stack", box);
  }
  if (item.text) {
    const t = el("div", "photo-text", item.text.length > 600 ? item.text.slice(0, 600) + "…" : item.text);
    row("Text in photo", t);
  }
  if (item.quality) {
    const box = el("span", "quality-note", item.quality === "dark" ? "Looks too dark " : "Looks blurry ");
    box.append(button("It's fine", "btn small", async () => {
      try { await post("/api/cleanup/fine", { ids: [item.id] }); item.quality = null; renderDetails(item); } catch (err) { flashNote(err.message); }
    }));
    row("Quality", box);
  }
  if (item.comments !== undefined && !item.locked) row("Comments", commentsBox(item));
  row("Size", fmtSize(item.size));
  if (item.width && item.height) row("Dimensions", `${item.width} × ${item.height}`);
  if (item.camera) row("Camera", item.camera);
  if (item.folder !== undefined) {
    const where = el("span");
    where.append(link(item.folder || "Library folder", folderHash(item.folder)), document.createTextNode(` / ${item.filename}`));
    row("Location in library", where);
  } else {
    row("Location in library", item.path);
  }
}

async function step(delta) {
  if (S.viewing < 0) return;   // a photo opened on its own
  const next = S.viewing + delta;
  if (next < 0) return;
  if (next >= S.items.length) {
    await loadMore();
    if (next >= S.items.length) return;
  }
  showItem(next);
}

$("grid").addEventListener("click", (e) => {
  const t = e.target.closest(".tile");
  if (!t) return;
  const index = Number(t.dataset.index);
  if (S.selecting || e.ctrlKey || e.metaKey) {
    if (!S.selecting) startSelecting();
    toggleTile(index, e.shiftKey);
    return;
  }
  showItem(index);
  if (!viewer.open) viewer.showModal();
});
$("v-close").addEventListener("click", () => viewer.close());
$("v-prev").addEventListener("click", () => step(-1));
$("v-next").addEventListener("click", () => step(1));
$("v-info").addEventListener("click", () => {
  const open = $("v-panel").hidden;
  $("v-panel").hidden = !open;
  $("v-info").setAttribute("aria-pressed", String(open));
});
$("v-fav").addEventListener("click", () => {
  if (S.detail) setFavorite([S.detail.id], !S.detail.favorite);
});
$("v-album").addEventListener("click", () => { if (S.detail) addToAlbum([S.detail.id]); });
$("v-edit").addEventListener("click", () => { if (S.detail) openEditor(S.detail); });
$("v-trim").addEventListener("click", () => { if (S.detail) trimDialog(S.detail); });
$("v-play").addEventListener("click", () => { const at = S.viewing; viewer.close(); startSlideshow(Math.max(0, at)); });
$("v-archive").addEventListener("click", async () => {
  if (!S.detail) return;
  const id = S.detail.id, index = S.viewing, value = !S.detail.archived;
  const leaves = (S.route.filter || {}).archived === (value ? "hide" : "only");
  await setArchived([id], value);
  if (!leaves) return;
  if (!S.items.length) { viewer.close(); return; }
  showItem(Math.min(index, S.items.length - 1));
});
$("v-trash").addEventListener("click", async () => {
  if (!S.detail) return;
  const id = S.detail.id;
  const index = S.viewing;
  if (S.route.name === "trash") await restore([id]); else await moveToTrash([id]);
  if (!S.items.length) { viewer.close(); return; }
  showItem(Math.min(index, S.items.length - 1));
});
viewer.addEventListener("keydown", (e) => {
  if (e.target.closest && e.target.closest("input, textarea, select")) return;
  if ($("v-stage").querySelector(".pano-view") && e.key.startsWith("Arrow")) return;  // looking around
  if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
  else if (S.detail && !S.detail.locked && S.route.name !== "trash") {
    const change = markForKey(e, S.detail);
    if (change) { e.preventDefault(); setMarks([S.detail.id], change); }
  }
});
viewer.addEventListener("close", () => {
  $("v-stage").replaceChildren(); // stops any playing video
  renderStackStrip(null);
  S.detail = null;
  const t = document.querySelector(`.tile[data-index="${S.viewing}"]`);
  if (t) t.focus({ preventScroll: false });
});
// Clicking the dark area around the picture closes the viewer.
$("v-stage").addEventListener("click", (e) => { if (e.target === $("v-stage")) viewer.close(); });
// Following a link inside the viewer (a person, a place, an album) closes it.
$("v-details").addEventListener("click", (e) => {
  const a = e.target.closest("a");
  if (a && !a.hasAttribute("download")) viewer.close();
});

// ------------------------------------------------------------------ account menu

const menu = $("account-menu");
$("account-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  menu.hidden = !menu.hidden;
  $("account-btn").setAttribute("aria-expanded", String(!menu.hidden));
  if (!menu.hidden) menu.querySelector("button:not([hidden])").focus();
});
document.addEventListener("click", (e) => {
  if (!menu.hidden && !menu.contains(e.target)) { menu.hidden = true; $("account-btn").setAttribute("aria-expanded", "false"); }
});
menu.addEventListener("keydown", (e) => { if (e.key === "Escape") { menu.hidden = true; $("account-btn").focus(); } });
menu.addEventListener("click", async (e) => {
  const action = e.target.closest("button")?.dataset.action;
  if (!action) return;
  menu.hidden = true;
  $("account-btn").setAttribute("aria-expanded", "false");
  if (action === "logout") {
    try { await post("/api/auth/logout"); } catch (_) { /* logged out anyway */ }
    location.replace("/login");
  } else if (action === "password") {
    changePassword();
  } else if (action === "users") {
    manageUsers();
  } else if (action === "pin") {
    setPin();
  } else if (action === "keys") {
    manageKeys();
  } else if (action === "security") {
    loginSecurity();
  } else if (action === "settings") {
    location.hash = "#/settings";
  } else if (action === "import") {
    importDialog();
  }
});

async function changePassword() {
  const done = await modal({
    title: "Change your password", ok: "Change password",
    build: (body) => {
      const [a, current] = field("Current password", { type: "password", autocomplete: "current-password" });
      const [b, fresh] = field("New password (8 characters or more)", { type: "password", autocomplete: "new-password" });
      const [c, again] = field("New password again", { type: "password", autocomplete: "new-password" });
      body.append(a, b, c);
      return { submit: async () => {
        if (fresh.value.length < 8) throw new Error("Passwords need at least 8 characters.");
        if (fresh.value !== again.value) throw new Error("The two new passwords are different.");
        await post("/api/auth/password", { current: current.value, new: fresh.value });
        return true;
      } };
    },
  });
  if (done) flashNote("Password changed. Your other devices have been logged out.");
}

async function manageUsers() {
  await modal({
    title: "Accounts", ok: null, cancel: "Close", wide: true,
    build: (body, ctl) => {
      const list = el("div");
      const draw = async () => {
        try {
          const { users } = await api("/api/users");
          list.replaceChildren();
          for (const u of users) {
            const rowEl = el("div", "user-row");
            const who = el("div", "who");
            who.append(el("div", null, u.username), el("div", "role", (u.is_admin ? "Administrator" : "Member") + (u.two_step ? " · two-step login" : "")));
            rowEl.append(who);
            rowEl.append(button("Reset password", "btn small", async () => {
              const pw = window.prompt(`New password for ${u.username} (8 characters or more):`);
              if (pw == null) return;
              try { await patch(`/api/users/${u.id}`, { password: pw }); ctl.error(""); flashNote(`Password changed for ${u.username}`); }
              catch (err) { ctl.error(err.message); }
            }));
            if (u.two_step) {
              rowEl.append(button("Turn off two-step", "btn small", async () => {
                if (!window.confirm(`Turn off two-step login for ${u.username}? Use this when they lost their phone and their recovery codes. They can turn it on again.`)) return;
                try { await patch(`/api/users/${u.id}`, { two_step_off: true }); ctl.error(""); flashNote(`Two-step login is off for ${u.username}`); draw(); }
                catch (err) { ctl.error(err.message); }
              }));
            }
            rowEl.append(button(u.is_admin ? "Make member" : "Make administrator", "btn small", async () => {
              try { await patch(`/api/users/${u.id}`, { is_admin: !u.is_admin }); ctl.error(""); draw(); }
              catch (err) { ctl.error(err.message); }
            }));
            if (!S.me || u.id !== S.me.id) {
              rowEl.append(button("Delete", "btn small", async () => {
                const extra = u.locked ? ` Their locked folder holds ${plural(u.locked, "item")}: those will be visible to everyone again.` : "";
                if (!window.confirm(`Delete the account ${u.username}? Their photos stay in the library.${extra}`)) return;
                try { await del(`/api/users/${u.id}`); ctl.error(""); draw(); } catch (err) { ctl.error(err.message); }
              }));
            }
            list.append(rowEl);
          }
        } catch (err) { ctl.error(err.message); }
      };
      body.append(el("p", "muted", "Everyone sees the same library and albums. Favourites are personal. Administrators can manage accounts and empty the trash."));
      body.append(list);
      body.append(el("h3", "section-title", "Add an account"));
      const [a, name] = field("User name", { maxLength: 64, autocomplete: "off" });
      const [b, pw] = field("Password (8 characters or more)", { type: "password", autocomplete: "new-password" });
      const adminWrap = el("label", "field inline");
      const admin = el("input");
      admin.type = "checkbox";
      adminWrap.append(admin, el("span", null, "Administrator"));
      const add = button("Add account", "btn primary", async () => {
        try {
          await post("/api/users", { username: name.value.trim(), password: pw.value, is_admin: admin.checked });
          name.value = ""; pw.value = ""; admin.checked = false; ctl.error("");
          draw();
        } catch (err) { ctl.error(err.message); }
      });
      body.append(a, b, adminWrap, add);
      draw();
    },
  });
}

// ------------------------------------------------------------------ videos

/** Play a video in the viewer. Videos the app can't play (HEVC, AVI, MKV...) are converted once first. */
async function playVideo(item, stage) {
  const msg = el("p", "msg", "Loading…");
  stage.replaceChildren(msg);
  let info;
  try { info = await api(`/api/assets/${item.id}/play`); } catch (_) { info = { url: `/api/assets/${item.id}/view?v=${item.v}` }; }
  const current = () => S.detail && S.detail.id === item.id && viewer.open;
  while (info.preparing && current()) {
    if (info.error) {
      msg.textContent = `${info.error} Use Download to play it in another app.`;
      return;
    }
    msg.textContent = `Preparing this video so it can play here… ${Math.round((info.progress || 0) * 100)}%`;
    await new Promise((ok) => setTimeout(ok, 1500));
    if (!current()) return;
    try { info = await api(`/api/assets/${item.id}/play`); } catch (err) { msg.textContent = err.message; return; }
  }
  if (!current()) return;
  const v = document.createElement("video");
  v.controls = true;
  v.preload = "metadata";
  v.src = info.url;
  v.addEventListener("error", () => {
    const text = info.can_convert === false
      ? "This video can't be played here. Installing ffmpeg lets Snapshots convert it. Use Download meanwhile."
      : "This video cannot be played here. Use Download.";
    stage.replaceChildren(el("p", "msg", text));
  });
  stage.replaceChildren(v);
}

// ------------------------------------------------------------------ 360° photos and panoramas

function panoButton(item) {
  const sphere = item.projection === "sphere";
  const b = button(sphere ? "Look around" : "Pan", "live-btn pano-btn");
  b.title = sphere ? "Drag to look around this 360° photo" : "Scroll along this panorama";
  b.setAttribute("aria-pressed", "false");
  b.addEventListener("click", (e) => {
    e.stopPropagation();
    const stage = $("v-stage");
    const open = stage.querySelector(".pano-view");
    if (open) { open.dispatchEvent(new Event("close")); open.remove(); b.setAttribute("aria-pressed", "false"); return; }
    const view = sphere ? sphereView(`/api/assets/${item.id}/pano?v=${item.v}`) : stripView(`/api/assets/${item.id}/view?v=${item.v}`);
    stage.insertBefore(view, b);
    b.setAttribute("aria-pressed", "true");
    view.focus();
  });
  return b;
}

/** A wide panorama at full height, to scroll or drag along. */
function stripView(src) {
  const box = el("div", "pano-view strip");
  box.tabIndex = 0;
  const i = new Image();
  i.src = src;
  i.alt = "";
  box.append(i);
  let drag = null;
  box.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, left: box.scrollLeft }; box.setPointerCapture(e.pointerId); });
  box.addEventListener("pointermove", (e) => { if (drag) box.scrollLeft = drag.left - (e.clientX - drag.x); });
  box.addEventListener("pointerup", () => { drag = null; });
  box.addEventListener("wheel", (e) => { if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) { box.scrollLeft += e.deltaY; e.preventDefault(); } }, { passive: false });
  box.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") { box.scrollLeft -= 120; e.preventDefault(); }
    if (e.key === "ArrowRight") { box.scrollLeft += 120; e.preventDefault(); }
  });
  i.addEventListener("load", () => { box.scrollLeft = (box.scrollWidth - box.clientWidth) / 2; });
  return box;
}

/** A 360° photo drawn on the inside of a sphere (WebGL). Drag to look around, scroll to zoom. */
function sphereView(src) {
  const box = el("div", "pano-view sphere");
  box.tabIndex = 0;
  const canvas = el("canvas");
  const hint = el("span", "pano-hint", "Drag to look around · scroll to zoom");
  box.append(canvas, hint);
  const gl = canvas.getContext("webgl", { antialias: false });
  if (!gl) { box.replaceChildren(el("p", "msg", "This computer can't show 360° photos here.")); return box; }
  const shader = (type, text) => { const sh = gl.createShader(type); gl.shaderSource(sh, text); gl.compileShader(sh); return sh; };
  const prog = gl.createProgram();
  gl.attachShader(prog, shader(gl.VERTEX_SHADER, "attribute vec2 p; void main(){ gl_Position = vec4(p, 0.0, 1.0); }"));
  gl.attachShader(prog, shader(gl.FRAGMENT_SHADER, `
    precision highp float;
    uniform sampler2D tex; uniform vec2 res; uniform float yaw; uniform float pitch; uniform float fov;
    const float PI = 3.14159265359;
    void main() {
      vec2 q = (gl_FragCoord.xy / res) * 2.0 - 1.0;
      float t = tan(fov * 0.5);
      vec3 d = normalize(vec3(q.x * t * res.x / res.y, q.y * t, -1.0));
      float cp = cos(pitch), sp = sin(pitch);
      d = vec3(d.x, d.y * cp - d.z * sp, d.y * sp + d.z * cp);
      float cy = cos(yaw), sy = sin(yaw);
      d = vec3(d.x * cy - d.z * sy, d.y, d.x * sy + d.z * cy);
      float u = atan(d.x, -d.z) / (2.0 * PI) + 0.5;
      float v = 0.5 - asin(clamp(d.y, -1.0, 1.0)) / PI;
      gl_FragColor = texture2D(tex, vec2(u, v));
    }`));
  gl.linkProgram(prog);
  gl.useProgram(prog);
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prog, "p");
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  const u = (n) => gl.getUniformLocation(prog, n);
  const view = { yaw: 0, pitch: 0, fov: 75 * Math.PI / 180 };
  let ready = false, frame = 0, alive = true;
  const draw = () => {
    frame = 0;
    if (!alive || !ready) return;
    const w = Math.round(canvas.clientWidth * devicePixelRatio), h = Math.round(canvas.clientHeight * devicePixelRatio);
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    gl.viewport(0, 0, w, h);
    gl.uniform2f(u("res"), w, h);
    gl.uniform1f(u("yaw"), view.yaw);
    gl.uniform1f(u("pitch"), view.pitch);
    gl.uniform1f(u("fov"), view.fov);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  };
  const redraw = () => { if (!frame) frame = requestAnimationFrame(draw); };
  const image = new Image();
  image.onload = () => {
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, image);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.uniform1i(u("tex"), 0);
    ready = true;
    redraw();
  };
  image.onerror = () => box.replaceChildren(el("p", "msg", "This photo could not be loaded."));
  image.src = src;
  const limit = () => { view.pitch = Math.max(-1.5, Math.min(1.5, view.pitch)); view.fov = Math.max(0.5, Math.min(1.9, view.fov)); };
  let drag = null;
  canvas.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY }; canvas.setPointerCapture(e.pointerId); hint.hidden = true; });
  canvas.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const k = view.fov / canvas.clientHeight;   // the picture follows the pointer
    view.yaw -= (e.clientX - drag.x) * k;
    view.pitch += (e.clientY - drag.y) * k;
    drag = { x: e.clientX, y: e.clientY };
    limit(); redraw();
  });
  canvas.addEventListener("pointerup", () => { drag = null; });
  canvas.addEventListener("wheel", (e) => { view.fov *= e.deltaY > 0 ? 1.08 : 1 / 1.08; limit(); redraw(); e.preventDefault(); }, { passive: false });
  box.addEventListener("keydown", (e) => {
    const step = 0.08;
    if (e.key === "ArrowLeft") view.yaw -= step;
    else if (e.key === "ArrowRight") view.yaw += step;
    else if (e.key === "ArrowUp") view.pitch += step;
    else if (e.key === "ArrowDown") view.pitch -= step;
    else return;
    e.preventDefault(); limit(); redraw();
  });
  const resize = new ResizeObserver(redraw);
  resize.observe(box);
  box.addEventListener("close", () => { alive = false; resize.disconnect(); const ext = gl.getExtension("WEBGL_lose_context"); if (ext) ext.loseContext(); });
  return box;
}

// ------------------------------------------------------------------ slideshow

const showDlg = $("show");
const SH = { list: [], pos: 0, timer: null, paused: false, seen: new Set(), layer: 0 };

function showPhotos() { return S.items.filter((it) => it.kind === "photo"); }

async function startSlideshow(startIndex = 0) {
  if (!S.items.length) await loadMore();
  if (!showPhotos().length) { flashNote("There are no photos to show here."); return; }
  const start = S.items[startIndex] && S.items[startIndex].kind === "photo" ? S.items[startIndex] : showPhotos()[0];
  SH.list = [start];
  SH.pos = 0;
  SH.seen = new Set([start.id]);
  SH.paused = false;
  $("show-pause").textContent = "Pause";
  $("show-stage").replaceChildren(el("img", "show-img"), el("img", "show-img"));
  showDlg.showModal();
  try { if (showDlg.requestFullscreen) await showDlg.requestFullscreen(); } catch (_) { /* a window is fine too */ }
  showSlide();
}

async function nextPhoto() {
  const shuffle = $("show-shuffle").checked;
  let pool = showPhotos().filter((it) => !SH.seen.has(it.id));
  if (!pool.length && !S.done) { await loadMore(); pool = showPhotos().filter((it) => !SH.seen.has(it.id)); }
  if (!pool.length) { SH.seen = new Set(); pool = showPhotos(); }   // start again from the beginning
  if (!pool.length) return null;
  if (shuffle) return pool[Math.floor(Math.random() * pool.length)];
  const cur = SH.list[SH.pos];
  const all = showPhotos();
  const at = all.findIndex((it) => it.id === cur.id);
  return all.slice(at + 1).find((it) => !SH.seen.has(it.id)) || pool[0];
}

function showSlide() {
  clearTimeout(SH.timer);
  const item = SH.list[SH.pos];
  if (!item) return;
  const layers = $("show-stage").querySelectorAll(".show-img");
  SH.layer = 1 - SH.layer;
  const front = layers[SH.layer], back = layers[1 - SH.layer];
  front.alt = item.filename;
  front.onload = () => { front.classList.add("on"); back.classList.remove("on"); };
  front.onerror = () => { if (!SH.paused) SH.timer = setTimeout(() => step2(1), 300); };
  front.src = `/api/assets/${item.id}/view?v=${item.v}`;
  const total = showPhotos().length;
  $("show-count").textContent = `${fmtInt(SH.seen.size)} of ${fmtInt(total)}${S.done ? "" : "+"}`;
  if (!SH.paused) SH.timer = setTimeout(() => step2(1), Number($("show-speed").value) * 1000);
  // Fetch the next picture ahead of time, so it appears without waiting.
  nextPhoto().then((n) => { if (n) { const pre = new Image(); pre.src = `/api/assets/${n.id}/view?v=${n.v}`; SH.peek = n; } });
}

async function step2(delta) {
  if (!showDlg.open) return;
  if (delta < 0) {
    if (SH.pos > 0) SH.pos--;
  } else if (SH.pos < SH.list.length - 1) {
    SH.pos++;
  } else {
    const n = SH.peek && !SH.seen.has(SH.peek.id) ? SH.peek : await nextPhoto();
    if (!n) return;
    SH.list.push(n);
    SH.seen.add(n.id);
    SH.pos = SH.list.length - 1;
  }
  showSlide();
}

function togglePause() {
  SH.paused = !SH.paused;
  $("show-pause").textContent = SH.paused ? "Play" : "Pause";
  if (SH.paused) clearTimeout(SH.timer); else showSlide();
}

$("show-btn").addEventListener("click", () => startSlideshow(0));
$("show-prev").addEventListener("click", () => step2(-1));
$("show-next").addEventListener("click", () => step2(1));
$("show-pause").addEventListener("click", togglePause);
$("show-close").addEventListener("click", () => showDlg.close());
$("show-speed").addEventListener("change", () => { if (!SH.paused) showSlide(); });
$("show-stage").addEventListener("click", () => step2(1));
showDlg.addEventListener("keydown", (e) => {
  if (e.target.closest("select, input")) return;
  if (e.key === "ArrowRight") { step2(1); e.preventDefault(); }
  else if (e.key === "ArrowLeft") { step2(-1); e.preventDefault(); }
  else if (e.key === " ") { togglePause(); e.preventDefault(); }
});
// The controls fade away while the mouse is still.
let showIdle = null;
showDlg.addEventListener("pointermove", () => {
  showDlg.classList.remove("idle");
  clearTimeout(showIdle);
  showIdle = setTimeout(() => showDlg.classList.add("idle"), 2500);
});
showDlg.addEventListener("close", () => {
  clearTimeout(SH.timer);
  $("show-stage").replaceChildren();
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
});

// ------------------------------------------------------------------ the editor

const editor = $("editor");
const ED = {};

/** Edit a photo: turn, mirror, straighten and crop. Only the recipe is saved; the file is untouched. */
async function openEditor(item) {
  let full = item;
  if (full.edits === undefined) { try { full = await api(`/api/assets/${item.id}`); } catch (err) { flashNote(err.message); return; } }
  const e = full.edits || {};
  Object.assign(ED, {
    item: full, image: null, rotate: e.rotate || 0, flip: !!e.flip, angle: e.straighten || 0,
    crop: e.crop ? [...e.crop] : [0, 0, 1, 1], ratio: "free", box: null,
  });
  $("ed-angle").value = ED.angle;
  $("ed-angle-out").textContent = `${ED.angle}°`;
  $("ed-flip").setAttribute("aria-pressed", String(ED.flip));
  document.querySelectorAll(".ed-ratio").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.ratio === "free")));
  $("ed-crop").hidden = true;
  editor.showModal();
  const image = new Image();
  image.onload = () => { ED.image = image; $("ed-crop").hidden = false; drawEditor(); };
  image.onerror = () => { flashNote("This photo can't be edited."); editor.close(); };
  image.src = `/api/assets/${full.id}/view?plain=true&v=${full.v}`;
}

function edFrame() {
  const w = ED.image.naturalWidth, h = ED.image.naturalHeight;
  return ED.rotate % 180 ? [h, w] : [w, h];
}

function drawEditor() {
  if (!ED.image) return;
  const canvas = $("ed-canvas");
  const stage = $("ed-stage");
  const [W, H] = edFrame();
  const pad = 24;
  const k = Math.min((stage.clientWidth - pad * 2) / W, (stage.clientHeight - pad * 2) / H);
  const cw = Math.max(1, Math.round(W * k)), ch = Math.max(1, Math.round(H * k));
  const dpr = devicePixelRatio || 1;
  canvas.width = Math.round(cw * dpr);
  canvas.height = Math.round(ch * dpr);
  canvas.style.width = `${cw}px`;
  canvas.style.height = `${ch}px`;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  // The same steps as the server: turn, mirror, straighten (enlarged to fill), then crop.
  const t = ED.angle * Math.PI / 180;
  const s = Math.cos(Math.abs(t)) + Math.sin(Math.abs(t)) * Math.max(W / H, H / W);
  ctx.translate(canvas.width / 2, canvas.height / 2);
  ctx.scale(k * dpr, k * dpr);
  ctx.rotate(t);
  ctx.scale(s, s);
  if (ED.flip) ctx.scale(-1, 1);
  ctx.rotate(ED.rotate * Math.PI / 180);
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(ED.image, -ED.image.naturalWidth / 2, -ED.image.naturalHeight / 2);
  ED.box = { left: canvas.offsetLeft, top: canvas.offsetTop, w: cw, h: ch, W, H };
  placeCrop();
}

function placeCrop() {
  const b = ED.box;
  if (!b) return;
  const [x, y, w, h] = ED.crop;
  const c = $("ed-crop");
  c.style.left = `${b.left + x * b.w}px`;
  c.style.top = `${b.top + y * b.h}px`;
  c.style.width = `${w * b.w}px`;
  c.style.height = `${h * b.h}px`;
}

function ratioValue() {
  if (ED.ratio === "free") return null;
  if (ED.ratio === "orig") { const [W, H] = edFrame(); return W / H; }
  return Number(ED.ratio);
}

/** The biggest centred crop with the chosen shape (as width / height of the picture, in pixels). */
function fitRatio(r) {
  const [W, H] = edFrame();
  let w = 1, h = (W / r) / H;
  if (h > 1) { h = 1; w = (H * r) / W; }
  ED.crop = [(1 - w) / 2, (1 - h) / 2, w, h];
}

let edDrag = null;
$("ed-crop").addEventListener("pointerdown", (e) => {
  e.preventDefault();
  $("ed-crop").setPointerCapture(e.pointerId);
  edDrag = { handle: e.target.dataset.h || "move", x: e.clientX, y: e.clientY, crop: [...ED.crop] };
});
$("ed-crop").addEventListener("pointermove", (e) => {
  if (!edDrag || !ED.box) return;
  const dx = (e.clientX - edDrag.x) / ED.box.w, dy = (e.clientY - edDrag.y) / ED.box.h;
  let [x, y, w, h] = edDrag.crop;
  const min = 0.05;
  if (edDrag.handle === "move") {
    x = Math.min(Math.max(0, x + dx), 1 - w);
    y = Math.min(Math.max(0, y + dy), 1 - h);
  } else {
    const hh = edDrag.handle;
    let x2 = x + w, y2 = y + h;
    if (hh.includes("w")) x = Math.min(Math.max(0, x + dx), x2 - min);
    if (hh.includes("e")) x2 = Math.max(Math.min(1, x2 + dx), x + min);
    if (hh.includes("n")) y = Math.min(Math.max(0, y + dy), y2 - min);
    if (hh.includes("s")) y2 = Math.max(Math.min(1, y2 + dy), y + min);
    w = x2 - x; h = y2 - y;
    const r = ratioValue();
    if (r) {
      // Keep the shape: the height follows the width (in pixels), within the picture.
      const { W, H } = ED.box;
      h = (w * W / r) / H;
      if (hh.includes("n")) y = y2 - h;
      if (y < 0 || y + h > 1 || h < min) {
        h = h < min ? min : hh.includes("n") ? y2 : 1 - y;
        w = (h * H * r) / W;
        if (hh.includes("w")) x = x2 - w;
        if (hh.includes("n")) y = y2 - h;
      }
    }
  }
  ED.crop = [x, y, w, h];
  placeCrop();
});
$("ed-crop").addEventListener("pointerup", () => { edDrag = null; });

$("ed-left").addEventListener("click", () => { ED.rotate = (ED.rotate + 270) % 360; ED.crop = [0, 0, 1, 1]; if (ratioValue()) fitRatio(ratioValue()); drawEditor(); });
$("ed-right").addEventListener("click", () => { ED.rotate = (ED.rotate + 90) % 360; ED.crop = [0, 0, 1, 1]; if (ratioValue()) fitRatio(ratioValue()); drawEditor(); });
$("ed-flip").addEventListener("click", () => { ED.flip = !ED.flip; $("ed-flip").setAttribute("aria-pressed", String(ED.flip)); drawEditor(); });
$("ed-angle").addEventListener("input", () => { ED.angle = Number($("ed-angle").value); $("ed-angle-out").textContent = `${ED.angle}°`; drawEditor(); });
$("ed-angle").addEventListener("dblclick", () => { ED.angle = 0; $("ed-angle").value = 0; $("ed-angle-out").textContent = "0°"; drawEditor(); });
document.querySelectorAll(".ed-ratio").forEach((b) => b.addEventListener("click", () => {
  ED.ratio = b.dataset.ratio;
  document.querySelectorAll(".ed-ratio").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  if (ratioValue()) fitRatio(ratioValue()); else ED.crop = [0, 0, 1, 1];
  placeCrop();
}));
$("ed-reset").addEventListener("click", () => {
  Object.assign(ED, { rotate: 0, flip: false, angle: 0, crop: [0, 0, 1, 1], ratio: "free" });
  $("ed-angle").value = 0; $("ed-angle-out").textContent = "0°";
  $("ed-flip").setAttribute("aria-pressed", "false");
  document.querySelectorAll(".ed-ratio").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.ratio === "free")));
  drawEditor();
});
$("ed-cancel").addEventListener("click", () => editor.close());
$("ed-save").addEventListener("click", async () => {
  const round = (v) => Math.round(v * 100000) / 100000;
  const crop = ED.crop.map(round);
  const whole = crop[0] <= 0.001 && crop[1] <= 0.001 && crop[2] >= 0.999 && crop[3] >= 0.999;
  const edits = { rotate: ED.rotate, flip: ED.flip, straighten: ED.angle, crop: whole ? null : crop };
  $("ed-save").disabled = true;
  try {
    const updated = await post(`/api/assets/${ED.item.id}/edits`, { edits });
    editor.close();
    applyUpdated(updated);
    flashNote(updated.edited ? "Saved. Your original file is unchanged." : "Back to the original picture.");
  } catch (err) { flashNote(err.message); }
  $("ed-save").disabled = false;
});
editor.addEventListener("close", () => { ED.image = null; });
new ResizeObserver(() => { if (editor.open) drawEditor(); }).observe($("ed-stage"));

/** An item changed on the server (an edit): refresh its tile and, if open, the viewer. */
function applyUpdated(updated) {
  const it = S.items.find((x) => x.id === updated.id);
  if (it) Object.assign(it, updated);
  const t = $("grid").querySelector(`.tile[data-id="${updated.id}"] img`);
  if (t) t.src = thumbUrl(updated.id, updated.v);
  if (viewer.open && S.detail && S.detail.id === updated.id) {
    showPicture({ ...S.detail, ...updated });
    loadDetail(updated.id);
  }
}

// ------------------------------------------------------------------ details: date, place, description

function descriptionBox(item) {
  const box = el("textarea", "desc-input");
  box.value = item.description || "";
  box.placeholder = "Add a description";
  box.rows = 2;
  box.maxLength = 5000;
  box.setAttribute("aria-label", "Description");
  box.addEventListener("change", async () => {
    try {
      await post("/api/assets/details", { ids: [item.id], description: box.value });
      item.description = box.value.trim();
      flashNote("Description saved");
    } catch (err) { flashNote(err.message); }
  });
  return box;
}

function toLocalInput(iso) { return (iso || "").slice(0, 16); }

/** Change the date, location or description of one or many items. */
async function editDetails(ids, item) {
  const one = ids.length === 1;
  if (one && (!item || item.description === undefined)) {
    try { item = await api(`/api/assets/${ids[0]}`); } catch (err) { flashNote(err.message); return; }
  }
  let map = null;
  const choice = { position: undefined };   // [lat, lon], null (remove) or "file" (from the file)
  const done = await modal({
    title: one ? "Edit details" : `Edit details of ${plural(ids.length, "item")}`, ok: "Save", wide: true,
    build: (body) => {
      body.append(el("p", "muted", "Snapshots remembers these changes. Your files are not changed."));
      body.append(el("h3", "section-title small", "Date and time"));
      const date = el("input");
      date.type = "datetime-local";
      date.step = 60;
      if (one) date.value = toLocalInput(item.taken_at);
      const dateWrap = el("label", "field");
      dateWrap.append(el("span", null, one ? "Taken" : "Set all to (leave empty to keep their dates)"), date);
      body.append(dateWrap);
      let shiftH = null, shiftM = null;
      if (!one) {
        const row = el("div", "field-row");
        const [a, h] = field("Or move all by hours", { type: "number", step: 1, value: "", placeholder: "0" });
        const [b, m] = field("and minutes", { type: "number", step: 1, value: "", placeholder: "0" });
        row.append(a, b);
        body.append(row, el("p", "muted small", "Useful when a camera's clock was wrong: use a minus sign to move them earlier."));
        shiftH = h; shiftM = m;
      }
      let resetDate = false;
      if (one && item.date_edited) {
        body.append(button("Use the date in the file", "btn small", () => { resetDate = true; date.value = ""; date.disabled = true; }));
      }

      body.append(el("h3", "section-title small", "Location"));
      const where = el("p", "muted");
      const say = () => {
        if (choice.position === null) where.textContent = "No location (it will be removed).";
        else if (choice.position === "file") where.textContent = "The location saved in the file.";
        else if (choice.position) where.textContent = `Chosen: ${choice.position[0].toFixed(5)}, ${choice.position[1].toFixed(5)}`;
        else if (one && item.position) where.textContent = `Now: ${item.place ? placeName(item.place) + " · " : ""}${item.position[0].toFixed(5)}, ${item.position[1].toFixed(5)}`;
        else where.textContent = one ? "No location yet. Search for a town, type coordinates, or click the map." : "Unchanged. Search for a town, type coordinates, or click the map.";
      };
      const [findWrap, find] = field("Town or coordinates", { placeholder: "Sedona, or 34.87, -111.76", autocomplete: "off" });
      const results = el("div", "pick-list compact");
      let timer = null;
      const pick = (lat, lon, zoom = 12) => {
        choice.position = [lat, lon];
        say();
        if (map) { map.setView([lat, lon], zoom); placePin(); }
      };
      find.addEventListener("input", () => {
        clearTimeout(timer);
        const text = find.value.trim();
        const m = text.match(/^(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)$/);
        results.replaceChildren();
        if (m) {
          const lat = Number(m[1]), lon = Number(m[2]);
          if (Math.abs(lat) <= 90 && Math.abs(lon) <= 180) results.append(button(`Use ${lat}, ${lon}`, "pick", () => pick(lat, lon)));
          return;
        }
        if (text.length < 2) return;
        timer = setTimeout(async () => {
          try {
            const { places } = await api(`/api/places/search?q=${encodeURIComponent(text)}`);
            results.replaceChildren();
            if (!places.length) results.append(el("p", "muted small", "No town found. The town list downloads once Places has run."));
            for (const p of places) {
              const b = button("", "pick", () => { pick(p.lat, p.lon); results.replaceChildren(); find.value = placeName(p); });
              const t = el("span", null, p.city);
              t.append(el("small", null, [p.state, p.country].filter(Boolean).join(", ")));
              b.append(t);
              results.append(b);
            }
          } catch (_) { /* typing on */ }
        }, 250);
      });
      const mapBox = el("div", "pick-map");
      const tools = el("div", "row-actions");
      tools.append(button("Remove location", "btn small", () => { choice.position = null; say(); placePin(); }));
      if (one && item.position_edited) tools.append(button("Use the location in the file", "btn small", () => { choice.position = "file"; say(); placePin(); }));
      body.append(findWrap, results, mapBox, where, tools);
      say();
      let pin = null;
      const placePin = () => {
        if (!map) return;
        const at = Array.isArray(choice.position) ? choice.position : (choice.position === undefined && one && item.position) ? item.position : null;
        if (pin) { pin.remove(); pin = null; }
        if (at) pin = L.circleMarker(at, { radius: 9, weight: 3, color: "#fff", fillColor: "#0d5751", fillOpacity: 1 }).addTo(map);
      };
      loadMapLibs().then(() => {
        if (!document.body.contains(mapBox)) return;
        const st = (S.status && S.status.map) || {};
        map = L.map(mapBox, { minZoom: 1, maxZoom: 19, worldCopyJump: true });
        map.attributionControl.setPrefix(false);
        if (st.tiles) L.tileLayer(st.tiles, { maxZoom: 19, attribution: st.attribution || "", className: "map-tiles", referrerPolicy: "no-referrer" }).addTo(map);
        else loadWorld().then((w) => L.geoJSON(w, { interactive: false, style: { className: "map-country", weight: 1, fillOpacity: 1 } }).addTo(map));
        const start = one && item.position ? item.position : null;
        if (start) map.setView(start, 11); else map.setView([30, 0], 2);
        map.on("click", (e) => { choice.position = [e.latlng.lat, ((e.latlng.lng + 540) % 360) - 180]; say(); placePin(); });
        placePin();
        setTimeout(() => map.invalidateSize(), 50);
      }).catch(() => { mapBox.replaceWith(el("p", "muted", "The map could not be loaded; type coordinates instead.")); });

      body.append(el("h3", "section-title small", "Description"));
      const desc = el("textarea", "desc-input light");
      desc.rows = 3;
      desc.maxLength = 5000;
      desc.placeholder = one ? "Add a description" : "Leave empty to keep each item's description";
      if (one) desc.value = item.description || "";
      body.append(desc);

      return { submit: async () => {
        const req = { ids };
        if (resetDate) req.reset_date = true;
        else if (date.value && (!one || date.value !== toLocalInput(item.taken_at))) req.taken_at = date.value.length === 16 ? date.value : date.value.slice(0, 19);
        else if (!one) {
          const minutes = (Number(shiftH.value) || 0) * 60 + (Number(shiftM.value) || 0);
          if (minutes) req.shift_minutes = minutes;
        }
        if (choice.position === null) req.clear_position = true;
        else if (choice.position === "file") req.reset_position = true;
        else if (choice.position) req.position = choice.position;
        if (one ? desc.value.trim() !== (item.description || "") : desc.value.trim()) req.description = desc.value;
        if (Object.keys(req).length === 1) return true;
        await post("/api/assets/details", req);
        return true;
      } };
    },
  });
  if (!done) return;
  flashNote(one ? "Details saved" : `Details of ${plural(ids.length, "item")} saved`);
  if (S.selecting) stopSelecting();
  if (viewer.open && S.detail && ids.includes(S.detail.id)) loadDetail(S.detail.id);
  if (S.route.view === "grid" && !viewer.open) reload();
}

// ------------------------------------------------------------------ comments

function commentList(list, comments, onDelete) {
  list.replaceChildren();
  if (!comments.length) list.append(el("p", "muted small", "No comments yet."));
  for (const c of comments) {
    const row = el("div", "comment");
    const head = el("div", "comment-head");
    head.append(el("strong", null, c.user), el("span", "muted", fmtDateTime(c.created_at)));
    if (c.can_delete) {
      const x = button("Delete", "linklike small", () => onDelete(c));
      x.setAttribute("aria-label", "Delete this comment");
      head.append(x);
    }
    row.append(head, el("div", "comment-text", c.text));
    list.append(row);
  }
}

function commentForm(onPost) {
  const form = el("div", "comment-form");
  const box = el("textarea", "desc-input");
  box.rows = 2;
  box.maxLength = 2000;
  box.placeholder = "Write a comment";
  box.setAttribute("aria-label", "Comment");
  const send = button("Post", "btn small", async () => {
    const text = box.value.trim();
    if (!text) return;
    send.disabled = true;
    try { await onPost(text); box.value = ""; } catch (err) { flashNote(err.message); }
    send.disabled = false;
  });
  box.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send.click(); } });
  form.append(box, send);
  return form;
}

/** Comments under a photo, in the viewer's details. */
function commentsBox(item) {
  const box = el("div", "comments");
  const list = el("div", "comment-list");
  const load = async () => {
    try {
      const { comments } = await api(`/api/assets/${item.id}/comments`);
      commentList(list, comments, async (c) => {
        try { await del(`/api/comments/${c.id}`); load(); } catch (err) { flashNote(err.message); }
      });
    } catch (_) { list.replaceChildren(el("p", "muted small", "Comments could not be loaded.")); }
  };
  box.append(list, commentForm(async (text) => { await post(`/api/assets/${item.id}/comments`, { text }); load(); }));
  if (item.comments) load(); else commentList(list, [], null);
  return box;
}

async function albumComments(album) {
  await modal({
    title: `Comments on ${album.name}`, ok: null, cancel: "Close", wide: true,
    build: (body) => {
      const list = el("div", "comment-list light");
      const load = async () => {
        const { comments } = await api(`/api/albums/${album.id}/comments`);
        commentList(list, comments, async (c) => { try { await del(`/api/comments/${c.id}`); load(); } catch (err) { flashNote(err.message); } });
      };
      body.append(list, commentForm(async (text) => { await post(`/api/albums/${album.id}/comments`, { text }); load(); }));
      load();
    },
  });
  renderHead();
}

// ------------------------------------------------------------------ activity

async function renderActivity() {
  const data = await api("/api/activity");
  headRow("Activity", null, [], "Comments, and photos people added to albums.");
  if (!data.items.length) {
    showEmpty("Nothing yet", "When someone comments on a photo or adds photos to an album, it shows up here.");
  } else {
    const list = el("div", "activity");
    for (const i of data.items) {
      const row = el("div", i.new ? "act new" : "act");
      let pic = el("span", "act-pic");
      let target = null;
      const text = el("div", "act-text");
      const who = i.mine ? "You" : i.user;
      if (i.kind === "comment") {
        if (i.asset) {
          pic = link("", i.asset.trashed ? "#/trash" : "#/activity", "act-pic");
          pic.append(img(thumbUrl(i.asset.id, i.asset.v)));
          if (!i.asset.trashed) pic.addEventListener("click", (e) => { e.preventDefault(); openPhoto(i.asset.id); });
          pic.setAttribute("aria-label", `Open ${i.asset.filename}`);
          target = i.asset.filename;
        } else if (i.album) {
          pic = link("", `#/albums/${i.album.id}`, "act-pic album");
          target = i.album.name;
        }
        const line = el("div");
        line.append(el("strong", null, who), document.createTextNode(` commented on ${target || "something deleted"}`));
        text.append(line, el("div", "act-quote", i.text));
      } else {
        pic = link("", `#/albums/${i.album.id}`, "act-pic");
        if (i.cover) pic.append(img(thumbUrl(i.cover, i.cover_v)));
        const line = el("div");
        line.append(el("strong", null, who), document.createTextNode(` added ${plural(i.count, "item")} to `), link(i.album.name, `#/albums/${i.album.id}`));
        text.append(line);
      }
      text.append(el("div", "muted small", fmtDateTime(i.at)));
      row.append(pic, text);
      list.append(row);
    }
    $("collection").replaceChildren(list);
  }
  try { await post("/api/activity/seen"); } catch (_) { /* fine */ }
  refreshStatus();
}

/** Show one photo in the viewer on its own (from the Activity page). */
async function openPhoto(id) {
  try {
    const item = await api(`/api/assets/${id}`);
    S.viewing = -1;
    showPicture(item);
    renderStackStrip(item);
    $("v-prev").disabled = true;
    $("v-next").disabled = true;
    if (!viewer.open) viewer.showModal();
  } catch (err) { flashNote(err.message); }
}

// ------------------------------------------------------------------ the locked folder

async function renderLockedGate() {
  const st = (S.status && S.status.locked) || {};
  headRow("Locked", null, [], "A folder only you can open, with a PIN. What you put in it is hidden from everyone everywhere else in Snapshots.");
  const box = el("div", "gate");
  if (!st.has_pin) {
    box.append(el("h2", null, "Set a PIN first"),
      el("p", "muted", "You'll type it each time you open your locked folder. It is separate from your password."),
      button("Set a PIN", "btn primary", async () => { if (await setPin()) render(); }));
  } else {
    const [wrap, pin] = field("PIN", { type: "password", inputMode: "numeric", autocomplete: "off", maxLength: 32 });
    const err = el("p", "form-error");
    const open = button("Open", "btn primary", async () => {
      err.textContent = "";
      try {
        await post("/api/locked/open", { pin: pin.value });
        await refreshStatus();
        render();
      } catch (x) { err.textContent = x.message; pin.select(); }
    });
    pin.addEventListener("keydown", (e) => { if (e.key === "Enter") open.click(); });
    box.append(el("h2", null, `Your locked folder${st.count ? ` · ${plural(st.count, "item")}` : ""}`), wrap, err, open,
      el("p", "muted small", "It closes again after 10 minutes, or when you press Lock now."));
    setTimeout(() => pin.focus(), 0);
  }
  $("collection").replaceChildren(box);
}

async function lockNow() {
  try { await post("/api/locked/close"); } catch (_) { /* closed anyway */ }
  await refreshStatus();
  render();
}

async function setLocked(ids, value) {
  const st = (S.status && S.status.locked) || {};
  if (value && !st.has_pin) {
    const ok = await confirmBox("Set a PIN first", "Your locked folder opens with a PIN. Set one now?", "Set a PIN");
    if (!ok || !(await setPin())) return;
  }
  if (value) {
    const ok = await confirmBox(`Move ${plural(ids.length, "item")} to your locked folder?`,
      "They'll be hidden everywhere else in Snapshots, for everyone, until you move them out. The files stay in your library folder, so anyone using this computer's folders can still find them.",
      "Move to Locked");
    if (!ok) return;
  }
  try {
    await post("/api/locked", { ids, value });
    removeItems(ids);
    if (viewer.open && S.detail && ids.includes(S.detail.id)) viewer.close();
    flashNote(value ? `${plural(ids.length, "item")} moved to your locked folder` : `${plural(ids.length, "item")} back in the library`);
    refreshStatus();
  } catch (err) {
    flashNote(err.message);
    if (/closed|Open the locked/.test(err.message)) { await refreshStatus(); render(); }
  }
}

async function setPin() {
  const st = (S.status && S.status.locked) || {};
  const done = await modal({
    title: st.has_pin ? "Change your locked folder PIN" : "Set a PIN for your locked folder", ok: "Save PIN",
    build: (body) => {
      body.append(el("p", "muted", "4 to 32 characters. Numbers are easiest to type on a phone."));
      const [a, pw] = field("Your account password", { type: "password", autocomplete: "current-password" });
      const [b, pin] = field("PIN", { type: "password", inputMode: "numeric", autocomplete: "off", maxLength: 32 });
      const [c, again] = field("PIN again", { type: "password", inputMode: "numeric", autocomplete: "off", maxLength: 32 });
      body.append(a, b, c);
      return { submit: async () => {
        if (pin.value !== again.value) throw new Error("The two PINs are different.");
        await post("/api/locked/pin", { pin: pin.value, password: pw.value });
        return true;
      } };
    },
  });
  if (done) { flashNote("PIN saved"); await refreshStatus(); }
  return !!done;
}

// ------------------------------------------------------------------ API keys

async function manageKeys() {
  await modal({
    title: "API keys", ok: null, cancel: "Close", wide: true,
    build: (body, ctl) => {
      body.append(el("p", "muted", "Keys let scripts and other apps use Snapshots as you, without your password. Send one as “Authorization: Bearer <key>”. A key can't open your locked folder or make more keys."));
      const list = el("div");
      const fresh = el("div", "key-fresh");
      fresh.hidden = true;
      const draw = async () => {
        try {
          const { keys } = await api("/api/keys");
          list.replaceChildren();
          if (!keys.length) list.append(el("p", "muted", "No keys yet."));
          for (const k of keys) {
            const r = el("div", "user-row");
            const who = el("div", "who");
            who.append(el("div", null, k.name), el("div", "role",
              `${k.hint}… · made ${fmtDay(k.created_at, { day: "numeric", month: "short", year: "numeric" })}` +
              (k.last_used_at ? ` · last used ${fmtDay(k.last_used_at, { day: "numeric", month: "short", year: "numeric" })}` : " · never used")));
            r.append(who, button("Delete", "btn small", async () => {
              if (!window.confirm(`Delete the key “${k.name}”? Anything using it will stop working.`)) return;
              try { await del(`/api/keys/${k.id}`); draw(); } catch (err) { ctl.error(err.message); }
            }));
            list.append(r);
          }
        } catch (err) { ctl.error(err.message); }
      };
      const [a, name] = field("Name for a new key", { placeholder: "What will use it, like “Backup script”", maxLength: 100 });
      const add = button("Make key", "btn primary", async () => {
        try {
          const made = await post("/api/keys", { name: name.value.trim() });
          name.value = ""; ctl.error("");
          fresh.replaceChildren(el("strong", null, `Your new key “${made.name}”. Copy it now: it won't be shown again.`));
          const code = el("input", "key-value");
          code.readOnly = true;
          code.value = made.key;
          code.addEventListener("focus", () => code.select());
          const copy = button("Copy", "btn small", async () => {
            try { await navigator.clipboard.writeText(made.key); copy.textContent = "Copied"; } catch (_) { code.select(); }
          });
          const line = el("div", "row-actions");
          line.append(code, copy);
          fresh.append(line);
          fresh.hidden = false;
          draw();
        } catch (err) { ctl.error(err.message); }
      });
      body.append(fresh, list, el("h3", "section-title small", "New key"), a, add);
      draw();
    },
  });
}

// ------------------------------------------------------------------ login security

let qrLib = null;
function loadQr() {
  qrLib = qrLib || new Promise((ok, fail) => {
    const t = document.createElement("script");
    t.src = "/static/vendor/qrcode.js";
    t.onload = ok;
    t.onerror = () => { qrLib = null; fail(new Error("The QR code could not be drawn.")); };
    document.head.append(t);
  });
  return qrLib;
}

/** A QR code on a canvas (dark squares on white, which phones read best in either theme). */
async function qrCanvas(text) {
  await loadQr();
  const qr = window.qrcode(0, "M");
  qr.addData(text);
  qr.make();
  const n = qr.getModuleCount(), cell = 5, quiet = 4;
  const canvas = el("canvas", "qr");
  canvas.width = canvas.height = (n + quiet * 2) * cell;
  const g = canvas.getContext("2d");
  g.fillStyle = "#fff";
  g.fillRect(0, 0, canvas.width, canvas.height);
  g.fillStyle = "#000";
  for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) {
    if (qr.isDark(r, c)) g.fillRect((c + quiet) * cell, (r + quiet) * cell, cell, cell);
  }
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", "QR code to scan with an authenticator app");
  return canvas;
}

function recoveryBlock(codes) {
  const box = el("div", "key-fresh");
  box.append(el("strong", null, "Your recovery codes. Keep them somewhere safe, away from your phone: each one logs you in once if you lose the phone. They won't be shown again."));
  const list = el("pre", "recovery-codes", codes.join("\n"));
  const copy = button("Copy codes", "btn small", async () => {
    try { await navigator.clipboard.writeText(codes.join("\n")); copy.textContent = "Copied"; } catch (_) { /* select by hand */ }
  });
  box.append(list, copy);
  return box;
}

async function loginSecurity() {
  await modal({
    title: "Login security", ok: null, cancel: "Close", wide: true,
    build: (body, ctl) => {
      const two = el("section");
      const devs = el("section");
      body.append(two, devs);

      const drawTwo = async () => {
        let st;
        try { st = await api("/api/auth/two-step"); } catch (err) { ctl.error(err.message); return; }
        two.replaceChildren(el("h3", "section-title small", "Two-step login"));
        if (!st.enabled) {
          two.append(el("p", "muted", "After your password, Snapshots also asks for a 6-digit code from an app on your phone (Google Authenticator, Microsoft Authenticator, 2FAS, Aegis, 1Password…). Someone who learns your password still can't get in."));
          two.append(button("Turn on two-step login", "btn primary", () => startTwo()));
          return;
        }
        two.append(el("p", null, "On. You'll be asked for a code from your authenticator app when you log in on a new device or after logging out."));
        two.append(el("p", "muted", st.recovery_left
          ? `${plural(st.recovery_left, "recovery code")} left.`
          : "No recovery codes left: make new ones, or you'll need an administrator if you lose your phone."));
        const [pw, pwIn] = field("Your password (to make new codes or turn this off)", { type: "password", autocomplete: "current-password" });
        const row = el("div", "row-actions");
        row.append(
          button("New recovery codes", "btn", async () => {
            try {
              const res = await post("/api/auth/two-step/recovery", { password: pwIn.value });
              pwIn.value = ""; ctl.error("");
              await drawTwo();
              two.append(recoveryBlock(res.recovery_codes));
            } catch (err) { ctl.error(err.message); }
          }),
          button("Turn off", "btn danger", async () => {
            try {
              await post("/api/auth/two-step/off", { password: pwIn.value });
              ctl.error(""); flashNote("Two-step login is off");
              drawTwo();
            } catch (err) { ctl.error(err.message); }
          }),
        );
        two.append(pw, row);
      };

      const startTwo = async () => {
        let res;
        try { res = await post("/api/auth/two-step/start"); } catch (err) { ctl.error(err.message); return; }
        two.replaceChildren(el("h3", "section-title small", "Two-step login"));
        two.append(el("p", null, "1. In your authenticator app, add an account and scan this code."));
        const qrBox = el("div", "qr-box");
        try { qrBox.append(await qrCanvas(res.uri)); } catch (err) { qrBox.append(el("p", "muted", err.message)); }
        const secret = el("p", "muted small");
        secret.append("Can't scan? Type this key instead: ", el("code", "secret", res.secret.replace(/(.{4})/g, "$1 ").trim()));
        qrBox.append(secret);
        two.append(qrBox);
        two.append(el("p", null, "2. Type the 6-digit code the app shows for Snapshots."));
        const [cw, code] = field("Code", { inputMode: "numeric", autocomplete: "one-time-code", maxLength: 7 });
        const row = el("div", "row-actions");
        row.append(
          button("Turn on", "btn primary", async () => {
            try {
              const done = await post("/api/auth/two-step/confirm", { code: code.value });
              ctl.error("");
              await drawTwo();
              two.append(recoveryBlock(done.recovery_codes));
              flashNote("Two-step login is on. Your other devices have been logged out.");
              drawDevs();
            } catch (err) { ctl.error(err.message); }
          }),
          button("Cancel", "btn", () => drawTwo()),
        );
        two.append(cw, row);
        code.focus();
      };

      const drawDevs = async () => {
        let list;
        try { ({ devices: list } = await api("/api/auth/devices")); } catch (err) { ctl.error(err.message); return; }
        devs.replaceChildren(el("h3", "section-title small", "Devices you log in from"));
        devs.append(el("p", "muted", "Wrong passwords typed on other devices can lock those out for a while, but never these: each has its own allowance. Forget a device you no longer use."));
        if (!list.length) devs.append(el("p", "muted", "None yet."));
        for (const d of list) {
          const r = el("div", "user-row");
          const who = el("div", "who");
          who.append(el("div", null, deviceName(d.user_agent) + (d.this ? " (this device)" : "")),
            el("div", "role", `First used ${fmtDay(d.created_at, { day: "numeric", month: "short", year: "numeric" })}` +
              ` · last ${fmtDay(d.last_used_at, { day: "numeric", month: "short", year: "numeric" })}`));
          r.append(who, button("Forget", "btn small", async () => {
            try { await del(`/api/auth/devices/${d.id}`); drawDevs(); } catch (err) { ctl.error(err.message); }
          }));
          devs.append(r);
        }
      };
      drawTwo();
      drawDevs();
    },
  });
}

/** "Chrome on Windows", from a browser's user agent text. */
function deviceName(ua) {
  ua = ua || "";
  const browser = /Edg\//.test(ua) ? "Edge" : /OPR\//.test(ua) ? "Opera" : /Firefox\//.test(ua) ? "Firefox"
    : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : /python|httpx|curl/i.test(ua) ? "A script" : "A browser";
  const os = /Windows/.test(ua) ? "Windows" : /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iPhone or iPad"
    : /Mac OS X/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux" : "";
  return os ? `${browser} on ${os}` : browser;
}

// ------------------------------------------------------------------ smart albums

/** Make a smart album (album = null) or change one's rules. preset: a starting rule. */
async function smartAlbumDialog(album, preset, suggestedName = "") {
  let people = [], tags = [], places = [];
  try {
    [people, tags, places] = await Promise.all([
      api("/api/people").then((x) => x.people.filter((p) => p.name)),
      api("/api/tags").then((x) => x.tags),
      api("/api/places").then((x) => x.places),
    ]);
  } catch (_) { /* the rest still works */ }
  const rule = { ...(preset || {}) };
  const done = await modal({
    title: album ? `Rules of “${album.name}”` : "New smart album", ok: album ? "Save rules" : "Create smart album", wide: true,
    build: (body) => {
      body.append(el("p", "muted", "A smart album shows every photo that matches its rules, now and later. Fill in what you want; leave the rest empty."));
      let nameInput = null;
      if (!album) {
        const [w, input] = field("Name", { value: suggestedName, maxLength: 200, placeholder: "Like “Sam at the beach”" });
        nameInput = input;
        body.append(w);
      }
      const grid = el("div", "rule-grid");
      const [w1, words] = field("Words, names, places or dates", { value: rule.words || "", maxLength: 200, placeholder: "Like “Rome 2024”" });
      const [w2, look] = field("What's in the photo", { value: rule.look || "", maxLength: 200, placeholder: "Like “beach” or “birthday cake”" });
      grid.append(w1, w2);
      const typeWrap = el("label", "field");
      typeWrap.append(el("span", null, "Photos or videos"));
      const kind = el("select");
      [["", "Both"], ["photo", "Photos only"], ["video", "Videos only"]].forEach(([v, t]) => { const o = el("option", null, t); o.value = v; kind.append(o); });
      kind.value = rule.kind || "";
      typeWrap.append(kind);
      const tagWrap = el("label", "field");
      tagWrap.append(el("span", null, "Tag"));
      const tag = el("select");
      const none = el("option", null, "Any");
      none.value = "";
      tag.append(none);
      tags.forEach((t) => { const o = el("option", null, t.name); o.value = String(t.id); tag.append(o); });
      tag.value = rule.tag != null ? String(rule.tag) : "";
      tagWrap.append(tag);
      grid.append(typeWrap, tagWrap);
      const [w3, start] = field("From", { type: "date", value: rule.start || "" });
      const [w4, end] = field("Until", { type: "date", value: rule.end || "" });
      grid.append(w3, w4);
      const placeWrap = el("label", "field");
      placeWrap.append(el("span", null, "Place"));
      const place = el("select");
      const anywhere = el("option", null, "Anywhere");
      anywhere.value = "";
      place.append(anywhere);
      const seen = new Set();
      for (const p of places) {
        for (const key of [JSON.stringify([p.country]), p.state ? JSON.stringify([p.country, p.state]) : null,
                           p.city ? JSON.stringify([p.country, p.state || "", p.city]) : null]) {
          if (!key || seen.has(key)) continue;
          seen.add(key);
          const parts = JSON.parse(key);
          const o = el("option", null, [...parts].reverse().filter(Boolean).join(", "));
          o.value = key;
          place.append(o);
        }
      }
      if (rule.country) {
        const key = JSON.stringify(rule.city ? [rule.country, rule.state || "", rule.city] : rule.state ? [rule.country, rule.state] : [rule.country]);
        if (!seen.has(key)) { const o = el("option", null, key); o.value = key; place.append(o); }
        place.value = key;
      }
      placeWrap.append(place);
      grid.append(placeWrap);
      body.append(grid);

      const chosen = new Set(rule.people || []);
      if (people.length) {
        body.append(el("h3", "section-title small", "People (all of them in the photo)"));
        const list = el("div", "people-pick");
        for (const p of people) {
          const b = button("", "person-chip", () => {
            if (chosen.has(p.id)) chosen.delete(p.id); else chosen.add(p.id);
            b.setAttribute("aria-pressed", String(chosen.has(p.id)));
          });
          if (p.face_id) b.append(img(faceUrl(p.face_id), "", "avatar tiny"));
          b.append(el("span", null, p.name));
          b.setAttribute("aria-pressed", String(chosen.has(p.id)));
          list.append(b);
        }
        body.append(list);
      } else if (chosen.size) {
        body.append(el("p", "muted small", "Includes a person who has no name yet."));
      }
      const checks = el("div", "check-row");
      const fav = el("input");
      fav.type = "checkbox";
      fav.checked = !!rule.favorite;
      const favLabel = el("label", "opt-check");
      favLabel.append(fav, document.createTextNode(" Only favourites (each person sees their own)"));
      const arch = el("input");
      arch.type = "checkbox";
      arch.checked = !!rule.hide_archived;
      const archLabel = el("label", "opt-check");
      archLabel.append(arch, document.createTextNode(" Leave out archived photos"));
      checks.append(favLabel, archLabel);
      body.append(checks);

      return { submit: async () => {
        const out = {};
        if (words.value.trim()) out.words = words.value.trim();
        if (look.value.trim()) out.look = look.value.trim();
        if (chosen.size) out.people = [...chosen];
        if (kind.value) out.kind = kind.value;
        if (tag.value) out.tag = Number(tag.value);
        if (start.value) out.start = start.value;
        if (end.value) out.end = end.value;
        if (place.value) {
          const [country, state, city] = JSON.parse(place.value);
          out.country = country;
          if (state) out.state = state;
          if (city) out.city = city;
        }
        if (fav.checked) out.favorite = true;
        if (arch.checked) out.hide_archived = true;
        if (album) return patch(`/api/albums/${album.id}`, { rule: out });
        const name = nameInput.value.trim();
        if (!name) throw new Error("Give the album a name.");
        return post("/api/albums", { name, rule: out });
      } };
    },
  });
  if (!done) return;
  if (album) { flashNote("Rules saved"); render(); }
  else location.hash = `#/albums/${done.id}`;
}

// ------------------------------------------------------------------ exporting

function startDownload(url) {
  const a = el("a");
  a.href = url;
  a.setAttribute("download", "");
  document.body.append(a);
  a.click();
  a.remove();
}

async function waitForTask(id, onProgress) {
  for (;;) {
    const t = await api(`/api/tasks/${id}`);
    if (onProgress) onProgress(t);
    if (t.done) {
      if (t.error) throw new Error(t.error);
      return t;
    }
    await new Promise((r) => setTimeout(r, 700));
  }
}

/** Export an album ({album}) or a selection ({ids}): a zip of the files, or a PDF photo book. */
async function exportDialog({ album = null, ids = null, title = "", count = 0 }) {
  const usPaper = /^en-(US|CA)$|^es-(MX|US)$/i.test(navigator.language || "");
  await modal({
    title: `Export ${plural(count, "item")}`, ok: null, cancel: "Close",
    build: (body, ctl) => {
      const choice = el("div", "export-choice");
      const radio = (value, label, text, checked) => {
        const wrap = el("label", "export-option");
        const r = el("input");
        r.type = "radio";
        r.name = "export-kind";
        r.value = value;
        r.checked = checked;
        const t = el("span");
        t.append(el("strong", null, label), el("small", null, text));
        wrap.append(r, t);
        choice.append(wrap);
        return r;
      };
      const zipR = radio("zip", "The files, in a zip", "Named by date, so they stay in order wherever you unpack them.", true);
      radio("pdf", "A photo book (PDF)", "To print or share: a title page, then the photos with their date and place. Photos only.", false);
      body.append(choice);

      const zipOpts = el("div", "export-opts");
      const check = (label, on) => {
        const l = el("label", "opt-check");
        const c = el("input");
        c.type = "checkbox";
        c.checked = on;
        l.append(c, document.createTextNode(" " + label));
        zipOpts.append(l);
        return c;
      };
      const edited = check("Edited photos as edited (not the original files)", true);
      const videos = check("Include videos", true);
      const pdfOpts = el("div", "export-opts");
      pdfOpts.hidden = true;
      const sel = (label, options, value) => {
        const w = el("label", "field inline");
        w.append(el("span", null, label));
        const s2 = el("select");
        options.forEach(([v, t]) => { const o = el("option", null, t); o.value = v; s2.append(o); });
        s2.value = value;
        w.append(s2);
        pdfOpts.append(w);
        return s2;
      };
      const paper = sel("Paper", [["letter", "Letter"], ["a4", "A4"]], usPaper ? "letter" : "a4");
      const perPage = sel("Photos a page", [["1", "1"], ["2", "2"], ["4", "4"], ["6", "6"]], "2");
      body.append(zipOpts, pdfOpts);
      choice.addEventListener("change", () => {
        zipOpts.hidden = !zipR.checked;
        pdfOpts.hidden = zipR.checked;
        status.textContent = "";
        bar.hidden = true;
      });

      const bar = el("div", "progress");
      bar.hidden = true;
      const fill = el("div", "progress-fill");
      bar.append(fill);
      const status = el("p", "muted small");
      const go = button("Export", "btn primary", async () => {
        ctl.error("");
        go.disabled = true;
        const what = album != null ? { album } : { ids };
        try {
          if (zipR.checked) {
            const r = await post("/api/exports", { ...what, format: "zip", edited: edited.checked, videos: videos.checked });
            startDownload(r.url);
            status.textContent = `Downloading ${plural(r.count, "item")}. The zip is made as it downloads.`;
          } else {
            const t = await post("/api/exports", { ...what, format: "pdf", paper: paper.value, per_page: Number(perPage.value), title: title || undefined });
            bar.hidden = false;
            status.textContent = `Making the photo book of ${plural(t.count, "photo")}…`;
            const done = await waitForTask(t.id, (x) => { fill.style.width = `${Math.round(x.progress * 100)}%`; });
            status.textContent = `Ready: ${plural(done.result.pages, "page")}.`;
            if (done.result.missing) status.textContent += ` ${plural(done.result.missing, "photo")} could not be read.`;
            const dl = link("Download the PDF", `/api/tasks/${t.id}/file`, "btn small");
            dl.setAttribute("download", "");
            status.append(" ", dl);
            startDownload(`/api/tasks/${t.id}/file`);
          }
        } catch (err) { ctl.error(err.message); }
        go.disabled = false;
      });
      body.append(go, bar, status);
    },
  });
}

// ------------------------------------------------------------------ trimming videos

function fmtSeconds(t) {
  const m = Math.floor(t / 60), s = t - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, "0")}`;
}

async function trimDialog(item) {
  let made = false;
  $("v-stage").querySelectorAll("video").forEach((v) => v.pause());
  await modal({
    title: `Trim ${item.filename}`, ok: null, cancel: "Close", wide: true,
    build: (body, ctl) => {
      body.append(el("p", "muted", "Save part of the video, or one moment of it as a photo. Both are saved as new files next to the video, with its date, place and albums. The video itself isn't changed."));
      const video = document.createElement("video");
      video.className = "trim-video";
      video.controls = true;
      video.playsInline = true;
      body.append(video);
      const load = async () => {
        for (;;) {
          let info;
          try { info = await api(`/api/assets/${item.id}/play`); } catch (err) { ctl.error(err.message); return; }
          if (info.url) { video.src = info.url; return; }
          ctl.error(`Preparing the video so it can play here… ${Math.round((info.progress || 0) * 100)}%`);
          await new Promise((r) => setTimeout(r, 1000));
          if (!$("modal").open) return;
        }
      };
      load();
      const row = el("div", "trim-row");
      const startIn = el("input", "trim-time");
      const endIn = el("input", "trim-time");
      for (const i of [startIn, endIn]) { i.type = "number"; i.min = "0"; i.step = "0.1"; }
      startIn.value = "0";
      startIn.setAttribute("aria-label", "Start, in seconds");
      endIn.setAttribute("aria-label", "End, in seconds");
      video.addEventListener("loadedmetadata", () => {
        ctl.error("");
        if (!endIn.value) endIn.value = video.duration.toFixed(1);
        endIn.max = startIn.max = video.duration.toFixed(1);
      });
      const label = (text, input, setText) => {
        const w = el("span", "trim-point");
        w.append(el("span", "muted small", text), input, button(setText, "btn small", () => {
          input.value = video.currentTime.toFixed(1);
        }));
        return w;
      };
      row.append(label("Start (seconds)", startIn, "Start here"), label("End (seconds)", endIn, "End here"));
      body.append(row);
      const status = el("p", "muted small");
      const save = button("Save trimmed copy", "btn primary", async () => {
        ctl.error("");
        const start = Number(startIn.value), end = Number(endIn.value);
        if (!(end > start)) { ctl.error("The end should be after the start."); return; }
        save.disabled = true;
        try {
          const t = await post(`/api/assets/${item.id}/trim`, { start, end });
          status.textContent = "Trimming… 0%";
          const done = await waitForTask(t.id, (x) => { status.textContent = `Trimming… ${Math.round(x.progress * 100)}%`; });
          status.textContent = `Saved ${fmtSeconds(end - start)} as a new video.`;
          made = true;
          void done;
        } catch (err) { ctl.error(err.message); status.textContent = ""; }
        save.disabled = false;
      });
      const still = button("Save this moment as a photo", "btn", async () => {
        ctl.error("");
        still.disabled = true;
        try {
          const p = await post(`/api/assets/${item.id}/still`, { at: video.currentTime || 0 });
          status.textContent = `Saved ${p.filename}.`;
          made = true;
        } catch (err) { ctl.error(err.message); }
        still.disabled = false;
      });
      const actions = el("div", "row-actions");
      actions.append(save, still);
      body.append(actions, status);
    },
  });
  if (made) { refreshStatus(); if (S.route.view === "grid") reload(); }
}

// ------------------------------------------------------------------ importing (administrators)

function importSummary(kind, s, dry) {
  const out = el("div", "import-summary");
  if (!s) return out;
  const lines = [];
  if (kind === "immich") {
    lines.push(`Immich ${s.immich_version}${s.immich_user ? ` (${s.immich_user})` : ""}: ${plural(s.assets, "item")}, ${plural(s.albums_found || 0, "album")}.`);
  } else {
    lines.push(`${plural(s.files, "photo and video", "photos and videos")} in the export, ${plural(s.albums_found || 0, "album")}.`);
    if (s.without_details) lines.push(`${plural(s.without_details, "file")} had no details from Google.`);
  }
  lines.push(`${fmtInt(s.matched)} already in Snapshots; ${fmtInt(s.missing)} not yet${s.missing_bytes ? ` (${fmtSize(s.missing_bytes)})` : ""}.`);
  if (s.skipped) lines.push(`${plural(s.skipped, "item")} skipped (not photos or videos, or in Immich's trash).`);
  if (!dry) {
    if (s.copied || s.copy_failed) lines.push(`Copied into the library: ${fmtInt(s.copied)}${s.copy_failed ? `; ${fmtInt(s.copy_failed)} could not be copied` : ""}.`);
    const parts = [];
    const add = (n, one, many) => { if (n) parts.push(plural(n, one, many)); };
    add(s.favourites, "favourite");
    add(s.albums, "new album");
    add(s.album_items, "photo added to albums", "photos added to albums");
    add(s.tags, "photo tagged", "photos tagged");
    add(s.archived, "archived");
    add(s.trashed, "moved to the trash", "moved to the trash");
    add(s.dates, "date", "dates");
    add(s.locations, "location");
    add(s.descriptions, "description");
    add(s.names, "person's name", "people's names");
    lines.push(parts.length ? `Brought over: ${parts.join(", ")}.` : "Nothing needed changing.");
    if (s.names) lines.push("Names go to faces as Snapshots finds them, so People fills in over time.");
  }
  lines.forEach((t) => out.append(el("p", null, t)));
  return out;
}

async function importDialog() {
  let users = [];
  try { users = (await api("/api/users")).users; } catch (_) { /* only yourself, then */ }
  let timer = null;
  await modal({
    title: "Import from Immich or Google Photos", ok: null, cancel: "Close", wide: true,
    build: (body, ctl) => {
      body.append(el("p", "muted", "Brings over albums, favourites, archived photos, tags, changed dates and places, descriptions and people's names. Photos are matched by their content, wherever they are. Photos Snapshots doesn't have can be copied into the library. Check first to see what would happen: it changes nothing."));
      const tabs = el("div", "tabs");
      const panes = {};
      const tabBtn = (key, label) => {
        const b = button(label, "tab", () => {
          tabs.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-pressed", String(t === b)));
          Object.entries(panes).forEach(([k, p]) => { p.hidden = k !== key; });
        });
        b.setAttribute("aria-pressed", String(key === "immich"));
        tabs.append(b);
      };
      tabBtn("immich", "Immich");
      tabBtn("takeout", "Google Photos (Takeout)");
      body.append(tabs);

      const whoField = () => {
        const w = el("label", "field");
        w.append(el("span", null, "Favourites become favourites of"));
        const s2 = el("select");
        const list = users.length ? users : [S.me];
        list.forEach((u) => { const o = el("option", null, u.username); o.value = String(u.id); s2.append(o); });
        s2.value = String(S.me.id);
        w.append(s2);
        return [w, s2];
      };
      const copyBox = (text) => {
        const l = el("label", "opt-check");
        const c = el("input");
        c.type = "checkbox";
        c.checked = true;
        l.append(c, document.createTextNode(" " + text));
        return [l, c];
      };

      const im = el("div", "import-pane");
      const [a1, url] = field("Immich's address", { value: "http://localhost:2283", maxLength: 500 });
      const [a2, key] = field("API key (in Immich: Account settings → API keys)", { type: "password", autocomplete: "off", maxLength: 500 });
      const [a3, imWho] = whoField();
      const [a4, imCopy] = copyBox("Download photos Snapshots doesn't have into “From Immich” in the library");
      im.append(a1, a2, a3, a4, el("p", "muted small", "If Snapshots already reads Immich's library folder (as the library or an extra folder), nothing needs downloading. Each person can import with their own Immich API key to bring over their own favourites."));
      panes.immich = im;

      const to = el("div", "import-pane");
      to.hidden = true;
      const [b1, path] = field("Where the export is (the folder with the takeout .zip files, or the unpacked folder)", { placeholder: "D:\\Downloads\\Takeout", maxLength: 1000 });
      const [b2, toWho] = whoField();
      const [b3, toCopy] = copyBox("Copy photos Snapshots doesn't have into “From Google Photos” in the library");
      to.append(b1, b2, b3, el("p", "muted small", "Get the export at takeout.google.com (choose Google Photos). The .zip files can stay zipped."));
      panes.takeout = to;
      body.append(im, to);

      const bar = el("div", "progress");
      const fill = el("div", "progress-fill");
      bar.append(fill);
      bar.hidden = true;
      const status = el("p", "muted");
      const result = el("div");
      const history = el("div", "import-history");
      const stopBtn = button("Stop", "btn small", async () => { try { await post("/api/imports/stop"); } catch (_) { /* finished */ } });
      stopBtn.hidden = true;

      const show = (state) => {
        const cur = state.current || {};
        const running = !!cur.running;
        checkBtn.disabled = importBtn.disabled = running;
        stopBtn.hidden = !running;
        bar.hidden = !running;
        if (running) {
          status.textContent = `${cur.phase}…${cur.total ? ` ${fmtInt(cur.done)} of ${fmtInt(cur.total)}` : cur.done ? ` ${fmtInt(cur.done)}` : ""}`;
          fill.style.width = cur.total ? `${Math.round((cur.done / cur.total) * 100)}%` : "8%";
          result.replaceChildren();
        } else if (cur.kind) {
          status.textContent = cur.error ? "" : (cur.dry_run ? "Checked. Nothing was changed." : "Import finished.");
          if (cur.error) ctl.error(cur.error);
          result.replaceChildren(importSummary(cur.kind, cur.summary, cur.dry_run));
        }
        history.replaceChildren();
        if (state.history && state.history.length) {
          history.append(el("h3", "section-title small", "Earlier imports"));
          for (const h of state.history.slice(0, 5)) {
            const s3 = h.summary || {};
            history.append(el("p", "muted small",
              `${fmtDateTime(h.started_at)} · ${h.kind === "immich" ? "Immich" : "Google Photos"}${h.by ? ` · by ${h.by}` : ""} · ` +
              (h.error ? h.error : `${fmtInt(s3.copied || 0)} copied, ${fmtInt(s3.matched || 0)} matched`)));
          }
        }
        return running;
      };
      const poll = async () => {
        clearTimeout(timer);
        let state;
        try { state = await api("/api/imports"); } catch (err) { ctl.error(err.message); return; }
        if (show(state) && $("modal").open) timer = setTimeout(poll, 1000);
      };
      const start = async (dry) => {
        ctl.error("");
        const immich = !panes.immich.hidden;
        try {
          if (immich) {
            await post("/api/imports/immich", { url: url.value.trim(), key: key.value.trim(), user_id: Number(imWho.value), copy_missing: imCopy.checked, dry_run: dry });
          } else {
            await post("/api/imports/takeout", { path: path.value.trim(), user_id: Number(toWho.value), copy_missing: toCopy.checked, dry_run: dry });
          }
          poll();
        } catch (err) { ctl.error(err.message); }
      };
      const checkBtn = button("Check first", "btn", () => start(true));
      const importBtn = button("Import", "btn primary", () => start(false));
      const actions = el("div", "row-actions");
      actions.append(checkBtn, importBtn, stopBtn);
      body.append(actions, bar, status, result, history);
      poll();
    },
  });
  clearTimeout(timer);
  refreshStatus();
}

// ------------------------------------------------------------------ settings: offsite copy and webhooks

function storageSection(ctl) {
  const box = el("div");
  const draw = (o) => {
    box.replaceChildren(el("p", "muted", "Uploaded photos and videos are filed in folders inside the library, by the date they were taken (or the day they arrived if the file doesn't say). Files already in the library are never moved."));
    const [wrap, input] = field("Folder pattern", { value: o.template, maxLength: 200, spellcheck: false });
    const preview = el("div", "template-preview");
    const names = el("p", "muted small");
    names.append("You can use: ");
    Object.entries(o.variables).forEach(([k, ex], i) => {
      if (i) names.append(", ");
      const b = button(`{${k}}`, "linklike chip-var", () => {
        const at = input.selectionStart ?? input.value.length;
        input.value = input.value.slice(0, at) + `{${k}}` + input.value.slice(input.selectionEnd ?? at);
        input.focus();
        show();
      });
      b.title = ex;
      names.append(b);
    });
    let timer = null;
    const paint = (r) => {
      preview.replaceChildren();
      if (r.problem) { preview.append(el("p", "form-error", r.problem)); return; }
      preview.append(el("span", "muted small", "For example: "));
      r.examples.forEach((x, i) => {
        if (i) preview.append(el("span", "muted small", " or "));
        preview.append(el("code", null, `${x}/`));
      });
    };
    const show = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        try { paint(await post("/api/settings/storage/preview", { template: input.value })); } catch (_) { /* closed */ }
      }, 200);
    };
    input.addEventListener("input", show);
    paint(o);
    const presets = el("div", "row-actions");
    for (const [label, t] of [["Year / month", "Uploads/{year}/{month}"], ["Year / month name", "Uploads/{year}/{monthname}"],
      ["Year / date", "Uploads/{year}/{date}"], ["Person / year", "Uploads/{user}/{year}"]]) {
      presets.append(button(label, "btn small", () => { input.value = t; show(); }));
    }
    const save = button("Save", "btn small primary", async () => {
      try { draw(await api("/api/settings/storage", { method: "PUT", json: { template: input.value } })); ctl.error(""); flashNote("Upload folders saved"); }
      catch (err) { ctl.error(err.message); }
    });
    box.append(wrap, names, preview, presets, save);
  };
  return { box, draw };
}

function xmpSection(ctl) {
  const box = el("div");
  const draw = (o) => {
    box.replaceChildren(el("p", "muted", "Write descriptions, tags, people's names, favourites, and dates and places you changed into small .xmp files beside the photos (photo.jpg.xmp). Lightroom, digiKam, darktable and Immich read them, so this work isn't locked inside Snapshots. The photos themselves are never changed; another program's .xmp file is updated, not replaced."));
    const wrap = el("label", "field inline");
    const on = el("input");
    on.type = "checkbox";
    on.checked = o.enabled;
    wrap.append(on, el("span", null, "Write .xmp files"));
    on.addEventListener("change", async () => {
      try {
        draw(await api("/api/settings/xmp", { method: "PUT", json: { enabled: on.checked } }));
        ctl.error("");
        flashNote(on.checked ? "Writing .xmp files for photos with details" : ".xmp files turned off (the ones written stay)");
      } catch (err) { on.checked = !on.checked; ctl.error(err.message); }
    });
    box.append(wrap);
    if (o.enabled) {
      const line = el("p", "small", o.pending ? `Checking ${plural(o.pending, "item")}…` : `${plural(o.written || 0, "file")} written since this was turned on.`);
      if (o.failed) line.append(el("span", "form-error", ` ${plural(o.failed, "file")} could not be written. ${o.message || ""}`));
      box.append(line);
    }
    box.append(el("p", "muted small", "New photos that arrive with an .xmp file beside them get its description, tags, people's names, and date and place (if the photo has none)."));
  };
  return { box, draw };
}

function startupSection(ctl) {
  const box = el("div");
  const draw = (o) => {
    box.replaceChildren();
    if (!o.supported) {
      box.append(el("p", "muted", "Starting with Windows is only available in the Windows app."));
      return;
    }
    box.append(el("p", "muted", "Start Snapshots by itself when you sign in to Windows, so your library is always up to date and reachable from your phone. It runs in the background with no window. Open it by double-clicking Snapshots.exe, or go to its address in a browser. This is the same list as Windows Settings > Apps > Startup, so you can turn it off there too."));
    const wrap = el("label", "field inline");
    const on = el("input");
    on.type = "checkbox";
    on.checked = o.enabled;
    wrap.append(on, el("span", null, "Start Snapshots when I sign in to Windows"));
    on.addEventListener("change", async () => {
      try {
        draw(await api("/api/settings/startup", { method: "PUT", json: { enabled: on.checked } }));
        ctl.error("");
        flashNote(on.checked ? "Snapshots will start when you sign in to Windows" : "Snapshots will no longer start with Windows");
      } catch (err) { on.checked = !on.checked; ctl.error(err.message); }
    });
    box.append(wrap);
    if (o.enabled && !o.current) {
      const fix = el("button", "btn small", "Use this copy");
      fix.type = "button";
      fix.addEventListener("click", async () => {
        try { draw(await api("/api/settings/startup", { method: "PUT", json: { enabled: true } })); ctl.error(""); flashNote("Windows will start this copy"); }
        catch (err) { ctl.error(err.message); }
      });
      const warn = el("p", "small");
      warn.append(el("span", "form-error", "Windows is set to start a different copy of Snapshots (or one that was moved). "), fix);
      box.append(warn);
    }
    if (o.enabled && !o.can_stop) box.append(el("p", "muted small", "It starts by itself from your next sign-in. Until then, closing this window stops Snapshots as usual."));
    box.append(el("p", "muted small", `Address when it runs in the background: http://localhost:${o.port}`));
    if (o.can_stop) {
      const stop = el("button", "btn small danger", "Stop Snapshots");
      stop.type = "button";
      stop.addEventListener("click", async () => {
        if (!window.confirm("Stop Snapshots? It stays off until you start it again (or until the next time you sign in to Windows, if starting with Windows is on).")) return;
        try {
          await api("/api/quit", { method: "POST" });
          document.body.replaceChildren(el("p", "muted", "Snapshots has stopped. You can close this tab."));
        } catch (err) { ctl.error(err.message); }
      });
      box.append(el("p", "small", "Running in the background right now."), stop);
    }
  };
  return { box, draw };
}

function facesSection(ctl) {
  const box = el("div");
  const draw = async (st) => {
    try { st = st || await api("/api/settings/faces"); } catch (err) { ctl.error(err.message); return; }
    box.replaceChildren(el("p", "muted", "How alike two faces must be to be grouped as one person. Stricter makes fewer mistakes but splits people into more groups; looser does the opposite. Either way, People asks “Same person?” when it isn't sure."));
    const grid = el("div", "rule-grid");
    const sWrap = el("label", "field");
    sWrap.append(el("span", null, "Grouping"));
    const strict = el("select");
    [["stricter", "Stricter"], ["normal", "Normal"], ["looser", "Looser"], ...(st.strictness === "custom" ? [["custom", "Custom (config.toml)"]] : [])]
      .forEach(([v, t]) => { const op = el("option", null, t); op.value = v; strict.append(op); });
    strict.value = st.strictness;
    sWrap.append(strict);
    const [mWrap, min] = field("Show unnamed people in at least (faces)", { type: "number", min: "1", max: "50", value: String(st.min_faces) });
    grid.append(sWrap, mWrap);
    const sugWrap = el("label", "field inline");
    const sug = el("input");
    sug.type = "checkbox";
    sug.checked = st.suggest;
    sugWrap.append(sug, el("span", null, "Ask “Same person?” about groups that look alike"));
    const save = button("Save", "btn small primary", async () => {
      try {
        const body = { min_faces: Number(min.value || 3), suggest: sug.checked };
        if (strict.value !== "custom") body.strictness = strict.value;
        const changed = strict.value !== st.strictness;
        const res = await api("/api/settings/faces", { method: "PUT", json: body });
        ctl.error("");
        draw(res);
        flashNote(changed ? "Saved. New faces use it; “Group again” applies it to faces already found." : "Saved");
      } catch (err) { ctl.error(err.message); }
    });
    const again = button(st.regrouping ? "Grouping again…" : "Group unnamed faces again", "btn small", async () => {
      const ok = await confirm2();
      if (!ok) return;
      try { draw(await post("/api/people/regroup")); flashNote("Grouping faces again in the background"); } catch (err) { ctl.error(err.message); }
    });
    again.disabled = st.regrouping;
    const acts = el("div", "row-actions");
    acts.append(save, again);
    box.append(grid, sugWrap, acts,
      el("p", "muted small", "Group again: the faces of people without a name are grouped from scratch with the current setting. Named people and hidden people stay as they are, though more faces may join them."));
  };
  // A second dialog would close the settings: ask in the page instead.
  const confirm2 = () => Promise.resolve(window.confirm("Group the faces of unnamed people again? Named and hidden people stay as they are. Answers to “Same person?” about unnamed groups are forgotten."));
  return { box, draw };
}

function offsiteSection(ctl) {
  const box = el("div");
  const draw = (o) => {
    box.replaceChildren();
    box.append(el("p", "muted", "A copy of the photos themselves, kept up to date: on another drive, a NAS, a folder OneDrive or Google Drive syncs, or (with rclone or restic) cloud storage. Files deleted from the library stay in the copy."));
    const grid = el("div", "rule-grid");
    const mWrap = el("label", "field");
    mWrap.append(el("span", null, "How"));
    const method = el("select");
    [["", "Off"], ["folder", "Copy to a folder"], ["rclone", "rclone (cloud storage)"], ["restic", "restic (encrypted, with history)"]]
      .forEach(([v, t]) => { const op = el("option", null, t); op.value = v; method.append(op); });
    method.value = o.method || "";
    mWrap.append(method);
    const [tWrap, target] = field("Where", { value: o.target || "", maxLength: 1000 });
    const [hWrap, hours] = field("Every (hours)", { type: "number", min: "0", step: "1", value: String(o.hours) });
    const hint = el("p", "muted small");
    const setHint = () => {
      target.placeholder = { folder: "E:\\Photo copy", rclone: "b2-crypt:family-photos", restic: "E:\\restic-repo or b2:bucket:photos" }[method.value] || "";
      hint.textContent = {
        folder: "A folder on another drive (not inside the library).",
        rclone: o.tools.rclone ? "An rclone remote and path, set up with “rclone config”. Use a crypt remote to have it encrypted." : "Install rclone (rclone.org) and set up a remote with “rclone config” first.",
        restic: o.tools.restic ? "A restic repository (made if it doesn't exist). Set offsite_password_file in config.toml to a file holding its password." : "Install restic (restic.net) first.",
      }[method.value] || "";
    };
    method.addEventListener("change", setHint);
    setHint();
    grid.append(mWrap, hWrap);
    box.append(grid, tWrap, hint);
    const st = o.state || {};
    const line = el("p", "small");
    if (st.running) line.textContent = `Copying now…${st.total ? ` ${fmtInt(st.done)} of ${fmtInt(st.total)}` : ""}`;
    else if (st.last) {
      const res = st.result || {};
      line.textContent = `Last copy ${fmtDateTime(st.last)}` + (res.files != null ? `: ${fmtInt(res.copied)} new or changed, ${fmtInt(res.unchanged)} already there.` : ".");
    } else if (o.method) line.textContent = "No copy made yet.";
    if (st.message) line.append(el("span", "form-error", " " + st.message));
    if (o.problem) line.append(el("span", "form-error", " " + o.problem));
    const actions = el("div", "row-actions");
    actions.append(button("Save", "btn small primary", async () => {
      try {
        let o2 = await post("/api/offsite", { method: method.value, target: target.value.trim(), hours: Number(hours.value || 0) });
        if (o2.method && !(o2.state && o2.state.running)) o2 = await post("/api/offsite/run");
        draw(o2);
        ctl.error("");
        flashNote(method.value ? "Offsite copy saved. Copying now." : "Offsite copy turned off");
        if (o2.state && o2.state.running) setTimeout(refresh, 1500);
      } catch (err) { ctl.error(err.message); }
    }));
    if (o.method) actions.append(button(st.running ? "Stop" : "Copy now", "btn small", async () => {
      try { draw(await post(st.running ? "/api/offsite/stop" : "/api/offsite/run")); ctl.error(""); setTimeout(refresh, 1500); }
      catch (err) { ctl.error(err.message); }
    }));
    box.append(line, actions);
  };
  const refresh = async () => {
    try {
      const o = (await api("/api/settings")).offsite;
      draw(o);
      if (o.state && o.state.running && box.isConnected) setTimeout(refresh, 2000);
    } catch (_) { /* closed */ }
  };
  return { box, draw, refresh };
}

function webhookSection(ctl) {
  const box = el("div");
  const draw = async () => {
    let data;
    try { data = await api("/api/webhooks"); } catch (err) { ctl.error(err.message); return; }
    box.replaceChildren(el("p", "muted", "Tell another program (Home Assistant, a script) when something happens. Snapshots sends a JSON POST to the address, signed with the webhook's secret in the X-Snapshots-Signature header."));
    for (const h of data.webhooks) {
      const r = el("div", "user-row");
      const who = el("div", "who");
      who.append(el("div", "path", h.url), el("div", "role",
        h.events.map((e) => data.events[e] || e).join(", ") +
        (h.last_at ? ` · last sent ${fmtDateTime(h.last_at)} (${h.last_status})` : " · nothing sent yet") +
        (h.waiting ? ` · ${fmtInt(h.waiting)} waiting` : "") + (h.failed ? ` · ${fmtInt(h.failed)} gave up` : "") +
        (h.enabled ? "" : " · turned off")));
      r.append(who,
        button("Send test", "btn small", async () => { try { await post(`/api/webhooks/${h.id}/test`); flashNote("Test sent"); setTimeout(draw, 2000); } catch (err) { ctl.error(err.message); } }),
        button(h.enabled ? "Turn off" : "Turn on", "btn small", async () => { try { await patch(`/api/webhooks/${h.id}`, { enabled: !h.enabled }); draw(); } catch (err) { ctl.error(err.message); } }),
        button("Delete", "btn small", async () => {
          if (!window.confirm(`Stop sending events to ${h.url}?`)) return;
          try { await del(`/api/webhooks/${h.id}`); draw(); } catch (err) { ctl.error(err.message); }
        }));
      box.append(r);
    }
    const [w, url] = field("New webhook address", { placeholder: "http://homeassistant.local:8123/api/webhook/photos", maxLength: 1000 });
    const events = el("div", "check-row");
    const picks = {};
    for (const [k, label] of Object.entries(data.events)) {
      const l = el("label", "opt-check");
      const c = el("input");
      c.type = "checkbox";
      c.checked = k === "assets.added";
      picks[k] = c;
      l.append(c, document.createTextNode(" " + label));
      events.append(l);
    }
    const add = button("Add webhook", "btn small primary", async () => {
      try {
        const chosen = Object.keys(picks).filter((k) => picks[k].checked);
        const made = await post("/api/webhooks", { url: url.value.trim(), events: chosen });
        ctl.error("");
        await draw();
        const fresh = el("div", "key-fresh");
        const code = el("input", "key-value");
        code.readOnly = true;
        code.value = made.secret;
        code.addEventListener("focus", () => code.select());
        fresh.append(el("strong", null, "The webhook's secret, for checking signatures. Copy it now: it won't be shown again."), code);
        box.prepend(fresh);
      } catch (err) { ctl.error(err.message); }
    });
    box.append(w, events, add);
  };
  return { box, draw };
}

// ------------------------------------------------------------------ settings (administrators)

const SETTINGS_TABS = [
  ["library", "Library"],
  ["backups", "Backups"],
  ["people", "People and accounts"],
  ["videos", "Videos"],
  ["system", "Windows and more"],
];

/** Settings, as a page with tabs (administrators). */
async function renderSettings() {
  if (!S.me || !S.me.is_admin) { showEmpty("Settings are for administrators", "Ask the person who set up Snapshots."); return; }
  const tab = SETTINGS_TABS.some(([k]) => k === S.route.tab) ? S.route.tab : "library";
  headRow("Settings", null, []);
  const wrap = el("div", "settings");
  const tabs = el("nav", "set-tabs");
  tabs.setAttribute("aria-label", "Settings");
  for (const [k, label] of SETTINGS_TABS) {
    const a = link(label, `#/settings/${k}`, "set-tab");
    if (k === tab) a.setAttribute("aria-current", "page");
    tabs.append(a);
  }
  queueMicrotask(() => { const on = tabs.querySelector('[aria-current="page"]'); if (on && on.scrollIntoView) on.scrollIntoView({ inline: "center", block: "nearest" }); });
  const panel = el("section", "set-panel");
  const err = el("p", "form-error set-error");
  err.setAttribute("role", "alert");
  const ctl = { error: (m) => { err.textContent = m || ""; if (m) err.scrollIntoView({ block: "nearest" }); } };
  panel.append(err);
  wrap.append(tabs, panel);
  $("collection").replaceChildren(wrap);

  let data;
  try { data = await api("/api/settings"); } catch (e) { ctl.error(e.message); return; }
  if (!panel.isConnected) return;
  const section = (title, ...content) => {
    const box = el("div", "set-section");
    box.append(el("h3", "section-title small", title), ...content);
    panel.append(box);
    return box;
  };

  if (tab === "library") {
    const libs = el("div");
    const drawLibs = async (fresh) => {
      if (fresh) { try { data = await api("/api/settings"); } catch (e) { ctl.error(e.message); return; } }
      libs.replaceChildren();
      for (const l of data.libraries) {
        const r = el("div", "user-row");
        const who = el("div", "who");
        who.append(el("div", "path", l.path), el("div", "role",
          `${l.main ? "Main library (uploads go here)" : "Extra folder"} · ${plural(l.items, "item")}${l.available ? "" : " · not connected"}`));
        r.append(who);
        if (!l.main) r.append(button("Remove", "btn small", async () => {
          const ok = await confirmBox("Remove this folder from Snapshots?",
            `Its ${plural(l.items, "item")} leave Snapshots, with their albums, tags and faces. The files in ${l.path} are not touched.`,
            "Remove folder", true);
          if (!ok) return;
          try { await api(`/api/libraries?path=${encodeURIComponent(l.path)}`, { method: "DELETE" }); ctl.error(""); drawLibs(true); refreshStatus(); }
          catch (e) { ctl.error(e.message); }
        }));
        libs.append(r);
      }
    };
    drawLibs(false);
    const [a, path] = field("Add another folder (another drive, or a phone backup folder)", { placeholder: "D:\\Phone backup", maxLength: 1000 });
    const add = button("Add folder", "btn primary", async () => {
      try {
        await post("/api/libraries", { path: path.value.trim() });
        path.value = ""; ctl.error("");
        flashNote("Folder added. Snapshots is reading it now.");
        drawLibs(true); refreshStatus();
      } catch (e) { ctl.error(e.message); }
    });
    section("Library folders", libs, a, add,
      el("p", "muted small", "Snapshots only reads these folders. Nothing in them is changed, except by Delete permanently in the trash (and .xmp files beside photos, if you turn those on below)."));
    const store = storageSection(ctl);
    section("Where uploads go", store.box);
    store.draw(data.storage);
    const side = xmpSection(ctl);
    section("XMP files beside photos", side.box);
    side.draw(data.xmp);
  } else if (tab === "backups") {
    const backups = el("div");
    const drawBackups = (b) => {
      backups.replaceChildren();
      backups.append(el("p", "muted", b.hours > 0
        ? `A copy of the database is made every ${b.hours} hours into ${b.folder}. The newest ${b.keep} are kept. Your photos are not part of it: they are your own files.`
        : "Automatic backups are off (backup_hours = 0)."));
      if (b.message) backups.append(el("p", "form-error", b.message));
      const blist = el("div", "backup-list");
      for (const x of b.list.slice(0, 6)) {
        const line = el("div", "backup");
        line.append(el("span", null, fmtDateTime(x.made_at)), el("span", "muted", fmtSize(x.size)));
        blist.append(line);
      }
      if (!b.list.length) blist.append(el("p", "muted small", "No backups yet."));
      if (b.list.length > 6) blist.append(el("p", "muted small", `and ${plural(b.list.length - 6, "older one")}`));
      backups.append(blist, button("Back up now", "btn small", async (e) => {
        e.target.disabled = true;
        try {
          await post("/api/backups"); ctl.error(""); flashNote("Database backed up");
          drawBackups((await api("/api/settings")).backups);
        } catch (x) { ctl.error(x.message); }
        e.target.disabled = false;
      }));
      backups.append(el("p", "muted small", "To restore one: close Snapshots, then run  Snapshots.exe --restore-backup \"<backup file>\". The database it replaces is kept beside it."));
    };
    drawBackups(data.backups);
    section("Database backups", backups);
    const off = offsiteSection(ctl);
    section("Offsite copy of the photos", off.box);
    off.draw(data.offsite);
    if (data.offsite.state.running) off.refresh();
  } else if (tab === "people") {
    const faceSet = facesSection(ctl);
    section("Face grouping", faceSet.box);
    faceSet.draw();
    const links = el("div", "set-links");
    links.append(button("Accounts…", "btn", () => manageUsers()), button("Import from Immich or Google…", "btn", () => importDialog()));
    section("Accounts and imports", el("p", "muted", "Add accounts for the rest of the family, reset a password, or bring in photos, albums and people from Immich or Google Photos."), links);
  } else if (tab === "videos") {
    const video = el("div");
    const v = data.video;
    video.append(el("p", "muted", v.ffmpeg
      ? `Videos the app can't play (HEVC from phones, AVI, MKV) are converted once when opened. Setting: ${v.encoder}. Converted copies use up to ${v.cache_gb} GB.`
      : "Install ffmpeg to convert videos the app can't play (HEVC from phones, AVI, MKV)."));
    if (v.ffmpeg) {
      const out = el("span", "muted small");
      video.append(button("Check the graphics card", "btn small", async (e) => {
        e.target.disabled = true;
        out.textContent = " Testing…";
        try {
          const r = await api("/api/video-encoder");
          out.textContent = r.using === "libx264" ? " Converting with the processor (no graphics card encoder found)."
            : ` Converting with the graphics card (${r.using}).`;
        } catch (x) { out.textContent = ` ${x.message}`; }
        e.target.disabled = false;
      }), out);
    }
    section("Video conversion", video);
  } else {
    const boot = startupSection(ctl);
    section("Start with Windows", boot.box);
    boot.draw(data.startup);
    const hooks = webhookSection(ctl);
    section("Webhooks", hooks.box);
    hooks.draw();
    if (data.config_file) panel.append(el("p", "muted small cfg-note", `Other settings are in ${data.config_file}.`));
  }
}

// ------------------------------------------------------------------ phones: tab bar, "More" sheet, search

const side = document.querySelector(".side");
const topBar = document.querySelector(".top");
const phone = window.matchMedia("(max-width: 720px)");

function openSheet() {
  side.style.bottom = `${$("tabbar").offsetHeight}px`;   // the tab bar stays usable (More closes it again)
  side.classList.add("open");
  $("sheet-backdrop").hidden = false;
  document.querySelector('.tab-item[data-tab="more"]').setAttribute("aria-expanded", "true");
  markTab();
  const shown = [...side.querySelectorAll(".nav")].filter((n) => !n.hidden && n.offsetParent !== null);
  const cur = shown.find((n) => n.getAttribute("aria-current") === "page") || shown[0];
  if (cur) cur.focus({ preventScroll: true });
}

function closeSheet() {
  if (!side.classList.contains("open")) return;
  side.classList.remove("open");
  $("sheet-backdrop").hidden = true;
  document.querySelector('.tab-item[data-tab="more"]').setAttribute("aria-expanded", "false");
  $("account-menu").hidden = true;
  markTab();
}

function markTab() {
  const n = S.route ? S.route.name : "all";
  const which = side.classList.contains("open") ? "more"
    : topBar.classList.contains("searching") ? "search"
      : ["all", "photos", "videos", "favorites"].includes(n) ? "photos"
        : n === "albums" || n === "people" ? n : "more";
  document.querySelectorAll(".tab-item").forEach((t) => {
    const on = t.dataset.tab === which;
    t.classList.toggle("on", on);
    if (t.tagName === "A") { if (on) t.setAttribute("aria-current", "page"); else t.removeAttribute("aria-current"); }
  });
}

function openSearch() {
  closeSheet();
  topBar.classList.add("searching");
  markTab();
  $("search").focus();
}

function closeSearch() {
  topBar.classList.remove("searching");
  if ($("search").value) {
    $("search").value = "";
    $("search").dispatchEvent(new Event("input"));
  }
  markTab();
}

$("tabbar").addEventListener("click", (e) => {
  const t = e.target.closest(".tab-item");
  if (!t) return;
  if (t.dataset.tab === "more") { if (side.classList.contains("open")) closeSheet(); else openSheet(); return; }
  if (t.dataset.tab === "search") { openSearch(); return; }
  closeSheet();
  if (topBar.classList.contains("searching") || $("search").value) closeSearch();
  // Tapping the tab of the page you are on scrolls back to the top.
  if (t.getAttribute("href") === location.hash || (t.dataset.tab === "photos" && (location.hash === "" || location.hash === "#/"))) {
    e.preventDefault();
    $("scroller").scrollTo({ top: 0, behavior: "smooth" });
  }
  markTab();
});
$("sheet-backdrop").addEventListener("click", closeSheet);
side.addEventListener("click", (e) => { if (e.target.closest("a.nav")) closeSheet(); });
$("account-menu").addEventListener("click", (e) => { if (e.target.closest("button")) closeSheet(); });
$("search-open").addEventListener("click", openSearch);
$("search-close").addEventListener("click", closeSearch);
$("search").addEventListener("keydown", (e) => {
  if (e.key === "Escape" && phone.matches) { e.preventDefault(); closeSearch(); }
  if (e.key === "Enter" && phone.matches) $("search").blur();   // closes the phone keyboard
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && side.classList.contains("open") && !document.querySelector("dialog[open]")) closeSheet();
});
phone.addEventListener("change", () => { closeSheet(); if (!phone.matches) topBar.classList.remove("searching"); markTab(); });

// ------------------------------------------------------------------ viewer: More menu, keys, swiping

const vMenu = $("v-menu");
function closeViewerMenu() {
  vMenu.hidden = true;
  $("v-more").setAttribute("aria-expanded", "false");
}
$("v-more").addEventListener("click", (e) => {
  e.stopPropagation();
  vMenu.hidden = !vMenu.hidden;
  $("v-more").setAttribute("aria-expanded", String(!vMenu.hidden));
  if (!vMenu.hidden) {
    const first = [...vMenu.querySelectorAll(".v-item")].find((x) => !x.hidden);
    if (first) first.focus();
  }
});
vMenu.addEventListener("click", (e) => { if (e.target.closest(".v-item")) closeViewerMenu(); });
vMenu.addEventListener("keydown", (e) => {
  const items = [...vMenu.querySelectorAll(".v-item")].filter((x) => !x.hidden);
  const at = items.indexOf(document.activeElement);
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    e.stopPropagation();   // not the next photo
    const next = items[(at + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length];
    if (next) next.focus();
  } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    e.stopPropagation();
  }
});
viewer.addEventListener("click", (e) => {
  if (!vMenu.hidden && !e.target.closest(".v-more-wrap")) closeViewerMenu();
}, true);
viewer.addEventListener("cancel", (e) => {
  // Escape closes the menu first, then the viewer.
  if (!vMenu.hidden) { e.preventDefault(); closeViewerMenu(); $("v-more").focus(); }
});
viewer.addEventListener("close", closeViewerMenu);
vMenu.addEventListener("focusout", (e) => { if (!vMenu.hidden && e.relatedTarget && !e.relatedTarget.closest(".v-more-wrap")) closeViewerMenu(); });
viewer.addEventListener("keydown", (e) => {
  if (e.target.closest && e.target.closest("input, textarea, select")) return;
  if ((e.key === "i" || e.key === "I") && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); $("v-info").click(); }
});

// Swipe left or right for the next or previous photo, down to close.
(() => {
  const stage = $("v-stage");
  let t0 = null;
  const reset = () => { stage.style.transform = ""; stage.style.opacity = ""; };
  stage.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 1 || e.target.closest(".pano-view, .live-btn, button")) { t0 = null; return; }
    const t = e.touches[0];
    if (e.target.closest("video")) {
      // The controls sit along the bottom of a video; a swipe anywhere above them changes photo.
      const r = e.target.closest("video").getBoundingClientRect();
      if (t.clientY > r.bottom - 64) { t0 = null; return; }
    }
    t0 = { x: t.clientX, y: t.clientY, at: Date.now(), dir: null };
  }, { passive: true });
  stage.addEventListener("touchmove", (e) => {
    if (!t0 || e.touches.length !== 1) { t0 = null; reset(); return; }
    const t = e.touches[0];
    const dx = t.clientX - t0.x, dy = t.clientY - t0.y;
    if (!t0.dir && Math.hypot(dx, dy) > 12) t0.dir = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
    if (t0.dir === "x") stage.style.transform = `translateX(${dx * 0.5}px)`;
    else if (t0.dir === "y" && dy > 0) { stage.style.transform = `translateY(${dy * 0.5}px)`; stage.style.opacity = String(Math.max(0.4, 1 - dy / 600)); }
  }, { passive: true });
  stage.addEventListener("touchend", (e) => {
    if (!t0) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - t0.x, dy = t.clientY - t0.y, dir = t0.dir, quick = Date.now() - t0.at < 900;
    t0 = null;
    reset();
    if (!quick) return;
    if (dir === "x" && Math.abs(dx) > 50) step(dx < 0 ? 1 : -1);
    else if (dir === "y" && dy > 100) viewer.close();
  });
  stage.addEventListener("touchcancel", () => { t0 = null; reset(); });
})();

// ------------------------------------------------------------------ photos in their own shape

const LAYOUT_KEY = "snapshots.layout";
let layoutChoice = null;   // used when the browser won't remember it
function shapeLayout() {
  if (layoutChoice !== null) return layoutChoice;
  try { return localStorage.getItem(LAYOUT_KEY) === "shape"; } catch (_) { return false; }
}
function applyLayout() {
  const on = shapeLayout();
  $("grid").classList.toggle("shape", on);
  const b = $("layout-btn");
  b.setAttribute("aria-pressed", String(on));
  const text = on ? "Show photos as even tiles" : "Show photos in their own shape";
  b.title = text;
  b.setAttribute("aria-label", text);
}
$("layout-btn").addEventListener("click", () => {
  const on = !shapeLayout();
  try { localStorage.setItem(LAYOUT_KEY, on ? "shape" : "tiles"); layoutChoice = null; } catch (_) { layoutChoice = on; }
  if (shapeLayout() !== on) layoutChoice = on;
  applyLayout();
  updateScrubPos();
});
applyLayout();

function tileShape(b, item) {
  const fit = (w, h) => Math.min(2.6, Math.max(0.5, w / h));
  b.style.setProperty("--ar", (item.width && item.height ? fit(item.width, item.height) : 4 / 3).toFixed(3));
  // An edited photo (cropped, turned) has the shape of its thumbnail, not of the original file.
  if (item.edited) {
    const i = b.querySelector("img");
    if (i) i.addEventListener("load", () => {
      if (i.naturalWidth && i.naturalHeight) b.style.setProperty("--ar", fit(i.naturalWidth, i.naturalHeight).toFixed(3));
    }, { once: true });
  }
}

// ------------------------------------------------------------------ timeline scrubber

const SCRUB = { months: [], starts: [], total: 0, current: null, drag: false, wake: null };

function nextMonthKey(m) {
  let [y, mo] = m.split("-").map(Number);
  mo += 1;
  if (mo > 12) { mo = 1; y += 1; }
  return `${y}-${String(mo).padStart(2, "0")}`;
}

function hideScrubber() {
  $("scrubber").hidden = true;
  document.querySelector(".main").classList.remove("has-scrubber");
  SCRUB.months = [];
}

function buildScrubber() {
  const months = Object.keys(S.counts).sort().reverse();
  const total = months.reduce((n, m) => n + S.counts[m], 0);
  if (S.look || months.length < 3 || total < 60) { hideScrubber(); return; }
  // Each month gets room by how many photos it has, plus a little so that small months can be reached.
  const weights = months.map((m) => S.counts[m] + total * 0.004);
  const sum = weights.reduce((a, b) => a + b, 0);
  let acc = 0;
  SCRUB.starts = weights.map((w) => { const at = acc / sum; acc += w; return at; });
  SCRUB.months = months;
  SCRUB.total = total;
  $("scrubber").hidden = false;
  document.querySelector(".main").classList.add("has-scrubber");
  drawScrubMarks();
  updateScrubPos();
}

function drawScrubMarks() {
  const marks = $("scrub-marks");
  marks.replaceChildren();
  const h = $("scrubber").clientHeight || 1;
  let lastY = -100;
  let lastYear = null;
  SCRUB.months.forEach((m, i) => {
    const year = m.slice(0, 4);
    if (year === lastYear) return;
    lastYear = year;
    const y = SCRUB.starts[i] * h;
    if (y - lastY >= 18) {
      const label = el("span", "scrub-year", year);
      label.style.top = `${Math.max(7, y)}px`;
      marks.append(label);
      lastY = Math.max(7, y);
    } else {
      const dot = el("span", "scrub-dot");
      dot.style.top = `${y}px`;
      marks.append(dot);
    }
  });
}

function monthAtFraction(f) {
  let i = 0;
  while (i + 1 < SCRUB.months.length && SCRUB.starts[i + 1] <= f) i++;
  return i;
}

function scrubLabel(m) { return monthLabel(m); }

function setScrubPos(fraction) {
  const h = $("scrubber").clientHeight;
  $("scrub-pos").style.top = `${Math.min(h - 2, Math.max(2, fraction * h))}px`;
}

function updateScrubPos() {
  if (!SCRUB.months.length || SCRUB.drag) return;
  const sc = $("scroller");
  const heads = $("grid").querySelectorAll(".month");
  let cur = null, next = null;
  for (const hd of heads) {
    if (hd.offsetTop - sc.offsetTop <= sc.scrollTop + 40) cur = hd;
    else { next = hd; break; }
  }
  if (!cur) cur = heads[0];
  if (!cur) return;
  const i = SCRUB.months.indexOf(cur.dataset.month);
  if (i < 0) return;
  const end = i + 1 < SCRUB.starts.length ? SCRUB.starts[i + 1] : 1;
  const top = cur.offsetTop, bottom = next ? next.offsetTop : $("grid").offsetTop + $("grid").offsetHeight;
  const within = bottom > top ? Math.min(1, Math.max(0, (sc.scrollTop + 40 - (top - sc.offsetTop)) / (bottom - top))) : 0;
  const f = SCRUB.starts[i] + (end - SCRUB.starts[i]) * within;
  setScrubPos(f);
  SCRUB.current = i;
  const sb = $("scrubber");
  sb.setAttribute("aria-valuenow", String(Math.round(f * 100)));
  sb.setAttribute("aria-valuetext", scrubLabel(SCRUB.months[i]));
}

function showScrubTip(i, y) {
  const tip = $("scrub-tip");
  tip.textContent = scrubLabel(SCRUB.months[i]);
  tip.style.top = `${y}px`;
  tip.hidden = false;
}

async function scrollToMonth(m) {
  const gen = S.gen;
  const find = () => $("grid").querySelector(`.month[data-month="${m}"]`);
  let head = find();
  if (!head && !S.jumpedTo && S.lastMonth && m < S.lastMonth) {
    // Not far: load the pages in between rather than starting over at that month.
    const between = SCRUB.months.filter((x) => x < S.lastMonth && x > m).reduce((n, x) => n + S.counts[x], 0);
    if (between <= 1500) {
      for (let tries = 0, waits = 0; !head && !S.done && tries < 20 && waits < 100 && S.gen === gen; ) {
        if (S.loading) { waits++; await new Promise((r) => setTimeout(r, 50)); head = find(); continue; }   // a page is already loading
        tries++;
        await loadMore();
        head = find();
      }
    }
  }
  if (S.gen !== gen) return;   // the page changed while we were loading
  if (!head && m === SCRUB.months[0]) { $("scroller").scrollTop = 0; await reload(); return; }   // the newest: start over
  if (!head) { await jumpToMonth(m); return; }
  const sc = $("scroller");
  sc.scrollTop = head.offsetTop - sc.offsetTop - ($("jumped").hidden ? 4 : 52);
}

/** Show the timeline from one month on (older photos load as you scroll; "Back to newest" returns). */
async function jumpToMonth(m) {
  S.gen++;
  S.cursor = `${nextMonthKey(m)}|0`;
  S.done = false; S.loading = false; S.loaded = false;
  S.items = []; S.lastMonth = null; S.lastClicked = -1;
  S.jumpedTo = m;
  $("grid").replaceChildren();
  const bar = $("jumped");
  bar.replaceChildren(el("span", null, `Showing from ${monthLabel(m)}`), button("Back to newest", "btn small", () => {
    $("scroller").scrollTop = 0;
    reload();
  }));
  bar.hidden = false;
  $("scroller").scrollTop = 0;
  await loadMore();
  updateScrubPos();
}

(() => {
  const sb = $("scrubber");
  const at = (e) => {
    const r = sb.getBoundingClientRect();
    const y = Math.min(r.height, Math.max(0, e.clientY - r.top));
    return { y, i: monthAtFraction(y / r.height) };
  };
  sb.addEventListener("pointerdown", (e) => {
    if (!SCRUB.months.length) return;
    e.preventDefault();
    sb.setPointerCapture(e.pointerId);
    SCRUB.drag = true;
    sb.classList.add("dragging");
    const { y, i } = at(e);
    setScrubPos(y / sb.clientHeight);
    showScrubTip(i, y);
  });
  sb.addEventListener("pointermove", (e) => {
    if (!SCRUB.months.length) return;
    const { y, i } = at(e);
    if (SCRUB.drag) setScrubPos(y / sb.clientHeight);
    if (SCRUB.drag || e.pointerType === "mouse") showScrubTip(i, y);
  });
  const end = (e, go) => {
    if (!SCRUB.drag) return;
    SCRUB.drag = false;
    sb.classList.remove("dragging");
    $("scrub-tip").hidden = true;
    if (go) scrollToMonth(SCRUB.months[at(e).i]).then(updateScrubPos);
  };
  sb.addEventListener("pointerup", (e) => end(e, true));
  sb.addEventListener("pointercancel", (e) => end(e, false));
  sb.addEventListener("pointerleave", () => { if (!SCRUB.drag) $("scrub-tip").hidden = true; });
  sb.addEventListener("keydown", (e) => {
    if (!SCRUB.months.length) return;
    const i = SCRUB.current ?? 0;
    let to = null;
    if (e.key === "ArrowDown") to = i + 1;
    else if (e.key === "ArrowUp") to = i - 1;
    else if (e.key === "PageDown" || e.key === "PageUp") {
      const year = SCRUB.months[i].slice(0, 4);
      if (e.key === "PageDown") { to = SCRUB.months.findIndex((m) => m.slice(0, 4) < year); }
      else { const newer = SCRUB.months.filter((m) => m.slice(0, 4) > year); to = newer.length ? SCRUB.months.indexOf(newer[newer.length - 1]) : 0; }
      if (to < 0) to = SCRUB.months.length - 1;
    } else if (e.key === "Home") to = 0;
    else if (e.key === "End") to = SCRUB.months.length - 1;
    if (to === null) return;
    e.preventDefault();
    to = Math.min(SCRUB.months.length - 1, Math.max(0, to));
    SCRUB.current = to;
    scrollToMonth(SCRUB.months[to]).then(updateScrubPos);
  });
  let ticking = false;
  $("scroller").addEventListener("scroll", () => {
    if (!SCRUB.months.length) return;
    sb.classList.add("awake");
    clearTimeout(SCRUB.wake);
    SCRUB.wake = setTimeout(() => sb.classList.remove("awake"), 1500);
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { ticking = false; updateScrubPos(); });
  }, { passive: true });
  new ResizeObserver(() => { if (SCRUB.months.length) { drawScrubMarks(); updateScrubPos(); } }).observe(sb);
})();

// ------------------------------------------------------------------ welcome (after creating the first account)

async function welcomeDialog() {
  let settings = null;
  try { settings = await api("/api/settings"); } catch (_) { /* the steps that need it are skipped */ }
  await modal({
    title: "Welcome to Snapshots", ok: null, cancel: null, wide: true,
    build: (body, ctl) => {
      $("modal").classList.add("welcome");
      const steps = ["library", "folders", ...(settings ? ["running"] : []), "family"];
      let at = 0;
      let poll = null;
      const after = { action: null };
      const bars = el("div", "welcome-steps");
      steps.forEach(() => bars.append(el("span")));
      const page = el("div");
      const nav = el("div", "modal-foot");
      body.append(bars, page);
      const finish = () => { clearTimeout(poll); ctl.close(true); if (after.action) after.action(); };

      const show = () => {
        clearTimeout(poll);
        [...bars.children].forEach((b, i) => b.classList.toggle("on", i <= at));
        page.replaceChildren();
        ctl.error("");
        const step = steps[at];
        if (step === "library") {
          const st = S.status || {};
          page.append(el("p", "welcome-big", "Snapshots is reading your photo folder:"), el("p", "muted", st.library_path || ""));
          const count = el("div", "welcome-count", "");
          const line = el("p", "muted", "");
          page.append(count, line, el("p", "muted small",
            "You can start looking right away: photos appear as they are found. Faces, places and search by what's in a photo are worked out in the background, which takes a while the first time."));
          const tick = async () => {
            try { await refreshStatus(); } catch (_) { /* keep the last numbers */ }
            const s2 = S.status || {};
            count.textContent = `${fmtInt((s2.photos || 0) + (s2.videos || 0))}`;
            line.textContent = s2.scan && s2.scan.running ? "photos and videos found so far…" : "photos and videos in your library";
            if (page.isConnected && $("modal").open && steps[at] === "library") poll = setTimeout(tick, 1500);
          };
          tick();
        } else if (step === "folders") {
          page.append(el("p", "welcome-big", "Are some photos somewhere else?"),
            el("p", "muted", "Add more folders, like another drive or the folder your phone backs up to. Snapshots only reads them. You can add or remove folders later in Settings."));
          const [wrap, input] = field("Folder", { placeholder: "D:\\Phone backup", maxLength: 1000 });
          const added = el("div");
          const add = button("Add folder", "btn", async () => {
            const path = input.value.trim();
            if (!path) { ctl.error("Type the folder's full path, like D:\\Phone backup."); return; }
            try {
              await post("/api/libraries", { path });
              input.value = "";
              ctl.error("");
              added.append(el("p", "small", `✓ Added ${path}`));
              refreshStatus();
            } catch (err) { ctl.error(err.message); }
          });
          input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add.click(); } });
          page.append(wrap, add, added);
        } else if (step === "running") {
          page.append(el("p", "welcome-big", "Keep Snapshots running"));
          const st = settings.startup || {};
          if (st.supported) {
            const choice = el("label", "welcome-choice");
            const box = el("input");
            box.type = "checkbox";
            box.checked = !!st.enabled;
            const text = el("span");
            text.append(el("strong", null, "Start Snapshots when I sign in to Windows"),
              el("span", "muted small", "It runs in the background with no window, so new photos are picked up and your phone can reach it."));
            choice.append(box, text);
            box.addEventListener("change", async () => {
              try {
                const r = await api("/api/settings/startup", { method: "PUT", json: { enabled: box.checked } });
                box.checked = r.enabled;
                ctl.error("");
              } catch (err) { box.checked = !box.checked; ctl.error(err.message); }
            });
            page.append(choice);
          }
          const b = settings.backups || {};
          page.append(el("p", "muted", b.hours > 0
            ? `Your albums, people, tags and favourites are backed up every ${b.hours} hours to ${b.folder}.`
            : "Automatic database backups are turned off in config.toml."),
          el("p", "muted small", "Your photos themselves are your own files. To keep a second copy on another drive or in the cloud, use Settings › Backups › Offsite copy."));
        } else {
          page.append(el("p", "welcome-big", "You're all set"),
            el("p", "muted", "Add accounts for the rest of the family (each person gets their own favourites and locked folder), or bring in photos from Immich or Google Photos."));
          const links = el("div", "set-links");
          links.append(
            button("Add accounts…", "btn", () => { after.action = manageUsers; finish(); }),
            button("Import from Immich or Google…", "btn", () => { after.action = importDialog; finish(); }),
          );
          page.append(links, el("p", "muted small", "Everything here is also in Settings."));
        }
        nav.replaceChildren();
        const last = at === steps.length - 1;
        const next = button(last ? "Start using Snapshots" : "Next", "btn primary", () => { if (last) finish(); else { at++; show(); } });
        nav.append(next);
        if (at > 0) nav.append(button("Back", "btn", () => { at--; show(); }));
        if (!last) nav.append(button("Skip", "btn", finish));
        next.focus();
      };
      // The dialog puts the body in place right after this; the buttons go under it.
      queueMicrotask(() => { body.after(nav); show(); });
    },
  });
}

// ------------------------------------------------------------------ start

(async function init() {
  let state;
  try { state = await api("/api/auth/state"); } catch (_) { return; }
  if (!state.user) { location.replace("/login"); return; }
  S.me = state.user;
  await refreshStatus();
  await render();
  let left = null;
  try { left = sessionStorage.getItem("snapshots-recovery-left"); sessionStorage.removeItem("snapshots-recovery-left"); } catch (_) { /* fine */ }
  let startNote = null;
  try { startNote = sessionStorage.getItem("snapshots-startup-note"); sessionStorage.removeItem("snapshots-startup-note"); } catch (_) { /* fine */ }
  if (startNote) flashNote(startNote);
  let welcome = null;
  try { welcome = sessionStorage.getItem("snapshots-welcome"); sessionStorage.removeItem("snapshots-welcome"); } catch (_) { /* fine */ }
  if (welcome && S.me && S.me.is_admin) welcomeDialog();
  if (left !== null) {
    flashNote(+left > 0 ? `You used a recovery code: ${plural(+left, "code")} left. Make new ones in Login security if you lost your phone.`
      : "You used your last recovery code. Make new ones now in Login security.");
  }
})();
