import { beforeEach, describe, expect, it, vi } from "vitest";

import type { DashboardEvent, Sample } from "./types";


const plotly = vi.hoisted(() => ({
  addTraces: vi.fn(async () => undefined),
  deleteTraces: vi.fn(async () => undefined),
  extendTraces: vi.fn(async () => undefined),
  newPlot: vi.fn(async () => undefined),
  purge: vi.fn(),
  react: vi.fn(async () => undefined),
  relayout: vi.fn(
    async (_element: unknown, _update: Record<string, unknown>) =>
      undefined
  ),
}));

vi.mock("plotly.js-dist-min", () => ({ default: plotly }));

import {
  appendChart,
  createLiveChart,
  replaceChart,
} from "./chart";


function sample(time: number): Sample {
  return {
    sensor_time_s: time,
    state_after: "rest",
    raw_vertical_acceleration_m_s2: 0,
    filtered_acceleration_m_s2: 0,
    start_threshold_m_s2: 0.1,
    gravity_baseline_g: 1,
    velocity_m_s: 0,
    displacement_m: 0,
    rest_confidence: 1,
    orientation_change_deg: 0,
    orientation_baseline_lower_deg: 0,
    orientation_baseline_upper_deg: 1,
    orientation_start_threshold_deg: 6,
    orientation_region_started: false,
    orientation_region_ended: false,
    orientation_region_confirmed: false,
    orientation_region_id: 0,
    estimated_sample_rate_hz: 47.6,
    rate_confidence: "measured",
  };
}

function event(time: number): DashboardEvent {
  return {
    sensor_time_s: time,
    kind: "top",
    reason: "test top",
    metrics: {},
    quality: {},
    trace: [],
  };
}

function plotElement() {
  let relayoutHandler: ((event: Record<string, unknown>) => void) | null =
    null;
  return {
    element: {
      on: (
        _name: string,
        handler: (event: Record<string, unknown>) => void
      ) => {
        relayoutHandler = handler;
      },
    } as unknown as HTMLElement,
    zoom: () => relayoutHandler?.({ "xaxis.range[0]": 1 }),
  };
}

describe("live Plotly updates", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("creates once and extends traces for later samples", async () => {
    const target = plotElement();
    const chart = createLiveChart(target.element, 90);
    await replaceChart(chart, [sample(1)], []);
    expect(plotly.newPlot).toHaveBeenCalledTimes(1);
    expect(plotly.react).not.toHaveBeenCalled();
    const initialLayout = (
      plotly.newPlot.mock.calls as unknown as Array<
        [
          unknown,
          unknown,
          {
            annotations: Array<{ text: string }>;
            shapes: unknown[];
          },
        ]
      >
    )[0][2];
    expect(initialLayout.annotations.map((item) => item.text)).toContain(
      "Velocity (m/s)"
    );
    expect(initialLayout.annotations.map((item) => item.text)).toContain(
      "Upward displacement (m)"
    );
    expect(initialLayout.shapes).toHaveLength(1);

    vi.clearAllMocks();
    await appendChart(chart, [sample(1.2)], [event(1.2)]);
    expect(plotly.extendTraces).toHaveBeenCalled();
    expect(plotly.newPlot).not.toHaveBeenCalled();
    expect(plotly.react).not.toHaveBeenCalled();
  });

  it("keeps a user zoom range while deltas arrive", async () => {
    const target = plotElement();
    const chart = createLiveChart(target.element, 90);
    await replaceChart(chart, [sample(1)], []);
    target.zoom();

    vi.clearAllMocks();
    await appendChart(chart, [sample(1.2)], []);
    const updates = plotly.relayout.mock.calls.map((call) => call[1]);
    expect(
      updates.some((update) =>
        Object.prototype.hasOwnProperty.call(update, "xaxis.range")
      )
    ).toBe(false);
  });

  it("trims in-memory samples to the selected history", async () => {
    const target = plotElement();
    const chart = createLiveChart(target.element, 15);
    await replaceChart(chart, [sample(0), sample(10)], []);
    await appendChart(chart, [sample(20)], []);
    expect(chart.samples.map((item) => item.sensor_time_s)).toEqual([
      10, 20,
    ]);
  });

  it("removes corrected traces that fall outside the history window", async () => {
    const target = plotElement();
    const chart = createLiveChart(target.element, 15);
    await replaceChart(chart, [sample(0), sample(10)], []);
    (target.element as HTMLElement & { data: unknown[] }).data = [
      ...Array.from({ length: 18 }, () => ({})),
      { meta: { agileVbtCorrectedEnd: 1 } },
      { meta: { agileVbtCorrectedEnd: 1 } },
    ];

    await appendChart(chart, [sample(20)], []);

    expect(plotly.deleteTraces).toHaveBeenCalledWith(
      target.element,
      [18, 19]
    );
  });
});
