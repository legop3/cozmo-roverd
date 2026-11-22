"use strict";
const $ = id => document.getElementById(id);
const state = {config: null, interfaces: [], networks: [], colors: [], color: "#3ba8ff", loading: false, dirty: false};
const field = id => $(id).value;
const show = (message, error = false) => {
  const box = $("toast");
  box.textContent = message;
  box.className = "toast show" + (error ? " error" : "");
  clearTimeout(show.timer);
  show.timer = setTimeout(() => box.className = "toast", 6500);
};
async function api(path, data) {
  const options = data === undefined ? {} : {
    method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(data)
  };
  let response;
  try { response = await fetch(path, options); }
  catch (_) { throw new Error("Cannot reach the setup service. Reload the page or check cozmo-roverd-setup.service."); }
  let result;
  try { result = await response.json(); }
  catch (_) { throw new Error("Setup service returned an invalid response."); }
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}
function busy(button, work, text) {
  if (button.disabled) return;
  const before = button.textContent;
  button.disabled = true;
  button.innerHTML = '<span class="spinner"></span> ' + text;
  return Promise.resolve().then(work).catch(e => show(e.message, true)).finally(() => {
    button.disabled = false; button.textContent = before;
  });
}
function selectOptions(select, options, value, placeholder) {
  select.replaceChildren();
  if (placeholder) {
    const opt = document.createElement("option"); opt.value=""; opt.textContent=placeholder; select.append(opt);
  }
  for (const item of options) {
    const opt = document.createElement("option");
    opt.value = String(item.value);
    opt.textContent = item.label;
    if (item.disabled) opt.disabled = true;
    select.append(opt);
  }
  select.value = String(value ?? "");
}
function markDirty() {
  state.dirty = true;
  $("save-title").textContent = "Unsaved changes";
  $("save-subtitle").textContent = "Save to apply these settings.";
}
function formatPercent(n) { return `${n}%`; }
function makeColorChoices() {
  const wrapper = $("color-choices");
  wrapper.replaceChildren();
  for (const color of state.colors) {
    const b = document.createElement("button"); b.type = "button";
    b.className = "color" + (state.color === color ? " selected" : "");
    b.style.backgroundColor = color;
    b.setAttribute("aria-label", `Rover color ${color}`);
    b.setAttribute("aria-pressed", String(state.color === color));
    b.title = color;
    b.addEventListener("click", () => {
      state.color = color; makeColorChoices(); markDirty();
    });
    wrapper.append(b);
  }
}
function renderNetworks() {
  const list = $("networks");
  list.replaceChildren();
  if (!state.networks.length) {
    const div = document.createElement("div"); div.className="empty";
    div.textContent = "No networks found. Check that Cozmo is showing its Wi-Fi credentials, then scan again.";
    list.append(div); return;
  }
  for (const net of state.networks) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "network" + (field("ssid") === net.ssid ? " selected" : "");
    const left = document.createElement("div"), title = document.createElement("strong"), tag = document.createElement("span");
    title.textContent = net.ssid;
    tag.textContent = net.cozmo ? "  · Cozmo detected" : "";
    left.append(title, tag);
    const right = document.createElement("span");
    right.textContent = `${net.signal}% signal · ${net.security || "Open"}`;
    button.append(left, right);
    button.addEventListener("click", () => {
      $("ssid").value = net.ssid;
      $("password").value = "";
      $("password-hint").textContent = "Enter the password shown on the robot.";
      renderNetworks(); markDirty();
    });
    list.append(button);
  }
}
function configFromUI() {
  return {
    name: field("rover-name").trim(),
    description: field("description").trim(),
    color: state.color,
    private: $("private").checked,
    server_url: field("server-url").trim(),
    interface: field("adapter-select"),
    ssid: field("ssid"),
    password: field("password"),
    max_speed: Number(field("speed")),
    watchdog_seconds: Number(field("watchdog")),
    fps: Number(field("fps")),
    video: $("video").checked,
    rtsp_url: field("rtsp-url").trim(),
  };
}
function applyConfig(config, options) {
  state.config = config;
  state.colors = options.colors || [];
  state.color = config.color;
  $("private").checked = !!config.private;
  $("rover-name").value = config.name;
  $("description").value = config.description;
  $("server-url").value = config.server_url;
  $("ssid").value = config.ssid;
  $("password").value = "";
  $("password-hint").textContent = config.has_password
      ? "Saved password exists. Leave blank to keep it, unless changing Wi-Fi network or adapter."
      : "Enter the password shown on Cozmo.";
  $("video").checked = !!config.video;
  $("rtsp-url").value = config.rtsp_url;
  selectOptions($("speed"), options.speeds.map(n => ({value:n,label:`${n} mm/s`})), config.max_speed);
  selectOptions($("fps"), options.fps.map(n => ({value:n,label:`${n} FPS`})), config.fps);
  selectOptions($("watchdog"), options.watchdog.map(n => ({value:n,label:`${n} seconds`})), config.watchdog_seconds);
  $("fps").disabled = !config.video;
  makeColorChoices();
  state.dirty = false;
}
async function loadInterfaces(preferred) {
  const result = await api("/api/interfaces");
  state.interfaces = result.interfaces;
  const blocked = !result.radio.enabled;
  const alert = $("radio-alert");
  alert.hidden = !blocked && !result.interfaces.some(x => x.unavailable);
  if (!alert.hidden) {
    $("radio-explanation").textContent = result.radio.hard_blocked
      ? "Wi-Fi is physically blocked. Turn on the laptop Wi-Fi switch or wireless function key, then refresh."
      : blocked ? "Wi-Fi radio is disabled. Enable it here to make the adapters available for scanning."
      : "The Wi-Fi adapters are detected but unavailable. Check for an RF-kill switch, driver, or firmware problem.";
    $("enable-wifi").hidden = result.radio.hard_blocked || result.radio.enabled;
  }
  const eligible = result.interfaces.filter(x => x.selectable);
  selectOptions($("adapter-select"),
    result.interfaces.map(d => ({
      value:d.name, label:`${d.name} — ${d.connection && d.connection !== "--" ? d.connection : d.state_label}${d.is_uplink ? " (in use for internet — protected)" : ""}`,
      disabled:!d.selectable
    })),
    preferred || (eligible.length === 1 ? eligible[0].name : ""), "Select a Wi-Fi adapter…");
  const warning = $("adapter-warning");
  if (!eligible.length) {
    warning.hidden = false;
    warning.textContent = "No spare Wi-Fi adapter detected. Plug in a second adapter, then click Refresh. The computer's current internet connection is protected.";
  } else warning.hidden = true;
  $("adapter-pill").textContent = `Wi-Fi: ${field("adapter-select") || "not selected"}`;
}
async function scan() {
  const adapter = field("adapter-select");
  if (!adapter) { show("Choose a dedicated Wi-Fi adapter first.", true); return; }
  await busy($("scan"), async () => {
    const result = await api("/api/scan", {interface:adapter});
    state.networks = result.networks;
    renderNetworks();
    show(result.networks.length ? `Found ${result.networks.length} nearby networks.` : "No Wi-Fi networks found.");
  }, "Scanning…");
}
async function refreshStatus() {
  try {
    const result = await api("/api/status");
    const running = result.service.active;
    $("status-dot").className = "dot " + (running ? "ok" : result.configured ? "warn" : "bad");
    $("status-text").textContent = running ? "Bridge service running" : (result.configured ? "Bridge service stopped" : "Needs setup");
    $("setup-pill").textContent = result.configured ? "Setup: saved" : "Setup: incomplete";
    $("service-label").textContent = running ? "Service is running" : `Service is ${result.service.state}`;
    $("start-service").disabled = running || !result.configured;
    $("stop-service").disabled = !running;
    $("logs").textContent = result.logs.length ? result.logs.join("\n") : "No service logs yet.";
  } catch(e) {
    $("status-dot").className = "dot bad";
    $("status-text").textContent = "Status unavailable";
    $("logs").textContent = e.message;
  }
}
async function save() {
  await busy($("save"), async () => {
    const values = configFromUI();
    const result = await api("/api/save", values);
    state.dirty = false;
    $("password").value = "";
    $("password-hint").textContent = "Password saved. Leave blank to keep it.";
    $("save-title").textContent = "Settings saved";
    $("save-subtitle").textContent = "Bridge is starting with your configuration.";
    show(result.message);
    await refreshStatus();
  }, "Saving & restarting…");
}
async function serviceAction(action, button, message) {
  await busy(button, async () => {
    const response = await api(`/api/${action}`, {});
    show(response.message);
    await new Promise(resolve => setTimeout(resolve, 800));
    await refreshStatus();
  }, message);
}
function setupListeners() {
  for (const element of document.querySelectorAll("input,select")) {
    element.addEventListener("change", markDirty);
  }
  $("adapter-select").addEventListener("change", () => {
    $("adapter-pill").textContent = `Wi-Fi: ${field("adapter-select") || "not selected"}`;
    state.networks = []; renderNetworks();
    $("password").value = "";
    $("password-hint").textContent = "Enter Cozmo's password for this adapter.";
  });
  $("ssid").addEventListener("input", () => { renderNetworks(); markDirty(); });
  $("video").addEventListener("change", () => $("fps").disabled = !$("video").checked);
  $("refresh-adapters").addEventListener("click", () =>
    busy($("refresh-adapters"), () => loadInterfaces(field("adapter-select")), "Checking…"));
  $("enable-wifi").addEventListener("click", () => busy($("enable-wifi"), async () => {
    const result = await api("/api/enable-wifi", {});
    show(result.radio.enabled ? "Wi-Fi enabled. You can scan for Cozmo now." : "Wi-Fi could not be enabled. Check the hardware wireless switch.", !result.radio.enabled);
    await loadInterfaces(field("adapter-select"));
  }, "Enabling…"));
  $("scan").addEventListener("click", scan);
  $("reveal").addEventListener("click", () => {
    const p = $("password");
    p.type = p.type === "password" ? "text" : "password";
    $("reveal").textContent = p.type === "password" ? "Show" : "Hide";
  });
  $("test-server").addEventListener("click", () => busy($("test-server"), async () => {
    const data = await api("/api/probe", {url:field("server-url")});
    $("probe-result").textContent = data.message;
    $("probe-result").style.color = data.reachable ? "var(--aqua)" : "var(--red)";
  }, "Testing…"));
  $("save").addEventListener("click", save);
  $("start-service").addEventListener("click", () => serviceAction("start", $("start-service"), "Starting…"));
  $("stop-service").addEventListener("click", () => serviceAction("stop", $("stop-service"), "Stopping…"));
  $("refresh-status").addEventListener("click", refreshStatus);
  for (const btn of document.querySelectorAll("nav button[data-nav]")) {
    btn.addEventListener("click", () => {
      document.getElementById(btn.dataset.nav).scrollIntoView({behavior:"smooth",block:"start"});
      for (const b of document.querySelectorAll("nav button[data-nav]")) b.classList.remove("active");
      btn.classList.add("active");
    });
  }
  window.addEventListener("beforeunload", e => {if (state.dirty) {e.preventDefault(); e.returnValue="";}});
}
async function initialize() {
  setupListeners();
  try {
    const {config,options} = await api("/api/config");
    applyConfig(config, options);
    await loadInterfaces(config.interface);
    const selected = state.interfaces.find(d => d.name === config.interface);
    if (config.interface && (!selected || !selected.selectable)) {
      $("adapter-warning").hidden = false;
      $("adapter-warning").textContent = "Previously selected adapter is missing or is now the host's uplink. Select another adapter.";
    }
    $("save-title").textContent = config.configured ? "Setup ready" : "Complete the four steps";
    $("save-subtitle").textContent = config.configured ? "Adjust settings or reconnect whenever you like." : "Choose an adapter, scan, enter Wi-Fi password, and specify MRR.";
  } catch(e) { show(e.message, true); }
  await refreshStatus();
  setInterval(refreshStatus, 7000);
}
document.addEventListener("DOMContentLoaded", initialize);
