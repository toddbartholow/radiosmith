<script lang="ts">
  import type { AnyChannel, Changed, Group, RadioId } from "../lib/api";
  import { channelFieldChanges, channelLabel, channelSummary, isRxOnly, memberDiff } from "../lib/format";

  type Kind = "channels" | "zones" | "scan_lists";
  type Item = AnyChannel | Group;

  let {
    kind,
    radio = "dm32",
    added,
    removed,
    changed,
    moved = [],
    beforeChannels,
    afterChannels,
  }: {
    kind: Kind;
    /** Which radio the channels belong to (their fields differ). */
    radio?: RadioId;
    added: Item[];
    removed: Item[];
    changed: Changed<Item>[];
    /** Unchanged items that only moved to a new number. */
    moved?: Changed<Item>[];
    /** Channels before and after, to name zone and scan list members (numbers change when channels are deleted). */
    beforeChannels: AnyChannel[];
    afterChannels: AnyChannel[];
  } = $props();

  const titles: Record<Kind, string> = { channels: "Channels", zones: "Zones", scan_lists: "Scan lists" };
  const nouns: Record<Kind, string> = { channels: "channel", zones: "zone", scan_lists: "scan list" };
  let total = $derived(added.length + removed.length + changed.length + moved.length);
  let namesBefore = $derived(new Map(beforeChannels.map((c) => [c.number, c.name])));
  let namesAfter = $derived(new Map(afterChannels.map((c) => [c.number, c.name])));

  const labelIn = (names: Map<number, string>) => (n: number) => (names.has(n) ? `${names.get(n)} (${n})` : `#${n}`);
  const nameOf = (names: Map<number, string>) => (n: number) => names.get(n) ?? `#${n}`;

  function groupDiff(b: Group, a: Group) {
    const m = memberDiff(b.members.map(nameOf(namesBefore)), a.members.map(nameOf(namesAfter)));
    const renumbered = !m.added.length && !m.removed.length && !m.reordered && b.name === a.name;
    return { ...m, renumbered };
  }

  function list(xs: string[]): string {
    if (!xs.length) return "none";
    return xs.slice(0, 12).join(", ") + (xs.length > 12 ? ` and ${xs.length - 12} more` : "");
  }

  const asChannel = (x: Item) => x as AnyChannel;
  const asGroup = (x: Item) => x as Group;

  const itemName = (x: Item) => (kind === "channels" ? channelLabel(x) : x.name);

  const numberText = (x: Changed<Item>) =>
    x.before.number === x.after.number ? String(x.after.number) : `${x.before.number} → ${x.after.number}`;
</script>

{#if total}
  <section class="changes">
    <h3>{titles[kind]} <span class="muted">({total})</span></h3>
    <div class="table-wrap">
      <table class="grid">
        <thead>
          <tr>
            <th class="what">Change</th>
            <th class="num">{radio === "rt950" && kind === "channels" ? "Slot" : "#"}</th>
            <th>Name</th>
            <th>Details</th>
          </tr>
        </thead>
        <tbody>
          {#each added as x (x.number)}
            <tr>
              <td><span class="badge ok">Added</span></td>
              <td class="num">{x.number}</td>
              <td>{itemName(x)}</td>
              <td class="wrap">
                {#if kind === "channels"}
                  {channelSummary(asChannel(x))}
                  {#if isRxOnly(asChannel(x))}<span class="badge rx">RX only</span>{/if}
                {:else}
                  {asGroup(x).members.length} channels: {list(asGroup(x).members.map(labelIn(namesAfter)))}
                {/if}
              </td>
            </tr>
          {/each}
          {#each removed as x (x.number)}
            <tr>
              <td><span class="badge bad">Removed</span></td>
              <td class="num">{x.number}</td>
              <td>{itemName(x)}</td>
              <td class="wrap muted">
                {#if kind === "channels"}{channelSummary(asChannel(x))}{:else}{asGroup(x).members.length} channels{/if}
              </td>
            </tr>
          {/each}
          {#each changed as x (x.after.number)}
            <tr>
              <td><span class="badge">Changed</span></td>
              <td class="num">{numberText(x)}</td>
              <td>{itemName(x.after)}</td>
              <td class="wrap">
                <ul class="fields">
                  {#if kind === "channels"}
                    {#each channelFieldChanges(x as Changed<AnyChannel>, radio) as f (f.label)}
                      <li>
                        <span class="label">{f.label}</span>
                        <span class="before">{f.before}</span>
                        <span class="arrow" aria-label="becomes">→</span>
                        <span class="after">{f.after}</span>
                      </li>
                    {/each}
                  {:else}
                    {@const b = asGroup(x.before)}
                    {@const a = asGroup(x.after)}
                    {@const m = groupDiff(b, a)}
                    {#if b.name !== a.name}
                      <li>
                        <span class="label">Name</span>
                        <span class="before">{b.name}</span>
                        <span class="arrow" aria-label="becomes">→</span>
                        <span class="after">{a.name}</span>
                      </li>
                    {/if}
                    {#if m.added.length}
                      <li><span class="label">Adds</span> {list(m.added)}</li>
                    {/if}
                    {#if m.removed.length}
                      <li><span class="label">Drops</span> {list(m.removed)}</li>
                    {/if}
                    {#if m.reordered}
                      <li><span class="label">Order</span> members reordered</li>
                    {/if}
                    {#if m.renumbered}
                      <li class="muted">Same channels; their numbers change because a channel was deleted</li>
                    {/if}
                  {/if}
                </ul>
              </td>
            </tr>
          {/each}
          {#if moved.length}
            <tr>
              <td><span class="badge">Renumbered</span></td>
              <td class="num"></td>
              <td>{moved.length} {nouns[kind]}{moved.length === 1 ? "" : "s"}</td>
              <td class="wrap">
                <span class="muted">Unchanged, at a new number:</span>
                {list(moved.map((x) => `${itemName(x.after)} (${x.before.number} → ${x.after.number})`))}
              </td>
            </tr>
          {/if}
        </tbody>
      </table>
    </div>
  </section>
{/if}

<style>
  .changes h3 {
    margin-bottom: 6px;
  }
  .what {
    width: 90px;
  }
  .fields {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .fields li {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    align-items: baseline;
  }
  .label {
    color: var(--muted);
    min-width: 72px;
  }
  .before {
    text-decoration: line-through;
    color: var(--muted);
  }
  .after {
    font-weight: 600;
  }
  .arrow {
    color: var(--muted);
  }
</style>
