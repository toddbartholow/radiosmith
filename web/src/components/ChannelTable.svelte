<script lang="ts">
  import type {
    AnalogChannel,
    AnyChannel,
    Channel,
    ChannelChanges,
    ChannelInput,
    DraftCodeplug,
    Operation,
    RadioId,
    RT950Channel,
  } from "../lib/api";
  import { channelLabel, isRxOnly, mhz, modeLabel, parseMhz } from "../lib/format";
  import CellInput from "./CellInput.svelte";
  import ChannelForm from "./ChannelForm.svelte";

  let {
    codeplug,
    radio,
    run,
  }: { codeplug: DraftCodeplug; radio: RadioId; run: (ops: Operation[]) => Promise<boolean> } = $props();

  const NAME_MAX: Record<RadioId, number> = { dm32: 16, rt950: 12 };

  let filter = $state("");
  let selected = $state<number[]>([]);
  let showAdd = $state(false);
  let replacing = $state<Channel | null>(null);
  let localError = $state("");
  let dialog = $state<HTMLDialogElement | null>(null);

  let rt950 = $derived(radio === "rt950");
  let channels = $derived(codeplug.channels);
  let shown = $derived.by(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return channels;
    return channels.filter(
      (c) => c.name.toLowerCase().includes(q) || mhz(c.rx_mhz).includes(q) || String(c.number) === q,
    );
  });
  let allShownSelected = $derived(shown.length > 0 && shown.every((c) => selected.includes(c.number)));
  let rxOnlyCount = $derived(channels.filter(isRxOnly).length);
  const columns = 13;

  const dm = (c: AnyChannel) => c as Channel;
  const rt = (c: AnyChannel) => c as RT950Channel;
  const field = (c: AnyChannel, key: string) => (c as unknown as Record<string, unknown>)[key];
  const isDcs = (tone: string | null) => !!tone && !/^\d+(\.\d+)?$/.test(tone);

  // Drop selections that no longer exist after a reload.
  $effect(() => {
    const numbers = new Set(channels.map((c) => c.number));
    if (selected.some((n) => !numbers.has(n))) selected = selected.filter((n) => numbers.has(n));
  });

  $effect(() => {
    if (replacing && dialog && !dialog.open) dialog.showModal();
  });

  const update = (c: AnyChannel, changes: ChannelChanges) =>
    run([{ op: "update_channel", channel: c.number, changes }]);

  function toggle(n: number, on: boolean) {
    selected = on ? [...selected, n] : selected.filter((x) => x !== n);
  }

  function toggleAll(on: boolean) {
    const nums = shown.map((c) => c.number);
    selected = on ? [...new Set([...selected, ...nums])] : selected.filter((n) => !nums.includes(n));
  }

  function commitName(c: AnyChannel) {
    return async (text: string) => {
      localError = "";
      // The DM-32UV needs a name; RT-950 PRO channels may be unnamed.
      if (!rt950 && !text.trim()) {
        localError = `Channel ${c.number}: a name is required.`;
        return;
      }
      await update(c, { name: text });
    };
  }

  function commitFreq(c: AnyChannel, key: "rx_mhz" | "tx_mhz") {
    return async (text: string) => {
      localError = "";
      const v = parseMhz(text);
      if (v === "invalid") {
        localError = `Channel ${c.number}: "${text}" is not a frequency in MHz.`;
        return;
      }
      if (key === "rx_mhz" && v === null) {
        localError = `Channel ${c.number}: RX cannot be empty.`;
        return;
      }
      const cur = c[key];
      if (v === cur || (v !== null && cur !== null && Math.abs(v - cur) < 5e-6)) return;
      const changes: ChannelChanges = { [key]: v };
      // On the DM-32UV a channel without a TX frequency must also forbid TX.
      if (!rt950 && key === "tx_mhz" && v === null && !dm(c).forbid_tx) changes.forbid_tx = true;
      await update(c, changes);
    };
  }

  function commitTone(c: AnyChannel, key: "rx_tone" | "tx_tone") {
    return async (text: string) => {
      localError = "";
      const v = text.trim() || null;
      if (v === c[key]) return;
      await update(c, { [key]: v });
    };
  }

  async function onSelect(
    e: Event & { currentTarget: HTMLSelectElement },
    c: AnyChannel,
    key: "power" | "bandwidth_khz" | "scan_list",
  ) {
    const el = e.currentTarget;
    const raw = el.value;
    const value = key === "power" ? raw : Number(raw);
    const ok = await update(c, { [key]: value } as ChannelChanges);
    if (!ok) el.value = String(field(c, key));
  }

  async function onCheck(
    e: Event & { currentTarget: HTMLInputElement },
    c: AnyChannel,
    key: "forbid_tx" | "scan_add" | "busy_lock" | "rx_am",
  ) {
    const el = e.currentTarget;
    const ok = await update(c, { [key]: el.checked });
    if (!ok) el.checked = !!field(c, key);
  }

  async function deleteSelected() {
    const nums = [...selected].sort((a, b) => a - b);
    if (await run([{ op: "delete_channels", channels: nums }])) selected = [];
  }

  async function add(ch: ChannelInput, at: number | null) {
    return run([{ op: "add_channels", channels: [ch], ...(at ? { at } : {}) }]);
  }

  async function replace(ch: ChannelInput) {
    if (!replacing) return false;
    const ok = await run([{ op: "replace_channel", channel: replacing.number, with: ch }]);
    if (ok) closeDialog();
    return ok;
  }

  function closeDialog() {
    dialog?.close();
    replacing = null;
  }

  function analogDefaults(c: Channel): Partial<AnalogChannel> {
    return {
      name: c.name,
      rx_mhz: c.rx_mhz ?? undefined,
      tx_mhz: c.tx_mhz,
      forbid_tx: c.forbid_tx,
      power: c.power,
      bandwidth_khz: 12.5,
      scan_list: c.scan_list,
    };
  }

  function rtExtras(c: RT950Channel): string {
    const out: string[] = [];
    if (c.ptt_id && c.ptt_id !== "off") out.push(`PTT ID ${c.ptt_id}`);
    if (c.scramble) out.push(`scramble ${c.scramble}`);
    if (c.fhss_learn) out.push("FHSS learn");
    return out.join(", ");
  }
