<script lang="ts">
  import { onMount } from "svelte";
  import { app, type DraftSection } from "../lib/app.svelte";
  import { api, errorText, type Channel, type DraftView, type Operation, type RadioId } from "../lib/api";
  import { describeOp, when } from "../lib/format";
  import ChannelTable from "./ChannelTable.svelte";
  import GroupEditor from "./GroupEditor.svelte";
  import ProfileCsv from "./ProfileCsv.svelte";
  import Review from "./Review.svelte";

  let { id, radio: tabRadio }: { id: string; radio: RadioId } = $props();

  let draft = $state<DraftView | null>(null);
  let loadError = $state("");
  let actionError = $state("");
  let pending = $state(0);
  let queue: Promise<unknown> = Promise.resolve();

  async function reload() {
    try {
      draft = await api.draft(id);
      loadError = "";
    } catch (e) {
      loadError = errorText(e);
    }
  }

  onMount(reload);

  /** Run draft actions one at a time, in order, and reload the draft after each. */
  function act<T>(fn: () => Promise<T>): Promise<T | undefined> {
    const p = queue.then(async () => {
      pending++;
      actionError = "";
      try {
        const out = await fn();
        await reload();
        return out;
      } catch (e) {
        actionError = errorText(e);
        return undefined;
      } finally {
        pending--;
      }
    });
    queue = p;
    return p;
  }

  /** Send edit operations; true when they were applied (all or nothing). */
  async function run(ops: Operation[]): Promise<boolean> {
    return (await act(() => api.edit(id, ops))) !== undefined;
  }

  async function undo(count: number) {
    await act(() => api.undo(id, count));
  }

  async function rename(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    const name = input.value.trim();
    if (!draft || name === draft.name) return;
    if ((await act(() => api.rename(id, name))) === undefined) input.value = draft?.name ?? "";
  }

  async function remove() {
    if (!confirm(`Delete the draft "${draft?.name ?? id}"? Its edits are lost; the backup is kept.`)) return;
    try {
      await api.deleteDraft(id);
      app.openDraft(null);
    } catch (e) {
      actionError = errorText(e);
    }
  }

  // Drafts made before RT-950 PRO editing existed have no radio; they are DM-32UV drafts.
  let radio = $derived<RadioId>(draft ? (draft.radio ?? "dm32") : tabRadio);
  let hasGroups = $derived(radio === "dm32");
  // The RT-950 PRO has no zone or scan list editing; fall back to the channels.
  let section = $derived<DraftSection>(
    !hasGroups && (app.draftSection === "zones" || app.draftSection === "scan_lists") ? "channels" : app.draftSection,
  );

  let sections = $derived<{ id: DraftSection; label: string }[]>([
    { id: "channels", label: `Channels (${draft?.codeplug.channels.length ?? 0})` },
    ...(hasGroups
      ? [
          { id: "zones" as const, label: `Zones (${draft?.codeplug.zones.length ?? 0})` },
          { id: "scan_lists" as const, label: `Scan lists (${draft?.codeplug.scan_lists.length ?? 0})` },
        ]
      : []),
    { id: "edits", label: `Edits (${draft?.operations.length ?? 0})` },
    { id: "import", label: "Profiles and CSV" },
    { id: "review", label: "Review and write" },
  ]);
</script>

