import type {
  FrontendRenderer,
  FrontendRendererArgs,
} from "@streamlit/component-v2-lib";

import {
  appendChart,
  createLiveChart,
  purgeChart,
  replaceChart,
  type LiveChart,
} from "./chart";
import "./style.css";
import type {
  ComponentData,
  DashboardEvent,
  DashboardSummary,
  DeltaMessage,
  ServerMessage,
} from "./types";


type FrontendState = Record<string, never>;

type Instance = {
  root: HTMLElement;
  chart: LiveChart;
  socket: WebSocket | null;
  reconnectTimer: number | null;
  pingTimer: number | null;
  watchdogTimer: number | null;
  stopped: boolean;
  revision: number;
  source: string | null;
  dataSignature: string;
  renderQueue: Promise<void>;
  pendingDeltas: DeltaMessage[];
  deltaFlushPending: boolean;
  lastMessageAt: number;
  reconnectAttempts: number;
  socketGeneration: number;
  visibilityHandler: () => void;
  onlineHandler: () => void;
};

const instances: WeakMap<
  FrontendRendererArgs["parentElement"],
  Instance
> = new WeakMap();

const PING_INTERVAL_MS = 2000;
const STALE_CONNECTION_MS = 5000;
const MAX_RECONNECT_DELAY_MS = 5000;

const TABLE_COLUMNS = [
  "Time",
  "Candidate",
  "Quality",
  "Top detection",
  "Duration",
  "Displacement",
  "Average velocity",
  "Peak velocity",
  "Drift correction",
  "Raw endpoint velocity",
  "Orientation excursion",
  "Final rest",
  "Sample rate",
  "Rate confidence",
  "Evidence",
  "Resynchronization",
  "Reason",
];

function renderShell(root: HTMLElement): void {
  root.innerHTML = `
    <header class="dashboard-header">
      <div class="source-line">
        <span data-role="source">Waiting for a recording</span>
        <span class="connection-detail" data-role="connection-detail"></span>
      </div>
      <h1>Agile VBT live movement</h1>
      <p>A read-only live view of the growing raw recording. Bluetooth tracking and Excel writing stay isolated in the sensor process.</p>
    </header>
    <section class="status-row" aria-label="Live dashboard status">
      ${[
        "Connection",
        "Profile",
        "State",
        "Repetitions",
        "Rejected",
        "Sample rate",
        "Missing",
        "Latency",
      ]
        .map(
          (label) => `
            <div class="metric-card">
              <div class="metric-label">${label}</div>
              <div class="metric-value" data-metric="${label}">—</div>
            </div>`
        )
        .join("")}
    </section>
    <section class="graph-guide" aria-label="Graph legend">
      <div class="guide-heading">
        <strong>Graph key</strong>
        <span>Move the pointer over any line to see its name, time, and value. Click a Plotly legend item to hide or show it.</span>
      </div>
      <div class="guide-groups">
        <div class="guide-group">
          <b>Acceleration</b>
          <span><i class="swatch raw"></i>Raw</span>
          <span><i class="swatch filtered"></i>Filtered</span>
          <span><i class="swatch threshold"></i>Movement threshold</span>
          <span><i class="swatch gravity"></i>Gravity baseline</span>
        </div>
        <div class="guide-group">
          <b>Calculated movement</b>
          <span><i class="swatch provisional"></i>Provisional</span>
          <span><i class="swatch corrected"></i>Corrected rep</span>
        </div>
        <div class="guide-group">
          <b>Rest and orientation</b>
          <span><i class="swatch rest"></i>Rest confidence</span>
          <span><i class="swatch orientation"></i>Orientation change</span>
          <span><i class="swatch orientation-baseline"></i>Local baseline band</span>
          <span><i class="swatch threshold"></i>Region start threshold</span>
        </div>
        <div class="guide-group movement-states">
          <b>Movement areas</b>
          <span><i class="area rest-state"></i>Rest</span>
          <span><i class="area up-state"></i>Up</span>
          <span><i class="area down-state"></i>Down</span>
          <span><i class="area recovery-state"></i>Recovery</span>
          <span><i class="area orientation-region"></i>Orientation candidate</span>
        </div>
      </div>
    </section>
    <div class="plot-card">
      <div class="plot" data-role="plot" aria-label="Live movement graph"></div>
    </div>
    <section class="candidate-card">
      <h3>Movement candidates</h3>
      <div class="candidate-scroll">
        <table>
          <thead>
            <tr>${TABLE_COLUMNS.map((column) => `<th>${column}</th>`).join("")}</tr>
          </thead>
          <tbody data-role="candidate-body"></tbody>
        </table>
      </div>
    </section>
  `;
}

