<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { ArrowRight, Layers3, Sparkles } from '@lucide/svelte';
  import { createApp, listApps } from '$lib/api';
  import type { WorkspaceApp } from '$lib/types';
  import { errorMessage } from '$lib/utils';
  import { Button } from '$lib/components/ui/button';

  let message = $state('');
  let apps = $state<WorkspaceApp[]>([]);
  let busy = $state(false);
  let error = $state('');

  onMount(() => {
    listApps().then((result) => (apps = result)).catch(() => undefined);
  });

  async function start() {
    if (!message.trim() || busy) return;
    busy = true;
    error = '';
    try {
      const result = await createApp({
        message: message.trim()
      });
      await goto(`/workspace/?app=${result.app_id}`);
    } catch (reason) {
      error = errorMessage(reason);
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>EasyDep · New application</title></svelte:head>

<main class="min-h-screen bg-[#f4f4f0] px-5 py-8">
  <header class="mx-auto flex max-w-5xl items-center justify-between">
    <div class="flex items-center gap-3">
      <div class="flex size-10 items-center justify-center rounded-xl bg-[#1f5d45] text-white"><Layers3 size={19} /></div>
      <div><strong class="block text-sm">EasyDep</strong><span class="text-[11px] text-[#777970]">Sequential development workspace</span></div>
    </div>
    {#if apps.length}
      <Button variant="outline" size="sm" onclick={() => goto(`/workspace/?app=${apps[0].app_id}`)}>Open recent work <ArrowRight size={13} /></Button>
    {/if}
  </header>

  <section class="mx-auto mt-[10vh] max-w-3xl text-center">
    <div class="mb-5 inline-flex items-center gap-2 rounded-full border border-[#d8ddd7] bg-white px-3 py-1.5 text-xs text-[#4d6256] shadow-sm">
      <Sparkles size={13} /> From requirements to verifiable artifacts
    </div>
    <h1 class="text-balance text-4xl font-semibold tracking-[-.045em] text-[#1b1d19] sm:text-5xl">What would you like to build?</h1>
    <p class="mx-auto mt-4 max-w-xl text-sm leading-6 text-[#686a63]">Describe your requirements in natural language. EasyDep connects requirements, design, implementation, and testing step by step.</p>

    <div class="mt-8 rounded-3xl border border-[#deded7] bg-white p-3 text-left shadow-[0_18px_60px_rgba(38,42,35,.1)]">
      <textarea
        bind:value={message}
        rows="7"
        class="w-full resize-none border-0 bg-transparent px-4 py-3 text-[15px] leading-7 outline-none placeholder:text-[#a0a29a]"
        placeholder="Example: During course registration, students must be able to browse, enroll in, and drop courses. Capacity and duplicate-enrollment rules must be enforced."
      ></textarea>
      <div class="flex justify-end border-t border-[#ecece7] p-2">
        <Button onclick={start} disabled={busy || !message.trim()}>{busy ? 'Starting…' : 'Start analysis'} <ArrowRight size={15} /></Button>
      </div>
    </div>
    {#if error}<p class="mt-4 text-sm text-[#a24037]">{error}</p>{/if}
  </section>

  {#if apps.length}
    <section class="mx-auto mt-12 max-w-3xl text-left">
      <h2 class="mb-3 text-xs font-bold uppercase tracking-[.14em] text-[#85877e]">Recent work</h2>
      <div class="grid gap-2 sm:grid-cols-2">
        {#each apps.slice(0, 4) as app}
          <button onclick={() => goto(`/workspace/?app=${app.app_id}`)} class="focus-ring rounded-2xl border border-[#e0e0d9] bg-white p-4 text-left hover:border-[#b8c9bf]">
            <strong class="block truncate text-sm">{app.title}</strong><span class="mt-2 block text-[11px] text-[#85877e]">{app.current_stage}</span>
          </button>
        {/each}
      </div>
    </section>
  {/if}
</main>
