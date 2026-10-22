// Typed client for the radiosmith JSON API. Errors come back as HTTP 400 with
// {"error": "message"}; that message is thrown verbatim as an ApiError.

export type RadioId = "dm32" | "rt950";
export type Power = "low" | "middle" | "high";

export interface Status {
  allow_writes: boolean;
  data_dir: string;
  radios: Record<RadioId, string>;
  busy: boolean;
  jobs_running: string[];
}

export interface Port {
  device: string;
  description: string | null;
  vid: string | null;
  pid: string | null;
  manufacturer?: string | null;
  is_ch340: boolean;
  last_seen_radio: RadioId | null;
}

export type JobKind = "read" | "write" | "dry-run" | "verify";

/** One step of a write or dry-run report. */
export interface WriteStep {
  step: string;
  [key: string]: unknown;
}

export interface Job {
  id: string;
  kind: JobKind;
  radio: RadioId;
  port: string;
  status: "running" | "done" | "failed";
  started_utc: string;
  finished_utc: string | null;
  log: string[];
  result: Record<string, unknown> | null;
  error: string | null;
  /** The edit set a write, dry run or verify job acts on. */
  draft?: string | null;
}

export interface IdentifyResult {
  identity: Record<string, unknown>;
  port: Record<string, unknown>;
  link_stats: Record<string, unknown>;
  radio: RadioId;
}

export interface Channel {
  number: number;
  name: string;
  rx_mhz: number | null;
  tx_mhz: number | null;
  mode: string;
  forbid_tx: boolean;
  power: Power;
  bandwidth_khz: number;
  scan_list: number;
  rx_tone: string | null;
  tx_tone: string | null;
  color_code: number | null;
  time_slot: number | null;
}

/** A channel decoded from an RT-950 PRO backup (read only). */
export interface RT950Channel {
  number: number;
  name: string;
  rx_mhz: number | null;
  tx_mhz: number | null;
  rx_only: boolean;
  power: string;
  bandwidth_khz: number;
  scan_add: boolean;
  busy_lock: boolean;
  rx_tone: string | null;
  tx_tone: string | null;
  ptt_id: string;
  scramble: number;
  fhss_learn: boolean;
  rx_am: boolean;
}

export type AnyChannel = Channel | RT950Channel;

export interface Group {
  number: number;
  name: string;
  members: number[];
}

export interface Codeplug {
  channels: Channel[];
  zones: Group[];
  scan_lists: Group[];
  problems: string[];
}

export interface BackupInfo {
  backup: string;
  radio: RadioId;
  model: string | null;
  firmware: string | null;
  created_utc: string | null;
  complete: boolean | null;
}

/** A decoded backup. DM-32UV backups have Channel objects, RT-950 PRO backups RT950Channel ones. */
export interface BackupView {
  backup: string;
  ok: boolean;
  problems: string[];
  radio: RadioId;
  channels: AnyChannel[];
  zones: Group[];
  scan_lists: Group[];
}

export interface Profile {
  profile: string;
  name: string | null;
  description: string | null;
  channels: number;
}

export interface Identity {
  model: string | null;
  firmware: string | null;
}

export interface DraftSummary {
  id: string;
  name: string;
  /** Missing on drafts made before RT-950 PRO editing existed; those are DM-32UV drafts. */
  radio?: RadioId;
  base_backup: string;
  identity: Identity;
  operations: number;
  pages_changed: number;
  created_utc: string;
  updated_utc: string;
  /** Set instead of the other fields when a draft cannot be loaded. */
  error?: string;
}

/** An analog channel as accepted by add_channels and replace_channel. */
export interface AnalogChannel {
  name: string;
  rx_mhz: number;
  tx_mhz: number | null;
  forbid_tx: boolean;
  power: Power;
  bandwidth_khz: number;
  rx_tone: string | null;
  tx_tone: string | null;
  scan_list: number;
}

/** An RT-950 PRO channel as accepted by its add_channels and replace_channel. */
export interface Rt950ChannelInput {
  name: string;
  rx_mhz: number;
  tx_mhz: number | null;
  power: Power;
  bandwidth_khz: number;
  rx_tone: string | null;
  tx_tone: string | null;
  scan_add: boolean;
  busy_lock: boolean;
  rx_am: boolean;
}

export type ChannelInput = Partial<AnalogChannel> | Partial<Rt950ChannelInput>;
export type ChannelChanges = Partial<AnalogChannel & Rt950ChannelInput>;
type Ref = number | string;

export type Operation =
  | { op: "add_channels"; channels: ChannelInput[]; at?: number }
  | { op: "update_channel"; channel: Ref; changes: ChannelChanges }
  | { op: "replace_channel"; channel: Ref; with: ChannelInput }
  | { op: "delete_channels"; channels: Ref[] }
  | { op: "add_zone"; name: string; members: Ref[] }
  | { op: "update_zone"; zone: Ref; name?: string; members?: Ref[] }
  | { op: "delete_zone"; zone: Ref }
  | { op: "update_scan_list"; scan_list: Ref; name?: string; members?: Ref[] };

