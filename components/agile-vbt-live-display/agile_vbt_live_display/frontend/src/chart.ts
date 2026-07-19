import Plotly from "plotly.js-dist-min";
import type { Config, Data, Layout, Shape } from "plotly.js";

import type { DashboardEvent, Sample } from "./types";


const COLORS = {
  raw: "#94A3B8",
  filtered: "#2563EB",
  threshold: "#DC2626",
  baseline: "#7C3AED",
  velocity: "#64748B",
  corrected: "#059669",
  rest: "#0F766E",
  orientation: "#D97706",
  rate: "#4F46E5",
  rep: "#16A34A",
  rejected: "#DC2626",
  gap: "#111827",
};

const STATE_COLORS: Record<string, string> = {
  calibrating: "#E2E8F0",
  rest: "#CCFBF1",
  up: "#DBEAFE",
  down: "#FEF3C7",
  recovery: "#FEE2E2",
};

const SAMPLE_TRACE_COUNT = 13;
const MARKER_KINDS = ["rep", "rejected", "gap", "top", "bottom"] as const;
const MARKER_TRACE_START = SAMPLE_TRACE_COUNT;

type PlotElement = HTMLElement & {
  data?: Data[];
  on: (
    eventName: string,
    handler: (event: Record<string, unknown>) => void
  ) => void;
};

export type LiveChart = {
  element: PlotElement;
  samples: Sample[];
  events: DashboardEvent[];
  historySeconds: number;
  initialized: boolean;
  userHasZoomed: boolean;
  programmaticLayout: boolean;
  lastShapeUpdateAt: number;
};

function times(samples: Sample[]): number[] {
  return samples.map((sample) => Number(sample.sensor_time_s));
}

function values(
  samples: Sample[],
  field: keyof Sample
): Array<number | string | boolean | null> {
  return samples.map((sample) => sample[field]);
}

function lineTrace(
  name: string,
  x: number[],
  y: Array<number | string | boolean | null>,
  color: string,
  xaxis: string,
  yaxis: string,
  extra: Partial<Data> = {}
): Data {
  return {
    type: "scattergl",
    mode: "lines",
    name,
    x,
    y,
    xaxis,
    yaxis,
    line: { color, width: 1.3 },
    ...extra,
  } as Data;
}

function baseTraces(samples: Sample[]): Data[] {
  const x = times(samples);
  const threshold = samples.map((sample) =>
    Number(sample.start_threshold_m_s2)
  );
  return [
    lineTrace(
      "Raw acceleration",
      x,
      values(samples, "raw_vertical_acceleration_m_s2"),
      COLORS.raw,
      "x",
      "y",
      { line: { color: COLORS.raw, width: 1 } }
    ),
    lineTrace(
      "Filtered acceleration",
      x,
      values(samples, "filtered_acceleration_m_s2"),
      COLORS.filtered,
      "x",
      "y",
      { line: { color: COLORS.filtered, width: 1.5 } }
    ),
    lineTrace(
      "Movement threshold",
      x,
      threshold,
      COLORS.threshold,
      "x",
      "y",
      {
        hoverinfo: "skip",
        line: { color: COLORS.threshold, width: 1, dash: "dot" },
      }
    ),
    lineTrace(
      "Negative movement threshold",
      x,
      threshold.map((value) => -value),
      COLORS.threshold,
      "x",
      "y",
      {
        showlegend: false,
        hoverinfo: "skip",
        line: { color: COLORS.threshold, width: 1, dash: "dot" },
      }
    ),
    lineTrace(
      "Gravity baseline",
      x,
      values(samples, "gravity_baseline_g"),
      COLORS.baseline,
      "x",
      "y2"
    ),
    lineTrace(
      "Provisional velocity",
      x,
      values(samples, "velocity_m_s"),
      COLORS.velocity,
      "x2",
      "y3"
    ),
    lineTrace(
      "Provisional displacement",
      x,
      values(samples, "displacement_m"),
      COLORS.velocity,
      "x3",
      "y4"
    ),
    lineTrace(
      "Rest confidence",
      x,
      values(samples, "rest_confidence"),
      COLORS.rest,
      "x4",
      "y5",
      {
        fill: "tozeroy",
        fillcolor: "rgba(15,118,110,0.10)",
      }
    ),
    lineTrace(
      "Orientation change",
      x,
      values(samples, "orientation_change_deg"),
      COLORS.orientation,
      "x4",
      "y6"
    ),
    lineTrace(
      "Orientation baseline lower",
      x,
      values(samples, "orientation_baseline_lower_deg"),
      "#A16207",
      "x4",
      "y6",
      { line: { color: "#A16207", width: 1.1, dash: "dot" } }
    ),
    lineTrace(
      "Orientation baseline upper",
      x,
      values(samples, "orientation_baseline_upper_deg"),
      "#A16207",
      "x4",
      "y6",
      { line: { color: "#A16207", width: 1.1, dash: "dot" } }
    ),
    lineTrace(
      "Orientation start threshold",
      x,
      values(samples, "orientation_start_threshold_deg"),
      COLORS.threshold,
      "x4",
      "y6",
      { line: { color: COLORS.threshold, width: 1.1, dash: "dash" } }
    ),
    lineTrace(
      "Estimated sample rate",
      x,
      values(samples, "estimated_sample_rate_hz"),
      COLORS.rate,
      "x5",
      "y7"
    ),
  ];
}

