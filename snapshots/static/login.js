"use strict";

const $ = (id) => document.getElementById(id);
let setupMode = false;

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Snapshots-Request": "1" },
    body: JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty */ }
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Something went wrong.");
  return data;
}

(async function init() {
  let state;
  try { state = await (await fetch("/api/auth/state")).json(); } catch (_) { state = { needs_setup: false }; }
  if (state.user) { location.replace("/"); return; }
  setupMode = !!state.needs_setup;
  if (setupMode) {
    $("intro").textContent = "Welcome. Create the first account. It will be the administrator, who can add accounts for the rest of the family.";
    $("confirm-row").hidden = false;
    $("password").autocomplete = "new-password";
    $("submit").textContent = "Create account";
  } else {
    $("intro").textContent = "Log in to see your photos.";
  }
})();

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = $("username").value.trim();
  const password = $("password").value;
  $("error").textContent = "";
  if (!username || !password) { $("error").textContent = "Fill in your user name and password."; return; }
  if (setupMode) {
    if (password.length < 8) { $("error").textContent = "Passwords need at least 8 characters."; return; }
    if (password !== $("confirm").value) { $("error").textContent = "The two passwords are different."; return; }
  }
  $("submit").disabled = true;
  try {
    await post(setupMode ? "/api/auth/setup" : "/api/auth/login", { username, password });
    location.replace("/");
  } catch (err) {
    $("error").textContent = err.message;
    $("submit").disabled = false;
    $("password").select();
  }
});