/** Decoded draft contents: DM-32UV Channel objects, or RT950Channel ones (no zones or scan lists). */
export interface DraftCodeplug {
  channels: AnyChannel[];
  zones: Group[];
  scan_lists: Group[];
  problems: string[];
}

export interface DraftView extends Omit<DraftSummary, "operations"> {
  codeplug: DraftCodeplug;
  operations: Operation[];
}

export interface EditResult {
  results: unknown[];
  operations: number;
  preview_id: string;
  pages_changed: number;
}

export interface CsvImportResult extends Partial<EditResult> {
  operations_added: number;
  note?: string;
}

export interface Changed<T> {
  number: number;
  before: T;
  after: T;
}

export interface Changes {
  channels_added: AnyChannel[];
  channels_removed: AnyChannel[];
  channels_changed: Changed<AnyChannel>[];
  zones_added: Group[];
  zones_removed: Group[];
  zones_changed: Changed<Group>[];
  scan_lists_added: Group[];
  scan_lists_removed: Group[];
  scan_lists_changed: Changed<Group>[];
}

export interface PageChange {
  address: string;
  tag: string;
  bytes_changed: number;
}

export interface Preview {
  draft: string;
  radio?: RadioId;
  base_backup: string;
  identity: Identity;
  preview_id: string;
  pages: PageChange[];
  changes: Changes;
  nothing_to_write: boolean;
}

export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError("Cannot reach the radiosmith service. Is radiosmith serve still running?", 0);
  }
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    const err = data as { error?: unknown; detail?: unknown } | null;
    let message = `HTTP ${res.status} ${res.statusText}`;
    if (err && typeof err.error === "string") message = err.error;
    else if (err && err.detail !== undefined) message = JSON.stringify(err.detail);
    throw new ApiError(message, res.status);
  }
  return data as T;
}

function query(params: Record<string, string | number | null | undefined>): string {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== null && v !== undefined && v !== "") s.set(k, String(v));
  }
  const out = s.toString();
  return out ? `?${out}` : "";
}

const backupPath = (backup: string) => backup.split("/").map(encodeURIComponent).join("/");
const draftPath = (id: string) => `/api/drafts/${encodeURIComponent(id)}`;

export const api = {
  status: () => request<Status>("GET", "/api/status"),
  ports: () => request<Port[]>("GET", "/api/ports"),
  identify: (radio: RadioId, port: string) =>
    request<IdentifyResult>("POST", `/api/radios/${radio}/identify${query({ port })}`),
  read: (radio: RadioId, port: string) => request<Job>("POST", `/api/radios/${radio}/read${query({ port })}`),
  jobs: () => request<Job[]>("GET", "/api/jobs"),
  job: (id: string, wait = 0) => request<Job>("GET", `/api/jobs/${encodeURIComponent(id)}${query({ wait })}`),
  backups: (radio?: RadioId) => request<BackupInfo[]>("GET", `/api/backups${query({ radio })}`),
  backup: (backup: string) => request<BackupView>("GET", `/api/backups/${backupPath(backup)}`),
  profiles: () => request<Profile[]>("GET", "/api/profiles"),
  drafts: () => request<DraftSummary[]>("GET", "/api/drafts"),
  createDraft: (backup?: string, name?: string, radio?: RadioId) =>
    request<DraftView>("POST", "/api/drafts", {
      ...(backup ? { backup } : {}),
      ...(name ? { name } : {}),
      ...(radio ? { radio } : {}),
    }),
  draft: (id: string) => request<DraftView>("GET", draftPath(id)),
  deleteDraft: (id: string) => request<{ deleted: string }>("DELETE", draftPath(id)),
  edit: (id: string, operations: Operation[]) =>
    request<EditResult>("POST", `${draftPath(id)}/operations`, { operations }),
  undo: (id: string, count = 1) => request<DraftSummary>("POST", `${draftPath(id)}/undo`, { count }),
  rename: (id: string, name: string) => request<DraftSummary>("POST", `${draftPath(id)}/rename`, { name }),
  applyProfile: (id: string, profile: string) =>
    request<EditResult>("POST", `${draftPath(id)}/profile`, { profile }),
  csvUrl: (id: string) => `${draftPath(id)}/channels.csv`,
  importCsv: (id: string, csv: string) => request<CsvImportResult>("POST", `${draftPath(id)}/channels.csv`, { csv }),
  preview: (id: string) => request<Preview>("GET", `${draftPath(id)}/preview`),
  write: (id: string, preview_id: string, port: string, dry_run: boolean) =>
    request<Job>("POST", `${draftPath(id)}/write`, { preview_id, ...(port ? { port } : {}), dry_run }),
  verify: (id: string, port: string) => request<Job>("POST", `${draftPath(id)}/verify`, port ? { port } : {}),
};

export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
