<script lang="ts">
  import { app, SHORT_NAMES } from "../lib/app.svelte";
  import {
    api,
    errorText,
    type AnyChannel,
    type Changed,
    type Channel,
    type DraftView,
    type Group,
    type Preview,
    type RadioId,
  } from "../lib/api";
  import { channelsDiff, groupsDiff } from "../lib/format";
  import ChangeList from "./ChangeList.svelte";
  import JobView from "./JobView.svelte";
  import PortControl from "./PortControl.svelte";

  let { draft, radio }: { draft: DraftView; radio: RadioId } = $props();

  interface Base {
    channels: Channel[];
    zones: Group[];
    scan_lists: Group[];
  }

  let preview = $state<Preview | null>(null);
  let previewError = $state("");
  let base = $state<Base | null>(null);
  let slotView = $state(false);
  let confirmed = $state(false);
  let starting = $state(false);
  let startError = $state("");

  let short = $derived(SHORT_NAMES[radio]);
  let rt950 = $derived(radio === "rt950");
  /** The RT-950 PRO is written in 4 KB memory sectors, the DM-32UV in pages. */
  let unit = $derived(rt950 ? "memory sector" : "page");

  // Reload the preview whenever the draft changes.
  $effect(() => {
    const id = draft.id;
    void draft.updated_utc;
    void draft.operations.length;
    let cancelled = false;
    api
      .preview(id)
      .then((p) => {
        if (cancelled) return;
        if (p.preview_id !== preview?.preview_id) confirmed = false;
        preview = p;
        previewError = "";
      })
      .catch((e) => {
        if (!cancelled) previewError = errorText(e);
      });
    return () => {
      cancelled = true;
    };
  });

  // DM-32UV only: the base backup names channels as they were, so changes can be paired by name.
  $effect(() => {
    const ref = draft.base_backup;
    if (rt950) {
      base = null;
      return;
    }
    api
      .backup(ref)
      .then((b) => (base = b as unknown as Base))
      .catch(() => (base = null));
  });

  let allowWrites = $derived(!!app.status?.allow_writes);
  let jobs = $derived(app.jobsForDraft(draft.id));
  let latest = $derived(jobs[0]);
  let lastWrite = $derived(jobs.find((j) => j.kind === "write"));
  let writeFinished = $derived(!!lastWrite && lastWrite.status !== "running");
  let blocked = $derived(app.busy || starting);

  type Item = AnyChannel | Group;
  interface Section {
    added: Item[];
    removed: Item[];
    changed: Changed<Item>[];
    moved: Changed<Item>[];
  }

  // DM-32UV: channels as they were, named as they are now when they were only renamed or moved, so a
  // renamed member does not show as dropped and re-added in zones and scan lists.
  let beforeChannels = $derived.by((): AnyChannel[] => {
    if (!base) return [];
    const now = draft.codeplug.channels as Channel[];
    const after = new Map(channelsDiff(base.channels, now).pairs.map(([b, a]) => [b.number, a.name]));
    return base.channels.map((c) => ({ ...c, name: after.get(c.number) ?? c.name }));
  });

  // The DM-32UV renumbers channels after a delete, so its changes are paired by name by default. The
  // RT-950 PRO never renumbers, so the service's slot-by-slot comparison is already the clear one.
  let sections = $derived.by((): Record<"channels" | "zones" | "scan_lists", Section> | null => {
    if (!preview) return null;
    const c = preview.changes;
    if (rt950 || slotView || !base) {
      return {
        channels: { added: c.channels_added, removed: c.channels_removed, changed: c.channels_changed, moved: [] },
        zones: { added: c.zones_added, removed: c.zones_removed, changed: c.zones_changed, moved: [] },
        scan_lists: { added: c.scan_lists_added, removed: c.scan_lists_removed, changed: c.scan_lists_changed, moved: [] },
      };
    }
    const cp = draft.codeplug;
    return {
      channels: channelsDiff(base.channels, cp.channels as Channel[]),
      zones: groupsDiff(base.zones, cp.zones),
      scan_lists: groupsDiff(base.scan_lists, cp.scan_lists),
    };
  });

  let counts = $derived.by(() => {
    if (!sections) return [];
    const out: string[] = [];
    const add = (n: number, what: string) => n && out.push(`${n} ${what}`);
    const ch = sections.channels;
    const parts: string[] = [];
    const part = (n: number, what: string) => n && parts.push(`${n} ${what}`);
    part(ch.added.length, "added");
    part(ch.changed.length, "changed");
    part(ch.removed.length, rt950 ? "removed (slot emptied)" : "removed");
    part(ch.moved.length, "renumbered");
    if (parts.length) out.push(`Channels: ${parts.join(", ")}`);
    const z = sections.zones;
    add(z.added.length + z.changed.length + z.removed.length + z.moved.length, "zones affected");
    const s = sections.scan_lists;
    add(s.added.length + s.changed.length + s.removed.length + s.moved.length, "scan lists affected");
    return out;
  });

  let kinds = $derived<("channels" | "zones" | "scan_lists")[]>(rt950 ? ["channels"] : ["channels", "zones", "scan_lists"]);

  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

  async function start(kind: "dry-run" | "write" | "verify") {
    if (!preview) return;
    starting = true;
    startError = "";
    try {
      const job =
        kind === "verify"
          ? await api.verify(draft.id, app.port[radio])
          : await api.write(draft.id, preview.preview_id, app.port[radio], kind === "dry-run");
      app.track(job);
      if (kind === "write") confirmed = false;
      void app.refreshStatus();
    } catch (e) {
      startError = errorText(e);
    } finally {
      starting = false;
    }
  }
