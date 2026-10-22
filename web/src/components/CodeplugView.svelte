<script lang="ts">
  import type { AnyChannel, Channel, Group, RadioId, RT950Channel } from "../lib/api";
  import { groupName, isRxOnly, mhz, modeLabel, powerLabel } from "../lib/format";

  /** A read-only view of decoded channels, zones and scan lists. Columns follow the radio. */
  let {
    radio,
    channels,
    zones,
    scanLists,
    problems = [],
  }: {
    radio: RadioId;
    channels: AnyChannel[];
    zones: Group[];
    scanLists: Group[];
    problems?: string[];
  } = $props();

  let names = $derived(new Map(channels.map((c) => [c.number, c.name])));
  const member = (n: number) => (names.has(n) ? `${n} ${names.get(n)}` : String(n));
  const dm = (c: AnyChannel) => c as Channel;
  const rt = (c: AnyChannel) => c as RT950Channel;

  function rtExtras(c: RT950Channel): string {
    const out: string[] = [];
    if (c.busy_lock) out.push("busy lock");
    if (c.ptt_id && c.ptt_id !== "off") out.push(`PTT ID ${c.ptt_id}`);
    if (c.scramble) out.push(`scramble ${c.scramble}`);
    if (c.rx_am) out.push("AM receive");
    if (c.fhss_learn) out.push("FHSS learn");
    return out.join(", ");
  }

  const columns = 11;
</script>

{#if problems.length}
  <div class="msg warn">
    Decoding problems:
    <ul>
      {#each problems as p, i (i)}<li>{p}</li>{/each}
    </ul>
  </div>
{/if}

<section>
  <h3 class="title">Channels <span class="muted">({channels.length})</span></h3>
  <div class="table-wrap tall">
    <table class="grid">
      <thead>
        <tr>
          <th class="num">#</th>
          <th>Name</th>
          {#if radio === "dm32"}<th>Mode</th>{/if}
          <th class="num">RX MHz</th>
          <th class="num">TX MHz</th>
          <th>Power</th>
          <th class="num">BW kHz</th>
          <th>RX tone</th>
          <th>TX tone</th>
          {#if radio === "dm32"}
            <th>Scan list</th>
          {:else}
            <th>Scan</th>
            <th>Other</th>
          {/if}
        </tr>
      </thead>
      <tbody>
        {#each channels as c (c.number)}
          <tr>
            <td class="num muted">{c.number}</td>
            <td>{c.name}</td>
            {#if radio === "dm32"}<td>{modeLabel(dm(c))}</td>{/if}
            <td class="num">{mhz(c.rx_mhz)}</td>
            <td class="num">
              {#if isRxOnly(c)}
                <span class="badge rx" title={c.tx_mhz === null ? "No TX frequency stored" : "TX is forbidden"}>RX only</span>
                {#if c.tx_mhz !== null}<span class="muted">{mhz(c.tx_mhz)}</span>{/if}
              {:else}
                {mhz(c.tx_mhz)}
              {/if}
            </td>
            <td>{powerLabel[c.power] ?? c.power}</td>
            <td class="num">{c.bandwidth_khz}</td>
            <td>{c.rx_tone ?? ""}</td>
            <td>{c.tx_tone ?? ""}</td>
            {#if radio === "dm32"}
              <td>{dm(c).scan_list ? groupName(scanLists, dm(c).scan_list) : ""}</td>
            {:else}
              <td>{rt(c).scan_add ? "Yes" : "No"}</td>
              <td class="muted">{rtExtras(rt(c))}</td>
            {/if}
          </tr>
        {:else}
          <tr><td colspan={columns} class="muted">No channels.</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
</section>

{#if zones.length || scanLists.length}
  <div class="groups">
    {#each [{ title: "Zones", items: zones }, { title: "Scan lists", items: scanLists }] as g (g.title)}
      <section>
        <h3 class="title">{g.title} <span class="muted">({g.items.length})</span></h3>
        <div class="table-wrap">
          <table class="grid">
            <thead>
              <tr><th class="num">#</th><th>Name</th><th class="num">Size</th><th>Channels</th></tr>
            </thead>
            <tbody>
              {#each g.items as z (z.number)}
                <tr>
                  <td class="num muted">{z.number}</td>
                  <td>{z.name}</td>
                  <td class="num">{z.members.length}</td>
                  <td class="wrap">{z.members.map(member).join(", ")}</td>
                </tr>
              {:else}
                <tr><td colspan="4" class="muted">None.</td></tr>
              {/each}
            </tbody>
          </table>
        </div>
      </section>
    {/each}
  </div>
{/if}

<style>
  .title {
    margin: 12px 0 6px;
  }
  section:first-of-type .title {
    margin-top: 0;
  }
  .tall {
    max-height: 60vh;
  }
  .groups {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(420px, 100%), 1fr));
    gap: 12px;
  }
  ul {
    margin: 4px 0 0;
    padding-left: 18px;
  }
</style>
