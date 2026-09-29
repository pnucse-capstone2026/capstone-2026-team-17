<script lang="ts">
  import { Check, Cloud, MapPinned, Search, X } from '@lucide/svelte';
  import { Dialog } from 'bits-ui';
  import type {
    CloudProvider,
    CloudRegionOption,
    DeploymentPreferences
  } from '$lib/types';
  import { Button } from '$lib/components/ui/button';

  type RegionMapComponent = typeof import('$lib/components/CloudRegionMap.svelte').default;

  let {
    regions,
    initialProvider,
    saving = false,
    onSave
  }: {
    regions: Record<CloudProvider, CloudRegionOption[]>;
    initialProvider?: CloudProvider;
    saving?: boolean;
    onSave: (preferences: DeploymentPreferences) => Promise<void>;
  } = $props();

  const providers: Array<{ id: CloudProvider; label: string; color: string }> = [
    { id: 'aws', label: 'AWS', color: '#d97706' },
    { id: 'azure', label: 'Azure', color: '#1677b8' },
    { id: 'gcp', label: 'GCP', color: '#2f7d55' }
  ];
  let open = $state(false);
  let selectedProviders = $state<CloudProvider[]>([]);
  let activeProvider = $state<CloudProvider>('aws');
  let selectedRegions = $state<Record<CloudProvider, string>>({ aws: '', azure: '', gcp: '' });
  let query = $state('');
  let error = $state('');
  let RegionMap = $state<RegionMapComponent | null>(null);
  let mapLoading = $state(false);
  let appliedInitialProvider = '';

  let activeRegions = $derived(regions[activeProvider] ?? []);
  let activeRegion = $derived(
    activeRegions.find((region) => region.code === selectedRegions[activeProvider]) ?? null
  );
  let filteredRegions = $derived(
    activeRegions.filter((region) => {
      const needle = query.trim().toLowerCase();
      return !needle || `${region.name} ${region.code}`.toLowerCase().includes(needle);
    })
  );
  let ready = $derived(
    selectedProviders.length > 0 &&
      selectedProviders.every((provider) => {
        const region = (regions[provider] ?? []).find(
          (candidate) => candidate.code === selectedRegions[provider]
        );
        return Boolean(region);
      })
  );

  $effect(() => {
    const provider = initialProvider ?? '';
    if (!provider) {
      appliedInitialProvider = '';
      return;
    }
    if (open || provider === appliedInitialProvider) return;
    selectedProviders = [provider];
    activeProvider = provider;
    query = '';
    appliedInitialProvider = provider;
  });

  $effect(() => {
    if (!open || RegionMap || mapLoading) return;
    mapLoading = true;
    void import('$lib/components/CloudRegionMap.svelte')
      .then((module) => {
        RegionMap = module.default;
      })
      .catch((reason) => {
        error = reason instanceof Error ? reason.message : String(reason);
      })
      .finally(() => {
        mapLoading = false;
      });
  });

  function toggleProvider(provider: CloudProvider) {
    query = '';
    if (selectedProviders.includes(provider)) {
      selectedProviders = selectedProviders.filter((item) => item !== provider);
      selectedRegions = { ...selectedRegions, [provider]: '' };
      activeProvider = selectedProviders[0] ?? providers.find((item) => item.id !== provider)?.id ?? 'aws';
    } else {
      selectedProviders = [...selectedProviders, provider];
      activeProvider = provider;
    }
  }

  function selectRegion(provider: CloudProvider, region: CloudRegionOption) {
    selectedRegions = { ...selectedRegions, [provider]: region.code };
  }

  async function save() {
    if (!ready || saving) return;
    error = '';
    try {
      await onSave({
        mode: 'alternatives',
        targets: selectedProviders.map((provider) => ({
          provider,
          region: selectedRegions[provider],
          zones: []
        }))
      });
      open = false;
    } catch (reason) {
      error = reason instanceof Error ? reason.message : String(reason);
    }
  }
</script>

