"use strict";
const $ = id => document.getElementById(id);
const state = {
  config: null, options: null, interfaces: [], networks: [], colors: [],
  color: "#3ba8ff", dirty: false, loading: false,
  service: null, telemetry: null, status: null, page: "monitor"
};
const field = id => $(id).value;
const pages = {
  monitor: ["Overview", "Live bridge and robot health"],
  adapter: ["Connections", "Cozmo Wi-Fi and the MultiRoombaRover server"],
  channel: ["Dock automation", "Wi-Fi channel selection and Home Assistant dock power"],
  options: ["Rover & video", "Rover identity, drive limits and video settings"],
  diagnostics: ["Diagnostics", "Full hardware telemetry and service logs"]
};
const show = (message, error = false) => {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = "toast show" + (error ? " error" : "");
  clearTimeout(show.timer);
  show.timer = setTimeout(() => toast.className = "toast", 6500);
};
async function api(path, data) {
  const options = data === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(data)
  };
  let response;
  try { response = await fetch(path, options); }
  catch (_) { throw new Error("Cannot reach the setup service. Check cozmo-roverd-setup.service."); }
  let result;
  try { result = await response.json(); }
  catch (_) { throw new Error("The setup service returned an invalid response."); }
  if (!response.ok) throw new Error(result.error || "Request failed (" + response.status + ")");
  return result;
}
function busy(button, action, label) {
  if (button.disabled) return Promise.resolve();
  const original = button.textContent;
  button.disabled = true;
  button.textContent = label;
  return Promise.resolve().then(action).catch(error => {
    show(error.message, true);
  }).finally(() => {
    button.textContent = original;
    button.disabled = false;
  });
}
function navigate(page, writeHash = true) {
  if (!Object.hasOwn(pages, page)) page = "monitor";
  state.page = page;
  for (const section of document.querySelectorAll("[data-page]")) {
    section.hidden = section.id !== page;
  }
  for (const button of document.querySelectorAll("[data-nav]")) {
    const active = button.dataset.nav === page;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  $("page-title").textContent = pages[page][0];
  $("page-subtitle").textContent = pages[page][1];
  if (writeHash && location.hash !== "#" + page) history.replaceState(null, "", "#" + page);
  window.scrollTo({top: 0, behavior: "instant"});
}
function updateSaveBar() {
  $("save-bar").hidden = !state.dirty;
  $("save-title").textContent = "Unsaved configuration changes";
  $("save-subtitle").textContent = "Save & restart applies all edited settings together.";
}
function markDirty() {
  if (state.loading) return;
  state.dirty = true;
  updateSaveBar();
}
function selectOptions(select, options, value, placeholder) {
  select.replaceChildren();
  if (placeholder) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = placeholder;
    select.append(option);
  }
  for (const item of options) {
    const option = document.createElement("option");
    option.value = String(item.value);
    option.textContent = item.label;
    option.disabled = !!item.disabled;
    select.append(option);
  }
  select.value = String(value ?? "");
}
function makeColorChoices() {
  const target = $("color-choices");
  target.replaceChildren();
  for (const color of state.colors) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "color" + (state.color === color ? " selected" : "");
    button.style.backgroundColor = color;
    button.setAttribute("aria-label", "Rover color " + color);
    button.setAttribute("aria-pressed", String(state.color === color));
    button.addEventListener("click", () => {
      state.color = color;
      makeColorChoices();
      markDirty();
    });
    target.append(button);
  }
}
function renderNetworks() {
  const list = $("networks");
  list.replaceChildren();
  if (!state.networks.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No networks listed. Click Scan networks when Cozmo is advertising its Wi-Fi.";
    list.append(empty);
    return;
  }
  for (const network of state.networks) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "network" + (field("ssid") === network.ssid ? " selected" : "");
    const left = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = network.ssid;
    const tag = document.createElement("span");
    tag.textContent = network.cozmo ? "  · Cozmo" : "";
    left.append(title, tag);
    const right = document.createElement("span");
    right.textContent = network.signal + "% signal";
    button.append(left, right);
    button.addEventListener("click", () => {
      $("ssid").value = network.ssid;
      $("password").value = "";
      $("password-hint").textContent = state.config?.has_password &&
        network.ssid === state.config.ssid
        ? "Stored password will be reused." : "Enter the password shown on Cozmo.";
      renderNetworks();
      markDirty();
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
    ssid: field("ssid").trim(),
    password: field("password"),
    max_speed: Number(field("speed")),
    speaker_volume: field("speaker-volume").trim() === "" ? null : Number(field("speaker-volume")),
    tts_gain: field("tts-gain").trim() === "" ? null : Number(field("tts-gain")),
    horn_gain: field("horn-gain").trim() === "" ? null : Number(field("horn-gain")),
    camera_mode: "auto",
    hud: $("hud").checked,
    fps: Number(field("fps")),
    video: $("video").checked,
    camera_enabled: $("camera-enabled").checked,
    diagnostic_minimal: $("diagnostic-minimal").checked,
    rtsp_url: field("rtsp-url").trim(),
    channel_reroll_enabled: $("channel-enable").checked,
    channel_target: Number(field("channel-target")),
    home_assistant_url: field("ha-url").trim(),
    home_assistant_switch: field("ha-switch").trim(),
    home_assistant_token: field("ha-token")
  };
}
function applyConfig(config, options) {
  state.loading = true;
  state.config = config;
  state.options = options;
  state.colors = options.colors || [];
  state.color = config.color;
  $("rover-name").value = config.name;
  $("description").value = config.description;
  $("server-url").value = config.server_url;
  $("private").checked = !!config.private;
  $("ssid").value = config.ssid;
  $("password").value = "";
  $("password-hint").textContent = config.has_password
    ? "Password saved. Leave blank to keep it." : "Enter the Wi-Fi password shown on Cozmo.";
  $("channel-enable").checked = config.channel_reroll_enabled === true;
  $("channel-target").value = String(config.channel_target || 1);
  $("ha-url").value = config.home_assistant_url || "";
  $("ha-switch").value = config.home_assistant_switch || "";
  $("ha-token").value = "";
  $("ha-token-hint").textContent = config.home_assistant_has_token
    ? "Token saved. Leave blank to keep it when URL and switch remain unchanged."
    : "Enter a long-lived Home Assistant access token.";
  $("speaker-volume").value = config.speaker_volume ?? 65535;
  $("tts-gain").value = config.tts_gain ?? 9000;
  $("horn-gain").value = config.horn_gain ?? 9000;
  $("video").checked = config.video !== false;
  $("camera-enabled").checked = config.camera_enabled !== false;
  $("hud").checked = config.hud !== false;
  $("diagnostic-minimal").checked = config.diagnostic_minimal === true;
  $("rtsp-url").value = config.rtsp_url || "";
  selectOptions($("speed"), options.speeds.map(n => ({value: n, label: n + " mm/s"})), config.max_speed);
  selectOptions($("fps"), options.fps.map(n => ({value: n, label: n + " FPS"})), config.fps);
  $("fps").disabled = !$("video").checked;
  makeColorChoices();
  $("side-name").textContent = config.name || "Cozmo";
  state.dirty = false;
  state.loading = false;
  updateSaveBar();
  renderConnectionSummary();
}
async function loadInterfaces(preferred) {
  const result = await api("/api/interfaces");
  state.interfaces = result.interfaces;
  const blocked = !result.radio.enabled;
  const alert = $("radio-alert");
  alert.hidden = !blocked && !result.interfaces.some(i => i.unavailable);
  if (!alert.hidden) {
    $("radio-explanation").textContent = result.radio.hard_blocked
      ? "Wi-Fi is physically blocked. Unlock the hardware switch."
      : blocked ? "Wi-Fi radio is disabled." : "One or more Wi-Fi adapters are unavailable.";
    $("enable-wifi").hidden = result.radio.hard_blocked || result.radio.enabled;
  }
  const available = result.interfaces.filter(i => i.selectable);
  const chosen = preferred ?? field("adapter-select") ?? "";
  selectOptions($("adapter-select"), result.interfaces.map(i => ({
    value: i.name,
    label: i.name + " — " + (i.connection && i.connection !== "--" ? i.connection : i.state_label) +
      (i.is_uplink ? " (host uplink · protected)" : ""),
    disabled: !i.selectable
  })), chosen || (available.length === 1 ? available[0].name : ""), "Choose a Wi-Fi adapter");
  const warning = $("adapter-warning");
  if (!available.length) {
    warning.hidden = false;
    warning.textContent = "No dedicated Wi-Fi adapter available. Connect a second adapter. The host uplink cannot be selected.";
  } else if (chosen && !available.some(i => i.name === chosen)) {
    warning.hidden = false;
    warning.textContent = "Saved Wi-Fi adapter is missing or protected. Choose another adapter.";
  } else warning.hidden = true;
  renderConnectionSummary();
}
async function scan() {
  const interfaceName = field("adapter-select");
  if (!interfaceName) { show("Select a dedicated Wi-Fi adapter first.", true); return; }
  $("wifi-discovery").open = true;
  await busy($("scan"), async () => {
    const result = await api("/api/scan", {interface: interfaceName});
    state.networks = result.networks;
    renderNetworks();
    if (!result.networks.length) show("No Wi-Fi networks found.", true);
  }, "Scanning…");
}
function displayMetric(value, suffix = "") {
  return value === undefined || value === null || value === ""
    ? "—" : (typeof value === "boolean" ? value ? "Yes" : "No" : String(value) + suffix);
}
function setText(id, value) { $(id).textContent = value; }
function formatStage(stage) {
  const names = {
    starting: "Starting hardware supervisor",
    connecting_wifi: "Connecting to Cozmo Wi-Fi",
    connecting_robot: "Opening PyCozmo connection",
    online: "Connected and operational",
    cleaning_up: "Closing old connection",
    retrying: "Waiting to reconnect",
    channel_shutting_down: "Shutting down Cozmo to change Wi-Fi channel",
    channel_closing_connection: "Closing old PyCozmo connection",
    channel_cycling_dock_power: "Cycling dock power through Home Assistant",
    channel_waiting_for_wifi: "Waiting for Cozmo Wi-Fi after restart",
    channel_connecting_robot: "Reconnecting to Cozmo",
    channel_verifying: "Checking Cozmo's new Wi-Fi channel"
  };
  return names[stage] || (stage ? stage.replaceAll("_", " ") : "Awaiting connection");
}
function renderConnectionSummary() {
  const config = state.config || {};
  const adapter = field("adapter-select") || config.interface || "";
  setText("adapter-pill", "Wi-Fi: " + (adapter || "not configured"));
  setText("summary-adapter", adapter || "Not configured");
  setText("summary-server", config.server_url || "Not configured");
  setText("summary-wifi", config.ssid || "Not configured");
  setText("side-location", config.server_url ? "MRR: " + config.server_url : "MRR not configured");
  setText("summary-reroll-switch", config.home_assistant_switch || "Not configured");
}
function rerollLabel(reroll) {
  if (!reroll || !Object.hasOwn(reroll, "enabled")) return "No live channel data";
  if (!reroll.enabled) return "Disabled";
  const statuses = {
    matched: "On target channel",
    waiting_for_dock: "Waiting until docked",
    waiting_for_wifi: "Waiting for robot Wi-Fi",
    channel_unknown: "Wi-Fi channel unknown",
    retry_cooldown: "Waiting after an operational failure",
    home_assistant_unavailable: "Home Assistant unavailable",
    shutting_down: "Shutting down Cozmo",
    closing_connection: "Closing robot connection",
    cycling_dock_power: "Cycling dock power",
    connecting_robot: "Connecting to Cozmo",
    verifying: "Checking new Wi-Fi channel",
    mismatch: "Wrong channel — restarting",
    error: "Reroll failed; recovery pending"
  };
  return statuses[reroll.status] || formatStage(reroll.status);
}
function telemetryFresh(data) {
  return !!data && Number.isFinite(data.snapshot_age_seconds) &&
    data.snapshot_age_seconds >= 0 && data.snapshot_age_seconds < 12;
}
function renderServiceStatus() {
  const service = state.service;
  const config = state.config || {};
  const data = state.telemetry;
  const fresh = telemetryFresh(data);
  const hardware = service?.active && fresh ? data.hardware_connected === true : false;
  const active = service?.active === true;
  const failed = !active || !fresh || !hardware;
  const stage = fresh ? data.hardware_stage : null;
  const rerolling = !!stage?.startsWith("channel_");
  setText("status-text", !service ? "Bridge status unknown" : !active ? "Bridge stopped" :
    !fresh ? "Bridge running · telemetry unavailable" : hardware ? "Cozmo connected" :
    rerolling ? "Cozmo restarting" : "Cozmo disconnected");
  $("status-dot").className = "dot " + (hardware ? "ok" : active ? "warn" : "bad");
  $("robot-indicator").className = "state-indicator " + (hardware ? "ok" : rerolling ? "warn" : "bad");
  setText("robot-headline", hardware ? "Cozmo connected" : rerolling ? "Changing Cozmo's Wi-Fi channel" :
    !active ? "Bridge service stopped" : "Cozmo unavailable");
  setText("robot-stage", !active ? "Start the bridge from Diagnostics after configuring it." :
    !fresh ? "The bridge is running, but its telemetry is missing or stale." :
    formatStage(stage) + (data.last_hardware_error && !hardware ? " — " + data.last_hardware_error : ""));
  setText("bridge-connection", "Service: " + (!service ? "unknown" : active ? "running" : service.state));
  setText("mrr-connection", "MRR: " + (fresh ? data.mrr_connected ? "connected" : "disconnected" : "unknown"));
  const stream = fresh ? data.video || {} : {};
  setText("video-connection", "RTSP: " + (stream.enabled ? stream.publisher_running ? "publishing" : "unavailable" : "disabled"));
  setText("service-label", active ? "Bridge service is running" : "Bridge service is " + (service?.state || "unknown"));
  setText("setup-pill", config.configured ? "Configured" : "Setup incomplete");
  $("start-service").disabled = active || !config.configured;
  $("stop-service").disabled = !active;
  setText("service-action-hint", !config.configured ? "Configure connections before starting." :
    active ? "Stopping disconnects Cozmo and MRR." : "Start the bridge with its saved settings.");
}
function renderOverview(data, fresh) {
  const c = fresh ? data.cozmo || {} : {};
  const stateInfo = fresh ? data.rover_state || {} : {};
  const battery = stateInfo.battery || {};
  const track = fresh ? data.battery_tracker || {} : {};
  const reroll = fresh ? data.channel_reroll || {} : {};
  const video = fresh ? data.video || {} : {};
  const voltage = track.voltage_v ?? c.battery_voltage_v;
  setText("overview-battery", battery.percent == null ? "—" : Math.round(battery.percent) + "%");
  setText("overview-voltage", voltage == null ? "No battery voltage" : Number(voltage).toFixed(2) + " V · last measured");
  const docked = c.on_charger ?? stateInfo.docked;
  setText("overview-dock", docked == null ? "Unknown" : docked ? "Docked" : "Undocked");
  const charging = c.charging ?? stateInfo.charging;
  setText("overview-charging", charging == null ? "No charging data" : charging ? "Charging" : "Not charging");
  setText("overview-channel", reroll.actual == null ? "—" : String(reroll.actual));
  setText("overview-target", !reroll.enabled ? "Monitoring disabled" : "Target: " + reroll.target);
  setText("overview-fps", video.camera_input_fps_2s == null ? "—" : video.camera_input_fps_2s + " fps");
  setText("overview-camera", !video.enabled ? "RTSP disabled" :
    video.publisher_running ? "RTSP publishing" : "RTSP not publishing");
  setText("summary-reroll", !reroll.enabled ? "Disabled" : "Enabled");
  setText("summary-reroll-progress", rerollLabel(reroll));
  let description = rerollLabel(reroll);
  if (reroll.enabled) {
    description += " · current " + (reroll.actual ?? "?") + " / target " + reroll.target;
    if (reroll.attempt) description += " · restart #" + reroll.attempt;
    if (reroll.retry_in_seconds > 0) description += " · retry in " + reroll.retry_in_seconds + " s";
    if (reroll.error) description += " · " + reroll.error;
  }
  setText("channel-live", description);
  $("channel-live").className = "notice" +
    (reroll.status === "error" || reroll.status === "home_assistant_unavailable" ? " error" :
    reroll.enabled && reroll.status !== "matched" ? " warn" : "");
  const age = fresh ? Number(track.age_seconds ?? data.telemetry_age_seconds) : NaN;
  setText("telemetry-age", !fresh ? "Telemetry unavailable or stale" :
    Number.isFinite(age) ? "Robot sample: " + age.toFixed(1) + " seconds ago" : "Robot sample not yet received");
  setText("diagnostic-update", !fresh ? "Telemetry unavailable or stale" :
    "Live snapshot · " + new Date(data.generated_at_ms).toLocaleTimeString());
}
function diagCard(title, details) {
  const card = document.createElement("div");
  card.className = "diag-card";
  const heading = document.createElement("h3");
  heading.textContent = title;
  const dl = document.createElement("dl");
  dl.className = "keyvalues";
  for (const [name, val] of details) {
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = name;
    dd.textContent = val;
    dl.append(dt, dd);
  }
  card.append(heading, dl);
  return card;
}
function renderDiagnostics(data, fresh) {
  const cards = $("telemetry-cards");
  cards.replaceChildren();
  if (!fresh) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No recent bridge telemetry. Start the service or check its logs.";
    cards.append(empty);
    setText("telemetry-raw", "No recent telemetry.");
    return;
  }
  const c = data.cozmo || {};
  const st = data.rover_state || {};
  const b = st.battery || {};
  const bt = data.battery_tracker || {};
  const pose = c.pose || {};
  const wheels = c.wheel_speeds_mmps || {};
  const video = data.video || {};
  const transport = data.transport || {};
  const speaker = data.speaker || {};
  const reroll = data.channel_reroll || {};
  cards.append(
    diagCard("Power & charging", [
      ["Battery estimate", displayMetric(b.percent, "%")],
      ["Voltage", displayMetric(bt.voltage_v ?? c.battery_voltage_v, " V")],
      ["Voltage sample age", displayMetric(bt.voltage_age_seconds, " s")],
      ["On charger", displayMetric(c.on_charger)],
      ["Charging", displayMetric(c.charging)],
      ["Needs charge", displayMetric(b.needsCharge)],
      ["Telemetry age", displayMetric(bt.age_seconds, " s")]
    ]),
    diagCard("Drive & sensors", [
      ["Left wheel", displayMetric(wheels.left, " mm/s")],
      ["Right wheel", displayMetric(wheels.right, " mm/s")],
      ["Head", displayMetric(c.head_angle_deg, "°")],
      ["Lift", displayMetric(c.lift_height_mm, " mm")],
      ["Picked up", displayMetric(c.picked_up)],
      ["Cliff", displayMetric(c.cliff_detected)],
      ["X", displayMetric(pose.x_mm, " mm")],
      ["Y", displayMetric(pose.y_mm, " mm")],
      ["Heading", displayMetric(pose.heading_deg, "°")]
    ]),
    diagCard("Connection & radio", [
      ["Hardware stage", formatStage(data.hardware_stage)],
      ["MRR connected", displayMetric(data.mrr_connected)],
      ["Reconnect count", displayMetric(data.hardware_reconnect_attempts)],
      ["Last error", displayMetric(data.last_hardware_error)],
      ["Wi-Fi channel", displayMetric(reroll.actual)],
      ["Target channel", displayMetric(reroll.target)],
      ["Channel status", rerollLabel(reroll)],
      ["Restart attempt", displayMetric(reroll.attempt)],
      ["Channel error", displayMetric(reroll.error)]
    ]),
    diagCard("Video pipeline", [
      ["Encoder", displayMetric(video.encoder)],
      ["Publisher running", displayMetric(video.publisher_running)],
      ["Input FPS", displayMetric(video.camera_input_fps_2s)],
      ["RTSP FPS", displayMetric(video.rtsp_output_fps_2s)],
      ["Camera age", displayMetric(video.last_frame_age_sec, " s")],
      ["Dropped frames", displayMetric(video.frames_dropped)],
      ["Repeated frames", displayMetric(video.rtsp_repeated_camera_frames)],
      ["Last video error", displayMetric(video.last_error)]
    ]),
    diagCard("Transport & packet statistics", [
      ["RobotState packets", displayMetric(data.packets_received)],
      ["ACK pending", displayMetric(transport.pending_reliable_packets)],
      ["Application queue", displayMetric(transport.pending_application_commands)],
      ["Outgoing packets", displayMetric(transport.outgoing_packets)],
      ["Transmissions + retries", displayMetric(transport.transmitted_packets_including_retries)],
      ["Camera packets", displayMetric(data.camera_transport?.image_chunks_received)],
      ["Complete camera images", displayMetric(data.camera_transport?.complete_images_received)],
      ["Packet error", displayMetric(data.last_packet_error)]
    ]),
    diagCard("Face & speech", [
      ["Last TTS", displayMetric(data.last_tts)],
      ["Speech status", displayMetric(data.last_tts_error)],
      ["Hardware speaker", displayMetric(speaker.enabled)],
      ["Face frames", displayMetric(data.face_frames_sent)],
      ["Last face error", displayMetric(data.face_last_error)]
    ])
  );
  setText("telemetry-raw", JSON.stringify(data, null, 2));
}
async function refreshTelemetry() {
  try {
    const data = await api("/api/telemetry");
    state.telemetry = data;
    const fresh = telemetryFresh(data);
    renderOverview(data, fresh);
    if (state.page === "diagnostics") renderDiagnostics(data, fresh);
  } catch (error) {
    state.telemetry = null;
    renderOverview({}, false);
    if (state.page === "diagnostics") renderDiagnostics({}, false);
  }
  renderServiceStatus();
}
async function refreshStatus() {
  try {
    const response = await api("/api/status");
    state.service = response.service;
    state.status = response;
    $("logs").textContent = response.logs.length ? response.logs.join("\n") : "No service logs yet.";
  } catch (error) {
    state.service = null;
    $("logs").textContent = error.message;
  }
  renderServiceStatus();
}
async function save() {
  await busy($("save"), async () => {
    const current = configFromUI();
    const reply = await api("/api/save", current);
    const configReply = await api("/api/config");
    applyConfig(configReply.config, configReply.options);
    await loadInterfaces(configReply.config.interface);
    state.networks = [];
    renderNetworks();
    show(reply.message);
    await refreshStatus();
  }, "Saving…");
}
async function discard() {
  await busy($("discard"), async () => {
    const response = await api("/api/config");
    applyConfig(response.config, response.options);
    await loadInterfaces(response.config.interface);
    state.networks = [];
    renderNetworks();
    show("Unsaved changes discarded.");
  }, "Reverting…");
}
async function serviceAction(action, button, label) {
  await busy(button, async () => {
    const response = await api("/api/" + action, {});
    show(response.message);
    await new Promise(resolve => setTimeout(resolve, 800));
    await refreshStatus();
  }, label);
}
function setupListeners() {
  for (const control of document.querySelectorAll("[data-page] input,[data-page] select")) {
    control.addEventListener("change", markDirty);
    if (control.matches('input:not([type=checkbox])')) {
      control.addEventListener("input", markDirty);
    }
  }
  $("adapter-select").addEventListener("change", () => {
    state.networks = [];
    renderNetworks();
    $("password").value = "";
    $("password-hint").textContent = "Leave blank to keep the password for the same SSID.";
    renderConnectionSummary();
  });
  $("ssid").addEventListener("input", renderNetworks);
  $("video").addEventListener("change", () => $("fps").disabled = !$("video").checked);
  $("channel-enable").addEventListener("change", () => {
    setText("channel-live", "Configuration changed. Save & restart to apply.");
  });
  $("refresh-adapters").addEventListener("click", () =>
    busy($("refresh-adapters"), async () => loadInterfaces(field("adapter-select")), "Refreshing…"));
  $("enable-wifi").addEventListener("click", () =>
    busy($("enable-wifi"), async () => {
      const result = await api("/api/enable-wifi", {});
      show(result.radio.enabled ? "Wi-Fi enabled." : "Could not enable Wi-Fi.", !result.radio.enabled);
      await loadInterfaces(field("adapter-select"));
    }, "Enabling…"));
  $("scan").addEventListener("click", scan);
  $("reveal").addEventListener("click", () => {
    const password = $("password");
    password.type = password.type === "password" ? "text" : "password";
    $("reveal").textContent = password.type === "password" ? "Show" : "Hide";
  });
  $("test-server").addEventListener("click", () =>
    busy($("test-server"), async () => {
      const response = await api("/api/probe", {url: field("server-url")});
      setText("probe-result", response.message);
    }, "Checking…"));
  $("test-dock-switch").addEventListener("click", () =>
    busy($("test-dock-switch"), async () => {
      const response = await api("/api/test-dock-switch", {
        home_assistant_url: field("ha-url").trim(),
        home_assistant_switch: field("ha-switch").trim(),
        home_assistant_token: field("ha-token")
      });
      setText("test-dock-result", response.message);
    }, "Checking…"));
  $("save").addEventListener("click", save);
  $("discard").addEventListener("click", discard);
  $("start-service").addEventListener("click", () =>
    serviceAction("start", $("start-service"), "Starting…"));
  $("stop-service").addEventListener("click", () =>
    serviceAction("stop", $("stop-service"), "Stopping…"));
  $("refresh-status").addEventListener("click", async () => {
    await Promise.all([refreshStatus(), refreshTelemetry()]);
  });
  $("refresh-diagnostics").addEventListener("click", async () => {
    await Promise.all([refreshStatus(), refreshTelemetry()]);
  });
  for (const button of document.querySelectorAll("[data-nav]")) {
    button.addEventListener("click", () => {
      navigate(button.dataset.nav);
      if (state.page === "diagnostics" && state.telemetry) {
        renderDiagnostics(state.telemetry,
          telemetryFresh(state.telemetry));
      }
    });
  }
  for (const button of document.querySelectorAll("[data-go]")) {
    button.addEventListener("click", () => navigate(button.dataset.go));
  }
  window.addEventListener("hashchange", () => navigate(location.hash.slice(1), false));
  window.addEventListener("beforeunload", event => {
    if (state.dirty) { event.preventDefault(); event.returnValue = ""; }
  });
}
async function initialize() {
  setupListeners();
  navigate(location.hash.slice(1), false);
  try {
    const response = await api("/api/config");
    applyConfig(response.config, response.options);
    await loadInterfaces(response.config.interface);
    if (!response.config.configured) navigate("adapter");
  } catch (error) {
    show(error.message, true);
  }
  await Promise.all([refreshStatus(), refreshTelemetry()]);
  setInterval(refreshStatus, 7000);
  setInterval(refreshTelemetry, 1500);
}
document.addEventListener("DOMContentLoaded", initialize);
