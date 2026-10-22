<script lang="ts">
  import { app, RADIO_IDS, RADIO_NAMES, SHORT_NAMES } from "../lib/app.svelte";
  import type { RadioId } from "../lib/api";
  import { when } from "../lib/format";

  function state(radio: RadioId): string {
    const job = app.runningJobs.find((j) => j.radio === radio);
    if (job) return job.kind === "read" ? "Reading..." : job.kind === "verify" ? "Verifying..." : "Writing...";
    if (app.connection[radio].checking) return "Checking...";
    return "";
  }
</script>

<aside>
  <h2 class="heading">Radios</h2>
  <ul class="radios">
    {#each RADIO_IDS as r (r)}
      {@const newest = app.newestBackup(r)}
      {@const port = app.rememberedPort(r)}
      {@const busy = state(r)}
      <li>
        <button class="radio" class:selected={app.radio === r} aria-current={app.radio === r} onclick={() => app.selectRadio(r)}>
          <span class="name">{RADIO_NAMES[r]}</span>
          <span class="line">
            {#if port}<span>{port}</span>{:else}<span class="warn">Port not found yet</span>{/if}
            {#if newest}<span class="muted">{[newest.model, newest.firmware].filter(Boolean).join(" ")}</span>{/if}
          </span>
          <span class="line muted">
            {#if busy}<span class="busy">{busy}</span>{:else if newest}Last read {when(newest.created_utc)}{:else if app.backupsLoaded}Not read yet{/if}
          </span>
        </button>
      </li>
    {/each}
  </ul>

  <details class="ports">
    <summary>Serial ports ({app.ports.length})</summary>
    <div class="ports-body">
      {#if app.portsError}
        <div class="msg error">{app.portsError}</div>
      {:else if !app.ports.length}
        <p class="muted">No serial ports found. Plug in the programming cable and refresh.</p>
      {:else}
        <table class="grid">
          <thead><tr><th>Port</th><th>Answered as</th></tr></thead>
          <tbody>
            {#each app.ports as p (p.device)}
              <tr title={[p.description, p.vid && p.pid ? `USB ${p.vid}:${p.pid}` : ""].filter(Boolean).join(", ")}>
                <td>
                  {p.device}
                  {#if p.is_ch340}<span class="muted">cable</span>{/if}
                </td>
                <td>{p.last_seen_radio ? SHORT_NAMES[p.last_seen_radio] : ""}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
      <button class="small" onclick={() => app.refreshPorts()} disabled={app.portsLoading}>
        {app.portsLoading ? "Refreshing..." : "Refresh ports"}
      </button>
    </div>
  </details>
</aside>

<style>
  aside {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .heading {
    font-size: 12px;
    font-weight: 600;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .radios {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .radio {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 2px;
    width: 100%;
    height: auto;
    padding: 10px 12px;
    text-align: left;
    white-space: normal;
    background: var(--panel);
    border: 1px solid var(--border);
    border-left: 3px solid transparent;
  }
  .radio:hover:not(:disabled) {
    background: var(--row-hover);
    border-color: var(--border-strong);
  }
  .radio.selected,
  .radio.selected:hover:not(:disabled) {
    background: var(--accent-soft);
    border-color: var(--accent-line);
    border-left-color: var(--accent);
  }
  .name {
    font-weight: 600;
    font-size: 14px;
  }
  .line {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    font-size: 12px;
  }
  .warn {
    color: var(--warn);
  }
  .busy {
    color: var(--warn);
    font-weight: 600;
  }
  .ports summary {
    cursor: pointer;
    color: var(--muted);
    font-size: 12px;
  }
  .ports-body {
    margin-top: 6px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    align-items: flex-start;
  }
  .ports-body table {
    border: 1px solid var(--border);
    background: var(--panel);
  }
</style>
