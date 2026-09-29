<script lang="ts">
  import type { TestingRunView } from '$lib/testing-results';
  import TestingPlanProgress from './TestingPlanProgress.svelte';

  let { run, onOpen }: { run: TestingRunView; onOpen?: () => void } = $props();
</script>

<article class="border-b border-[#e6e6e0] px-4 py-4" aria-label="Testing run summary">
  <div class="min-w-0"><p class="text-[10px] font-bold uppercase tracking-[.14em] text-[#8fa296]">Testing run</p><h3 class="mt-1 truncate text-sm font-semibold text-[#30362f]">Arazzo workflow verification</h3></div>
  <div class="mt-3 grid grid-cols-2 gap-2 text-[11px] text-[#686b63] sm:grid-cols-4">
    <div><span class="block text-[9px] uppercase tracking-wide text-[#9a9c94]">Workflows</span><strong>{run.workflowCounts.total}</strong></div>
    <div><span class="block text-[9px] uppercase tracking-wide text-[#9a9c94]">Passed</span><strong class="text-[#2d7354]">{run.workflowCounts.passed}</strong></div>
    <div><span class="block text-[9px] uppercase tracking-wide text-[#9a9c94]">Failed</span><strong class="text-[#a24037]">{run.workflowCounts.failed}</strong></div>
    <div><span class="block text-[9px] uppercase tracking-wide text-[#9a9c94]">Phase</span><strong>{run.phase ?? '—'}</strong></div>
  </div>
  {#if run.blockingReason}<p class="mt-3 border-t border-[#ece8dc] pt-3 text-xs leading-5 text-[#76554f]">{run.blockingReason}</p>{/if}
  {#if run.currentLabel && run.status === 'RUNNING'}<p class="mt-3 flex items-center gap-2 border-t border-[#ece8dc] pt-3 text-xs text-[#555950]"><span class="size-1.5 animate-pulse rounded-full bg-[#2d7354]"></span>{run.currentLabel}</p>{/if}
  {#if run.repair}<p class="mt-2 text-[11px] text-[#777970]">Repair {run.repair.status ?? 'active'} · {run.repair.attemptCount ?? 0} attempts · {run.repair.acceptedCount ?? 0} accepted</p>{/if}
  <TestingPlanProgress plans={run.plans} total={run.planTotal} />
  {#if onOpen}<button class="focus-ring mt-3 rounded-lg border border-[#cddbd1] px-2.5 py-1.5 text-[11px] font-semibold text-[#2d674b] hover:bg-[#f1f7f3]" onclick={onOpen} aria-label="Open testing results">Open Testing Results</button>{/if}
</article>
