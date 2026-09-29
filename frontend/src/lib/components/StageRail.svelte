<script lang="ts">
  import { Check, Circle, LoaderCircle } from '@lucide/svelte';
  import type { Stage, WorkspaceCommand } from '$lib/types';
  import { cn } from '$lib/utils';

  let { current, command }: { current: Stage; command?: WorkspaceCommand | null } = $props();
  const steps: Array<{ id: Stage; label: string }> = [
    { id: 'requirements', label: 'Requirements' },
    { id: 'design', label: 'Design' },
    { id: 'implementation', label: 'Implementation' },
    { id: 'testing', label: 'Testing' }
  ];
  let activeIndex = $derived(Math.max(0, steps.findIndex((step) => step.id === current)));
</script>

<div class="flex items-center gap-1.5 overflow-x-auto" aria-label="Development stages">
  {#each steps as step, index}
    <div class="flex items-center gap-1.5">
      <div
        class={cn(
          'flex h-7 items-center gap-1.5 rounded-full px-2.5 text-[11px] font-semibold',
          index < activeIndex && 'bg-[#e1eee7] text-[#246347]',
          index === activeIndex && 'bg-[#242622] text-white',
          index > activeIndex && 'bg-[#eee] text-[#85877e]'
        )}
      >
        {#if index < activeIndex}
          <Check size={12} />
        {:else if index === activeIndex && ['RUNNING', 'QUEUED'].includes(command?.status ?? '')}
          <LoaderCircle class="animate-spin" size={12} />
        {:else}
          <Circle size={9} fill="currentColor" />
        {/if}
        {step.label}
      </div>
      {#if index < steps.length - 1}<span class="h-px w-3 bg-[#d7d8d1]"></span>{/if}
    </div>
  {/each}
</div>
