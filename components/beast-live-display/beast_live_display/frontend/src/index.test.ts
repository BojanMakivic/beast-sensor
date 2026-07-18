// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ComponentData, ServerMessage } from "./types";


const chartFunctions = vi.hoisted(() => ({
  appendChart: vi.fn(
    async (
      chart: { samples: unknown[]; events: unknown[] },
      samples: unknown[],
      events: unknown[]
    ) => {
      chart.samples.push(...samples);
      chart.events.push(...events);
    }
  ),
  createLiveChart: vi.fn(
    (element: HTMLElement, historySeconds: number) => ({
      element,
      samples: [],
      events: [],
      historySeconds,
      initialized: false,
      userHasZoomed: false,
      programmaticLayout: false,
    })
  ),
  purgeChart: vi.fn(),
  replaceChart: vi.fn(
    async (
      chart: { samples: unknown[]; events: unknown[] },
      samples: unknown[],
      events: unknown[]
    ) => {
      chart.samples = [...samples];
      chart.events = [...events];
    }
  ),
}));

vi.mock("./chart", () => chartFunctions);

import BeastLiveDisplay from "./index";


class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  sent: string[] = [];

  constructor(public readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(message: string): void {
    this.sent.push(message);
  }

  close(): void {}

  open(): void {
    this.onopen?.();
  }

  receive(message: ServerMessage): void {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  disconnect(): void {
    this.onclose?.();
  }
}

const DATA: ComponentData = {
  websocketPath: "/api/beast/live",
  source: { mode: "latest", path: null },
  exercise: "bench",
  historySeconds: 90,
  paused: false,
};

function parentElement(): HTMLElement {
  const parent = document.createElement("div");
  parent.innerHTML = '<div class="beast-live-root"></div>';
  document.body.append(parent);
  return parent;
}

function render(parent: HTMLElement, data: ComponentData = DATA) {
  return BeastLiveDisplay(
    {
      parentElement: parent,
      data,
      state: {},
      setState: vi.fn(),
    } as never
  );
}

function summary() {
  return {
    status: "receiving",
    source_name: "live.jsonl",
    exercise: "bench",
    algorithm: "generic-velocity-v4",
    state: "up",
    accepted_reps: 1,
    rejected_candidates: 1,
    sample_count: 120,
    sample_rate_hz: 47.6,
    rate_confidence: "measured",
    missing_samples: 0,
    file_age_s: 0.1,
  };
}

describe("Beast live dashboard component", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
    FakeWebSocket.instances = [];
    vi.clearAllMocks();
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });

  it("keeps one plot container while WebSocket messages update the display", async () => {
    const parent = parentElement();
    const cleanup = render(parent);
    const plot = parent.querySelector('[data-role="plot"]');
    const socket = FakeWebSocket.instances[0];
    socket.open();

    expect(socket.url).toBe("ws://localhost:3000/api/beast/live");
    expect(JSON.parse(socket.sent[0])).toMatchObject({
      type: "subscribe",
      exercise: "bench",
      history_seconds: 90,
    });

    socket.receive({
      type: "snapshot",
      protocol: 1,
      revision: 1,
      server_time_ms: Date.now(),
      source: "live.jsonl",
      samples: [],
      events: [
        {
          sensor_time_s: 3.2,
          kind: "rep",
          reason: "accepted",
          metrics: {
            duration_s: 0.8,
            displacement_m: 0.3,
            average_velocity_m_s: 0.4,
            peak_velocity_m_s: 0.8,
          },
          quality: {
            quality_status: "accepted",
            top_detection: "velocity",
          },
          trace: [],
        },
        {
          sensor_time_s: 4.2,
          kind: "rejected",
          reason: "too short",
          metrics: {},
          quality: {},
          trace: [],
        },
      ],
      summary: summary(),
    });
    await vi.waitFor(() => {
      expect(chartFunctions.replaceChart).toHaveBeenCalledTimes(1);
    });

    expect(parent.querySelectorAll("th")).toHaveLength(17);
    expect(parent.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(parent.textContent).toContain("Rep 1");
    expect(parent.textContent).toContain("47.60 Hz");
    expect(parent.querySelector('[data-role="plot"]')).toBe(plot);

    socket.receive({
      type: "heartbeat",
      protocol: 1,
      server_time_ms: Date.now(),
      summary: summary(),
    });
    await Promise.resolve();
    expect(parent.querySelector('[data-role="plot"]')).toBe(plot);

    cleanup?.();
    expect(chartFunctions.purgeChart).toHaveBeenCalledTimes(1);
  });

  it("does not open a WebSocket while paused", () => {
    const parent = parentElement();
    const cleanup = render(parent, { ...DATA, paused: true });

    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(
      parent.querySelector('[data-metric="Connection"]')?.textContent
    ).toBe("Paused");

    cleanup?.();
  });

  it("reconnects after a dropped local socket", () => {
    vi.useFakeTimers();
    const parent = parentElement();
    const cleanup = render(parent);

    FakeWebSocket.instances[0].disconnect();
    vi.advanceTimersByTime(750);

    expect(FakeWebSocket.instances).toHaveLength(2);
    cleanup?.();
    vi.useRealTimers();
  });
});
