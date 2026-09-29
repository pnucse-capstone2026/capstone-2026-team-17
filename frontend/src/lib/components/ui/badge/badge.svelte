<script lang="ts">
  import type { HTMLAttributes } from 'svelte/elements';
  import type { Snippet } from 'svelte';
  import { cn } from '$lib/utils';

  let {
    class: className,
    tone = 'neutral',
    children,
    ...rest
  }: HTMLAttributes<HTMLSpanElement> & {
    tone?: 'neutral' | 'success' | 'warning' | 'danger' | 'accent';
    children?: Snippet;
  } = $props();
  const tones = {
    neutral: 'bg-[#ecece7] text-[#5b5d56]',
    success: 'bg-[#dceee5] text-[#1f6547]',
    warning: 'bg-[#f5e8c8] text-[#74520c]',
    danger: 'bg-[#f8dfdc] text-[#963c34]',
    accent: 'bg-[#dce9f1] text-[#245877]'
  };
</script>

<span
  class={cn('inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-semibold', tones[tone], className)}
  {...rest}
>
  {@render children?.()}
</span>