function nearestAcceleration(
  samples: Sample[],
  sensorTime: number
): number {
  if (samples.length === 0) return 0;
  let nearest = samples[0];
  let nearestDistance = Math.abs(nearest.sensor_time_s - sensorTime);
  for (const sample of samples.slice(1)) {
    const distance = Math.abs(sample.sensor_time_s - sensorTime);
    if (distance < nearestDistance) {
      nearest = sample;
      nearestDistance = distance;
    }
  }
  return Number(nearest.filtered_acceleration_m_s2);
}

function markerTrace(
  kind: (typeof MARKER_KINDS)[number],
  samples: Sample[],
  events: DashboardEvent[]
): Data {
  const config = {
    rep: ["Accepted rep", COLORS.rep, "star"],
    rejected: ["Rejected", COLORS.rejected, "x"],
    gap: ["Packet gap", COLORS.gap, "line-ns"],
    top: ["Top", "#9333EA", "triangle-down"],
    bottom: ["Bottom", "#92400E", "triangle-up"],
  } as const;
  const [name, color, symbol] = config[kind];
  const selected = events.filter((event) => event.kind === kind);
  return {
    type: "scatter",
    mode: "markers",
    name,
    x: selected.map((event) => event.sensor_time_s),
    y: selected.map((event) =>
      nearestAcceleration(samples, event.sensor_time_s)
    ),
    text: selected.map((event) => event.reason ?? name),
    hoverinfo: "x+text",
    xaxis: "x",
    yaxis: "y",
    marker: {
      color,
      symbol,
      size: 9,
      line: { color: "#FFFFFF", width: 1 },
    },
  } as Data;
}

function correctedTraces(events: DashboardEvent[]): Data[] {
  const traces: Data[] = [];
  for (const event of events) {
    if (event.kind !== "rep" || event.trace.length === 0) continue;
    const fallbackStart =
      event.sensor_time_s - event.trace[event.trace.length - 1].elapsed_s;
    const phaseStart = Number(
      event.quality.phase_started_s ?? fallbackStart
    );
    const x = event.trace.map((point) => phaseStart + point.elapsed_s);
    const recovered = event.quality.top_detection !== "velocity";
    const color = recovered ? COLORS.orientation : COLORS.corrected;
    const traceMetadata = {
      agileVbtCorrectedEnd: x[x.length - 1],
    };
    traces.push(
      lineTrace(
        "Corrected velocity",
        x,
        event.trace.map((point) => point.velocity_m_s),
        color,
        "x2",
        "y3",
        {
          legendgroup: "corrected",
          showlegend: traces.length === 0,
          meta: traceMetadata,
          line: { color, width: 2.2 },
        }
      ),
      lineTrace(
        "Corrected displacement",
        x,
        event.trace.map((point) => point.displacement_m),
        color,
        "x3",
        "y4",
        {
          legendgroup: "corrected",
          showlegend: false,
          meta: traceMetadata,
          line: { color, width: 2.2 },
        }
      )
    );
  }
  return traces;
}