<Dialog.Root bind:open>
  <section class="mb-5 ml-11 rounded-2xl border border-[#d9ddd6] bg-white p-4 shadow-sm">
    <div class="flex items-start gap-3">
      <div class="flex size-9 shrink-0 items-center justify-center rounded-xl bg-[#e8f1eb] text-[#2f6b50]">
        <Cloud size={17} />
      </div>
      <div class="min-w-0 flex-1">
        <h3 class="text-sm font-semibold text-[#30342e]">Choose deployment alternatives</h3>
        <p class="mt-1 max-w-xl text-xs leading-5 text-[#73776f]">
          Choose cloud providers and regions while application analysis continues. Budget and topology decisions come later.
        </p>
      </div>
      <Dialog.Trigger class="focus-ring inline-flex h-9 shrink-0 items-center gap-2 rounded-xl bg-[#2d6b4d] px-3.5 text-xs font-semibold text-white hover:bg-[#245b41]">
        <MapPinned size={14} /> Open map
      </Dialog.Trigger>
    </div>
  </section>

  <Dialog.Portal>
    <Dialog.Overlay class="fixed inset-0 z-[100] bg-[#16251d]/35 backdrop-blur-sm" />
    <Dialog.Content class="fixed inset-0 z-[101] flex min-h-0 flex-col overflow-hidden bg-[#f7f8f5] text-[#30342e] outline-none">
      <header class="flex h-16 shrink-0 items-center gap-4 border-b border-[#dfe2dc] bg-white px-4 sm:px-6">
        <div class="flex size-9 items-center justify-center rounded-xl bg-[#e8f1eb] text-[#2f6b50]">
          <MapPinned size={18} />
        </div>
        <div class="min-w-0 flex-1">
          <Dialog.Title class="text-sm font-semibold sm:text-base">Deployment alternatives</Dialog.Title>
          <Dialog.Description class="truncate text-[11px] text-[#777b73] sm:text-xs">
            Choose one region per CSP. CSP targets are alternatives, not one multi-cloud runtime.
          </Dialog.Description>
        </div>
        <Dialog.Close class="focus-ring flex size-9 items-center justify-center rounded-xl border border-[#dfe1db] bg-[#fafaf8] text-[#686c64] hover:bg-[#f0f2ed]" aria-label="Close deployment map">
          <X size={17} />
        </Dialog.Close>
      </header>

      <div class="flex min-h-0 flex-1 flex-col lg:grid lg:grid-cols-[minmax(0,1fr)_390px]">
        <main class="flex min-h-[420px] min-w-0 flex-1 flex-col border-b border-[#dfe2dc] lg:min-h-0 lg:border-b-0 lg:border-r">
          <div class="flex shrink-0 flex-wrap items-center gap-2 border-b border-[#e2e5df] bg-white px-4 py-3">
            <span class="mr-1 text-[10px] font-bold uppercase tracking-[.14em] text-[#858980]">Providers</span>
            {#each providers as provider}
              <button
                type="button"
                class="focus-ring flex h-9 items-center gap-2 rounded-xl border px-3 text-xs font-semibold transition-colors {selectedProviders.includes(provider.id) ? 'border-[#9ab5a4] bg-[#edf5f0] text-[#285b43]' : 'border-[#dcddd7] bg-[#fafaf8] text-[#666a62]'}"
                onclick={() => toggleProvider(provider.id)}
              >
                <span class="size-2.5 rounded-full" style={`background:${provider.color}`}></span>
                {provider.label}
                <span class="flex size-4 items-center justify-center rounded border border-current/25">
                  {#if selectedProviders.includes(provider.id)}<Check size={11} />{/if}
                </span>
              </button>
            {/each}
            {#if selectedProviders.length}
              <span class="ml-auto hidden text-[11px] text-[#858980] sm:inline">Click a large marker to select its region</span>
            {/if}
          </div>

          {#if selectedProviders.length}
            <div class="flex shrink-0 gap-1.5 overflow-x-auto border-b border-[#e2e5df] bg-[#f8f9f6] px-4 py-2.5">
              {#each selectedProviders as provider}
                <button
                  type="button"
                  class="focus-ring whitespace-nowrap rounded-lg px-3 py-1.5 text-xs font-semibold {activeProvider === provider ? 'bg-[#2d6b4d] text-white' : 'bg-white text-[#686c64] shadow-sm'}"
                  onclick={() => { activeProvider = provider; query = ''; }}
                >
                  {provider.toUpperCase()}
                  {#if selectedRegions[provider]} · {selectedRegions[provider]}{/if}
                </button>
              {/each}
            </div>
            <div class="min-h-[340px] flex-1">
              {#if RegionMap}
                {#key activeProvider}
                  <RegionMap
                    provider={activeProvider}
                    regions={activeRegions}
                    selectedRegionCode={selectedRegions[activeProvider]}
                    onSelect={(region) => selectRegion(activeProvider, region)}
                  />
                {/key}
              {:else}
                <div class="flex h-full min-h-[340px] items-center justify-center bg-[#e9eee8] text-xs text-[#737970]">
                  {mapLoading ? 'Loading interactive map…' : 'The map could not be loaded.'}
                </div>
              {/if}
            </div>
          {:else}
            <div class="flex flex-1 items-center justify-center p-8 text-center">
              <div>
                <Cloud class="mx-auto text-[#9aa198]" size={36} />
                <p class="mt-3 text-sm font-semibold">Select at least one cloud provider</p>
                <p class="mt-1 text-xs text-[#7d8179]">Its available regions will appear as interactive map markers.</p>
              </div>
            </div>
          {/if}
        </main>

        <aside class="min-h-0 overflow-y-auto bg-white p-4 sm:p-5">
          <div class="space-y-5">
            <section>
              <div class="flex items-center justify-between gap-3">
                <h3 class="text-xs font-bold uppercase tracking-[.12em] text-[#777b73]">Region</h3>
                {#if activeRegion}<span class="text-[11px] font-medium text-[#2d6b4d]">Selected</span>{/if}
              </div>
              {#if selectedProviders.includes(activeProvider)}
                <label class="relative mt-2 block">
                  <Search class="pointer-events-none absolute left-3 top-2.5 text-[#8a8e86]" size={14} />
                  <input bind:value={query} class="focus-ring h-9 w-full rounded-xl border border-[#d9dbd5] bg-[#fafaf8] pl-9 pr-3 text-xs" placeholder={`Search ${activeProvider.toUpperCase()} regions`} />
                </label>
                <div class="mt-2 max-h-40 space-y-1 overflow-y-auto rounded-xl border border-[#e1e3dd] p-1.5">
                  {#each filteredRegions as region (region.code)}
                    <button
                      type="button"
                      class="focus-ring flex w-full items-center justify-between gap-3 rounded-lg px-2.5 py-2 text-left text-xs {selectedRegions[activeProvider] === region.code ? 'bg-[#eaf3ed] text-[#285b43]' : 'hover:bg-[#f4f5f1]'}"
                      onclick={() => selectRegion(activeProvider, region)}
                    >
                      <span class="min-w-0"><strong class="block truncate font-medium">{region.name}</strong><span class="text-[10px] opacity-70">{region.code}</span></span>
                      {#if selectedRegions[activeProvider] === region.code}<Check class="shrink-0" size={14} />{/if}
                    </button>
                  {:else}
                    <p class="px-2 py-4 text-center text-xs text-[#858980]">No matching regions.</p>
                  {/each}
                </div>
              {:else}
                <p class="mt-2 rounded-xl bg-[#f5f6f2] p-3 text-xs text-[#7d8179]">Select a provider on the map panel first.</p>
              {/if}
            </section>

            <p class="border-t border-[#eceee9] pt-4 text-[11px] leading-5 text-[#777b73]">
              Monthly budget, VM count, availability behavior, ingress, workload placement, and resource sizing will be handled in later stages.
            </p>
          </div>
        </aside>
      </div>

      <footer class="flex min-h-16 shrink-0 items-center justify-between gap-3 border-t border-[#dfe2dc] bg-white px-4 py-3 sm:px-6">
        <div class="min-w-0 text-[11px] text-[#7d8179]">
          {#if selectedProviders.length}
            {selectedProviders.length} provider{selectedProviders.length === 1 ? '' : 's'} · {selectedProviders.filter((provider) => selectedRegions[provider]).length} region{selectedProviders.filter((provider) => selectedRegions[provider]).length === 1 ? '' : 's'}
          {:else}
            Select a provider and region to continue.
          {/if}
        </div>
        <div class="flex shrink-0 gap-2">
          <button type="button" class="focus-ring h-9 rounded-xl border border-[#d9dbd5] bg-white px-4 text-xs font-semibold text-[#686c64]" onclick={() => (open = false)}>Cancel</button>
          <Button onclick={save} disabled={!ready || saving}>{saving ? 'Saving…' : 'Use these targets'}</Button>
        </div>
        {#if error}<p class="absolute bottom-16 right-5 rounded-lg bg-[#fff1ef] px-3 py-2 text-xs text-[#a24037] shadow">{error}</p>{/if}
      </footer>
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
