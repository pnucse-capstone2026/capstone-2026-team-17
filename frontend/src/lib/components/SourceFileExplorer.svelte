<script lang="ts">
  import { ChevronDown, ChevronRight, FileCode2, Folder, FolderOpen, Maximize2 } from '@lucide/svelte';
  import type { FileArtifactSnapshot, LiveSourceFile } from '$lib/types';
  import { buildSourceFileTree, visibleSourceTreeRows } from '$lib/source-file-tree';

  type SourceFile = FileArtifactSnapshot['files'][number] | LiveSourceFile;

  let {
    files,
    selectedFile,
    mode = 'sidebar',
    onSelect
  }: {
    files: SourceFile[];
    selectedFile: string;
    mode?: 'sidebar' | 'modal';
    onSelect: (path: string) => void;
  } = $props();

  let collapsedDirectories = $state<Set<string>>(new Set());
  let rows = $derived(buildSourceFileTree(files));
  let visibleRows = $derived(visibleSourceTreeRows(rows, collapsedDirectories));
  let writingCount = $derived(
    files.filter((file) => 'status' in file && file.status === 'writing').length
  );

  function toggleDirectory(path: string) {
    const next = new Set(collapsedDirectories);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    collapsedDirectories = next;
  }
</script>

<nav
  class="scrollbar-thin select-none overflow-auto py-0.5 {mode === 'modal'
    ? 'h-full bg-[#202722] text-[#d7dfd8]'
    : 'max-h-[calc(100dvh-15rem)]'}"
  aria-label="Source files"
>
  <div class="sticky top-0 z-10 flex h-5 items-center justify-between gap-1.5 px-2 {mode === 'modal' ? 'bg-[#202722]' : 'bg-[#f5f7f3]'}">
    <p class="text-[8px] font-bold uppercase tracking-[.12em] {mode === 'modal' ? 'text-[#aab9ae]' : 'text-[#83887e]'}">Explorer</p>
    {#if writingCount > 0}
      <span class="flex items-center gap-1 text-[8px] font-semibold {mode === 'modal' ? 'text-[#8fd0a6]' : 'text-[#3c7b57]'}">
        <span class="size-1 animate-pulse rounded-full bg-[#45a76b]"></span>
        {writingCount} writing
      </span>
    {:else}
      <span class="text-[8px] {mode === 'modal' ? 'text-[#87978b]' : 'text-[#92978e]'}">{files.length} files</span>
    {/if}
  </div>
  <div>
    {#each visibleRows as row (`${row.kind}:${row.path}`)}
      {#if row.kind === 'directory'}
        <button
          class="focus-ring flex h-5 w-full items-center gap-0.5 pr-1 text-left font-medium leading-none {mode === 'modal' ? 'text-[#b8c3bb] hover:bg-white/[.06]' : 'text-[#666c64] hover:bg-[#e5e9e4]'}"
          style={`padding-left: ${2 + row.depth * 10}px`}
          onclick={() => toggleDirectory(row.path)}
          aria-expanded={!collapsedDirectories.has(row.path)}
          title={row.path}
        >
          {#if collapsedDirectories.has(row.path)}
            <ChevronRight size={10} class="shrink-0 opacity-75" />
            <Folder size={11} class="ml-0.5 shrink-0 opacity-80" />
          {:else}
            <ChevronDown size={10} class="shrink-0 opacity-75" />
            <FolderOpen size={11} class="ml-0.5 shrink-0 opacity-80" />
          {/if}
          <span class="ml-0.5 min-w-0 flex-1 truncate" style="font-size: 11px; line-height: 15px;">{row.label}</span>
        </button>
      {:else}
        <button
          class="focus-ring flex h-5 w-full items-center gap-1 pr-1.5 text-left font-normal leading-none transition disabled:cursor-wait disabled:opacity-60 {selectedFile === row.path
            ? mode === 'modal'
              ? 'bg-[#354d40] text-white'
              : 'bg-[#d4e7da] text-[#214c37]'
            : mode === 'modal'
              ? 'text-[#c5cec7] hover:bg-white/[.06]'
              : 'text-[#555c54] hover:bg-[#e5e9e4]'}"
          style={`padding-left: ${14 + row.depth * 10}px`}
          onclick={() => onSelect(row.path)}
          disabled={'exists' in row.file && !row.file.exists}
          aria-current={selectedFile === row.path ? 'true' : undefined}
          title={row.path}
        >
          <FileCode2 size={10} class="shrink-0 opacity-85" />
          <span class="min-w-0 flex-1 truncate font-mono" style="font-size: 11px; line-height: 15px;">{row.label}</span>
          {#if 'status' in row.file && row.file.status === 'writing'}
            <span class="size-1 shrink-0 animate-pulse rounded-full bg-[#45a76b]" title="Writing"></span>
          {/if}
          {#if mode === 'sidebar'}
            <Maximize2 size={8} class="shrink-0 opacity-40" />
          {/if}
        </button>
      {/if}
    {/each}
  </div>
</nav>
