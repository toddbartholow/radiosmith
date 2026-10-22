<script lang="ts">
  import { app, SHORT_NAMES } from "../lib/app.svelte";
  import type { RadioId } from "../lib/api";

  /** "Port: Auto (COM3) [change]": the serial port actions for this radio use. */
  let { radio, disabled = false }: { radio: RadioId; disabled?: boolean } = $props();

  let editing = $state(false);
  let remembered = $derived(app.rememberedPort(radio));
  let label = $derived(
    app.port[radio] ? app.port[radio] : remembered ? `Auto (${remembered})` : "Auto (not found yet)",
  );
  const lastSeen = (r: RadioId | null) => (r ? ` - last answered as ${SHORT_NAMES[r]}` : "");
</script>

<span class="port">
  {#if editing}
    <label>
      <span class="muted">Port for the {SHORT_NAMES[radio]}</span>
      <select bind:value={app.port[radio]} {disabled}>
        <option value="">Auto{remembered ? ` (${remembered})` : ""}</option>
        {#each app.ports as p (p.device)}
          <option value={p.device}>{p.device}{lastSeen(p.last_seen_radio)}{!p.last_seen_radio && p.is_ch340 ? " - programming cable" : ""}</option>
        {/each}
      </select>
    </label>
    <button class="small" onclick={() => (editing = false)}>Done</button>
  {:else}
    <span class="muted">Port:</span>
    <span class:missing={!app.port[radio] && !remembered}>{label}</span>
    <button class="link" onclick={() => (editing = true)} {disabled} aria-label="Change the port for the {SHORT_NAMES[radio]}">change</button>
  {/if}
</span>

<style>
  .port {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    white-space: nowrap;
  }
  label {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .missing {
    color: var(--warn);
  }
</style>
