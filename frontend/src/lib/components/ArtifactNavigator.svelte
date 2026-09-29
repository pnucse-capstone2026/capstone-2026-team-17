<script lang="ts">
  import { CheckCircle2, Circle, LoaderCircle } from '@lucide/svelte';
  import type { ArtifactDocument, FileArtifactSnapshot, LiveDiagramPreview } from '$lib/types';
  import { artifactLabels, artifactPresent, fileArtifactTypes } from '$lib/artifacts';

  let {
    document,
    fileArtifacts,
    liveSourceAvailable = false,
    testingResultsAvailable = false,
    classPreview,
    classGenerating = false,
    selected,
    onSelect
  }: {
    document?: ArtifactDocument | null;
    fileArtifacts: Record<string, FileArtifactSnapshot>;
    liveSourceAvailable?: boolean;
    testingResultsAvailable?: boolean;
    classPreview?: LiveDiagramPreview | null;
    classGenerating?: boolean;
    selected: string;
    onSelect: (stage: string) => void;
  } = $props();

  const groups = [
    {
      label: 'Requirements',
      stages: ['refined_requirements', 'usecase_spec', 'usecase_diagram']
    },
    {
      label: 'Design',
      stages: ['class_diagram', 'sequence_diagram', 'api_spec', 'erd', 'deployment_diagram']
    },
    { label: 'Implementation', stages: ['LIVE_SOURCE', ...fileArtifactTypes] },
    { label: 'Testing', stages: ['TESTING_RESULTS'] }
  ];

  function available(stage: string) {
    return (
      Boolean(fileArtifacts[stage]) ||
      (stage === 'LIVE_SOURCE' && liveSourceAvailable) ||
      (stage === 'TESTING_RESULTS' && testingResultsAvailable) ||
      artifactPresent(document?.artifacts?.[stage]) ||
      (stage === 'class_diagram' && (classGenerating || Boolean(classPreview)))
    );
  }

  function generating(stage: string) {
    return stage === 'class_diagram' && (classGenerating || Boolean(classPreview)) && !artifactPresent(document?.artifacts?.[stage]);
  }

</script>

<nav class="scrollbar-thin grid max-h-72 grid-cols-2 gap-2 overflow-y-auto p-3 sm:grid-cols-4" aria-label="Project artifacts">
    {#each groups as group}
      <section aria-label={group.label}>
        <h3 class="mb-1.5 px-1 text-[9px] font-bold uppercase tracking-[.12em] text-[#979990]">{group.label}</h3>
        <div class="space-y-0.5">
          {#each group.stages as stage}
            {@const ready = available(stage)}
            <button
              class="focus-ring flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-left text-[11px] transition {selected === stage
                ? 'bg-[#edf4ef] text-[#1f5d45]'
                : ready
                  ? 'text-[#4e5149] hover:bg-[#f5f6f2]'
                  : 'cursor-not-allowed text-[#a6a89f]'}"
              disabled={!ready}
              onclick={() => onSelect(stage)}
            >
              {#if generating(stage)}
                <LoaderCircle size={14} class="shrink-0 animate-spin text-[#39745a]" />
              {:else if !ready}
                <Circle size={13} class="shrink-0" />
              {:else}
                <CheckCircle2 size={14} class="shrink-0 text-[#4f8066]" />
              {/if}
              <span class="min-w-0 flex-1 truncate">{artifactLabels[stage]}</span>
            </button>
          {/each}
        </div>
      </section>
    {/each}
</nav>