function stateRegions(samples: Sample[]): Array<Partial<Shape>> {
  if (samples.length === 0) return [];
  const shapes: Array<Partial<Shape>> = [];
  let state = samples[0].state_after;
  let started = samples[0].sensor_time_s;
  const addRegion = (ended: number) => {
    shapes.push({
      type: "rect",
      x0: started,
      x1: ended,
      y0: 0,
      y1: 1,
      xref: "x",
      yref: "paper",
      fillcolor: STATE_COLORS[state] ?? "#F8FAFC",
      opacity: 0.13,
      line: { width: 0 },
      layer: "below",
    });
  };
  for (const sample of samples.slice(1)) {
    if (sample.state_after === state) continue;
    addRegion(sample.sensor_time_s);
    state = sample.state_after;
    started = sample.sensor_time_s;
  }
  addRegion(samples[samples.length - 1].sensor_time_s);
  return shapes;
}

function orientationRegions(samples: Sample[]): Array<Partial<Shape>> {
  const shapes: Array<Partial<Shape>> = [];
  let started: number | null = null;
  for (const sample of samples) {
    if (sample.orientation_region_started) {
      started = sample.sensor_time_s;
    }
    if (sample.orientation_region_ended && started !== null) {
      shapes.push({
        type: "rect",
        x0: started,
        x1: sample.sensor_time_s,
        y0: 0.17,
        y1: 0.34,
        xref: "x4",
        yref: "paper",
        fillcolor: sample.orientation_region_confirmed
          ? "rgba(217,119,6,0.18)"
          : "rgba(148,163,184,0.10)",
        line: { width: 1, color: "rgba(217,119,6,0.35)" },
        layer: "below",
      });
      started = null;
    }
  }
  if (started !== null && samples.length > 0) {
    shapes.push({
      type: "rect",
      x0: started,
      x1: samples[samples.length - 1].sensor_time_s,
      y0: 0.17,
      y1: 0.34,
      xref: "x4",
      yref: "paper",
      fillcolor: "rgba(148,163,184,0.10)",
      line: { width: 1, color: "rgba(217,119,6,0.35)" },
      layer: "below",
    });
  }
  return shapes;
}

function chartShapes(samples: Sample[]): Array<Partial<Shape>> {
  return [...stateRegions(samples), ...orientationRegions(samples)];
}

function axis(
  domain: [number, number],
  extra: Record<string, unknown> = {}
): Record<string, unknown> {
  return {
    domain,
    zeroline: true,
    gridcolor: "rgba(148,163,184,0.20)",
    ...extra,
  };
}

function themeValue(
  element: HTMLElement,
  name: string,
  fallback: string
): string {
  if (typeof window === "undefined") return fallback;
  const value = window
    .getComputedStyle(element)
    .getPropertyValue(name)
    .trim();
  return value || fallback;
}

function layout(
  samples: Sample[],
  element: HTMLElement
): Partial<Layout> {
  const textColor = themeValue(element, "--st-text-color", "#CBD5E1");
  const cardColor = themeValue(
    element,
    "--st-background-color",
    "#0E1117"
  );
  const annotationLabels: Array<[number, string]> = [
    [0.995, "World-up acceleration (m/s²)"],
    [0.695, "Velocity (m/s)"],
    [0.495, "Upward displacement (m)"],
    [0.335, "Rest confidence and orientation change (degrees)"],
    [0.095, "Adaptive sample clock (Hz)"],
  ];
  return {
    height: 1050,
    autosize: true,
    margin: { l: 58, r: 58, t: 82, b: 48 },
    hovermode: "x unified",
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: textColor, size: 11 },
    legend: {
      orientation: "h",
      y: 1.045,
      x: 0,
      bgcolor: cardColor,
      font: { size: 10 },
    },
    uirevision: "agile-vbt-live-dashboard",
    xaxis: { anchor: "y", showticklabels: false },
    yaxis: axis([0.75, 1.0]),
    yaxis2: {
      overlaying: "y",
      side: "right",
      showgrid: false,
    },
    xaxis2: { anchor: "y3", matches: "x", showticklabels: false },
    yaxis3: axis([0.54, 0.70]),
    xaxis3: { anchor: "y4", matches: "x", showticklabels: false },
    yaxis4: axis([0.39, 0.50]),
    xaxis4: { anchor: "y5", matches: "x", showticklabels: false },
    yaxis5: axis([0.17, 0.34], { range: [0, 1.05] }),
    yaxis6: {
      overlaying: "y5",
      side: "right",
      showgrid: false,
    },
    xaxis5: {
      anchor: "y7",
      matches: "x",
      title: { text: "Sensor time (s)" },
    },
    yaxis7: axis([0, 0.10], { range: [43, 52] }),
    shapes: chartShapes(samples),
    annotations: annotationLabels.map(([position, text]) => ({
      x: 0,
      xref: "paper",
      xanchor: "left",
      y: Number(position),
      yref: "paper",
      yanchor: "top",
      text,
      showarrow: false,
      bgcolor: "rgba(14,17,23,0.70)",
      borderpad: 3,
      font: { color: textColor, size: 13 },
    })),
  };
}

