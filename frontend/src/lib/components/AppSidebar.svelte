<script lang="ts">
  import { ChevronLeft, ChevronRight, Layers3, Plus, Search } from '@lucide/svelte';
  import type { WorkspaceApp } from '$lib/types';
  import { cn } from '$lib/utils';
  import { Button } from '$lib/components/ui/button';

  let {
    apps,
    currentAppId,
    collapsed,
    onSelect,
    onNew,
    onToggle
  }: {
    apps: WorkspaceApp[];
    currentAppId?: string;
    collapsed: boolean;
    onSelect: (appId: string) => void;
    onNew: () => void;
    onToggle: () => void;
  } = $props();

  let query = $state('');
  let filtered = $derived(
    apps.filter((app) => app.title.toLowerCase().includes(query.trim().toLowerCase()))
  );
</script>

<div
  class={cn(
    'relative z-20 h-full min-h-0 shrink-0 overflow-visible transition-[width] duration-200',
    collapsed ? 'w-[68px]' : 'w-[260px]'
  )}
>
  <aside class="flex h-full min-h-0 w-full flex-col overflow-hidden border-r border-[#deded7] bg-[#e9e9e4]">
  <div class="flex h-16 items-center gap-3 px-4">
    <div class="flex size-9 shrink-0 items-center justify-center rounded-xl bg-[#1f5d45] text-white shadow-sm">
      <Layers3 size={18} />
    </div>
    {#if !collapsed}
      <div class="min-w-0 flex-1">
        <strong class="block truncate text-sm tracking-tight">EasyDep</strong>
        <span class="block text-[11px] text-[#777970]">Development workspace</span>
      </div>
    {/if}
  </div>

  <div class="px-3">
    <Button class={cn('w-full', collapsed && 'px-0')} onclick={onNew}>
      <Plus size={16} />
      {#if !collapsed}<span>New application</span>{/if}
    </Button>
  </div>

  {#if !collapsed}
    <div class="relative mx-3 mt-4">
      <Search class="absolute left-3 top-2.5 text-[#8b8d84]" size={15} />
      <input
        bind:value={query}
        class="focus-ring h-9 w-full rounded-xl border border-[#d7d8d0] bg-white/70 pl-9 pr-3 text-xs placeholder:text-[#94968e]"
        placeholder="Search recent applications"
        aria-label="Search recent applications"
      />
    </div>
  {/if}

  <nav class="scrollbar-thin mt-4 min-h-0 flex-1 overflow-y-auto px-2" aria-label="Recent applications">
    {#if !collapsed}
      <p class="px-2 pb-2 text-[10px] font-bold uppercase tracking-[.14em] text-[#85877e]">Recent work</p>
    {/if}
    {#each filtered as item (item.app_id)}
      <button
        class={cn(
          'focus-ring mb-1 flex w-full items-center rounded-xl text-left transition-colors',
          collapsed ? 'h-11 justify-center px-0' : 'gap-3 px-3 py-2.5',
          currentAppId === item.app_id ? 'bg-white shadow-sm' : 'hover:bg-white/60'
        )}
        title={item.title}
        onclick={() => onSelect(item.app_id)}
      >
        <span
          class={cn(
            'size-2 shrink-0 rounded-full',
            item.command?.status === 'RUNNING' || item.command?.status === 'QUEUED'
              ? 'pulse-dot bg-[#2f8a62]'
              : item.command?.status === 'FAILED'
                ? 'bg-[#b84b42]'
                : 'bg-[#aeb0a7]'
          )}
        ></span>
        {#if !collapsed}
          <span class="min-w-0">
            <span class="block truncate text-xs font-medium text-[#30312d]">{item.title}</span>
            <span class="mt-0.5 block text-[10px] text-[#85877e]">{item.current_stage}</span>
          </span>
        {/if}
      </button>
    {/each}
  </nav>

  </aside>

  <button
    class="focus-ring absolute -right-3 top-20 z-30 flex size-6 items-center justify-center rounded-full border border-[#d1d2ca] bg-white text-[#66685f] shadow-sm"
    onclick={onToggle}
    aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
  >
    {#if collapsed}<ChevronRight size={13} />{:else}<ChevronLeft size={13} />{/if}
  </button>
</div>
