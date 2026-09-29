<script lang="ts">
  import { ArrowUpRight, Braces, CheckCircle2, FileCode2, Image } from '@lucide/svelte';
  import type { ArtifactSummary, FileArtifactSnapshot } from '$lib/types';
  import { artifactLabels, diagramArtifactTypes } from '$lib/artifacts';
  import { Badge } from '$lib/components/ui/badge';

  let {
    appId,
    stage,
    value,
    fileArtifact,
    validation,
    onOpen
  }: {
    appId: string;
    stage: string;
    value?: unknown;
    fileArtifact?: FileArtifactSnapshot;
    validation?: ArtifactSummary['validation'];
    onOpen: (stage: string) => void;
  } = $props();

  function parsed(value: unknown): any {
    if (typeof value !== 'string') return value;
    try {
      return JSON.parse(value);
    } catch {
      return value;
    }
  }

  function summary(stage: string, value: unknown): string {
    if (fileArtifact) {
      const count = fileArtifact.files.length;
      return `${count} generated ${count === 1 ? 'file' : 'files'} · version ${fileArtifact.version_no}`;
    }
    const data = parsed(value);
    if (stage === 'refined_requirements' && Array.isArray(data)) {
      const functional = data.filter((item) => String(item?.type ?? '').toUpperCase() === 'FR').length;
      const nonFunctional = data.filter((item) => String(item?.type ?? '').toUpperCase() === 'NFR').length;
      return `${functional} functional · ${nonFunctional} non-functional requirements`;
    }
    if (stage === 'usecase_spec' && data && typeof data === 'object') {
      return `${data.actors?.length ?? 0} actors · ${data.use_cases?.length ?? 0} use cases · ${data.use_case_specs?.length ?? 0} specifications`;
    }
    if (stage === 'api_spec' && data && typeof data === 'object') {
      return `${Object.keys(data.paths ?? {}).length} API paths documented`;
    }
    if (diagramArtifactTypes.has(stage)) return 'Rendered diagram ready for review';
    return 'Structured development artifact ready for review';
  }

  let hasFindings = $derived(Boolean(validation?.errors?.length || validation?.findings?.length));
</script>

<button
  class="focus-ring group overflow-hidden rounded-2xl border border-[#dfe1da] bg-white text-left shadow-[0_2px_8px_rgba(31,45,37,.05)] transition hover:-translate-y-0.5 hover:border-[#b9c9bf] hover:shadow-[0_5px_16px_rgba(31,45,37,.09)]"
  onclick={() => onOpen(stage)}
  aria-label={`Open ${artifactLabels[stage] ?? stage}`}
>
  {#if diagramArtifactTypes.has(stage)}
    <div class="flex h-44 items-center justify-center overflow-hidden border-b border-[#e7e8e2] bg-[#f7f7f3] p-3">
      <img
        class="h-full w-full object-contain transition group-hover:scale-[1.01]"
        src={`/api/apps/${appId}/stages/${stage}/image.svg`}
        alt={`${artifactLabels[stage]} preview`}
      />
    </div>
  {/if}
  <div class="p-3.5">
    <div class="mb-2 flex items-start justify-between gap-3">
      <div class="flex min-w-0 items-center gap-2">
        {#if diagramArtifactTypes.has(stage)}<Image size={15} class="shrink-0 text-[#4f735f]" />
        {:else if fileArtifact}<FileCode2 size={15} class="shrink-0 text-[#4f735f]" />
        {:else}<Braces size={15} class="shrink-0 text-[#4f735f]" />{/if}
        <strong class="truncate text-xs">{artifactLabels[stage] ?? stage}</strong>
      </div>
      <ArrowUpRight size={14} class="shrink-0 text-[#868980] transition group-hover:text-[#2d7354]" />
    </div>
    <p class="text-[11px] leading-5 text-[#71746b]">{summary(stage, value)}</p>
    <div class="mt-2 flex items-center gap-1.5">
      {#if hasFindings}
        <Badge tone="warning">Review findings</Badge>
      {:else}
        <span class="flex items-center gap-1 text-[10px] text-[#4f8066]"><CheckCircle2 size={11} /> Ready</span>
      {/if}
    </div>
  </div>
</button>