const config: Partial<Config> = {
  displaylogo: false,
  responsive: true,
  scrollZoom: true,
};

export function createLiveChart(
  element: HTMLElement,
  historySeconds: number
): LiveChart {
  return {
    element: element as PlotElement,
    samples: [],
    events: [],
    historySeconds,
    initialized: false,
    userHasZoomed: false,
    programmaticLayout: false,
    lastShapeUpdateAt: 0,
  };
}

export async function replaceChart(
  chart: LiveChart,
  samples: Sample[],
  events: DashboardEvent[]
): Promise<void> {
  chart.samples = trimSamples(samples, chart.historySeconds);
  chart.events = trimEvents(events, chart.samples);
  const traces = [
    ...baseTraces(chart.samples),
    ...MARKER_KINDS.map((kind) =>
      markerTrace(kind, chart.samples, chart.events)
    ),
    ...correctedTraces(chart.events),
  ];
  if (chart.initialized) {
    await Plotly.react(
      chart.element,
      traces,
      layout(chart.samples, chart.element),
      config
    );
  } else {
    await Plotly.newPlot(
      chart.element,
      traces,
      layout(chart.samples, chart.element),
      config
    );
    chart.element.on("plotly_relayout", (event) => {
      if (chart.programmaticLayout) return;
      if (
        Object.keys(event).some((key) =>
          key.startsWith("xaxis.range")
        )
      ) {
        chart.userHasZoomed = true;
      }
      if (event["xaxis.autorange"] === true) {
        chart.userHasZoomed = false;
      }
    });
    chart.initialized = true;
  }
  chart.userHasZoomed = false;
  chart.lastShapeUpdateAt = Date.now();
  await followLatest(chart);
}

export async function appendChart(
  chart: LiveChart,
  samples: Sample[],
  events: DashboardEvent[]
): Promise<void> {
  if (samples.length === 0 && events.length === 0) return;
  if (!chart.initialized) {
    await replaceChart(chart, samples, events);
    return;
  }
  const maximumPoints = Math.ceil(chart.historySeconds * 52) + 16;
  if (samples.length > 0) {
    const x = times(samples);
    const threshold = samples.map((sample) =>
      Number(sample.start_threshold_m_s2)
    );
    const ys = [
      values(samples, "raw_vertical_acceleration_m_s2"),
      values(samples, "filtered_acceleration_m_s2"),
      threshold,
      threshold.map((value) => -value),
      values(samples, "gravity_baseline_g"),
      values(samples, "velocity_m_s"),
      values(samples, "displacement_m"),
      values(samples, "rest_confidence"),
      values(samples, "orientation_change_deg"),
      values(samples, "orientation_baseline_lower_deg"),
      values(samples, "orientation_baseline_upper_deg"),
      values(samples, "orientation_start_threshold_deg"),
      values(samples, "estimated_sample_rate_hz"),
    ];
    await Plotly.extendTraces(
      chart.element,
      {
        x: Array.from({ length: SAMPLE_TRACE_COUNT }, () => x),
        y: ys,
      } as unknown as Data,
      Array.from({ length: SAMPLE_TRACE_COUNT }, (_, index) => index),
      maximumPoints
    );
  }

  const combinedSamples = trimSamples(
    [...chart.samples, ...samples],
    chart.historySeconds
  );
  for (const [offset, kind] of MARKER_KINDS.entries()) {
    const selected = events.filter((event) => event.kind === kind);
    if (selected.length === 0) continue;
    await Plotly.extendTraces(
      chart.element,
      {
        x: [selected.map((event) => event.sensor_time_s)],
        y: [
          selected.map((event) =>
            nearestAcceleration(combinedSamples, event.sensor_time_s)
          ),
        ],
        text: [
          selected.map((event) => event.reason ?? kind),
        ],
      } as unknown as Data,
      [MARKER_TRACE_START + offset],
      1000
    );
  }

  const corrected = correctedTraces(events);
  if (corrected.length > 0) {
    await Plotly.addTraces(chart.element, corrected);
  }
  chart.samples = combinedSamples;
  chart.events = trimEvents(
    [...chart.events, ...events],
    chart.samples
  );
  await trimCorrectedTraceHistory(chart);
  await updateLiveLayout(chart, events.length > 0);
}

