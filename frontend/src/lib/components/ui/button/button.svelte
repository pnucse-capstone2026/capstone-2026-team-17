<script lang="ts">
  import type { HTMLButtonAttributes } from 'svelte/elements';
  import type { Snippet } from 'svelte';
  import { cn } from '$lib/utils';

  let {
    class: className,
    variant = 'default',
    size = 'default',
    children,
    ...rest
  }: HTMLButtonAttributes & {
    variant?: 'default' | 'outline' | 'ghost' | 'danger';
    size?: 'default' | 'sm' | 'icon';
    children?: Snippet;
  } = $props();

  const variants = {
    default: 'bg-[#1f5d45] text-white hover:bg-[#174b38] shadow-sm',
    outline: 'border border-[#d8d9d1] bg-white hover:bg-[#f4f4f0] text-[#272822]',
    ghost: 'bg-transparent hover:bg-black/5 text-[#454740]',
    danger: 'bg-[#a53b32] text-white hover:bg-[#8f3028]'
  };
  const sizes = {
    default: 'h-10 px-4 py-2',
    sm: 'h-8 rounded-lg px-3 text-xs',
    icon: 'h-9 w-9'
  };
</script>

<button
  class={cn(
    'focus-ring inline-flex shrink-0 items-center justify-center gap-2 rounded-xl text-sm font-medium transition-colors disabled:pointer-events-none disabled:opacity-45',
    variants[variant],
    sizes[size],
    className
  )}
  {...rest}
>
  {@render children?.()}
</button>
