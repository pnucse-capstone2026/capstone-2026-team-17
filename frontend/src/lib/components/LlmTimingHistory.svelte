<script lang="ts">
  import { LoaderCircle } from '@lucide/svelte';
  import { getEventLlmTimings } from '$lib/api';
  import { errorMessage } from '$lib/utils';

  const PAGE_SIZE = 20;

  let {
    appId,
    eventId,
    count
  }: {
    appId: string;
    eventId: number;
    count: number;
  } = $props();

  let open = $state(false);
  let timings = $state.raw<Array<Record<string, any>>>([]);
  let total = $state(0);
  let loading = $state(false);
  let loadError = $state('');
  let expanded = $state<Record<number, boolean>>({});
  let displayTotal = $derived(Math.max(count, total));

  async function loadMore() {
    if (loading || timings.length >= displayTotal) return;
    loading = true;
    loadError = '';
    try {
      const page = await getEventLlmTimings(appId, eventId, timings.length, PAGE_SIZE);
      if (page.event_id !== eventId) return;
      total = page.total;
      timings = [...timings, ...page.timings];
    } catch (reason) {
      loadError = errorMessage(reason);
    } finally {
      loading = false;
    }
  }

  function toggleHistory(domEvent: Event) {
    open = (domEvent.currentTarget as HTMLDetailsElement).open;
    if (open && timings.length === 0) void loadMore();
  }

  function toggleTiming(index: number, domEvent: Event) {
    expanded = {
      ...expanded,
      [index]: (domEvent.currentTarget as HTMLDetailsElement).open
    };
  }
</script>

<details class="rounded-lg border border-[#dfe3dc] bg-white px-3 py-2" ontoggle={toggleHistory}>
  <summary class="cursor-pointer font-semibold text-[#343831]">
    LLM raw responses ({displayTotal})
  </summary>
  {#if open}
    <div class="mt-2 space-y-2">
      {#each timings as timing, index (`${eventId}:${index}`)}
        <details
          class="rounded-md border border-[#e5e7e1] bg-[#fafbf8] px-2.5 py-2"
          ontoggle={(domEvent) => toggleTiming(index, domEvent)}
        >
          <summary class="cursor-pointer font-mono text-[10px] text-[#555950]">
            {index + 1}. {String(timing.operation ?? 'LLM')}
            · {String(timing.status ?? 'unknown')}
          </summary>
          {#if expanded[index]}
            {#if timing.reasoningContent}
              <h4 class="mb-1 mt-2 font-semibold text-[#555950]">Reasoning</h4>
              <pre class="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded bg-[#f2f3ef] p-2 text-[10px] leading-4">{String(timing.reasoningContent)}</pre>
            {/if}
            {#if timing.responseContent}
              <h4 class="mb-1 mt-2 font-semibold text-[#555950]">Response</h4>
              <pre class="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded bg-[#f2f3ef] p-2 text-[10px] leading-4">{String(timing.responseContent)}</pre>
            {/if}
            {#if Array.isArray(timing.schemaValidationErrors) && timing.schemaValidationErrors.length}
              <h4 class="mb-1 mt-2 font-semibold text-[#8a473f]">Schema validation</h4>
              <pre class="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-[#fff3f1] p-2 text-[10px] leading-4">{JSON.stringify(timing.schemaValidationErrors, null, 2)}</pre>
            {/if}
          {/if}
        </details>
      {/each}

      {#if loadError}
        <p class="rounded-md bg-[#fff3f1] p-2 text-[10px] text-[#8a473f]">{loadError}</p>
      {/if}
      {#if timings.length < displayTotal}
        <button
          class="focus-ring flex w-full items-center justify-center gap-1.5 rounded-md border border-[#dfe3dc] bg-[#f7f8f5] px-2 py-1.5 text-[10px] font-semibold text-[#555950] hover:bg-[#f0f2ed] disabled:cursor-wait disabled:opacity-60"
          onclick={loadMore}
          disabled={loading}
        >
          {#if loading}<LoaderCircle size={11} class="animate-spin" />{/if}
          {timings.length === 0 ? 'Load records' : `Load next ${Math.min(PAGE_SIZE, displayTotal - timings.length)}`}
        </button>
      {/if}
    </div>
  {/if}
</details>
