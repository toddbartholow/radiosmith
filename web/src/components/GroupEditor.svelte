<script lang="ts">
  import type { Channel, Group, Operation } from "../lib/api";
  import { isRxOnly, mhz } from "../lib/format";

  let {
    kind,
    groups,
    channels,
    run,
  }: {
    kind: "zone" | "scan_list";
    groups: Group[];
    channels: Channel[];
    run: (ops: Operation[]) => Promise<boolean>;
  } = $props();

  const config = {
    zone: { noun: "zone", title: "Zones", max: 64, nameMax: 16, canAdd: true, canDelete: true },
    scan_list: { noun: "scan list", title: "Scan lists", max: 16, nameMax: 11, canAdd: false, canDelete: false },
  } as const;
  let cfg = $derived(config[kind]);

  let current = $state<number | "new" | null>(null);
  let name = $state("");
  let members = $state<number[]>([]);
  let filter = $state("");
  let saving = $state(false);

  let group = $derived(typeof current === "number" ? (groups.find((g) => g.number === current) ?? null) : null);
  let names = $derived(new Map(channels.map((c) => [c.number, c.name])));
  let shownChannels = $derived.by(() => {
    const q = filter.trim().toLowerCase();
    return q ? channels.filter((c) => c.name.toLowerCase().includes(q) || mhz(c.rx_mhz).includes(q)) : channels;
  });
  let uniqueSaved = $derived(group ? [...new Set(group.members)] : []);
  let nameChanged = $derived(group ? name !== group.name : false);
  let membersChanged = $derived(
    group ? members.length !== group.members.length || members.some((m, i) => m !== group!.members[i]) : false,
  );
  let dirty = $derived(nameChanged || membersChanged);
  let tooMany = $derived(members.length > cfg.max);

  // Pick the first group by default, and fall back when the current one disappears.
  $effect(() => {
    if (current === "new") return;
    if (current === null || !groups.some((g) => g.number === current)) current = groups[0]?.number ?? null;
  });

  // Load the selected group into the form whenever it (or its saved contents) changes.
  $effect(() => {
    const g = group;
    if (current === "new") return;
    name = g?.name ?? "";
    members = g ? [...new Set(g.members)] : [];
  });

  function select(n: number | "new") {
    current = n;
    filter = "";
    if (n === "new") {
      name = "";
      members = [];
    }
  }

  function toggle(n: number, on: boolean) {
    // Keep the existing order; new members go at the end.
    members = on ? [...members, n] : members.filter((m) => m !== n);
  }

  function selectShown() {
    const add = shownChannels.map((c) => c.number).filter((n) => !members.includes(n));
    members = [...members, ...add];
  }

  function revert() {
    name = group?.name ?? "";
    members = [...uniqueSaved];
  }

  async function save() {
    if (!group) return;
    const op: Operation =
      kind === "zone"
        ? { op: "update_zone", zone: group.number }
        : { op: "update_scan_list", scan_list: group.number };
    if (nameChanged) op.name = name;
    if (membersChanged) op.members = members;
    saving = true;
    await run([op]);
    saving = false;
  }

  async function add() {
    saving = true;
    const before = groups.length;
    if (await run([{ op: "add_zone", name, members }])) current = before + 1;
    saving = false;
  }

  async function remove() {
    if (!group) return;
    saving = true;
    await run([{ op: "delete_zone", zone: group.number }]);
    saving = false;
  }

  const label = (n: number) => `${n} ${names.get(n) ?? "?"}`;
</script>