<div class="editor">
  <div class="crumbs">
    <button class="link" onclick={() => app.openDraft(null)}>Saved edits</button>
  </div>

  {#if loadError && !draft}
    <div class="msg error">{loadError}</div>
    <div class="row" style="margin-top: 8px">
      <button onclick={reload}>Try again</button>
      <button class="danger" onclick={remove}>Delete these edits</button>
    </div>
  {:else if !draft}
    <p class="muted">Loading draft...</p>
  {:else}
    <header class="panel head">
      <div class="title-row">
        <input
          class="name"
          type="text"
          value={draft.name}
          aria-label="Draft name"
          title="Draft name; edit to rename"
          onchange={rename}
          onkeydown={(e) => e.key === "Enter" && e.currentTarget.blur()}
        />
        <span class="status" aria-live="polite">
          {#if pending}<span class="muted">Saving...</span>{/if}
        </span>
        <span class="spacer"></span>
        <button onclick={() => undo(1)} disabled={!draft.operations.length || pending > 0}>Undo last change</button>
        <button class="danger" onclick={remove}>Delete these edits</button>
      </div>
      <div class="meta muted">
        {draft.identity.model ?? ""} {draft.identity.firmware ?? ""}
        <span class="sep">|</span> from <span class="mono">{draft.base_backup}</span>
        <span class="sep">|</span> {draft.operations.length} edit{draft.operations.length === 1 ? "" : "s"}
        <span class="sep">|</span> updated {when(draft.updated_utc)}
      </div>
      <div class="sections" role="tablist" aria-label="Draft sections">
        {#each sections as s (s.id)}
          <button
            role="tab"
            class="section"
            class:active={section === s.id}
            class:review={s.id === "review"}
            aria-selected={section === s.id}
            onclick={() => (app.draftSection = s.id)}>{s.label}</button
          >
        {/each}
      </div>
    </header>

    {#if actionError}
      <div class="msg error action-error" role="alert">
        <span>{actionError}</span>
        <button class="small" onclick={() => (actionError = "")}>Dismiss</button>
      </div>
    {/if}
    {#if loadError}
      <div class="msg error">{loadError}</div>
    {/if}
    {#if draft.codeplug.problems.length}
      <div class="msg warn">Decoding problems: {draft.codeplug.problems.join("; ")}</div>
    {/if}

    <div class="body">
      {#if section === "channels"}
        <ChannelTable codeplug={draft.codeplug} {radio} {run} />
      {:else if section === "zones"}
        <GroupEditor kind="zone" groups={draft.codeplug.zones} channels={draft.codeplug.channels as Channel[]} {run} />
      {:else if section === "scan_lists"}
        <GroupEditor kind="scan_list" groups={draft.codeplug.scan_lists} channels={draft.codeplug.channels as Channel[]} {run} />
      {:else if section === "edits"}
        <section class="panel">
          <div class="panel-head">
            <h3>Edits in this draft</h3>
            <span class="muted">Replayed in order on the base backup. {radio === "rt950" ? "Channel numbers are slots and never change." : "Channel numbers are as they were when each edit was made."}</span>
            <button onclick={() => undo(1)} disabled={!draft.operations.length || pending > 0}>Undo last change</button>
          </div>
          {#if !draft.operations.length}
            <p class="empty">No edits yet. The draft is identical to its backup.</p>
          {:else}
            <table class="grid">
              <thead><tr><th class="num">#</th><th>Edit</th><th></th></tr></thead>
              <tbody>
                {#each draft.operations as op, i (i)}
                  {@const later = draft.operations.length - i}
                  <tr>
                    <td class="num muted">{i + 1}</td>
                    <td class="wrap">
                      {describeOp(op, radio)}
                      <details>
                        <summary class="muted">JSON</summary>
                        <pre class="mono">{JSON.stringify(op, null, 2)}</pre>
                      </details>
                    </td>
                    <td class="undo-cell">
                      <button class="small" onclick={() => undo(later)} disabled={pending > 0}>
                        {later === 1 ? "Undo" : `Undo this and ${later - 1} later`}
                      </button>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          {/if}
        </section>
      {:else if section === "import"}
        <ProfileCsv {id} {radio} {act} />
      {:else}
        <Review {draft} {radio} />
      {/if}
    </div>
  {/if}
</div>

<style>
  .crumbs {
    margin-bottom: 8px;
  }
  .head {
    padding: 10px 12px 0;
  }
  .title-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .name {
    font-size: 15px;
    font-weight: 600;
    height: 30px;
    width: min(420px, 100%);
    border-color: transparent;
    background: transparent;
    padding: 0 4px;
    margin-left: -5px;
  }
  .name:hover {
    border-color: var(--border-strong);
  }
  .name:focus {
    background: var(--panel);
  }
  .spacer {
    flex: 1;
  }
  .meta {
    margin: 2px 0 8px;
    font-size: 12px;
  }
  .sep {
    margin: 0 6px;
    color: var(--border-strong);
  }
  .sections {
    display: flex;
    flex-wrap: wrap;
    margin: 0 -12px;
    padding: 0 6px;
    border-top: 1px solid var(--border);
  }
  .section {
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    background: none;
    height: 34px;
    color: var(--muted);
  }
  .section:hover:not(:disabled) {
    background: var(--sunken);
    color: var(--text);
  }
  .section.active {
    color: var(--text);
    font-weight: 600;
    border-bottom-color: var(--accent);
  }
  .section.review {
    margin-left: auto;
    color: var(--accent);
    font-weight: 600;
  }
  .action-error {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    justify-content: space-between;
    margin-top: 8px;
    position: sticky;
    top: 48px;
    z-index: 5;
  }
  .body {
    margin-top: 12px;
  }
  .undo-cell {
    width: 1%;
    text-align: right;
  }
  details summary {
    cursor: pointer;
    font-size: 11px;
    margin-left: 6px;
  }
  details pre {
    margin: 4px 0;
    white-space: pre-wrap;
  }
  .editor > .msg {
    margin-top: 8px;
  }
</style>