function metric(root: HTMLElement, name: string, value: string): void {
  const element = root.querySelector<HTMLElement>(
    `[data-metric="${name}"]`
  );
  if (element) element.textContent = value;
}

function renderConnection(
  instance: Instance,
  status: string,
  detail = ""
): void {
  metric(instance.root, "Connection", status);
  const element = instance.root.querySelector<HTMLElement>(
    '[data-role="connection-detail"]'
  );
  if (element) element.textContent = detail;
}

function renderSummary(
  instance: Instance,
  summary: DashboardSummary,
  serverTimeMs: number
): void {
  const latency = Math.max(0, Date.now() - Number(serverTimeMs));
  metric(
    instance.root,
    "Connection",
    summary.status === "receiving" ? "Receiving" : "Waiting"
  );
  metric(instance.root, "Profile", summary.exercise || "generic");
  metric(instance.root, "State", summary.state || "waiting");
  metric(
    instance.root,
    "Repetitions",
    String(summary.accepted_reps ?? 0)
  );
  metric(
    instance.root,
    "Rejected",
    String(summary.rejected_candidates ?? 0)
  );
  metric(
    instance.root,
    "Sample rate",
    `${Number(summary.sample_rate_hz ?? 47.6).toFixed(2)} Hz`
  );
  metric(
    instance.root,
    "Missing",
    String(summary.missing_samples ?? 0)
  );
  metric(instance.root, "Latency", `${latency} ms`);
  const source = instance.root.querySelector<HTMLElement>(
    '[data-role="source"]'
  );
  if (source) {
    source.textContent = summary.source_name
      ? `${summary.source_name} · ${Number(summary.sample_count).toLocaleString()} decoded samples · ${summary.algorithm}`
      : "Waiting for a recording";
  }
}

