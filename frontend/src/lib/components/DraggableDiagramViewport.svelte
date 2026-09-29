<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    label,
    children
  }: {
    label: string;
    children: Snippet;
  } = $props();

  let viewport: HTMLDivElement;
  let dragging = $state(false);
  let pointerId: number | null = null;
  let startX = 0;
  let startY = 0;
  let startScrollLeft = 0;
  let startScrollTop = 0;

  function beginDrag(event: PointerEvent) {
    if (event.button !== 0) return;
    dragging = true;
    pointerId = event.pointerId;
    startX = event.clientX;
    startY = event.clientY;
    startScrollLeft = viewport.scrollLeft;
    startScrollTop = viewport.scrollTop;
    viewport.setPointerCapture(event.pointerId);
    event.preventDefault();
  }

  function moveDrag(event: PointerEvent) {
    if (!dragging || event.pointerId !== pointerId) return;
    viewport.scrollLeft = startScrollLeft - (event.clientX - startX);
    viewport.scrollTop = startScrollTop - (event.clientY - startY);
  }

  function endDrag(event: PointerEvent) {
    if (event.pointerId !== pointerId) return;
    if (viewport.hasPointerCapture(event.pointerId)) {
      viewport.releasePointerCapture(event.pointerId);
    }
    dragging = false;
    pointerId = null;
  }

  function moveWithKeyboard(event: KeyboardEvent) {
    const distance = event.shiftKey ? 160 : 48;
    const movement: Record<string, [number, number]> = {
      ArrowLeft: [-distance, 0],
      ArrowRight: [distance, 0],
      ArrowUp: [0, -distance],
      ArrowDown: [0, distance]
    };
    if (event.key === 'Home') {
      viewport.scrollTo({ left: 0, top: 0, behavior: 'smooth' });
      event.preventDefault();
      return;
    }
    const delta = movement[event.key];
    if (!delta) return;
    viewport.scrollBy({ left: delta[0], top: delta[1], behavior: 'smooth' });
    event.preventDefault();
  }
</script>

<div class="relative h-full w-full overflow-hidden">
  <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
  <div
    bind:this={viewport}
    class="diagram-pan-viewport h-full w-full overflow-auto overscroll-contain bg-[#f5f5f1] p-5 outline-none [scrollbar-width:none] md:p-8 [&::-webkit-scrollbar]:hidden {dragging ? 'cursor-grabbing select-none' : 'cursor-grab'}"
    role="region"
    aria-roledescription="movable diagram viewer"
    aria-label={label}
    tabindex="0"
    style="touch-action: none; -ms-overflow-style: none;"
    onpointerdown={beginDrag}
    onpointermove={moveDrag}
    onpointerup={endDrag}
    onpointercancel={endDrag}
    onkeydown={moveWithKeyboard}
    ondragstart={(event) => event.preventDefault()}
  >
    {@render children()}
  </div>
  <span class="pointer-events-none absolute bottom-3 left-1/2 z-10 -translate-x-1/2 rounded-full bg-[#26342d]/80 px-3 py-1.5 text-[10px] font-medium text-white shadow-lg">
    Drag to move · Arrow keys also work
  </span>
</div>
