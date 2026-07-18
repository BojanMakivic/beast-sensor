export type ComponentData = {
  websocketPath: string;
  source: {
    mode: "latest" | "file";
    path: string | null;
  };
  exercise: string | null;
  historySeconds: number;
  paused: boolean;
};

export type Sample = {
  sensor_time_s: number;
  state_after: string;
  raw_vertical_acceleration_m_s2: number;
  filtered_acceleration_m_s2: number;
  start_threshold_m_s2: number;
  gravity_baseline_g: number | null;
  velocity_m_s: number;
  displacement_m: number;
  rest_confidence: number;
  orientation_change_deg: number;
  orientation_baseline_lower_deg: number;
  orientation_baseline_upper_deg: number;
  orientation_start_threshold_deg: number;
  orientation_region_started: boolean;
  orientation_region_ended: boolean;
  orientation_region_confirmed: boolean;
  orientation_region_id: number;
  estimated_sample_rate_hz: number;
  rate_confidence: string;
};

export type MovementTracePoint = {
  elapsed_s: number;
  velocity_m_s: number;
  displacement_m: number;
};

export type DashboardEvent = {
  sensor_time_s: number;
  kind: string;
  reason: string | null;
  metrics: Record<string, unknown>;
  quality: Record<string, unknown>;
  trace: MovementTracePoint[];
};

export type DashboardSummary = {
  status: string;
  source_name: string | null;
  exercise: string;
  algorithm: string;
  state: string;
  accepted_reps: number;
  rejected_candidates: number;
  sample_count: number;
  sample_rate_hz: number;
  rate_confidence: string;
  missing_samples: number;
  file_age_s: number | null;
};

export type SnapshotMessage = {
  type: "snapshot";
  protocol: number;
  revision: number;
  server_time_ms: number;
  source: string | null;
  samples: Sample[];
  events: DashboardEvent[];
  summary: DashboardSummary;
};

export type DeltaMessage = {
  type: "delta";
  protocol: number;
  revision: number;
  server_time_ms: number;
  samples: Sample[];
  events: DashboardEvent[];
  summary: DashboardSummary;
};

export type HeartbeatMessage = {
  type: "heartbeat";
  protocol: number;
  server_time_ms: number;
  summary: DashboardSummary;
};

export type ResetMessage = {
  type: "reset";
  protocol: number;
  reason: string;
};

export type ErrorMessage = {
  type: "error";
  protocol: number;
  message: string;
};

export type ServerMessage =
  | SnapshotMessage
  | DeltaMessage
  | HeartbeatMessage
  | ResetMessage
  | ErrorMessage
  | { type: "pong"; protocol: number };
