<script lang="ts">
  let { data, skip = [] }: { data: Record<string, unknown>; skip?: string[] } = $props();

  const label = (k: string) => k.replace(/_/g, " ");

  function show(v: unknown): string {
    if (v === null || v === undefined) return "none";
    if (typeof v === "boolean") return v ? "yes" : "no";
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
  }
</script>

<dl class="kv">
  {#each Object.entries(data).filter(([k]) => !skip.includes(k)) as [k, v] (k)}
    <dt>{label(k)}</dt>
    <dd class:mono={typeof v === "object" && v !== null}>{show(v)}</dd>
  {/each}
</dl>