async function updateLiveLayout(
  chart: LiveChart,
  forceShapes: boolean
): Promise<void> {
  const update: Record<string, unknown> = {};
  const now = Date.now();
  if (forceShapes || now - chart.lastShapeUpdateAt >= 1000) {
    update.shapes = chartShapes(chart.samples);
    chart.lastShapeUpdateAt = now;
  }
  if (!chart.userHasZoomed && chart.samples.length > 0) {
    const latest = chart.samples[chart.samples.length - 1].sensor_time_s;
    const earliest = Math.max(
      chart.samples[0].sensor_time_s,
      latest - chart.historySeconds
    );
    update["xaxis.range"] = [earliest, latest];
  }
  if (Object.keys(update).length === 0) return;
  chart.programmaticLayout = true;
  try {
    await Plotly.relayout(chart.element, update);
  } finally {
    chart.programmaticLayout = false;
  }
}

async function trimCorrectedTraceHistory(
  chart: LiveChart
): Promise<void> {
  if (chart.samples.length === 0 || !chart.element.data) return;
  const cutoff = chart.samples[0].sensor_time_s;
  const expired: number[] = [];
  chart.element.data.forEach((trace, index) => {
    if (index < SAMPLE_TRACE_COUNT + MARKER_KINDS.length) return;
    const metadata = (trace as Data & { meta?: unknown }).meta;
    if (
      typeof metadata === "object" &&
      metadata !== null &&
      "agileVbtCorrectedEnd" in metadata &&
      Number(metadata.agileVbtCorrectedEnd) < cutoff
    ) {
      expired.push(index);
    }
  });
  if (expired.length > 0) {
    await Plotly.deleteTraces(chart.element, expired);
  }
}

async function followLatest(chart: LiveChart): Promise<void> {
  if (chart.userHasZoomed || chart.samples.length === 0) return;
  const latest = chart.samples[chart.samples.length - 1].sensor_time_s;
  const earliest = Math.max(
    chart.samples[0].sensor_time_s,
    latest - chart.historySeconds
  );
  chart.programmaticLayout = true;
  try {
    await Plotly.relayout(chart.element, {
      "xaxis.range": [earliest, latest],
    });
  } finally {
    chart.programmaticLayout = false;
  }
}

function trimSamples(
  samples: Sample[],
  historySeconds: number
): Sample[] {
  if (samples.length === 0) return [];
  const cutoff =
    samples[samples.length - 1].sensor_time_s - historySeconds;
  return samples.filter((sample) => sample.sensor_time_s >= cutoff);
}

function trimEvents(
  events: DashboardEvent[],
  samples: Sample[]
): DashboardEvent[] {
  if (samples.length === 0) return [];
  const cutoff = samples[0].sensor_time_s;
  return events.filter((event) => event.sensor_time_s >= cutoff);
}

export function purgeChart(chart: LiveChart): void {
  if (chart.initialized) {
    Plotly.purge(chart.element);
    chart.initialized = false;
  }
}