</script>

<section class="panel">
  <div class="panel-head toolbar">
    <h3>Channels</h3>
    <span class="muted">{channels.length} channels, {rxOnlyCount} receive only</span>
    <input type="search" placeholder="Filter by name or MHz" aria-label="Filter channels" bind:value={filter} />
    <button class="danger" onclick={deleteSelected} disabled={!selected.length}>
      Delete selected{selected.length ? ` (${selected.length})` : ""}
    </button>
    <button class:primary={!showAdd} onclick={() => (showAdd = !showAdd)} aria-expanded={showAdd}>
      {showAdd ? "Close add channel" : "Add channel"}
    </button>
  </div>

  {#if showAdd}
    <div class="add">
      <h4>{rt950 ? "New channel" : "New analog channel"}</h4>
      <ChannelForm {radio} scanLists={codeplug.scan_lists} submitLabel="Add channel" onsubmit={add} idPrefix="add" />
    </div>
  {/if}

  {#if localError}
    <div class="panel-body"><div class="msg error">{localError}</div></div>
  {/if}

  <div class="table-wrap flush">
    <table class="grid channels">
      <thead>
        <tr>
          <th class="c">
            <input
              type="checkbox"
              aria-label="Select all shown channels"
              checked={allShownSelected}
              onchange={(e) => toggleAll(e.currentTarget.checked)}
            />
          </th>
          <th class="num" title={rt950 ? "Slot number; slots never move" : ""}>{rt950 ? "Slot" : "#"}</th>
          <th>Name</th>
          {#if !rt950}<th>Mode</th>{/if}
          <th class="num">RX MHz</th>
          <th class:num={!rt950}>TX MHz</th>
          {#if !rt950}<th>Forbid TX</th>{/if}
          <th>Power</th>
          <th>BW</th>
          <th>RX tone</th>
          <th>TX tone</th>
          {#if rt950}
            <th class="c">Scan</th>
            <th class="c">Busy lock</th>
            <th class="c" title="AM receive, for airband">AM</th>
            <th>Other</th>
          {:else}
            <th>Scan list</th>
            <th></th>
          {/if}
        </tr>
      </thead>
      <tbody>
        {#each shown as c (c.number)}
          {@const toneEditable = rt950 || dm(c).mode === "analog"}
          {@const rxOnly = isRxOnly(c)}
          <tr class:selected={selected.includes(c.number)}>
            <td class="c">
              <input
                type="checkbox"
                aria-label="Select channel {c.number}"
                checked={selected.includes(c.number)}
                onchange={(e) => toggle(c.number, e.currentTarget.checked)}
              />
            </td>
            <td class="num muted">{c.number}</td>
            <td>
              <CellInput
                value={c.name}
                label="Channel {c.number} name"
                placeholder={rt950 ? "no name" : ""}
                maxlength={NAME_MAX[radio]}
                width="{NAME_MAX[radio]}ch"
                oncommit={commitName(c)}
              />
            </td>
            {#if !rt950}
              <td class="mode">
                {modeLabel(dm(c))}
                {#if rxOnly}<span class="badge rx" title={c.tx_mhz === null ? "No TX frequency stored" : "TX is forbidden"}>RX only</span>{/if}
              </td>
            {/if}
            <td class="num">
              <CellInput value={mhz(c.rx_mhz)} label="Channel {c.number} RX MHz" numeric width="10ch" oncommit={commitFreq(c, "rx_mhz")} />
            </td>
            <td class="tx" class:num={!rt950}>
              <CellInput
                value={mhz(c.tx_mhz)}
                label="Channel {c.number} TX MHz"
                placeholder="none"
                numeric
                width="10ch"
                oncommit={commitFreq(c, "tx_mhz")}
              />
              {#if rt950 && rxOnly}<span class="badge rx" title="No TX frequency: receive only">RX only</span>{/if}
            </td>
            {#if !rt950}
              <td class="c">
                <input
                  type="checkbox"
                  aria-label="Channel {c.number} forbid TX"
                  title={c.tx_mhz === null ? "No TX frequency: set one before allowing TX" : ""}
                  checked={dm(c).forbid_tx}
                  disabled={c.tx_mhz === null && dm(c).forbid_tx}
                  onchange={(e) => onCheck(e, c, "forbid_tx")}
                />
              </td>
            {/if}
            <td>
              <select class="cell" aria-label="Channel {c.number} power" value={c.power} onchange={(e) => onSelect(e, c, "power")}>
                <option value="low">Low</option>
                <option value="middle">Mid</option>
                <option value="high">High</option>
              </select>
            </td>
            <td>
              {#if toneEditable}
                <select
                  class="cell"
                  aria-label="Channel {c.number} bandwidth"
                  value={String(c.bandwidth_khz)}
                  onchange={(e) => onSelect(e, c, "bandwidth_khz")}
                >
                  <option value="25">25</option>
                  <option value="12.5">12.5</option>
                </select>
              {:else}
                <span class="fixed">{c.bandwidth_khz}</span>
              {/if}
            </td>
            {#each ["rx_tone", "tx_tone"] as const as key (key)}
              <td>
                {#if !toneEditable}
                  <span class="fixed muted" title="Tones apply to analog channels only">n/a</span>
                {:else if isDcs(c[key])}
                  <span class="fixed" title="DCS tones are shown but cannot be changed here yet">{c[key]}</span>
                {:else}
                  <CellInput
                    value={c[key] ?? ""}
                    label="Channel {c.number} {key === 'rx_tone' ? 'RX' : 'TX'} tone"
                    placeholder="none"
                    width="7ch"
                    oncommit={commitTone(c, key)}
                  />
                {/if}
              </td>
            {/each}
            {#if rt950}
              <td class="c">
                <input type="checkbox" aria-label="Channel {c.number} scan" checked={rt(c).scan_add} onchange={(e) => onCheck(e, c, "scan_add")} />
              </td>
              <td class="c">
                <input type="checkbox" aria-label="Channel {c.number} busy lock" checked={rt(c).busy_lock} onchange={(e) => onCheck(e, c, "busy_lock")} />
              </td>
              <td class="c">
                <input type="checkbox" aria-label="Channel {c.number} AM receive" checked={rt(c).rx_am} onchange={(e) => onCheck(e, c, "rx_am")} />
              </td>
              <td class="muted" title="Shown for reference; not editable here">{rtExtras(rt(c))}</td>
            {:else}
              <td>
                <select class="cell" aria-label="Channel {c.number} scan list" value={String(dm(c).scan_list)} onchange={(e) => onSelect(e, c, "scan_list")}>
                  <option value="0">None</option>
                  {#each codeplug.scan_lists as s (s.number)}
                    <option value={String(s.number)}>{s.number} {s.name}</option>
                  {/each}
                </select>
              </td>
              <td>
                {#if dm(c).mode !== "analog"}
                  <button class="small" onclick={() => (replacing = dm(c))}>Make analog</button>
                {/if}
              </td>
            {/if}
          </tr>
        {:else}
          <tr><td colspan={columns} class="muted">{filter ? "No channels match the filter." : "No channels."}</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
  <p class="muted foot">
    Edits save when you press Enter or leave a cell; Escape cancels. An empty TX frequency makes a channel receive
    only. Tones are CTCSS in Hz.
    {#if rt950}Deleting a channel empties its slot; other channels keep their numbers.{/if}
  </p>
</section>

<dialog bind:this={dialog} onclose={() => (replacing = null)} aria-labelledby="replace-title">
  {#if replacing}
    <h3 id="replace-title">Replace channel {replacing.number} with an analog channel</h3>
    <p class="muted">
      {channelLabel(replacing)} is {modeLabel(replacing)}. Replacing it builds a new analog channel with the settings
      below; its digital settings are dropped. Zones and scan lists keep pointing at channel {replacing.number}.
    </p>
    <ChannelForm
      radio="dm32"
      initial={analogDefaults(replacing)}
      scanLists={codeplug.scan_lists}
      submitLabel="Replace channel {replacing.number}"
      onsubmit={replace}
      oncancel={closeDialog}
      idPrefix="replace"
    />
  {/if}
</dialog>

<style>
  .toolbar input[type="search"] {
    width: 200px;
  }
  .add {
    padding: 10px 12px 12px;
    border-bottom: 1px solid var(--border);
    background: var(--sunken);
  }
  .add h4 {
    font-size: 12px;
    margin-bottom: 6px;
  }
  .flush {
    border: none;
    border-radius: 0;
    max-height: calc(100vh - 260px);
    min-height: 200px;
  }
  .channels th,
  .channels td {
    padding: 1px 4px;
  }
  .channels th:first-child,
  .channels td:first-child {
    padding-left: 10px;
  }
  .mode .badge,
  .tx .badge {
    margin-left: 4px;
  }
  select.cell {
    height: 22px;
    border-color: transparent;
    background: transparent;
  }
  select.cell:hover:not(:disabled) {
    border-color: var(--border-strong);
    background: var(--panel);
  }
  .fixed {
    padding: 0 4px;
  }
  .foot {
    font-size: 12px;
    margin: 0;
    padding: 6px 12px;
    border-top: 1px solid var(--border);
  }
  dialog {
    border: 1px solid var(--border-strong);
    border-radius: var(--radius);
    background: var(--panel);
    color: var(--text);
    padding: 16px;
    max-width: min(760px, calc(100vw - 32px));
    box-shadow: 0 6px 24px rgb(0 0 0 / 0.25);
  }
  dialog::backdrop {
    background: rgb(0 0 0 / 0.35);
  }
  dialog h3 {
    margin-bottom: 8px;
  }
</style>