<div class="layout">
  <section class="panel list">
    <div class="panel-head">
      <h3>{cfg.title}</h3>
      {#if cfg.canAdd}
        <button class="small" onclick={() => select("new")} disabled={current === "new"}>New {cfg.noun}</button>
      {/if}
    </div>
    <table class="grid">
      <thead><tr><th class="num">#</th><th>Name</th><th class="num">Size</th></tr></thead>
      <tbody>
        {#each groups as g (g.number)}
          <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
          <tr class="clickable" class:selected={current === g.number} onclick={() => select(g.number)}>
            <td class="num muted">{g.number}</td>
            <td>
              <button class="link plain" aria-current={current === g.number} onclick={() => select(g.number)}>{g.name}</button>
            </td>
            <td class="num">{g.members.length}</td>
          </tr>
        {:else}
          <tr><td colspan="3" class="muted">None.</td></tr>
        {/each}
      </tbody>
    </table>
    {#if !cfg.canAdd}
      <p class="muted note">The radio's scan lists can be renamed and refilled here; adding or deleting them is not supported.</p>
    {/if}
  </section>

  {#if current === "new" || group}
    <section class="panel editor">
      <div class="panel-head">
        <h3>{current === "new" ? `New ${cfg.noun}` : `${cfg.noun === "zone" ? "Zone" : "Scan list"} ${group?.number}`}</h3>
        {#if current !== "new" && cfg.canDelete}
          <button class="danger small" onclick={remove} disabled={saving || groups.length <= 1}
            title={groups.length <= 1 ? "The radio needs at least one zone" : ""}>Delete {cfg.noun}</button>
        {/if}
      </div>
      <div class="panel-body stack">
        <div class="row end">
          <label class="field">
            <span>Name (up to {cfg.nameMax} characters)</span>
            <input type="text" bind:value={name} maxlength={cfg.nameMax} autocomplete="off" />
          </label>
          <span class="spacer"></span>
          {#if current === "new"}
            <button onclick={() => select(groups[0]?.number ?? "new")}>Cancel</button>
            <button class="primary" onclick={add} disabled={saving || !name.trim() || !members.length || tooMany}>
              {saving ? "Adding..." : `Add ${cfg.noun}`}
            </button>
          {:else}
            <button onclick={revert} disabled={!dirty || saving}>Revert</button>
            <button class="primary" onclick={save} disabled={!dirty || saving || !members.length || tooMany || !name.trim()}>
              {saving ? "Saving..." : "Save changes"}
            </button>
          {/if}
        </div>

        <div>
          <div class="row members-head">
            <strong>Channels</strong>
            <span class:over={tooMany} class="muted">{members.length} of {cfg.max} max</span>
            <span class="spacer"></span>
            <input type="search" placeholder="Filter channels" aria-label="Filter channels" bind:value={filter} />
            <button class="small" onclick={selectShown}>Select shown</button>
            <button class="small" onclick={() => (members = [])} disabled={!members.length}>Clear</button>
          </div>
          {#if tooMany}
            <div class="msg error">A {cfg.noun} holds at most {cfg.max} channels.</div>
          {/if}
          <div class="picker" role="group" aria-label="Channels in this {cfg.noun}">
            {#each shownChannels as c (c.number)}
              <label class="pick" class:on={members.includes(c.number)}>
                <input type="checkbox" checked={members.includes(c.number)} onchange={(e) => toggle(c.number, e.currentTarget.checked)} />
                <span class="muted num n">{c.number}</span>
                <span class="nm">{c.name}</span>
                {#if isRxOnly(c)}<span class="badge rx">RX</span>{/if}
              </label>
            {:else}
              <p class="muted">No channels match.</p>
            {/each}
          </div>
        </div>

        <div class="order">
          <span class="muted">Order on the radio:</span>
          {#if members.length}
            {members.map(label).join(", ")}
          {:else}
            <span class="muted">no channels selected</span>
          {/if}
        </div>
        {#if group && group.members.length !== uniqueSaved.length}
          <p class="muted">This {cfg.noun} lists a channel twice on the radio; saving keeps one of each.</p>
        {/if}
      </div>
    </section>
  {/if}
</div>

<style>
  .layout {
    display: grid;
    grid-template-columns: minmax(220px, 300px) 1fr;
    gap: 12px;
    align-items: start;
  }
  @media (max-width: 760px) {
    .layout {
      grid-template-columns: 1fr;
    }
  }
  .link.plain {
    text-decoration: none;
    color: var(--text);
  }
  .note {
    font-size: 12px;
    padding: 8px 12px;
    margin: 0;
    border-top: 1px solid var(--border);
  }
  .field input {
    width: 220px;
  }
  .members-head {
    margin-bottom: 6px;
  }
  .members-head input {
    width: 180px;
  }
  .over {
    color: var(--danger);
    font-weight: 600;
  }
  .picker {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 0 12px;
    max-height: 340px;
    overflow: auto;
    padding: 6px 8px;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--sunken);
  }
  .pick {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 2px 4px;
    border-radius: 2px;
    cursor: pointer;
    white-space: nowrap;
    overflow: hidden;
  }
  .pick:hover {
    background: var(--row-hover);
  }
  .pick.on .nm {
    font-weight: 600;
  }
  .n {
    min-width: 3ch;
  }
  .nm {
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .order {
    font-size: 12px;
  }
</style>