function numberValue(
  value: unknown,
  digits: number,
  suffix = ""
): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${value.toFixed(digits)}${suffix}`;
}

function textValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

function candidateRows(events: DashboardEvent[]): string[][] {
  let repetitionNumber = 0;
  return events
    .filter((event) => event.kind === "rep" || event.kind === "rejected")
    .map((event) => {
      if (event.kind === "rep") repetitionNumber += 1;
      const quality = event.quality;
      const metrics = event.metrics;
      return [
        numberValue(event.sensor_time_s, 2, " s"),
        event.kind === "rep" ? `Rep ${repetitionNumber}` : "Rejected",
        textValue(
          quality.quality_status ??
            (event.kind === "rep" ? "accepted" : "rejected")
        ),
        textValue(quality.top_detection),
        numberValue(metrics.duration_s, 3, " s"),
        numberValue(metrics.displacement_m, 3, " m"),
        numberValue(metrics.average_velocity_m_s, 3, " m/s"),
        numberValue(metrics.peak_velocity_m_s, 3, " m/s"),
        numberValue(quality.drift_correction_m_s, 3, " m/s"),
        numberValue(quality.raw_final_velocity_m_s, 3, " m/s"),
        numberValue(quality.orientation_excursion_deg, 1, "°"),
        numberValue(quality.final_rest_duration_s, 2, " s"),
        numberValue(quality.estimated_sample_rate_hz, 2, " Hz"),
        textValue(quality.rate_confidence),
        textValue(quality.evidence),
        textValue(quality.resynchronization_reason),
        textValue(event.reason),
      ];
    });
}

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderCandidates(
  instance: Instance,
  events: DashboardEvent[]
): void {
  const body = instance.root.querySelector<HTMLElement>(
    '[data-role="candidate-body"]'
  );
  if (!body) return;
  const candidates = candidateRows(events);
  if (candidates.length === 0) {
    body.innerHTML = `
      <tr class="empty-row">
        <td colspan="${TABLE_COLUMNS.length}">No movement candidates in the visible history.</td>
      </tr>`;
    return;
  }
  body.innerHTML = candidates
    .map((row, index) => {
      const event = events.filter(
        (candidate) =>
          candidate.kind === "rep" || candidate.kind === "rejected"
      )[index];
      const className = event.kind === "rep" ? "accepted" : "rejected";
      return `<tr class="${className}">${row
        .map((cell) => `<td>${escapeHtml(cell)}</td>`)
        .join("")}</tr>`;
    })
    .join("");
}

function websocketUrl(path: string): string {
  const url = new URL(path, window.location.href);
  url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

function subscription(data: ComponentData): Record<string, unknown> {
  return {
    type: "subscribe",
    source: data.source,
    exercise: data.exercise,
    history_seconds: data.historySeconds,
  };
}

async function handleMessage(
  instance: Instance,
  message: ServerMessage
): Promise<void> {
  if (message.type === "snapshot") {
    instance.revision = message.revision;
    instance.source = message.source;
    await replaceChart(instance.chart, message.samples, message.events);
    renderSummary(instance, message.summary, message.server_time_ms);
    renderCandidates(instance, message.events);
    return;
  }
  if (message.type === "heartbeat") {
    renderSummary(instance, message.summary, message.server_time_ms);
    return;
  }
  if (message.type === "reset") {
    instance.revision = 0;
    renderConnection(instance, "Resynchronizing", message.reason);
    return;
  }
  if (message.type === "error") {
    renderConnection(instance, "Error", message.message);
  }
}

function renderError(instance: Instance, error: unknown): void {
  renderConnection(
    instance,
    "Error",
    error instanceof Error ? error.message : String(error)
  );
}

function queueDelta(instance: Instance, message: DeltaMessage): void {
  if (message.revision <= instance.revision) return;
  instance.pendingDeltas.push(message);
  scheduleDeltaDrain(instance);
}

function scheduleDeltaDrain(instance: Instance): void {
  if (instance.deltaFlushPending) return;
  instance.deltaFlushPending = true;
  instance.renderQueue = instance.renderQueue
    .then(async () => {
      const pending = instance.pendingDeltas.splice(0);
      const fresh = pending.filter(
        (delta) => delta.revision > instance.revision
      );
      if (fresh.length === 0) return;
      const latest = fresh[fresh.length - 1];
      instance.revision = latest.revision;
      await appendChart(
        instance.chart,
        fresh.flatMap((delta) => delta.samples),
        fresh.flatMap((delta) => delta.events)
      );
      renderSummary(instance, latest.summary, latest.server_time_ms);
      if (fresh.some((delta) => delta.events.length > 0)) {
        renderCandidates(instance, instance.chart.events);
      }
    })
    .catch((error: unknown) => renderError(instance, error))
    .finally(() => {
      instance.deltaFlushPending = false;
      if (instance.pendingDeltas.length > 0 && !instance.stopped) {
        scheduleDeltaDrain(instance);
      }
    });
}

function clearLivenessTimers(instance: Instance): void {
  if (instance.pingTimer !== null) {
    window.clearInterval(instance.pingTimer);
    instance.pingTimer = null;
  }
  if (instance.watchdogTimer !== null) {
    window.clearInterval(instance.watchdogTimer);
    instance.watchdogTimer = null;
  }
}

function reconnectDelay(instance: Instance): number {
  const delay = Math.min(
    MAX_RECONNECT_DELAY_MS,
    500 * 2 ** instance.reconnectAttempts
  );
  instance.reconnectAttempts += 1;
  return delay;
}

function scheduleReconnect(
  instance: Instance,
  data: ComponentData,
  detail: string
): void {
  if (
    instance.stopped ||
    data.paused ||
    instance.reconnectTimer !== null
  ) {
    return;
  }
  clearLivenessTimers(instance);
  const delay = reconnectDelay(instance);
  renderConnection(
    instance,
    "Reconnecting",
    `${detail} Retrying in ${(delay / 1000).toFixed(1)} s.`
  );
  instance.reconnectTimer = window.setTimeout(() => {
    instance.reconnectTimer = null;
    connect(instance, data);
  }, delay);
}

function restartConnection(
  instance: Instance,
  data: ComponentData,
  detail: string
): void {
  const socket = instance.socket;
  instance.socket = null;
  clearLivenessTimers(instance);
  if (socket !== null) {
    socket.onclose = null;
    socket.onerror = null;
    try {
      socket.close();
    } catch {
      // The browser can reject close() while the handshake is incomplete.
    }
  }
  scheduleReconnect(instance, data, detail);
}

function startLivenessTimers(
  instance: Instance,
  data: ComponentData,
  socket: WebSocket,
  generation: number
): void {
  clearLivenessTimers(instance);
  instance.pingTimer = window.setInterval(() => {
    if (
      instance.stopped ||
      instance.socket !== socket ||
      instance.socketGeneration !== generation
    ) {
      return;
    }
    try {
      socket.send(JSON.stringify({ type: "ping" }));
    } catch {
      restartConnection(instance, data, "The live connection stopped.");
    }
  }, PING_INTERVAL_MS);
  instance.watchdogTimer = window.setInterval(() => {
    if (
      instance.stopped ||
      instance.socket !== socket ||
      instance.socketGeneration !== generation
    ) {
      return;
    }
    if (Date.now() - instance.lastMessageAt > STALE_CONNECTION_MS) {
      restartConnection(
        instance,
        data,
        "No dashboard data arrived for five seconds."
      );
    }
  }, 1000);
}

function connect(instance: Instance, data: ComponentData): void {
  if (instance.stopped || data.paused) return;
  if (instance.reconnectTimer !== null) {
    window.clearTimeout(instance.reconnectTimer);
    instance.reconnectTimer = null;
  }
  const generation = instance.socketGeneration + 1;
  instance.socketGeneration = generation;
  renderConnection(instance, "Connecting");
  const socket = new WebSocket(websocketUrl(data.websocketPath));
  instance.socket = socket;
  socket.onopen = () => {
    if (
      instance.stopped ||
      instance.socket !== socket ||
      instance.socketGeneration !== generation
    ) {
      return;
    }
    instance.lastMessageAt = Date.now();
    renderConnection(instance, "Connected");
    socket.send(JSON.stringify(subscription(data)));
    startLivenessTimers(instance, data, socket, generation);
  };
  socket.onmessage = (event) => {
    if (
      instance.socket !== socket ||
      instance.socketGeneration !== generation
    ) {
      return;
    }
    instance.lastMessageAt = Date.now();
    instance.reconnectAttempts = 0;
    let message: ServerMessage;
    try {
      message = JSON.parse(String(event.data)) as ServerMessage;
    } catch {
      renderConnection(instance, "Error", "Invalid server message");
      return;
    }
    if (message.type === "pong") return;
    if (message.type === "delta") {
      queueDelta(instance, message);
      return;
    }
    if (message.type === "snapshot" || message.type === "reset") {
      instance.pendingDeltas.length = 0;
    }
    instance.renderQueue = instance.renderQueue
      .then(() => handleMessage(instance, message))
      .catch((error: unknown) => renderError(instance, error));
  };
  socket.onerror = () => {
    if (instance.socket === socket) {
      restartConnection(
        instance,
        data,
        "The WebSocket reported a connection error."
      );
    }
  };
  socket.onclose = () => {
    if (instance.socket !== socket) return;
    instance.socket = null;
    scheduleReconnect(instance, data, "The live connection closed.");
  };
}

function destroy(instance: Instance): void {
  instance.stopped = true;
  clearLivenessTimers(instance);
  if (instance.reconnectTimer !== null) {
    window.clearTimeout(instance.reconnectTimer);
  }
  if (instance.socket !== null) {
    instance.socket.onclose = null;
    instance.socket.close();
  }
  document.removeEventListener(
    "visibilitychange",
    instance.visibilityHandler
  );
  window.removeEventListener("online", instance.onlineHandler);
  purgeChart(instance.chart);
}

const AgileVbtLiveDisplay: FrontendRenderer<
  FrontendState,
  ComponentData
> = ({ parentElement, data }) => {
  const root = parentElement.querySelector<HTMLElement>(".agile-vbt-live-root");
  if (!root) throw new Error("Agile VBT dashboard root element was not found.");

  const dataSignature = JSON.stringify(data);
  let instance = instances.get(parentElement);
  if (instance && instance.dataSignature !== dataSignature) {
    destroy(instance);
    instances.delete(parentElement);
    instance = undefined;
  }
  if (!instance) {
    renderShell(root);
    const plot = root.querySelector<HTMLElement>('[data-role="plot"]');
    if (!plot) throw new Error("Agile VBT dashboard plot element was not found.");
    instance = {
      root,
      chart: createLiveChart(plot, data.historySeconds),
      socket: null,
      reconnectTimer: null,
      pingTimer: null,
      watchdogTimer: null,
      stopped: false,
      revision: 0,
      source: null,
      dataSignature,
      renderQueue: Promise.resolve(),
      pendingDeltas: [],
      deltaFlushPending: false,
      lastMessageAt: Date.now(),
      reconnectAttempts: 0,
      socketGeneration: 0,
      visibilityHandler: () => undefined,
      onlineHandler: () => undefined,
    };
    instance.visibilityHandler = () => {
      if (
        document.visibilityState === "visible" &&
        Date.now() - instance!.lastMessageAt > PING_INTERVAL_MS
      ) {
        restartConnection(
          instance!,
          data,
          "The dashboard tab became active again."
        );
      }
    };
    instance.onlineHandler = () => {
      restartConnection(
        instance!,
        data,
        "The browser network connection returned."
      );
    };
    document.addEventListener(
      "visibilitychange",
      instance.visibilityHandler
    );
    window.addEventListener("online", instance.onlineHandler);
    instances.set(parentElement, instance);
    if (data.paused) {
      renderConnection(instance, "Paused");
    } else {
      connect(instance, data);
    }
  }

  const mountedInstance = instance;
  return () => {
    if (instances.get(parentElement) === mountedInstance) {
      destroy(mountedInstance);
      instances.delete(parentElement);
    }
  };
};

export default AgileVbtLiveDisplay;
