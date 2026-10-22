<script lang="ts">
  import type { AnalogChannel, ChannelInput, Group, Power, RadioId } from "../lib/api";
  import { mhz, parseMhz } from "../lib/format";

  /** A new channel (or a replacement) for either radio. */
  let {
    radio,
    initial,
    scanLists,
    submitLabel,
    onsubmit,
    oncancel,
    idPrefix,
  }: {
    radio: RadioId;
    initial?: Partial<AnalogChannel>;
    scanLists: Group[];
    submitLabel: string;
    /** Return true when the channel was saved, to reset the form. `at` is the RT-950 PRO slot, if given. */
    onsubmit: (ch: ChannelInput, at: number | null) => Promise<boolean>;
    oncancel?: () => void;
    idPrefix: string;
  } = $props();

  const NAME_MAX: Record<RadioId, number> = { dm32: 16, rt950: 12 };
  const SLOTS = 960;

  const start = () => ({
    slot: "",
    name: initial?.name ?? "",
    rx: mhz(initial?.rx_mhz ?? null),
    tx: mhz(initial?.tx_mhz ?? null),
    forbid: initial?.forbid_tx ?? true,
    power: (initial?.power ?? "low") as Power,
    bw: String(initial?.bandwidth_khz ?? 25),
    rxTone: initial?.rx_tone ?? "",
    txTone: initial?.tx_tone ?? "",
    scan: initial?.scan_list ?? 0,
    scanAdd: true,
    busyLock: false,
    am: false,
  });

  let f = $state(start());
  let problem = $state("");
  let saving = $state(false);
  let rt950 = $derived(radio === "rt950");
  let noTx = $derived(f.tx.trim() === "");
  let nameMax = $derived(NAME_MAX[radio]);
  let nameEl = $state<HTMLInputElement | null>(null);

  function tone(s: string): string | null | "invalid" {
    const t = s.trim();
    if (!t) return null;
    return Number.isFinite(Number(t)) ? t : "invalid";
  }

  function check(): string {
    const rx = parseMhz(f.rx);
    if (!rt950 && !f.name.trim()) return "Give the channel a name.";
    if (f.name && !new RegExp(`^[\\x20-\\x7e]{1,${nameMax}}$`).test(f.name)) {
      return `Names are up to ${nameMax} plain ASCII characters.`;
    }
    if (rx === null || rx === "invalid") return "RX must be a frequency in MHz, for example 146.520.";
    if (parseMhz(f.tx) === "invalid") return "TX must be a frequency in MHz, or empty for receive only.";
    if (tone(f.rxTone) === "invalid" || tone(f.txTone) === "invalid") {
      return "Tones are CTCSS in Hz, for example 100.0, or empty.";
    }
    if (rt950 && f.slot.trim()) {
      const n = Number(f.slot);
      if (!Number.isInteger(n) || n < 1 || n > SLOTS) return `Slot must be a number from 1 to ${SLOTS}, or empty.`;
    }
    return "";
  }

  async function submit(e: SubmitEvent) {
    e.preventDefault();
    problem = check();
    if (problem) return;
    const rx = parseMhz(f.rx) as number;
    const tx = parseMhz(f.tx) as number | null;
    const common = {
      name: f.name,
      rx_mhz: rx,
      tx_mhz: tx,
      power: f.power,
      bandwidth_khz: Number(f.bw),
      rx_tone: tone(f.rxTone) as string | null,
      tx_tone: tone(f.txTone) as string | null,
    };
    const ch: ChannelInput = rt950
      ? { ...common, scan_add: f.scanAdd, busy_lock: f.busyLock, rx_am: f.am }
      : { ...common, forbid_tx: tx === null ? true : f.forbid, scan_list: Number(f.scan) };
    const at = rt950 && f.slot.trim() ? Number(f.slot) : null;
    saving = true;
    const ok = await onsubmit(ch, at);
    saving = false;
    if (ok && !initial) {
      // Keep the settings for the next channel; clear what is unique to this one.
      f.slot = "";
      f.name = "";
      f.rx = "";
      f.tx = "";
      nameEl?.focus();
    }
  }
