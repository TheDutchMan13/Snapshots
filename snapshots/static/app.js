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
  switch (parts[0] || "") {
    case "photos": return { name: "photos", view: "grid", filter: { kind: "photo" } };
    case "videos": return { name: "videos", view: "grid", filter: { kind: "video" } };
    case "favorites": return { name: "favorites", view: "grid", filter: { favorite: "true" }, title: "Favourites" };
    case "trash": return { name: "trash", view: "grid", filter: { trashed: "true" }, title: "Trash" };
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
    default: return { name: "all", view: "grid", filter: {} };
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

async function renderHead() {
  const r = S.route;
  if (r.album) {
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
  for (const [k, v] of Object.entries(S.route.filter || {})) p.set(k, v);
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
  if (item.kind === "video") b.append(el("span", "badge", "Video"));
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
    if (month !== S.lastMonth) {
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
  try {
    const t = await api(`/api/timeline?${filterParams()}`);
    S.counts = Object.fromEntries(t.months.map((m) => [m.month, m.count]));
  } catch (_) { S.counts = {}; }
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
  if (S.q) showEmpty("No matches", "Try a different word, or clear the search.");
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
  const allFav = ids().every((id) => (S.items.find((it) => it.id === id) || {}).favorite);
  actions.append(button(allFav ? "Unfavourite" : "Favourite", "btn", () => setFavorite(ids(), !allFav)));
  actions.append(button("Add to album", "btn", () => addToAlbum(ids())));
  if (r.album) actions.append(button("Remove from album", "btn", () => removeFromAlbum(ids())));
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
  const busy = sc.running || st.faces.running || st.places.running;
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
  $("st-work").textContent = work.join(" · ");
  const notes = [];
  if (!st.heif) notes.push("HEIC photos need pillow-heif");
  if (st.videos && !st.ffmpeg) notes.push("Video thumbnails need ffmpeg");
  if (st.faces.message && st.faces.enabled) notes.push(st.faces.message);
  if (st.places.message && st.places.enabled) notes.push(st.places.message);
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
    // Search looks through everything, except that All, Photos, Videos and Favourites keep their filter.
    if (S.q && !["all", "photos", "videos", "favorites"].includes(S.route.name)) {
      keepSearch = true;
      location.hash = "#/";
      return;
    }
    stopSelecting();
    reload();
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

function showItem(index) {
  const item = S.items[index];
  if (!item) return;
  S.viewing = index;
  S.detail = { ...item };
  $("v-title").textContent = item.filename;
  $("v-download").href = `/api/assets/${item.id}/original?download=true`;
  $("v-prev").disabled = index === 0;
  $("v-next").disabled = index === S.items.length - 1 && S.done;
  showFavorite(item.favorite);
  const trashed = S.route.name === "trash";
  $("v-trash").querySelector(".v-label").textContent = trashed ? "Restore" : "Move to trash";
  $("v-fav").hidden = trashed;
  $("v-album").hidden = trashed;

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
  }
  renderDetails(item);
  loadDetail(item.id);
  if (index >= S.items.length - 6) loadMore();
}

async function loadDetail(id) {
  try {
    const full = await api(`/api/assets/${id}`);
    if (!S.detail || S.detail.id !== id) return;
    S.detail = full;
    renderDetails(full);
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
  row("Size", fmtSize(item.size));
  if (item.width && item.height) row("Dimensions", `${item.width} × ${item.height}`);
  if (item.camera) row("Camera", item.camera);
  row("Location in library", item.path);
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
