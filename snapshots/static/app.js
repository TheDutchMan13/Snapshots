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
    dlg.onclose = () => finish(null);
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
function flashNote(text) {
  flash = { text, until: Date.now() + 8000 };
  $("st-note").textContent = text;
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
      return { name: "cleanup", view: "cleanup" };
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

async function render() {
  S.route = parseRoute();
  document.querySelectorAll(".side .nav[data-route]").forEach((a) => {
    if (a.dataset.route === S.route.name) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  if (!keepSearch) { S.q = ""; $("search").value = ""; }
  S.look = null;
  keepSearch = false;
  stopSelecting();
  $("new-items").hidden = true;
  $("scroller").scrollTop = 0;

  const grid = S.route.view === "grid";
  $("grid").hidden = !grid;
  $("sentinel").hidden = !grid;
  $("select-btn").hidden = !grid;
  $("collection").hidden = grid;
  $("empty").hidden = true;
  $("page-head").replaceChildren();
  $("page-head").hidden = true;
  $("collection").replaceChildren();
  $("collection").classList.toggle("full", S.route.view === "map");
  S.folderInfo = null;

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
    } else if (S.route.view === "tags") {
      await renderTags();
    } else if (S.route.view === "map") {
      await renderMap();
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
  } else if (r.archive) {
    headRow("Archive", null, [], "Archived photos are hidden from All, Photos, Videos, memories and the map. They still show in albums, people, places, folders and search.");
  } else if (r.event) {
    await renderEventHead(r.event);
  } else if (r.quality) {
    renderQualityHead();
  } else if (r.album) {
    const album = await api(`/api/albums/${r.album}`);
    const range = album.first ? `${fmtMonth(album.first)}${fmtMonth(album.last) !== fmtMonth(album.first) ? " – " + fmtMonth(album.last) : ""}` : "";
    headRow(album.name, [plural(album.count, "item"), range].filter(Boolean).join(" · "), [
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
    ]);
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
  headRow(el("h1", null, `“${q}”`), null, [tabs], note);
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
    button(person.hidden ? "Show in People" : "Hide", "btn small", async () => {
      try {
        await patch(`/api/people/${person.id}`, { hidden: !person.hidden });
        if (!person.hidden) location.hash = "#/people"; else renderHead();
      } catch (err) { flashNote(err.message); }
    }),
  ]);
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
  if (item.stack && item.stack_count > 1) badges.push(`${item.stack_count} in stack`);
  if (badges.length) {
    const row = el("span", "badges");
    badges.forEach((t) => row.append(el("span", "badge", t)));
    b.append(row);
  }
  if (item.stack && item.stack_count > 1) b.classList.add("stacked");
  if (item.favorite) b.insertAdjacentHTML("beforeend", HEART);
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
  await loadMore();
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
    const next = h.nextElementSibling;
    if (!next || next.classList.contains("month")) { h.remove(); return; }
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
  $("grid").classList.add("selecting");
  $("select-btn").setAttribute("aria-pressed", "true");
  updateSelection();
}

function stopSelecting() {
  S.selecting = false;
  S.selected.clear();
  S.lastClicked = -1;
  $("grid").classList.remove("selecting");
  $("select-btn").setAttribute("aria-pressed", "false");
  $("grid").querySelectorAll(".tile.selected").forEach((t) => { t.classList.remove("selected"); t.removeAttribute("aria-pressed"); });
  $("actionbar").hidden = true;
}

function toggleTile(index, range) {
  const ids = [];
  if (range && S.lastClicked >= 0) {
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
  if (r.album) actions.append(button("Remove from album", "btn", () => removeFromAlbum(ids())));
  actions.append(button("Tag", "btn", () => addTag(ids())));
  if (r.tag) actions.append(button("Remove tag", "btn", () => removeTag(ids())));
  const allArchived = chosen().every((it) => it.archived);
  actions.append(button(allArchived ? "Unarchive" : "Archive", "btn", () => setArchived(ids(), !allArchived)));
  if (chosen().some((it) => it.stack)) actions.append(button("Unstack", "btn", () => unstackPhotos(ids())));
  else if (n >= 2) actions.append(button("Stack", "btn", () => stackPhotos(ids())));
  actions.append(button("Move to trash", "btn", () => moveToTrash(ids())));
}

$("select-btn").addEventListener("click", () => { if (S.selecting) stopSelecting(); else startSelecting(); });
$("sel-cancel").addEventListener("click", stopSelecting);

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
  const { albums } = await api("/api/albums");
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
  const grid = el("div", "people");
  for (const p of data.people) {
    const a = link("", `#/people/${p.id}`, "person");
    a.append(p.face_id ? img(faceUrl(p.face_id), "", "avatar") : el("span", "avatar"));
    a.append(p.name ? el("span", null, p.name) : el("span", "unnamed", "Add a name"));
    a.append(el("small", null, plural(p.photos, "photo")));
    grid.append(a);
  }
  coll.replaceChildren(grid);
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

const folderHash = (path) => "#/folders" + (path ? "/" + path.split("/").map(encodeURIComponent).join("/") : "");

async function renderFolderHead(path) {
  const info = await api(`/api/folders?path=${encodeURIComponent(path)}`);
  if (S.route.folder !== path) return;
  S.folderInfo = info;
  const crumbs = el("nav", "crumbs");
  crumbs.setAttribute("aria-label", "Folder");
  const parts = path ? path.split("/") : [];
  const root = (S.status && S.status.library_path) ? S.status.library_path.split(/[\\/]/).filter(Boolean).pop() : "Library";
  const items = [[root || "Library", ""], ...parts.map((p, i) => [p, parts.slice(0, i + 1).join("/")])];
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
      text.append(el("span", "folder-name", f.name), el("small", null, plural(f.count, "item")));
      a.append(pic, text);
      list.append(a);
    }
    $("page-head").append(list);
  }
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

async function renderCleanup() {
  const sum = await api("/api/cleanup");
  const st = sum.status || {};
  headRow("Clean up", null, [], !sum.enabled ? "Photo checks are turned off in the settings."
    : st.pending ? `Still checking photos: ${plural(st.pending, "photo")} to go.` : null);
  if (!sum.enabled) { showEmpty("Photo checks are off", "Turn on photo_checks in config.toml."); return; }
  const frag = document.createDocumentFragment();

  const q = el("a", "cleanup-link");
  q.href = "#/cleanup/quality";
  q.append(el("strong", null, "Blurry or dark photos"), el("span", "muted", sum.low_quality ? plural(sum.low_quality, "photo") : "None found"));
  frag.append(q);

  const h = el("h2", "section-title", "Copies and bursts");
  if (sum.groups) h.append(el("span", "muted", `${plural(sum.groups, "group")} · ${plural(sum.extra, "extra photo")}`));
  frag.append(h);
  if (!sum.groups) {
    frag.append(el("p", "muted", "No copies or bursts found."));
    $("collection").replaceChildren(frag);
    return;
  }
  frag.append(el("p", "muted", "In each group the best photo (most pixels, then sharpest) is marked to keep. Click photos to change what to keep. Nothing is deleted: the rest go to the trash."));
  const bulk = button(`Keep the best of every group`, "btn", async () => {
    const ok = await confirmBox(`Move ${plural(sum.extra, "photo")} to the trash?`,
      "Each group keeps its best photo. The others can be restored from the trash until it is emptied.", "Move to trash");
    if (!ok) return;
    try {
      let moved = 0;
      for (;;) {
        const page = await api("/api/cleanup/groups?limit=100");
        if (!page.groups.length) break;
        for (const g of page.groups) {
          const res = await post("/api/cleanup/resolve", { keep: [g.keep], trash: g.items.map((i) => i.id).filter((i) => i !== g.keep) });
          moved += res.trashed;
        }
        if (page.groups.length < 100 && !page.next) break;
      }
      flashNote(`${plural(moved, "photo")} moved to the trash`);
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
      const page = await api(`/api/cleanup/groups?offset=${offset}&limit=20`);
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
  head.append(el("strong", null, g.kind === "burst" ? "Burst" : "Copies"),
    el("span", "muted", `${fmtDateTime(g.items[0].taken_at)} · ${plural(g.items.length, "photo")}`));
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
        flashNote(trash.length ? `${plural(trash.length, "photo")} moved to the trash` : "Kept them all");
        refreshStatus();
      } catch (err) { flashNote(err.message); }
    }),
    button(g.kind === "burst" ? "Stack them" : "Stack them instead", "btn small", async () => {
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
  $("v-fav").hidden = trashed;
  $("v-album").hidden = trashed;
  $("v-archive").hidden = trashed;

  const stage = $("v-stage");
  stage.replaceChildren();
  const src = `/api/assets/${item.id}/view?v=${item.v}`;
  if (item.kind === "video") {
    const v = document.createElement("video");
    v.controls = true;
    v.preload = "metadata";
    v.src = src;
    v.addEventListener("error", () => stage.replaceChildren(el("p", "msg", "This video cannot be played in the browser. Use Download.")));
    stage.append(v);
  } else {
    const i = new Image();
    i.alt = item.filename;
    i.src = src;
    i.addEventListener("error", () => stage.replaceChildren(el("p", "msg", "This file cannot be displayed. Use Download.")));
    stage.append(i);
    if (item.live) stage.append(liveButton(item));
  }
  renderDetails(item);
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
  row("Taken", fmtDateTime(item.taken_at));
  if (item.place) {
    row("Place", link(placeName(item.place), placeHash(item.place.country, item.place.state, item.place.city)));
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
  if (e.target.closest && e.target.closest("input")) return;
  if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
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
$("v-details").addEventListener("click", (e) => { if (e.target.closest("a")) viewer.close(); });

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
            who.append(el("div", null, u.username), el("div", "role", u.is_admin ? "Administrator" : "Member"));
            rowEl.append(who);
            rowEl.append(button("Reset password", "btn small", async () => {
              const pw = window.prompt(`New password for ${u.username} (8 characters or more):`);
              if (pw == null) return;
              try { await patch(`/api/users/${u.id}`, { password: pw }); ctl.error(""); flashNote(`Password changed for ${u.username}`); }
              catch (err) { ctl.error(err.message); }
            }));
            rowEl.append(button(u.is_admin ? "Make member" : "Make administrator", "btn small", async () => {
              try { await patch(`/api/users/${u.id}`, { is_admin: !u.is_admin }); ctl.error(""); draw(); }
              catch (err) { ctl.error(err.message); }
            }));
            if (!S.me || u.id !== S.me.id) {
              rowEl.append(button("Delete", "btn small", async () => {
                if (!window.confirm(`Delete the account ${u.username}? Their photos stay in the library.`)) return;
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

// ------------------------------------------------------------------ start

(async function init() {
  let state;
  try { state = await api("/api/auth/state"); } catch (_) { return; }
  if (!state.user) { location.replace("/login"); return; }
  S.me = state.user;
  await refreshStatus();
  await render();
})();
