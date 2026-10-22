<script lang="ts">
  /**
   * A table cell text input that commits on Enter or when focus leaves, and
   * reverts on Escape. While focused it keeps what the user typed even if the
   * draft reloads underneath it.
   */
  let {
    value,
    oncommit,
    label,
    placeholder = "",
    maxlength,
    numeric = false,
    disabled = false,
    width = "100%",
  }: {
    value: string;
    /** Called with the new text when it differs from value. */
    oncommit: (text: string) => Promise<void> | void;
    label: string;
    placeholder?: string;
    maxlength?: number;
    numeric?: boolean;
    disabled?: boolean;
    width?: string;
  } = $props();

  let text = $state("");
  // Deliberately not reactive: the sync below should run only when value changes.
  let focused = false;
  let el = $state<HTMLInputElement | null>(null);

  $effect.pre(() => {
    const v = value;
    if (!focused) text = v;
  });

  async function commit() {
    if (text === value) return;
    const typed = text;
    await oncommit(typed);
    if (!focused) text = value;
  }

  function onkeydown(e: KeyboardEvent) {
    if (e.key === "Enter") {
      e.preventDefault();
      el?.blur();
    } else if (e.key === "Escape") {
      text = value;
      el?.blur();
    }
  }
</script>

<input
  bind:this={el}
  type="text"
  class="cell"
  class:numeric
  style:width
  bind:value={text}
  aria-label={label}
  {placeholder}
  {maxlength}
  {disabled}
  inputmode={numeric ? "decimal" : undefined}
  spellcheck="false"
  autocomplete="off"
  onfocus={() => (focused = true)}
  onblur={() => {
    focused = false;
    void commit();
  }}
  {onkeydown}
/>

<style>
  .cell {
    height: 22px;
    border-color: transparent;
    background: transparent;
    padding: 0 4px;
  }
  .cell:hover:not(:disabled) {
    border-color: var(--border-strong);
    background: var(--panel);
  }
  .cell:focus {
    background: var(--panel);
  }
  .cell::placeholder {
    color: var(--muted);
    opacity: 0.7;
  }
  .numeric {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
</style>
