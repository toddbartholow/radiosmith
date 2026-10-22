<script lang="ts">
  import { onMount } from "svelte";
  import { app, SHORT_NAMES } from "./lib/app.svelte";
  import RadioPage from "./components/RadioPage.svelte";
  import Sidebar from "./components/Sidebar.svelte";

  const kindLabel: Record<string, string> = {
    read: "Reading",
    write: "Writing",
    "dry-run": "Dry run on",
    verify: "Verifying",
  };

  app.fromHash(location.hash);

  $effect(() => {
    const h = app.toHash();
    if (h !== location.hash) history.replaceState(null, "", h);
  });

  onMount(() => {
    void app.refreshStatus();
    void app.refreshPorts();
    void app.refreshBackups();
    void app.loadJobs();
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") void app.refreshStatus();
    }, 5000);
    const onHash = () => {
      if (location.hash !== app.toHash()) app.fromHash(location.hash);
    };
    window.addEventListener("hashchange", onHash);
    return () => {
      clearInterval(timer);
      window.removeEventListener("hashchange", onHash);
    };
  });
</script>

<header class="top">
  <span class="brand">radiosmith</span>
  <span class="spacer"></span>
  {#if app.statusError}
    <span class="badge bad" title={app.statusError}>Service unreachable</span>
  {:else if app.status}
    {#if app.status.allow_writes}
      <span class="badge warn" title="The service was started with --allow-writes">Writes enabled</span>
    {:else}
      <span class="badge" title="Start the service with --allow-writes to write to a radio">Writes disabled</span>
    {/if}
    <span class="muted mono data-dir" title="Data folder">{app.status.data_dir}</span>
  {/if}
</header>

{#if app.statusError}
  <div class="banner error" role="alert">{app.statusError}</div>
{/if}
{#each app.runningJobs as job (job.id)}
  <div class="banner busy" role="status">
    <strong>Radio busy:</strong>
    {kindLabel[job.kind] ?? job.kind}
    {SHORT_NAMES[job.radio] ?? job.radio} on {job.port}.
    <span class="mono last-line">{job.log[job.log.length - 1] ?? ""}</span>
    <span class="spacer"></span>
    <span>Other radio actions wait until it finishes.</span>
  </div>
{:else}
  {#if app.status?.busy}
    <div class="banner busy" role="status"><strong>Radio busy:</strong> another radio operation is running.</div>
  {/if}
{/each}

<div class="layout">
  <Sidebar />
  <main>
    {#key app.radio}
      <RadioPage radio={app.radio} />
    {/key}
  </main>
</div>

<style>
  .top {
    display: flex;
    align-items: center;
    gap: 16px;
    height: 40px;
    padding: 0 16px;
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    z-index: 10;
  }
  .brand {
    font-weight: 700;
    font-size: 14px;
    letter-spacing: 0.01em;
  }
  .data-dir {
    max-width: 40ch;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .spacer {
    flex: 1;
  }
  .banner {
    display: flex;
    gap: 6px;
    align-items: baseline;
    padding: 6px 16px;
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }
  .banner.busy {
    background: var(--warn-soft);
    color: var(--warn);
  }
  .banner.error {
    background: var(--danger-soft);
    color: var(--danger);
  }
  .last-line {
    color: var(--text);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 60ch;
  }
  .layout {
    display: grid;
    grid-template-columns: 220px minmax(0, 1fr);
    gap: 16px;
    padding: 16px;
    max-width: 1800px;
    align-items: start;
  }
  @media (max-width: 760px) {
    .layout {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
