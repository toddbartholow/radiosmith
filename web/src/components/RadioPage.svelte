<script lang="ts">
  import { app, RADIO_NAMES, READ_TIME, SHORT_NAMES, type RadioView } from "../lib/app.svelte";
  import type { RadioId } from "../lib/api";
  import { timeOfDay } from "../lib/format";
  import EditTab from "./EditTab.svelte";
  import History from "./History.svelte";
  import JobView from "./JobView.svelte";
  import PortControl from "./PortControl.svelte";
  import RadioChannels from "./RadioChannels.svelte";

  let { radio }: { radio: RadioId } = $props();

  const tabs: { id: RadioView; label: string }[] = [
    { id: "channels", label: "Channels on the radio" },
    { id: "edit", label: "Edit channels" },
    { id: "history", label: "History" },
  ];

  let conn = $derived(app.connection[radio]);
  let identity = $derived(conn.result?.identity ?? null);
  let readJob = $derived(app.jobsFor(radio).find((j) => j.kind === "read" && !app.dismissed.includes(j.id)));
  let locked = $derived(app.busy || app.startingRead !== null);
  let short = $derived(SHORT_NAMES[radio]);

  function identityText(id: Record<string, unknown>): string {
    return [id.model, id.firmware ? `firmware ${id.firmware}` : ""].filter(Boolean).join(", ");
  }
</script>

<div class="page">
  <section class="panel head">
    <div class="title-row">
      <h1>{RADIO_NAMES[radio]}</h1>
      <PortControl {radio} disabled={locked} />
      <span class="conn" aria-live="polite">
        {#if conn.checking}
          <span class="muted">Checking connection...</span>
        {:else if identity}
          <span class="badge ok">Connected</span>
          <span>{identityText(identity)}</span>
          <span class="muted">at {timeOfDay(conn.at)}</span>
        {:else if conn.error}
          <span class="badge bad">No answer</span>
          <span class="muted">at {timeOfDay(conn.at)}</span>
        {:else}
          <span class="muted">Not checked yet</span>
        {/if}
      </span>
      <span class="actions">
      <button onclick={() => app.checkConnection(radio)} disabled={locked} title="Ask the {short} for its model and firmware. Changes nothing.">
        Check connection
      </button>
      <button onclick={() => app.startRead(radio)} disabled={locked} title="Copy everything on the {short} to this computer ({READ_TIME[radio]}). Changes nothing on the radio.">
        {app.startingRead === radio ? "Starting..." : "Read radio"}
      </button>
      </span>
    </div>
    {#if conn.error}
      <div class="msg error">{conn.error}</div>
    {/if}
    {#if app.readError[radio]}
      <div class="msg error">{app.readError[radio]}</div>
    {/if}
    {#if readJob}
      <JobView job={readJob} onclose={readJob.status === "running" ? undefined : () => (app.dismissed = [...app.dismissed, readJob.id])} />
    {/if}

    <div class="tabs" role="tablist" aria-label="{short} views">
      {#each tabs as t (t.id)}
        <button role="tab" class="tab" class:active={app.view === t.id} aria-selected={app.view === t.id} onclick={() => (app.view = t.id)}>
          {t.label}
        </button>
      {/each}
    </div>
  </section>

  <div class="body">
    {#if app.view === "channels"}
      <RadioChannels {radio} />
    {:else if app.view === "edit"}
      <EditTab {radio} />
    {:else}
      <History {radio} />
    {/if}
  </div>
</div>

<style>
  .head {
    padding: 12px 12px 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .title-row {
    display: flex;
    align-items: center;
    gap: 8px 14px;
    flex-wrap: wrap;
  }
  h1 {
    font-size: 17px;
    font-weight: 600;
  }
  .conn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }
  .actions {
    display: flex;
    gap: 8px;
    margin-left: auto;
  }
  .tabs {
    display: flex;
    flex-wrap: wrap;
    margin: 0 -12px;
    padding: 0 6px;
    border-top: 1px solid var(--border);
  }
  .tab {
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    background: none;
    height: 36px;
    padding: 0 14px;
    color: var(--muted);
  }
  .tab:hover:not(:disabled) {
    background: var(--sunken);
    color: var(--text);
  }
  .tab.active {
    color: var(--text);
    font-weight: 600;
    border-bottom-color: var(--accent);
  }
  .body {
    margin-top: 12px;
  }
</style>
