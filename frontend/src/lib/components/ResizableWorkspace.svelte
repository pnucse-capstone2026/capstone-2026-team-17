<script lang="ts">
  import { onMount } from 'svelte';
  import type { Snippet } from 'svelte';
  import {
    clampSidebarSize,
    DEFAULT_SIDEBAR_PERCENT,
    sidebarSizeBounds
  } from '$lib/resizable-pane';

  let {
    sidebarOpen,
    children,
    sidebar
  }: {
    sidebarOpen: boolean;
    children: Snippet;
    sidebar?: Snippet;
  } = $props();

  let container: HTMLDivElement;
  let sidebarPercent = $state(DEFAULT_SIDEBAR_PERCENT);
  let dragging = $state(false);
  let minimum = $state(22);
  let maximum = $state(68);

  onMount(() => {
    const observer = new ResizeObserver(() => updateBounds());
    observer.observe(container);
    updateBounds();
    return () => observer.disconnect();
  });

  function updateBounds() {
    if (!container) return;
    const bounds = sidebarSizeBounds(container.clientWidth);
    minimum = bounds.minimum;
    maximum = bounds.maximum;
    sidebarPercent = clampSidebarSize(sidebarPercent, container.clientWidth);
  }

  function updateFromPointer(event: PointerEvent) {
    if (!container) return;
    const bounds = container.getBoundingClientRect();
    const next = ((bounds.right - event.clientX) / bounds.width) * 100;
    sidebarPercent = clampSidebarSize(next, bounds.width);
  }

  function beginResize(event: PointerEvent) {
    if (event.button !== 0) return;
    dragging = true;
    const handle = event.currentTarget as HTMLDivElement;
    handle.setPointerCapture(event.pointerId);
    updateFromPointer(event);
  }

  function moveResize(event: PointerEvent) {
    if (dragging) updateFromPointer(event);
  }

  function endResize(event: PointerEvent) {
    dragging = false;
    const handle = event.currentTarget as HTMLDivElement;
    if (handle.hasPointerCapture(event.pointerId)) {
      handle.releasePointerCapture(event.pointerId);
    }
  }

  function resizeWithKeyboard(event: KeyboardEvent) {
    const step = event.shiftKey ? 8 : 2;
    let next = sidebarPercent;
    if (event.key === 'ArrowLeft') next += step;
    else if (event.key === 'ArrowRight') next -= step;
    else if (event.key === 'Home') next = minimum;
    else if (event.key === 'End') next = maximum;
    else return;
    event.preventDefault();
    sidebarPercent = Math.min(maximum, Math.max(minimum, next));
  }
</script>

<div
  bind:this={container}
  class="relative flex min-h-0 min-w-0 flex-1 overflow-hidden {dragging ? 'cursor-col-resize select-none' : ''}"
>
  <div class="flex h-full min-h-0 min-w-0 flex-1 overflow-hidden">
    {@render children()}
  </div>

  {#if sidebarOpen && sidebar}
    <!-- A focusable ARIA separator is interactive even though Svelte classifies the role as static. -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <div
      class="group relative z-30 w-2 shrink-0 cursor-col-resize touch-none bg-[#deded7]/60 outline-none transition-colors hover:bg-[#9eb6a6] focus-visible:bg-[#78a88a]"
      role="separator"
      aria-label="Resize artifact panel"
      aria-orientation="vertical"
      aria-valuemin={Math.round(minimum)}
      aria-valuemax={Math.round(maximum)}
      aria-valuenow={Math.round(sidebarPercent)}
      tabindex="0"
      onpointerdown={beginResize}
      onpointermove={moveResize}
      onpointerup={endResize}
      onpointercancel={endResize}
      onkeydown={resizeWithKeyboard}
    >
      <span class="absolute left-1/2 top-1/2 h-9 w-1 -translate-x-1/2 -translate-y-1/2 rounded-full bg-[#a9ada5] transition-colors group-hover:bg-[#527961] group-focus-visible:bg-[#24553d]"></span>
    </div>
    <div
      class="h-full min-h-0 min-w-0 shrink-0 overflow-hidden"
      style={`width: ${sidebarPercent}%`}
    >
      {@render sidebar()}
    </div>
  {/if}
</div>
