<script lang="ts">
  import { onMount } from "svelte";
  import { api, errorText, type CsvImportResult, type EditResult, type Profile, type RadioId } from "../lib/api";

  let {
    id,
    radio,
    act,
  }: {
    id: string;
    radio: RadioId;
    act: <T>(fn: () => Promise<T>) => Promise<T | undefined>;
  } = $props();

  let profiles = $state<Profile[]>([]);
  let profilesError = $state("");
  let profile = $state("");
  let profileResult = $state<EditResult | null>(null);

  let csv = $state("");
  let csvResult = $state<CsvImportResult | null>(null);
  let fileError = $state("");
  let busy = $state(false);

  let chosen = $derived(profiles.find((p) => p.profile === profile) ?? null);

  onMount(async () => {
    try {
      profiles = await api.profiles();
      profile = profiles[0]?.profile ?? "";
    } catch (e) {
      profilesError = errorText(e);
    }
  });

  async function applyProfile() {
    busy = true;
    profileResult = (await act(() => api.applyProfile(id, profile))) ?? null;
    busy = false;
  }

  async function loadExport() {
    fileError = "";
    try {
      const res = await fetch(api.csvUrl(id));
      if (!res.ok) throw new Error(((await res.json().catch(() => null)) as { error?: string } | null)?.error ?? `HTTP ${res.status}`);
      csv = await res.text();
    } catch (e) {
      fileError = errorText(e);
    }
  }

  async function pickFile(e: Event & { currentTarget: HTMLInputElement }) {
    fileError = "";
    const file = e.currentTarget.files?.[0];
    if (!file) return;
    try {
      csv = await file.text();
    } catch (err) {
      fileError = errorText(err);
    }
    e.currentTarget.value = "";
  }

  async function importCsv() {
    busy = true;
    csvResult = (await act(() => api.importCsv(id, csv))) ?? null;
    busy = false;
  }
</script>

<div class="cols">
  <section class="panel">
    <div class="panel-head"><h3>Apply a profile</h3></div>
    <div class="panel-body stack">
      <p class="muted">
        {#if radio === "rt950"}
          A profile adds a set of channels after the last used channel, as one edit you can undo.
        {:else}
          A profile adds a set of channels and a zone holding them, as one edit you can undo.
        {/if}
      </p>
      {#if profilesError}
        <div class="msg error">{profilesError}</div>
      {:else if !profiles.length}
        <p class="muted">No profiles found in the service's profiles folder.</p>
      {:else}
        <div class="row end">
          <label class="field">
            <span>Profile</span>
            <select bind:value={profile}>
              {#each profiles as p (p.profile)}
                <option value={p.profile}>{p.name ?? p.profile} ({p.channels} channels)</option>
              {/each}
            </select>
          </label>
          <button class="primary" onclick={applyProfile} disabled={busy || !profile}>Apply profile</button>
        </div>
        {#if chosen?.description}<p>{chosen.description}</p>{/if}
        {#if profileResult}
          <div class="msg ok">Applied. The draft now has {profileResult.operations} edits touching {profileResult.pages_changed} pages.</div>
        {/if}
      {/if}
    </div>
  </section>

  <section class="panel">
    <div class="panel-head">
      <h3>Channels as CSV</h3>
      <a class="button" href={api.csvUrl(id)} download="{id}.csv">Download CSV</a>
    </div>
    <div class="panel-body stack">
      <p class="muted">
        Edit the downloaded file in a spreadsheet and import it back.
        {#if radio === "rt950"}
          Rows with a number update that channel, or put a channel in that slot if it is empty. Rows without a number
          go after the last channel, and channels missing from the file are deleted (their slots are emptied).
        {:else}
          Rows with a number update that channel, rows without a number are added, and channels missing from the file
          are deleted. Set mode to analog to replace a digital channel.
        {/if}
      </p>
      <div class="row">
        <label class="button file">
          Choose CSV file...
          <input type="file" accept=".csv,text/csv" onchange={pickFile} />
        </label>
        <button onclick={loadExport}>Load current channels</button>
        <span class="spacer"></span>
        <button class="primary" onclick={importCsv} disabled={busy || !csv.trim()}>Import CSV</button>
      </div>
      {#if fileError}<div class="msg error">{fileError}</div>{/if}
      <textarea rows="14" bind:value={csv} aria-label="CSV text" placeholder={radio === "rt950"
          ? "number,name,rx_mhz,tx_mhz,power,bandwidth_khz,rx_tone,tx_tone,scan_add,busy_lock,rx_am"
          : "number,name,rx_mhz,tx_mhz,mode,forbid_tx,power,bandwidth_khz,rx_tone,tx_tone,scan_list"}></textarea>
      {#if csvResult}
        {#if csvResult.operations_added === 0}
          <div class="msg info">{csvResult.note ?? "The CSV matches the draft; nothing changed."}</div>
        {:else}
          <div class="msg ok">Imported as {csvResult.operations_added} edit{csvResult.operations_added === 1 ? "" : "s"}. See Edits to review or undo them.</div>
        {/if}
      {/if}
    </div>
  </section>
</div>

<style>
  .cols {
    display: grid;
    grid-template-columns: minmax(300px, 1fr) minmax(400px, 1.6fr);
    gap: 12px;
    align-items: start;
  }
  @media (max-width: 900px) {
    .cols {
      grid-template-columns: 1fr;
    }
  }
  .file {
    position: relative;
    overflow: hidden;
  }
  .file input {
    position: absolute;
    inset: 0;
    opacity: 0;
    cursor: pointer;
  }
  .file:focus-within {
    outline: 2px solid var(--focus);
    outline-offset: 1px;
  }
  .spacer {
    flex: 1;
  }
</style>