</script>

<div class="stack">
  {#if previewError}
    <div class="msg error">{previewError}</div>
  {:else if !preview}
    <p class="muted">Working out what would change...</p>
  {:else}
    <section class="panel">
      <div class="panel-head">
        <h3>What writing these edits changes on the {short}</h3>
        <span class="muted">
          {preview.identity.model ?? ""}{preview.identity.firmware ? ` firmware ${preview.identity.firmware}` : ""}
        </span>
      </div>
      <div class="panel-body stack">
        {#if preview.nothing_to_write}
          <div class="msg info">These edits do not change anything on the radio yet. Make some changes first.</div>
        {:else}
          <p>
            <strong>{counts.join(", ") || "Byte-level changes only"}.</strong>
            <span class="muted">
              Compared with backup <span class="mono">{preview.base_backup}</span>. Writes
              {plural(preview.pages.length, unit)}{rt950 ? `: ${preview.pages.map((p) => p.address).join(", ")}` : ""}.
            </span>
          </p>
          {#if sections}
            {#each kinds as kind (kind)}
              <ChangeList
                {kind}
                {radio}
                added={sections[kind].added}
                removed={sections[kind].removed}
                changed={sections[kind].changed}
                moved={sections[kind].moved}
                beforeChannels={slotView ? (base?.channels ?? []) : beforeChannels}
                afterChannels={draft.codeplug.channels}
              />
            {/each}
          {/if}
          {#if rt950}
            <p class="muted">Compared slot by slot: channels on the RT-950 PRO never change number.</p>
          {:else}
            <label class="check muted">
              <input type="checkbox" bind:checked={slotView} disabled={!base} />
              Compare slot by slot (by channel, zone and scan list number, as stored in the radio)
            </label>
          {/if}
          <details open={rt950}>
            <summary>{rt950 ? "Memory sectors" : "Memory pages"} touched ({preview.pages.length})</summary>
            <table class="grid pages">
              <thead>
                <tr>
                  <th>{rt950 ? "Sector" : "Address"}</th>
                  {#if !rt950}<th>Tag</th>{/if}
                  <th class="num">Bytes changed</th>
                </tr>
              </thead>
              <tbody>
                {#each preview.pages as p (p.address)}
                  <tr>
                    <td class="mono">{p.address}</td>
                    {#if !rt950}<td class="mono">{p.tag}</td>{/if}
                    <td class="num">{p.bytes_changed}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </details>
        {/if}
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h3>Write to the {short}</h3>
        <span class="muted">Preview ID <span class="mono">{preview.preview_id}</span></span>
      </div>
      <div class="panel-body stack">
        <div class="row">
          <PortControl {radio} disabled={blocked} />
          <span class="spacer"></span>
          <button
            class:primary={!allowWrites}
            onclick={() => start("dry-run")}
            disabled={blocked || preview.nothing_to_write}
          >
            Dry run on the {short}
          </button>
        </div>
        <p class="muted hint">
          {#if rt950}
            Dry run reads the RT-950 PRO, checks that the changes replay on it exactly as previewed, then stops
            without writing.
          {:else}
            Dry run reads the radio, checks its model, firmware and memory match this draft's backup and that the
            change replays exactly as previewed, then stops without writing. About 4 minutes.
          {/if}
        </p>

        <div class="write-box" class:off={!allowWrites}>
          {#if !allowWrites}
            <p>
              <strong>Writing is turned off.</strong> The service was started without
              <code>--allow-writes</code>. Restart it with <code>radiosmith serve --allow-writes</code> to write this
              draft; dry runs work without it.
            </p>
            <button class="danger solid" disabled>Write to the {short}</button>
          {:else}
            <label class="check">
              <input type="checkbox" bind:checked={confirmed} disabled={blocked || preview.nothing_to_write} />
              I reviewed these changes
            </label>
            <div class="row">
              <button
                class="danger solid"
                onclick={() => start("write")}
                disabled={!confirmed || blocked || preview.nothing_to_write}
              >
                Write to the {short}
              </button>
              <span class="muted hint">
                {#if rt950}
                  Reads the radio first, writes only the {plural(preview.pages.length, "changed memory sector")}, then
                  reads it again to compare. Keep the radio on and connected.
                {:else}
                  Takes a full backup first, writes only the {plural(preview.pages.length, "changed page")}, reads each
                  back, then compares a second full backup. About 8 minutes; keep the radio on and connected.
                {/if}
              </span>
            </div>
          {/if}
        </div>

        {#if startError}<div class="msg error">{startError}</div>{/if}

        <div class="row verify" class:after-write={writeFinished}>
          {#if writeFinished}
            <span>The write has finished. Read the radio back and compare it with this draft:</span>
          {:else}
            <span class="muted">Check the radio against these edits at any time:</span>
          {/if}
          <button class:primary={writeFinished} onclick={() => start("verify")} disabled={blocked}>
            Check the {short} matches these edits
          </button>
        </div>
      </div>
    </section>
  {/if}

  {#if latest}
    <JobView job={latest} draftChannels={draft.codeplug.channels} />
  {/if}
</div>

<style>
  .hint {
    font-size: 12px;
    margin: 0;
    max-width: 80ch;
  }
  .write-box {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 12px;
    border: 1px solid var(--danger);
    border-radius: var(--radius);
    background: var(--danger-soft);
  }
  .write-box.off {
    border-color: var(--border);
    background: var(--sunken);
  }
  .write-box code {
    white-space: nowrap;
  }
  .write-box p {
    margin: 0;
  }
  .write-box .hint {
    color: var(--text);
    opacity: 0.8;
  }
  .write-box button {
    align-self: flex-start;
  }
  .verify {
    padding-top: 8px;
    border-top: 1px solid var(--border);
  }
  .verify.after-write {
    font-weight: 600;
  }
  details summary {
    cursor: pointer;
    color: var(--muted);
  }
  .pages {
    width: auto;
    margin-top: 6px;
    border: 1px solid var(--border);
  }
</style>
