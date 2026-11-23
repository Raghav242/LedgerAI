import type { ExperimentId } from "@/types/api";
import { http } from "./client";

/**
 * The LOCAL experiment controller (python -m labctl), reached through the Vite dev proxy at
 * /controller. It is a separate process on 127.0.0.1; the API itself can never stop a
 * service. When it is not running, every call fails with a network/5xx error.
 */
export interface ControllerStatus {
  enabled: boolean;
  failures_enabled: boolean;
  benchmarks_enabled: boolean;
  benchmark_running: boolean;
  busy: boolean;
  active_run_id: string | null;
  experiments: string[];
  interrupted_recovery: { run_id: string; restored: string[]; ok: boolean; error?: string } | null;
}

export const controllerApi = {
  status: async (signal?: AbortSignal) => (await http.get<ControllerStatus>("/controller/status", { signal, timeoutMs: 4_000 })).data,
  start: async (experiment: ExperimentId, params: Record<string, number>) =>
    (await http.post<{ id: string; experiment: ExperimentId; status: string }>("/controller/runs", { experiment, params, confirm: true })).data,
};
