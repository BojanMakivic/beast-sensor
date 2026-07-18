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
  ServerMessage,
} from "./types";


type FrontendState = Record<string, never>;

type Instance = {
  root: HTMLElement;
  chart: LiveChart;
  socket: WebSocket | null;
  reconnectTimer: number | null;
  stopped: boolean;
  revision: number;
  source: string | null;
  dataSignature: string;
  renderQueue: Promise<void>;
};

const instances: WeakMap<
  FrontendRendererArgs["parentElement"],
  Instance
> = new WeakMap();

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
    <div class="source-line">
      <span data-role="source">Waiting for a recording</span>
      <span class="connection-detail" data-role="connection-detail"></span>
    </div>
    <div class="plot" data-role="plot" aria-label="Live movement graph"></div>
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
  if (message.type === "delta") {
    if (message.revision <= instance.revision) return;
    instance.revision = message.revision;
    await appendChart(instance.chart, message.samples, message.events);
    renderSummary(instance, message.summary, message.server_time_ms);
    renderCandidates(instance, instance.chart.events);
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

function connect(instance: Instance, data: ComponentData): void {
  if (instance.stopped || data.paused) return;
  renderConnection(instance, "Connecting");
  const socket = new WebSocket(websocketUrl(data.websocketPath));
  instance.socket = socket;
  socket.onopen = () => {
    if (instance.stopped) return;
    renderConnection(instance, "Connected");
    socket.send(JSON.stringify(subscription(data)));
  };
  socket.onmessage = (event) => {
    let message: ServerMessage;
    try {
      message = JSON.parse(String(event.data)) as ServerMessage;
    } catch {
      renderConnection(instance, "Error", "Invalid server message");
      return;
    }
    instance.renderQueue = instance.renderQueue
      .then(() => handleMessage(instance, message))
      .catch((error: unknown) => {
        renderConnection(
          instance,
          "Error",
          error instanceof Error ? error.message : String(error)
        );
      });
  };
  socket.onerror = () => {
    renderConnection(instance, "Connection error");
  };
  socket.onclose = () => {
    if (instance.stopped) return;
    renderConnection(instance, "Reconnecting");
    instance.reconnectTimer = window.setTimeout(
      () => connect(instance, data),
      750
    );
  };
}

function destroy(instance: Instance): void {
  instance.stopped = true;
  if (instance.reconnectTimer !== null) {
    window.clearTimeout(instance.reconnectTimer);
  }
  if (instance.socket !== null) {
    instance.socket.onclose = null;
    instance.socket.close();
  }
  purgeChart(instance.chart);
}

const BeastLiveDisplay: FrontendRenderer<
  FrontendState,
  ComponentData
> = ({ parentElement, data }) => {
  const root = parentElement.querySelector<HTMLElement>(".beast-live-root");
  if (!root) throw new Error("Beast dashboard root element was not found.");

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
    if (!plot) throw new Error("Beast dashboard plot element was not found.");
    instance = {
      root,
      chart: createLiveChart(plot, data.historySeconds),
      socket: null,
      reconnectTimer: null,
      stopped: false,
      revision: 0,
      source: null,
      dataSignature,
      renderQueue: Promise.resolve(),
    };
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

export default BeastLiveDisplay;