</script>

<form class="form" onsubmit={submit} novalidate>
  {#if rt950}
    <label class="field slot">
      <span>Slot</span>
      <input type="text" bind:value={f.slot} inputmode="numeric" placeholder="next free" autocomplete="off" />
    </label>
  {/if}
  <label class="field name">
    <span>Name{rt950 ? " (optional)" : ""}</span>
    <input type="text" bind:this={nameEl} bind:value={f.name} maxlength={nameMax} required={!rt950} autocomplete="off" />
  </label>
  <label class="field freq">
    <span>RX MHz</span>
    <input type="text" bind:value={f.rx} inputmode="decimal" placeholder="146.520" required autocomplete="off" />
  </label>
  <label class="field freq">
    <span>TX MHz</span>
    <input type="text" bind:value={f.tx} inputmode="decimal" placeholder="none" autocomplete="off" />
  </label>
  {#if !rt950}
    <label class="check forbid" for="{idPrefix}-forbid" title={noTx ? "No TX frequency: always receive only" : ""}>
      <input
        id="{idPrefix}-forbid"
        type="checkbox"
        checked={noTx || f.forbid}
        disabled={noTx}
        onchange={(e) => (f.forbid = e.currentTarget.checked)}
      />
      Forbid TX
    </label>
  {/if}
  <label class="field">
    <span>Power</span>
    <select bind:value={f.power}>
      <option value="low">Low</option>
      <option value="middle">Mid</option>
      <option value="high">High</option>
    </select>
  </label>
  <label class="field">
    <span>Bandwidth</span>
    <select bind:value={f.bw}>
      <option value="25">25 kHz</option>
      <option value="12.5">12.5 kHz</option>
    </select>
  </label>
  <label class="field tone">
    <span>RX tone Hz</span>
    <input type="text" bind:value={f.rxTone} inputmode="decimal" placeholder="none" autocomplete="off" />
  </label>
  <label class="field tone">
    <span>TX tone Hz</span>
    <input type="text" bind:value={f.txTone} inputmode="decimal" placeholder="none" autocomplete="off" />
  </label>
  {#if rt950}
    <label class="check flag"><input type="checkbox" bind:checked={f.scanAdd} /> Scan</label>
    <label class="check flag"><input type="checkbox" bind:checked={f.busyLock} /> Busy lock</label>
    <label class="check flag" title="AM receive, for airband (118-137 MHz)"><input type="checkbox" bind:checked={f.am} /> AM</label>
  {:else}
    <label class="field">
      <span>Scan list</span>
      <select bind:value={f.scan}>
        <option value={0}>None</option>
        {#each scanLists as s (s.number)}
          <option value={s.number}>{s.number} {s.name}</option>
        {/each}
      </select>
    </label>
  {/if}
  <div class="actions">
    <button type="submit" class="primary" disabled={saving}>{saving ? "Saving..." : submitLabel}</button>
    {#if oncancel}<button type="button" onclick={oncancel}>Cancel</button>{/if}
  </div>
  {#if problem}<div class="msg error problem">{problem}</div>{/if}
  <p class="muted note">
    {#if noTx}No TX frequency, so this channel is receive only.{/if}
    {#if rt950}Leave Slot empty to put the channel after the last one.{/if}
  </p>
</form>

<style>
  .form {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 8px 10px;
  }
  .slot input {
    width: 72px;
    text-align: right;
  }
  .name input {
    width: 150px;
  }
  .freq input {
    width: 90px;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .tone input {
    width: 72px;
  }
  .forbid,
  .flag {
    height: 26px;
  }
  .actions {
    display: flex;
    gap: 6px;
  }
  .problem,
  .note {
    flex-basis: 100%;
    margin: 0;
  }
  .note:empty {
    display: none;
  }
</style>
