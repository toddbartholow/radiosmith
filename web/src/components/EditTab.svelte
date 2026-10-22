<script lang="ts">
  import { app, SHORT_NAMES } from "../lib/app.svelte";
  import { api, errorText, type DraftSummary, type RadioId } from "../lib/api";
  import { when } from "../lib/format";
  import DraftEditor from "./DraftEditor.svelte";

  /** Edit and write for one radio: its saved drafts, or the open one. */
  let { radio }: { radio: RadioId } = $props();

  let all = $state<DraftSummary[]>([]);
  // Drafts made before RT-950 PRO editing existed have no radio; they are DM-32UV drafts.
  let drafts = $derived(all.filter((d) => (d.radio ?? "dm32") === radio));
  let short = $derived(SHORT_NAMES[radio]);
  let loading = $state(true);
  let error = $state("");
  let working = $state(false);

  let newest = $derived(app.newestBackup(radio));
  let usable = $derived(drafts.filter((d) => !d.error));
  let latestDraft = $derived(
    [...usable].sort((a, b) => (b.updated_utc ?? "").localeCompare(a.updated_utc ?? ""))[0],
  );
  let open = $derived(drafts.find((d) => d.id === app.draft));
  let locked = $derived(app.busy || app.startingRead !== null);

  async function load() {
    loading = true;
    try {
      all = await api.drafts();
      error = "";
    } catch (e) {
      error = errorText(e);
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    void app.draft; // reload when a draft is opened or closed
    void load();
  });

  const readTime = (backup: string) => when(app.backups.find((b) => b.backup === backup)?.created_utc) || backup;

  async function startNew() {
    if (!newest) return;
    working = true;
    error = "";
    try {
      const d = await api.createDraft(newest.backup, `Edits from the read at ${when(newest.created_utc)}`);
      app.openDraft(d.id, "channels", radio);
    } catch (e) {
      error = errorText(e);
    } finally {
      working = false;
    }
  }

  function editChannels() {
    if (latestDraft) app.openDraft(latestDraft.id, "channels", radio);
    else void startNew();
  }

  async function remove(d: DraftSummary) {
    if (!confirm(`Delete the saved edits "${d.name ?? d.id}"? The radio and its reads are not affected.`)) return;
    try {
      await api.deleteDraft(d.id);
      await load();
    } catch (e) {
      error = errorText(e);
    }
  }
</script>

{#if app.draft}
  {#if open && newest && open.base_backup !== newest.backup}
    <div class="msg info stale">
      <span>
        These edits start from the read at {readTime(open.base_backup)}. The radio was read again at
        {when(newest.created_utc)}. Writing reads the radio first and stops if the changes would come out differently.
      </span>
      <button class="small" onclick={startNew} disabled={working}>Start over from the newest read</button>
    </div>
  {/if}
  {#key app.draft}
    <DraftEditor id={app.draft} {radio} />
  {/key}
{:else}
  <div class="stack">
    <section class="panel intro">
      <h2>Edit the {short}'s channels</h2>
      <p>
        {#if radio === "rt950"}
          Change, add and remove channels here. Each channel keeps its slot number.
        {:else}
          Change channels, zones and scan lists here.
        {/if}
        Your changes are saved on this computer (as a draft) and nothing is sent to the radio until you review them
        and choose Write.
      </p>
      {#if error}<div class="msg error">{error}</div>{/if}
      {#if !newest && !loading && !usable.length}
        <p class="muted">The {short} has not been read yet, so there is nothing to edit.</p>
        <button class="primary" onclick={() => app.startRead(radio)} disabled={locked}>Read the {short}</button>
      {:else}
        <div class="row">
          <button class="primary" onclick={editChannels} disabled={loading || working}>
            {working ? "Starting..." : "Edit channels"}
          </button>
          <span class="muted">
            {#if latestDraft}
              Continues "{latestDraft.name}" ({latestDraft.operations} change{latestDraft.operations === 1 ? "" : "s"}).
            {:else if newest}
              Starts from the read at {when(newest.created_utc)}.
            {/if}
          </span>
        </div>
      {/if}
    </section>

    {#if drafts.length}
      <section class="panel">
        <div class="panel-head">
          <h3>Saved edits</h3>
          <span class="muted">Drafts on this computer, newest first.</span>
          {#if newest}
            <button class="small" onclick={startNew} disabled={working}>Start fresh from the newest read</button>
          {/if}
        </div>
        <table class="grid">
          <thead>
            <tr><th>Name</th><th>Starts from read at</th><th class="num">Changes</th><th>Updated</th><th></th></tr>
          </thead>
          <tbody>
            {#each drafts as d (d.id)}
              <tr>
                <td class="wrap name">
                  <button class="link" onclick={() => app.openDraft(d.id, "channels", radio)}>{d.name ?? d.id}</button>
                  {#if d.error}<div class="msg error small-msg">{d.error}</div>{/if}
                </td>
                <td>{d.base_backup ? readTime(d.base_backup) : ""}</td>
                <td class="num">{d.operations ?? ""}</td>
                <td>{when(d.updated_utc)}</td>
                <td class="actions">
                  <button class="small" onclick={() => app.openDraft(d.id, "channels", radio)}>Open</button>
                  <button class="small danger" onclick={() => remove(d)}>Delete</button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </section>
    {/if}
  </div>
{/if}

<style>
  .intro {
    padding: 16px;
  }
  .intro h2 {
    margin-bottom: 6px;
  }
  .intro p {
    max-width: 80ch;
    margin-bottom: 12px;
  }
  .stale span {
    white-space: normal;
  }
  .stale {
    display: flex;
    gap: 12px;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
  }
  .name {
    overflow-wrap: anywhere;
  }
  .name .link {
    white-space: normal;
    text-align: left;
  }
  .actions {
    width: 1%;
    text-align: right;
  }
  .actions button + button {
    margin-left: 4px;
  }
  .small-msg {
    margin-top: 4px;
    white-space: normal;
  }
</style>
