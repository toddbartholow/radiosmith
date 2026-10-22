<script lang="ts">
  import { app, SHORT_NAMES } from "../lib/app.svelte";
  import { api, errorText, type RadioId } from "../lib/api";
  import { lastSegment, timeOfDay, when } from "../lib/format";
  import BackupContents from "./BackupContents.svelte";
  import JobView from "./JobView.svelte";

  let { radio }: { radio: RadioId } = $props();

  const kindLabel: Record<string, string> = { read: "Read", write: "Write", "dry-run": "Dry run", verify: "Verify" };

  let backups = $derived(app.backupsFor(radio));
  let hasFirmware = $derived(backups.some((b) => b.firmware));
  let selected = $derived(backups.find((b) => b.backup === app.backup) ?? null);
  let jobs = $derived(app.jobsFor(radio));
  let shownJob = $state<string | null>(null);
  let short = $derived(SHORT_NAMES[radio]);

  let creating = $state(false);
  let createError = $state("");

  async function editFrom(backup: string, created: string | null) {
    creating = true;
    createError = "";
    try {
      const d = await api.createDraft(backup, `Edits from the read at ${when(created)}`);
      app.openDraft(d.id, "channels", radio);
    } catch (e) {
      createError = errorText(e);
    } finally {
      creating = false;
    }
  }
</script>

<div class="stack">
  <section class="panel">
    <div class="panel-head">
      <h3>Reads of the {short}</h3>
      <span class="muted">Each read is saved on this computer as a backup.</span>
      <button class="small" onclick={() => app.refreshBackups()}>Refresh</button>
    </div>
    {#if !backups.length}
      <p class="empty">No reads yet.</p>
    {:else}
      <div class="list">
        <table class="grid">
          <thead>
            <tr><th>Read at</th><th>Model</th>{#if hasFirmware}<th>Firmware</th>{/if}<th>State</th><th>Folder</th></tr>
          </thead>
          <tbody>
            {#each backups as b (b.backup)}
              <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
              <tr class="clickable" class:selected={b.backup === app.backup} onclick={() => (app.backup = b.backup)}>
                <td>
                  <button class="link" aria-current={b.backup === app.backup} onclick={() => (app.backup = b.backup)}>{when(b.created_utc)}</button>
                </td>
                <td>{b.model ?? ""}</td>
                {#if hasFirmware}<td class="mono">{b.firmware ?? ""}</td>{/if}
                <td>{#if b.complete}<span class="badge ok">Complete</span>{:else}<span class="badge bad">Did not finish</span>{/if}</td>
                <td class="mono muted">{lastSegment(b.backup)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </section>

  {#if selected}
    <section class="panel">
      <div class="panel-head">
        <h3>Channels as read at {when(selected.created_utc)}</h3>
        {#if selected.complete}
          <button onclick={() => selected && editFrom(selected.backup, selected.created_utc)} disabled={creating}>
            {creating ? "Starting..." : "Edit channels from this read"}
          </button>
        {/if}
        <button class="small" onclick={() => (app.backup = null)}>Close</button>
      </div>
      <div class="panel-body">
        <p class="muted mono path">{selected.backup}</p>
        {#if createError}<div class="msg error">{createError}</div>{/if}
        {#if !selected.complete}
          <div class="msg warn">This read did not finish, so its channels may be missing or wrong.</div>
        {/if}
        {#key selected.backup}
          <BackupContents backup={selected.backup} {radio} />
        {/key}
      </div>
    </section>
  {:else if app.backup && app.backupsLoaded}
    <div class="msg warn">The read {app.backup} was not found.</div>
  {/if}

  <section class="panel">
    <div class="panel-head">
      <h3>Radio jobs for the {short}</h3>
      <span class="muted">Since the service started, including MCP.</span>
    </div>
    {#if !jobs.length}
      <p class="empty">None since the service started.</p>
    {:else}
      <table class="grid">
        <thead><tr><th>Started</th><th>Job</th><th>Port</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {#each jobs as j (j.id)}
            <tr class:selected={shownJob === j.id}>
              <td>{timeOfDay(j.started_utc)}</td>
              <td>{kindLabel[j.kind] ?? j.kind}</td>
              <td>{j.port}</td>
              <td>
                {#if j.status === "running"}<span class="badge warn">Running</span>
                {:else if j.status === "failed"}<span class="badge bad">Failed</span>
                {:else}<span class="badge ok">Done</span>{/if}
              </td>
              <td class="show-cell">
                <button class="small" aria-expanded={shownJob === j.id} onclick={() => (shownJob = shownJob === j.id ? null : j.id)}>
                  {shownJob === j.id ? "Hide" : "Show"}
                </button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
      {#if shownJob && app.jobs[shownJob]}
        <div class="panel-body"><JobView job={app.jobs[shownJob]} /></div>
      {/if}
    {/if}
  </section>
</div>

<style>
  .list {
    max-height: 260px;
    overflow: auto;
  }
  .path {
    font-size: 11px;
  }
  .show-cell {
    width: 1%;
    text-align: right;
  }
</style>
