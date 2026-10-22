<script lang="ts">
  import { app } from "../lib/app.svelte";
  import type { AnyChannel, Codeplug, Group, Job, WriteStep } from "../lib/api";
  import { diffByNumber, progressOf, timeOfDay } from "../lib/format";
  import ChangeList from "./ChangeList.svelte";
  import KeyValue from "./KeyValue.svelte";

  /** draftChannels: the draft's channels, to name zone and scan list members in a verify result. */
  let {
    job,
    draftChannels = [],
    onclose,
  }: { job: Job; draftChannels?: AnyChannel[]; onclose?: () => void } = $props();

  const title: Record<string, string> = {
    read: "Read radio",
    write: "Write to radio",
    "dry-run": "Dry run",
    verify: "Verify radio",
  };

  let logEl = $state<HTMLPreElement | null>(null);
  let progress = $derived(job.status === "running" ? progressOf(job.log) : null);
  let result = $derived(job.result ?? {});
  let steps = $derived((Array.isArray(result.steps) ? result.steps : []) as WriteStep[]);
  let differences = $derived(
    (result.differences ?? {}) as Partial<Record<keyof Omit<Codeplug, "problems">, { radio: unknown[]; draft: unknown[] }>>,
  );

  $effect(() => {
    void job.log.length;
    if (logEl) logEl.scrollTop = logEl.scrollHeight;
  });

  function stepDetails(s: WriteStep): Record<string, unknown> {
    const { step: _step, ...rest } = s;
    return rest;
  }
</script>

<section class="job panel" aria-live="polite">
  <div class="panel-head">
    <h3>{title[job.kind] ?? job.kind}: {app.status?.radios[job.radio] ?? job.radio} on {job.port}</h3>
    {#if job.status === "running"}
      <span class="badge warn">Running</span>
    {:else if job.status === "failed"}
      <span class="badge bad">Failed</span>
    {:else if job.kind === "write" && result.passed === false}
      <span class="badge bad">Finished, not verified</span>
    {:else if job.kind === "verify" && result.matches_draft === false}
      <span class="badge bad">Differs from draft</span>
    {:else}
      <span class="badge ok">Done</span>
    {/if}
    <span class="muted">
      started {timeOfDay(job.started_utc)}{job.finished_utc ? `, finished ${timeOfDay(job.finished_utc)}` : ""}
    </span>
    {#if job.draft && app.draft !== job.draft}
      <button class="small" onclick={() => job.draft && app.openDraft(job.draft, "review", job.radio)}>Open the edits</button>
    {/if}
    {#if onclose}<button class="small" onclick={onclose}>Close</button>{/if}
  </div>
  <div class="panel-body stack">
    {#if progress}
      <div class="row">
        <progress max={progress.total} value={progress.done}></progress>
        <span class="muted num">{progress.done} / {progress.total} pages</span>
      </div>
    {/if}

    <!-- The log scrolls, so it takes focus for keyboard scrolling. -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <pre class="log" bind:this={logEl} tabindex="0" role="log" aria-label="Job log">{job.log.join("\n") || "Starting..."}</pre>

    {#if job.status === "failed"}
      <div class="msg error">{job.error}</div>
      {#if typeof result.partial_backup === "string"}
        <p>Partial backup kept at <code>{result.partial_backup}</code>.</p>
      {/if}
    {:else if job.status === "done"}
      {#if job.kind === "read"}
        <div class="row">
          <span>Read finished and saved as <code>{String(result.backup)}</code>.</span>
          <button class="small" onclick={() => { app.radio = job.radio; app.view = "channels"; }}>Show channels</button>
        </div>
        <details>
          <summary class="muted">Details</summary>
          <KeyValue data={result} skip={["backup"]} />
        </details>
      {:else if job.kind === "dry-run"}
        {#if result.checks_passed === false}
          <div class="msg error">The dry run found a problem. Check the steps below. Nothing was written.</div>
        {:else}
          <div class="msg ok">Dry run passed: every check against the radio passed. Nothing was written.</div>
        {/if}
      {:else if job.kind === "write"}
        {#if result.passed}
          <div class="msg ok">Write passed: the changes were written, read back, and a fresh read of the radio matches the plan.</div>
        {:else}
          <div class="msg error">The write finished but did not verify. Check the steps below and read the radio again.</div>
        {/if}
      {:else if job.kind === "verify"}
        {#if result.matches_draft}
          <div class="msg ok">The radio matches these edits{job.radio === "dm32" ? " (channels, zones and scan lists)" : ""}.</div>
        {:else}
          <div class="msg error">The radio differs from these edits (edits shown as before, radio as after).</div>
          {#each ["channels", "zones", "scan_lists"] as const as kind (kind)}
            {@const d = differences[kind]}
            {#if d}
              {@const diff = diffByNumber(d.draft as (AnyChannel | Group)[], d.radio as (AnyChannel | Group)[])}
              <ChangeList
                {kind}
                radio={job.radio}
                added={diff.added}
                removed={diff.removed}
                changed={diff.changed}
                beforeChannels={(differences.channels?.draft as AnyChannel[] | undefined) ?? draftChannels}
                afterChannels={(differences.channels?.radio as AnyChannel[] | undefined) ?? draftChannels}
              />
            {/if}
          {/each}
        {/if}
        <p class="muted">Backup taken: <code>{String(result.backup)}</code></p>
      {/if}
    {/if}

    {#if job.status !== "running" && steps.length}
        <h4>Steps</h4>
        <div class="table-wrap">
          <table class="grid">
            <thead><tr><th>Step</th><th>Details</th></tr></thead>
            <tbody>
              {#each steps as s, i (i)}
                <tr>
                  <td class:bad={s.step === "error"}>{s.step.replace(/_/g, " ")}</td>
                  <td class="wrap"><KeyValue data={stepDetails(s)} /></td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
        {#if result.report_dir}
          <p class="muted">Report: <code>{String(result.report_dir)}</code></p>
        {/if}
    {/if}
  </div>
</section>

<style>
  .log {
    margin: 0;
    padding: 8px;
    max-height: 16em;
    min-height: 4em;
    overflow: auto;
    font-family: var(--mono);
    font-size: 12px;
    background: var(--sunken);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    white-space: pre-wrap;
  }
  progress {
    width: 280px;
    height: 10px;
    accent-color: var(--accent);
  }
  td.bad {
    color: var(--danger);
    font-weight: 600;
  }
  details summary {
    cursor: pointer;
  }
  h4 {
    font-size: 12px;
    color: var(--muted);
  }
</style>
