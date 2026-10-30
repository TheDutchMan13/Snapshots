"use strict";

const $ = (id) => document.getElementById(id);
let setupMode = false;
let ticket = null;

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
    const res = await post(setupMode ? "/api/auth/setup" : "/api/auth/login", { username, password });
    if (res.two_step) {
      ticket = res.ticket;
      $("form").hidden = true;
      $("code-form").hidden = false;
      $("intro").textContent = "Two-step login is on for this account.";
      $("code").value = "";
      $("code").focus();
      return;
    }
    location.replace("/");
  } catch (err) {
    $("error").textContent = err.message;
    $("submit").disabled = false;
    $("password").select();
  }
});

$("code-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const code = $("code").value.trim();
  $("code-error").textContent = "";
  if (!code) { $("code-error").textContent = "Type the code your app shows."; return; }
  $("code-submit").disabled = true;
  try {
    const res = await post("/api/auth/login/code", { ticket, code });
    if (res.recovery_left !== undefined) {
      try { sessionStorage.setItem("snapshots-recovery-left", String(res.recovery_left)); } catch (_) { /* fine */ }
    }
    location.replace("/");
  } catch (err) {
    $("code-error").textContent = err.message;
    $("code-submit").disabled = false;
    $("code").select();
    if (/log in again/.test(err.message)) setTimeout(goBack, 1500);
  }
});

function goBack() {
  ticket = null;
  $("code-form").hidden = true;
  $("form").hidden = false;
  $("intro").textContent = "Log in to see your photos.";
  $("submit").disabled = false;
  $("password").value = "";
  $("password").focus();
}
$("code-back").addEventListener("click", goBack);
