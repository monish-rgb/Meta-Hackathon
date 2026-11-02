// Typed fetch wrappers for the FlowGate backend API (see CONTRACT.md).

const API_BASE: string = import.meta.env.VITE_API_URL ?? 'http://localhost:8000';

export const apiBase = API_BASE;

// The admin key is never bundled into the frontend. The operator enters
// it at runtime; it lives only in sessionStorage for this browser tab.
const ADMIN_KEY_STORAGE = 'flowgate-admin-key';

export function getAdminKey(): string {
  try {
    return sessionStorage.getItem(ADMIN_KEY_STORAGE) ?? '';
  } catch {
    return '';
  }
}

export function setAdminKey(key: string): void {
  try {
    if (key) {
      sessionStorage.setItem(ADMIN_KEY_STORAGE, key);
    } else {
      sessionStorage.removeItem(ADMIN_KEY_STORAGE);
    }
  } catch {
    // sessionStorage unavailable (private mode etc.) — key stays unset.
  }
}

export interface Run {
  run_id: string;
  project: string;
  tenant?: string;
  clearance: number;
  suspended: boolean;
  created_at: string;
  effective_level: number;
  effective_compartments: string[];
}

export interface Resource {
  resource_id: string;
  kind: string;
  project: string;
  tenant?: string;
  sensitivity: number;
  compartments?: string[];
  version?: number;
  created_by?: string | null;
}

export interface EventItem {
  event_id: number;
  ts: string;
  run_id: string | null;
  tool: string | null;
  operation: string | null;
  resource_id: string | null;
  decision: string;
  action: string | null;
  policy_id: string | null;
  reason: string | null;
  similarity: number | null;
}

export interface GraphNode {
  id: string;
  label: string;
  type: string;
  project?: string;
  clearance?: number;
  suspended?: boolean;
  sensitivity?: number;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  operation: string;
  decision: string;
  policy_id: string;
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface DemoStep {
  step: number;
  actor: string;
  action: string;
  expected: string;
  actual: string;
  ok: boolean;
}

export interface DemoResult {
  steps: DemoStep[];
  alpha_run_id: string;
  beta_run_id: string;
}

type ReqInit = RequestInit & { admin?: boolean };

async function req<T>(path: string, init?: ReqInit): Promise<T> {
  const { admin, headers, ...rest } = init ?? {};
  const adminKey = getAdminKey();
  const res = await fetch(`${API_BASE}${path}`, {
    ...rest,
    headers: {
      'Content-Type': 'application/json',
      // All dashboard reads/writes are admin-gated at the backend; send
      // the runtime-entered key whenever we have one.
      ...(adminKey ? { 'X-Admin-Key': adminKey } : {}),
      ...(headers ?? {}),
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${rest.method ?? 'GET'} ${path} failed (${res.status}): ${text}`);
  }
  return (await res.json()) as T;
}

export const listRuns = () => req<Run[]>('/runs');

export const suspendRun = (runId: string) =>
  req<Run>(`/runs/${encodeURIComponent(runId)}/suspend`, { method: 'POST', admin: true });

export const reactivateRun = (runId: string) =>
  req<Run>(`/runs/${encodeURIComponent(runId)}/reactivate`, { method: 'POST', admin: true });

export const listResources = () => req<Resource[]>('/resources');

export function listEvents(params?: {
  run_id?: string;
  decision?: string;
  limit?: number;
}): Promise<EventItem[]> {
  const q = new URLSearchParams();
  if (params?.run_id) q.set('run_id', params.run_id);
  if (params?.decision) q.set('decision', params.decision);
  if (params?.limit !== undefined) q.set('limit', String(params.limit));
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return req<EventItem[]>(`/events${suffix}`);
}

export interface AlertItem extends EventItem {
  run_project?: string | null;
  run_tenant?: string | null;
  run_suspended?: boolean | null;
}

export const listAlerts = (limit = 50) =>
  req<{ count: number; alerts: AlertItem[] }>(`/alerts?limit=${limit}`);

export interface ToolReceipt {
  receipt_id?: number;
  ts?: string;
  run_id?: string | null;
  tool?: string | null;
  operation?: string | null;
  resource_id?: string | null;
  version?: number | null;
}

export interface ToolReceiptsResult {
  count: number;
  receipts: ToolReceipt[];
}

export function listToolReceipts(params?: {
  run_id?: string;
  resource_id?: string;
}): Promise<ToolReceiptsResult> {
  const q = new URLSearchParams();
  if (params?.run_id) q.set('run_id', params.run_id);
  if (params?.resource_id) q.set('resource_id', params.resource_id);
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return req<ToolReceiptsResult>(`/tool-receipts${suffix}`);
}

export type ChallengeWriter = 'fresh' | 'tainted';

export interface ChallengeResult {
  decision: string;
  policy_id: string;
  reason: string;
  similarity: number | null;
  suspended: boolean;
  writer: ChallengeWriter;
  layer: string;
  run_id: string;
  event_id: number | null;
}

export const runChallenge = (body: { content: string; writer: ChallengeWriter }) =>
  req<ChallengeResult>('/demo/challenge', {
    method: 'POST',
    admin: true,
    body: JSON.stringify(body),
  });

export const getGraph = () => req<Graph>('/graph');

export const resetDemo = () =>
  req<{ status: string }>('/demo/reset', { method: 'POST', admin: true });

export const runDemo = () => req<DemoResult>('/demo/run', { method: 'POST', admin: true });
