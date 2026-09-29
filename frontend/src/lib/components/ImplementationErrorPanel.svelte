<script lang="ts">
  import { AlertTriangle, ChevronDown, Clipboard, Check } from '@lucide/svelte';

  let {
    errors
  }: {
    errors: string[];
  } = $props();

  let copied = $state(false);

  let combined = $derived(errors.join('\n\n---\n\n'));

  async function copyLog() {
    try {
      await navigator.clipboard.writeText(combined);
      copied = true;
      window.setTimeout(() => (copied = false), 1600);
    } catch {
      copied = false;
    }
  }
</script>

{#if errors.length}
  <section
    class="mx-auto mt-3 mb-0 w-full rounded-xl border border-[#eccbc7] bg-[#fff8f7] px-3 pt-2.5 pb-2 text-xs text-[#633e3a]"
    aria-label="Implementation error log"
  >
    <details open>
      <summary class="flex cursor-pointer list-none items-center gap-2 py-1">
        <AlertTriangle size={15} class="shrink-0 text-[#a8433a]" />
        <span class="min-w-0 flex-1 font-semibold">Error log</span>
        <ChevronDown size={14} class="shrink-0" />
      </summary>
      <div class="mt-0 border-t border-[#f0d8d4] pt-2">
        <div class="max-h-72 overflow-auto rounded-lg border border-[#ecd5d1] bg-[#fffdfc] px-2 pb-0.5 pt-2">
          {#each errors as entry, index}
            {#if index > 0}<div class="my-2 border-t border-[#f0e0dd]"></div>{/if}
            <pre class="whitespace-pre-wrap break-words font-mono text-[10px] leading-4 text-[#5e4642]">{entry}</pre>
          {/each}
        </div>
        <div class="mt-0 flex justify-end">
          <button
            type="button"
            class="inline-flex items-center gap-1 rounded-md border border-[#e8c8c3] bg-white px-2 py-1 text-[10px] font-medium text-[#7b4d47] hover:bg-[#fff1ef]"
            onclick={copyLog}
          >
            {#if copied}<Check size={11} /> Copied{:else}<Clipboard size={11} /> Copy log{/if}
          </button>
        </div>
      </div>
    </details>
  </section>
{/if}
