<script lang="ts">
  import { api, errorText, type BackupView, type RadioId } from "../lib/api";
  import CodeplugView from "./CodeplugView.svelte";

  /** Loads a backup and shows its channels, zones and scan lists read-only. */
  let { backup, radio }: { backup: string; radio: RadioId } = $props();

  let view = $state<BackupView | null>(null);
  let error = $state("");
  let loading = $state(true);

  $effect(() => {
    const ref = backup;
    let cancelled = false;
    loading = true;
    error = "";
    api
      .backup(ref)
      .then((v) => {
        if (!cancelled) view = v;
      })
      .catch((e) => {
        if (!cancelled) {
          view = null;
          error = errorText(e);
        }
      })
      .finally(() => {
        if (!cancelled) loading = false;
      });
    return () => {
      cancelled = true;
    };
  });
</script>

{#if error}
  <div class="msg error">{error}</div>
{:else if loading && !view}
  <p class="muted">Loading channels...</p>
{:else if view}
  {#if !view.ok && view.problems.length}
    <div class="msg warn">Problems with this read: {view.problems.join("; ")}</div>
  {/if}
  <CodeplugView
    radio={view.radio ?? radio}
    channels={view.channels}
    zones={view.zones}
    scanLists={view.scan_lists}
    problems={view.ok ? view.problems : []}
  />
{/if}
