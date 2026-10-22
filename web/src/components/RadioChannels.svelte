<script lang="ts">
  import { app, READ_TIME, SHORT_NAMES } from "../lib/app.svelte";
  import type { RadioId } from "../lib/api";
  import { when } from "../lib/format";
  import BackupContents from "./BackupContents.svelte";

  let { radio }: { radio: RadioId } = $props();

  let newest = $derived(app.newestBackup(radio));
  let incomplete = $derived(app.backupsFor(radio).filter((b) => !b.complete).length);
  let short = $derived(SHORT_NAMES[radio]);
  let locked = $derived(app.busy || app.startingRead !== null);
</script>

{#if app.backupsError}
  <div class="msg error">{app.backupsError}</div>
{:else if !app.backupsLoaded}
  <p class="muted">Loading...</p>
{:else if !newest}
  <section class="panel empty-state">
    <h2>No channels to show yet</h2>
    <p>
      The {short} has not been read on this computer{incomplete ? " (earlier reads did not finish)" : ""}. Reading copies
      its channels here so you can see and edit them. It takes {READ_TIME[radio]} and changes nothing on the radio.
    </p>
    <button class="primary" onclick={() => app.startRead(radio)} disabled={locked}>Read the {short}</button>
  </section>
{:else}
  <section class="panel">
    <div class="panel-head">
      <p class="source">
        From the read at <strong title="Saved as {newest.backup}">{when(newest.created_utc)}</strong>. Read radio to refresh.
      </p>
      <button onclick={() => (app.view = "edit")}>Edit channels</button>
    </div>
    <div class="panel-body">
      {#key newest.backup}
        <BackupContents backup={newest.backup} {radio} />
      {/key}
    </div>
  </section>
{/if}

<style>
  .empty-state {
    padding: 24px;
    max-width: 640px;
  }
  .empty-state h2 {
    margin-bottom: 8px;
  }
  .empty-state p {
    margin-bottom: 14px;
  }
  .source {
    margin: 0 auto 0 0;
  }
</style>
