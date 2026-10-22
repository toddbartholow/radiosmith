import type { AnyChannel, Channel, Changed, Group, Operation, RadioId, RT950Channel } from "./api";

/** MHz with at least three decimals and at most five (the radio uses a 10 Hz step). */
export function mhz(v: number | null | undefined): string {
  if (v === null || v === undefined) return "";
  let s = v.toFixed(5);
  while (s.endsWith("0") && s.length - s.indexOf(".") > 4) s = s.slice(0, -1);
  return s;
}

/** Local date and time (to the minute) for an ISO UTC timestamp. */
export function when(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function timeOfDay(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString();
}

/** Receive only: TX forbidden (DM-32UV), RX-only flag (RT-950 PRO), or no TX frequency stored. */
export const isRxOnly = (c: { forbid_tx?: boolean; rx_only?: boolean; tx_mhz: number | null }) =>
  !!c.forbid_tx || !!c.rx_only || c.tx_mhz === null;

export const lastSegment = (path: string) => path.split("/").pop() ?? path;

export const powerLabel: Record<string, string> = { low: "Low", middle: "Mid", high: "High" };

export function modeLabel(c: Channel): string {
  if (c.mode === "analog") return "Analog";
  if (c.mode === "digital") {
    const extra = [c.color_code !== null ? `CC${c.color_code}` : "", c.time_slot !== null ? `TS${c.time_slot}` : ""];
    return ["Digital", ...extra].filter(Boolean).join(" ");
  }
  return c.mode.charAt(0).toUpperCase() + c.mode.slice(1);
}

export function toneLabel(t: string | null): string {
  return t ?? "";
}

/** Parse a frequency typed by the user. Empty gives null; anything else must be a number. */
export function parseMhz(text: string): number | null | "invalid" {
  const s = text.trim();
  if (!s) return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : "invalid";
}

type FieldKey = keyof Channel | keyof RT950Channel;

export const CHANNEL_FIELDS: { key: keyof Channel; label: string }[] = [
  { key: "name", label: "Name" },
  { key: "mode", label: "Mode" },
  { key: "rx_mhz", label: "RX" },
  { key: "tx_mhz", label: "TX" },
  { key: "forbid_tx", label: "Forbid TX" },
  { key: "power", label: "Power" },
  { key: "bandwidth_khz", label: "Bandwidth" },
  { key: "rx_tone", label: "RX tone" },
  { key: "tx_tone", label: "TX tone" },
  { key: "scan_list", label: "Scan list" },
  { key: "color_code", label: "Color code" },
  { key: "time_slot", label: "Time slot" },
];

export const RT950_FIELDS: { key: keyof RT950Channel; label: string }[] = [
  { key: "name", label: "Name" },
  { key: "rx_mhz", label: "RX" },
  { key: "tx_mhz", label: "TX" },
  { key: "power", label: "Power" },
  { key: "bandwidth_khz", label: "Bandwidth" },
  { key: "rx_tone", label: "RX tone" },
  { key: "tx_tone", label: "TX tone" },
  { key: "scan_add", label: "Scan" },
  { key: "busy_lock", label: "Busy lock" },
  { key: "ptt_id", label: "PTT ID" },
  { key: "scramble", label: "Scramble" },
  { key: "fhss_learn", label: "FHSS learn" },
  { key: "rx_am", label: "AM receive" },
];

export const fieldsFor = (radio: RadioId) => (radio === "rt950" ? RT950_FIELDS : CHANNEL_FIELDS) as { key: FieldKey; label: string }[];

export function fieldValue(key: FieldKey, c: AnyChannel): string {
  const v = (c as unknown as Record<string, unknown>)[key];
  if (key === "rx_mhz" || key === "tx_mhz") return v === null ? "none (receive only)" : `${mhz(v as number)} MHz`;
  if (key === "bandwidth_khz") return `${v} kHz`;
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (key === "scan_list") return v === 0 ? "none" : String(v);
  if (key === "name" && v === "") return "(no name)";
  if (v === null || v === undefined || v === "") return "none";
  return String(v);
}

/** A channel name for display; RT-950 PRO channels may have none. */
export const channelLabel = (c: { number: number; name: string }) => c.name || `Channel ${c.number}`;

/** A one-line summary of a channel, for review and verify lists. */
export function channelSummary(c: AnyChannel): string {
  const parts: string[] = [];
  if ("mode" in c) parts.push(modeLabel(c));
  parts.push(`RX ${mhz(c.rx_mhz)}`);
  if (c.tx_mhz !== null && c.tx_mhz !== c.rx_mhz) parts.push(`TX ${mhz(c.tx_mhz)}`);
  parts.push(powerLabel[c.power] ?? c.power, `${c.bandwidth_khz} kHz`);
  if (c.rx_tone) parts.push(`RX tone ${c.rx_tone}`);
  if (c.tx_tone) parts.push(`TX tone ${c.tx_tone}`);
  if ("scan_list" in c && c.scan_list) parts.push(`scan list ${c.scan_list}`);
  if ("scan_add" in c) parts.push(c.scan_add ? "scan" : "no scan");
  if ("busy_lock" in c && c.busy_lock) parts.push("busy lock");
  return parts.join(", ");
}

export interface FieldChange {
  label: string;
  before: string;
  after: string;
}

export function channelFieldChanges(ch: Changed<AnyChannel>, radio: RadioId = "dm32"): FieldChange[] {
  const out: FieldChange[] = [];
  const get = (c: AnyChannel, k: FieldKey) => (c as unknown as Record<string, unknown>)[k];
  for (const { key, label } of fieldsFor(radio)) {
    if (get(ch.before, key) !== get(ch.after, key)) {
      out.push({ label, before: fieldValue(key, ch.before), after: fieldValue(key, ch.after) });
    }
  }
  return out;
}

export interface MemberDiff {
  added: string[];
  removed: string[];
  reordered: boolean;
}

/** Compare member lists by label (channel name), so renumbering alone is not a change. */
export function memberDiff(before: string[], after: string[]): MemberDiff {
  const b = new Set(before);
  const a = new Set(after);
  const added = after.filter((n) => !b.has(n));
  const removed = before.filter((n) => !a.has(n));
  const same = before.length === after.length && before.every((x, i) => x === after[i]);
  return { added, removed, reordered: !added.length && !removed.length && !same };
}

/** Items compared by number: what was added, removed and changed from `before` to `after`. */
export function diffByNumber<T extends { number: number }>(before: T[], after: T[]) {
  const old = new Map(before.map((x) => [x.number, x]));
  const now = new Map(after.map((x) => [x.number, x]));
  const same = (x: T, y: T) => JSON.stringify(x) === JSON.stringify(y);
  return {
    added: after.filter((x) => !old.has(x.number)),
    removed: before.filter((x) => !now.has(x.number)),
    changed: after
      .filter((x) => old.has(x.number) && !same(old.get(x.number)!, x))
      .map((x) => ({ number: x.number, before: old.get(x.number)!, after: x })),
  };
}

export interface MatchedDiff<T> {
  added: T[];
  removed: T[];
  /** Content changed (and maybe moved). */
  changed: Changed<T>[];
  /** Same content at a new number, e.g. after an earlier channel was deleted. */
  moved: Changed<T>[];
  /** Every before/after pair, including unchanged ones. */
  pairs: [T, T][];
}

/**
 * Pair items before and after an edit by what they are rather than by slot
 * number, so deleting channel 3 shows as one removal and a renumbering instead
 * of every later slot changing. Pairs are found by name, then by the `rules`
 * in order; anything left unpaired is added or removed. Every item before and
 * after appears in exactly one result list (or is unchanged).
 */
export function matchDiff<T extends { number: number; name: string }>(
  before: T[],
  after: T[],
  same: (b: T, a: T) => boolean,
  rules: ((b: T, a: T) => boolean)[] = [],
): MatchedDiff<T> {
  const freeB = new Set(before);
  const freeA = new Set(after);
  const pairs: [T, T][] = [];
  const pairBy = (rule: (b: T, a: T) => boolean) => {
    for (const a of after) {
      if (!freeA.has(a)) continue;
      const b = before.find((x) => freeB.has(x) && rule(x, a));
      if (b) {
        freeB.delete(b);
        freeA.delete(a);
        pairs.push([b, a]);
      }
    }
  };
  pairBy((b, a) => b.name === a.name);
  for (const r of rules) pairBy(r);
  pairBy((b, a) => b.number === a.number);
  const out: MatchedDiff<T> = {
    added: after.filter((a) => freeA.has(a)),
    removed: before.filter((b) => freeB.has(b)),
    changed: [],
    moved: [],
    pairs,
  };
  for (const [b, a] of pairs.sort((x, y) => x[1].number - y[1].number)) {
    if (!same(b, a)) out.changed.push({ number: a.number, before: b, after: a });
    else if (b.number !== a.number) out.moved.push({ number: a.number, before: b, after: a });
  }
  return out;
}

export function channelsDiff(before: Channel[], after: Channel[]): MatchedDiff<Channel> {
  const same = (b: Channel, a: Channel) => CHANNEL_FIELDS.every(({ key }) => b[key] === a[key]);
  return matchDiff(before, after, same, [
    (b, a) => b.number === a.number && b.rx_mhz === a.rx_mhz,
    (b, a) => b.rx_mhz === a.rx_mhz && b.mode === a.mode,
  ]);
}

/** Zones or scan lists, paired by name, then number. Member renumbering counts as a change. */
export function groupsDiff(before: Group[], after: Group[]): MatchedDiff<Group> {
  const same = (b: Group, a: Group) =>
    b.name === a.name && b.members.length === a.members.length && b.members.every((m, i) => m === a.members[i]);
  return matchDiff(before, after, same);
}

export function groupName(groups: Group[], n: number): string {
  return groups.find((g) => g.number === n)?.name ?? `#${n}`;
}

function refs(xs: (number | string)[]): string {
  const shown = xs.slice(0, 8).map((x) => (typeof x === "number" ? String(x) : `"${x}"`));
  return shown.join(", ") + (xs.length > 8 ? ` and ${xs.length - 8} more` : "");
}

function changeText(changes: Record<string, unknown>): string {
  return Object.entries(changes)
    .map(([k, v]) => {
      const label = k.replace(/_mhz$/, "").replace(/_khz$/, "").replace(/_/g, " ");
      const value = v === null ? "none" : v === "" ? "(blank)" : typeof v === "boolean" ? (v ? "on" : "off") : String(v);
      return `${label} ${value}`;
    })
    .join(", ");
}

/** One line describing an edit operation. */
export function describeOp(op: Operation, radio: RadioId = "dm32"): string {
  switch (op.op) {
    case "add_channels": {
      const names = op.channels.map((c) => c.name || "(no name)");
      const where = op.at ? ` at slot ${op.at}` : "";
      return `Add ${names.length} channel${names.length === 1 ? "" : "s"}${where}: ${refs(names)}`;
    }
    case "update_channel":
      return `Channel ${op.channel}: ${changeText(op.changes)}`;
    case "replace_channel":
      return radio === "rt950"
        ? `Replace channel ${op.channel} with "${op.with.name ?? ""}"`
        : `Replace channel ${op.channel} with analog "${op.with.name ?? "?"}"`;
    case "delete_channels":
      return radio === "rt950"
        ? `Empty slot${op.channels.length === 1 ? "" : "s"} ${refs(op.channels)}`
        : `Delete channel${op.channels.length === 1 ? "" : "s"} ${refs(op.channels)}`;
    case "add_zone":
      return `Add zone "${op.name}" with ${op.members.length} channel${op.members.length === 1 ? "" : "s"}`;
    case "update_zone":
      return `Zone ${op.zone}: ${[
        op.name !== undefined ? `name "${op.name}"` : "",
        op.members ? `${op.members.length} members` : "",
      ]
        .filter(Boolean)
        .join(", ")}`;
    case "delete_zone":
      return `Delete zone ${op.zone}`;
    case "update_scan_list":
      return `Scan list ${op.scan_list}: ${[
        op.name !== undefined ? `name "${op.name}"` : "",
        op.members ? `${op.members.length} members` : "",
      ]
        .filter(Boolean)
        .join(", ")}`;
    default:
      return JSON.stringify(op);
  }
}

/** Progress from a "12/233 pages" line, when that is the latest line of a job log. */
export function progressOf(log: string[]): { done: number; total: number } | null {
  const m = /^\s*(\d+)\s*\/\s*(\d+)\s+pages\s*$/.exec(log[log.length - 1] ?? "");
  return m ? { done: Number(m[1]), total: Number(m[2]) } : null;
}
