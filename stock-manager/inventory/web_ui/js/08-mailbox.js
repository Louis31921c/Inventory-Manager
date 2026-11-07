"use strict";

// ---- Mailbox settings -----------------------------------------------------

let mailProviders = [];

function mailByHand() {
  const chosen = $("mail-provider").value;
  return chosen === "" || chosen === "other";
}

function showMailHelp() {
  const chosen = mailProviders.find((row) => row.id === $("mail-provider").value);
  $("mail-help").textContent = chosen ? chosen.help : "";
  $("mail-by-hand").hidden = !mailByHand();
  // Only Microsoft takes a token in place of a password, so only Microsoft is asked.
  $("mail-oauth").hidden = $("mail-provider").value !== "outlook";
  $("mail-oauth-on").setAttribute("aria-pressed", String(oauthOn));
  $("mail-password").disabled = oauthOn;
}

let oauthOn = false;
let oauthWatch = null;

async function loadMailOauth() {
  let state;
  try {
    state = await api("/api/mail/oauth");
  } catch {
    return;
  }

  $("mail-oauth-state").textContent = !state.available
    ? "The library is missing: pip install 'stock-manager[outlook]'"
    : state.signed_in ? `Signed in${state.who ? ` as ${state.who}` : ""}.`
    : state.waiting ? "Waiting for the code to be typed in..."
    : state.error ? `Last try: ${state.error}`
    : "Not signed in yet.";

  $("mail-oauth-code").hidden = !state.waiting;
  if (state.waiting) {
    $("mail-oauth-digits").textContent = state.code ?? "";
    if (state.uri) $("mail-oauth-uri").href = state.uri;
  }

  // Keep looking only while somebody is away typing the code into a browser.
  if (state.waiting && !oauthWatch) {
    oauthWatch = setInterval(loadMailOauth, 4000);
  } else if (!state.waiting && oauthWatch) {
    clearInterval(oauthWatch);
    oauthWatch = null;
    if (state.signed_in) notice($("mail-oauth-note"), "ok", "Signed in. Test the connection.");
    else if (state.error) notice($("mail-oauth-note"), "bad", state.error);
  }
}

$("mail-oauth-on").addEventListener("click", () => {
  oauthOn = !oauthOn;
  showMailHelp();
});

$("mail-oauth-start").addEventListener("click", async () => {
  const button = $("mail-oauth-start");
  button.disabled = true;
  spinner($("mail-oauth-note"), "Asking Microsoft for a code...");
  try {
    await api("/api/mail/oauth/start", { method: "POST" });
    notice($("mail-oauth-note"), "", "");
    await loadMailOauth();
  } catch (e) {
    notice($("mail-oauth-note"), "bad", e.message);
  } finally {
    button.disabled = false;
  }
});

$("mail-oauth-forget").addEventListener("click", async () => {
  try {
    await api("/api/mail/oauth/forget", { method: "POST" });
    notice($("mail-oauth-note"), "ok", "Forgotten. Sign in again when you need the mailbox.");
    await loadMailOauth();
  } catch (e) {
    notice($("mail-oauth-note"), "bad", e.message);
  }
});

async function loadMailSettings() {
  let data;
  try {
    data = await api("/api/mail/settings");
  } catch (e) {
    notice($("mail-settings-note"), "bad", e.message);
    return;
  }
  mailProviders = data.providers ?? [];
  const now = data.current ?? {};

  $("mail-provider").replaceChildren(
    el("option", { value: "", textContent: "Not set" }),
    ...mailProviders.map((row) => el("option", { value: row.id, textContent: row.label })));
  $("mail-provider").value = now.provider ?? "";
  $("mail-user").value = now.user ?? "";
  $("mail-folder").value = now.folder ?? "INBOX";
  $("mail-every").value = now.every_minutes ?? 15;
  $("mail-auto").checked = now.auto_read ?? true;
  $("mail-host").value = now.host ?? "";
  $("mail-port").value = now.port ?? "";
  $("mail-security").value = now.security ?? "ssl";
  $("mail-stored").textContent = now.password_set
    ? `password stored · ${now.configured ? "mailbox ready" : "user name missing"}`
    : (now.oauth ? "signed in with Microsoft, no password needed"
                 : "no password stored yet");
  $("mail-client-id").value = now.client_id ?? "";
  $("mail-tenant").value = now.tenant ?? "";
  oauthOn = Boolean(now.oauth);
  showMailHelp();
  await loadMailOauth();

  const state = now.configured
    ? `${now.user} on ${now.host}, folder ${now.folder}, every ${now.every_minutes} min`
    : "No mailbox set up yet. Settings, then Mailbox.";
  $("mail-state").textContent = data.at ? `${state} · last run ${data.at}: ${data.detail}` : state;
}

$("mail-provider").addEventListener("change", showMailHelp);

$("mail-save").addEventListener("click", async () => {
  const button = $("mail-save");
  button.disabled = true;
  spinner($("mail-settings-note"), "Saving...");
  try {
    const answer = await api("/api/mail/settings", postJson({
      provider: $("mail-provider").value,
      user: $("mail-user").value.trim(),
      password: $("mail-password").value || null,
      folder: $("mail-folder").value.trim() || "INBOX",
      every_minutes: Number($("mail-every").value) || 15,
      auto_read: $("mail-auto").checked,
      host: $("mail-host").value.trim(),
      port: Number($("mail-port").value) || null,
      security: $("mail-security").value,
      oauth: oauthOn,
      client_id: $("mail-client-id").value.trim(),
      tenant: $("mail-tenant").value.trim(),
    }));
    $("mail-password").value = "";
    notice($("mail-oauth-note"), "", "");
    notice($("mail-settings-note"), "ok",
      answer.current.configured ? "Mailbox saved. Test the connection to be sure."
        : (answer.current.oauth ? "Saved. Sign in with Microsoft to finish."
                                : "Saved. A user name and a password are still needed."));
    await loadMailSettings();
  } catch (e) {
    notice($("mail-settings-note"), "bad", e.message);
  } finally {
    button.disabled = false;
  }
});

$("mail-settings-check").addEventListener("click", async () => {
  spinner($("mail-settings-note"), "Knocking...");
  try {
    const answer = await api("/api/mail/check", postJson({}));
    notice($("mail-settings-note"), answer.ok ? "ok" : "warn", answer.detail);
  } catch (e) {
    notice($("mail-settings-note"), "bad", e.message);
  }
});
