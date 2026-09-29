<script lang="ts">
  import { Braces, Check, CheckCircle2, Clock3, Copy, FileText, Image, Layers3, LoaderCircle, Maximize2, ShieldCheck, X } from '@lucide/svelte';
  import { Dialog } from 'bits-ui';
  import type { ArtifactDocument, ArtifactTraceResponse, FileArtifactSnapshot, LiveDiagramPreview, LiveSourceSnapshot, SequenceDiagramSummary, WorkspaceCommand, WorkspaceEvent } from '$lib/types';
  import { getArtifactFile, getArtifactTrace, getFileArtifactVersions, getLiveImplementationFile, getSequenceDiagrams, getVersions } from '$lib/api';
  import ArtifactVisualization from '$lib/components/ArtifactVisualization.svelte';
  import ArtifactNavigator from '$lib/components/ArtifactNavigator.svelte';
  import DraggableDiagramViewport from '$lib/components/DraggableDiagramViewport.svelte';
  import DeploymentSizingPanel from '$lib/components/DeploymentSizingPanel.svelte';
  import ReadOnlySourceViewer from '$lib/components/ReadOnlySourceViewer.svelte';
  import SourceFileExplorer from '$lib/components/SourceFileExplorer.svelte';
  import TestingResultsPanel from '$lib/components/TestingResultsPanel.svelte';
  import { Badge } from '$lib/components/ui/badge';
  import { artifactLabels, artifactPresent, diagramArtifactTypes, requirementsArtifactTypes } from '$lib/artifacts';
  import { projectTestingRun } from '$lib/testing-results';
  import { isAutomaticRepairActive } from '$lib/repair-presentation';

  let {
    appId,
    document,
    fileArtifacts,
    liveSources = null,
    preferredFile = '',
    events,
    command = null,
    classPreview,
    classGenerating = false,
    selected,
    onSelect,
    sequenceMethodApprovalAvailable = false,
    onSequenceMethodApproval,
    onFileSelect,
    onDeploymentSizingApplied,
    onClose
  }: {
    appId: string;
    document?: ArtifactDocument | null;
    fileArtifacts: Record<string, FileArtifactSnapshot>;
    liveSources?: LiveSourceSnapshot | null;
    preferredFile?: string;
    events: WorkspaceEvent[];
    command?: WorkspaceCommand | null;
    classPreview?: LiveDiagramPreview | null;
    classGenerating?: boolean;
    selected: string;
    onSelect: (stage: string) => void;
    sequenceMethodApprovalAvailable?: boolean;
    onSequenceMethodApproval?: () => void;
    onFileSelect?: (path: string) => void;
    onDeploymentSizingApplied?: () => void | Promise<void>;
    onClose?: () => void;
  } = $props();
  let tab = $state<'artifact' | 'validation' | 'changes' | 'evidence'>('artifact');
  let diagramExpanded = $state(false);
  let deploymentView = $state<'runtime' | 'provisioning'>('runtime');
  let deploymentTargetId = $state('');
  let indexOpen = $state(false);
  let versions = $state<Array<Record<string, any>>>([]);
  let versionsError = $state('');
  let selectedFile = $state('');
  let fileContent = $state('');
  let fileError = $state('');
  let fileLoading = $state(false);
  let sourceExpanded = $state(false);
  let loadedFileKey = '';
  let fileRequestVersion = 0;
  let copiedFile = $state(false);
  let sequenceDiagrams = $state<SequenceDiagramSummary[]>([]);
  let sequenceError = $state('');
  let sequenceLoading = $state(false);
  let expandedSequence = $state<SequenceDiagramSummary | null>(null);
  let sequenceLoadVersion = 0;
  let sequenceImageEpoch = $state(0);
  let trace = $state<ArtifactTraceResponse | null>(null);
  let traceRefs = $state<string[]>([]);
  let selectedArtifactTraceRef = $state('');
  let traceError = $state('');
  let traceLoading = $state(false);
  let traceRequestVersion = 0;
  let previouslySelected = '';
  let content = $derived(document?.artifacts?.[selected]);
  let testingRun = $derived(projectTestingRun({ command, events }));
  let liveClassPreview = $derived(
    selected === 'class_diagram' ? classPreview ?? null : null
  );
  let displayContent = $derived(liveClassPreview?.puml ?? content);
  let fileArtifact = $derived(
    selected === 'LIVE_SOURCE' ? liveSources ?? undefined : fileArtifacts[selected]
  );
  let implementationStages = $derived(
    (liveSources ? ['LIVE_SOURCE'] : []).concat(
      Object.keys(fileArtifacts).filter((stage) => Boolean(fileArtifacts[stage]))
    )
  );
  let validation = $derived(document?.validation?.[selected]);
  let automaticRepairActive = $derived(isAutomaticRepairActive(command));
  let deploymentMetadata = $derived(document?.artifact_metadata?.deployment_diagram);
  let deploymentTargets = $derived(deploymentMetadata?.targets ?? []);
  // The sequence artifact itself changes when feedback is applied, even when
  // the UC summary list keeps the same IDs.  Track it separately so images
  // receive a new URL and cannot retain a previous revision in the DOM/cache.
  let sequenceArtifactSource = $derived(document?.artifacts?.sequence_diagram ?? '');
  // 저장 snapshot은 application 폴더를 생략하고, frontend snapshot은 frontend도
  // 생략한다. RTM은 실제 구현 작업 폴더 기준 경로를 쓰므로 조회 직전에만 되붙인다.
  // 설계 산출물은 한 문서 안에 여러 항목이 있으므로, 아래 목록에서 사용자가 고른
  // 정확한 ref를 사용한다. 화면이 이름을 보고 임의의 대상을 추측하지 않는다.
  let selectedTraceRef = $derived.by(() => {
    if (fileArtifact && selectedFile) {
      if (selected === 'FRONTEND_SOURCE_CODE') {
        return `file:application/frontend/${selectedFile}`;
      }
      return `file:application/${selectedFile}`;
    }
    return selectedArtifactTraceRef;
  });
  let traceGroups = $derived([
    { label: 'Direct sources', values: trace?.sources ?? [] },
    { label: 'Direct consumers', values: trace?.consumers ?? [] },
    { label: 'Upstream', values: trace?.upstream ?? [] },
    { label: 'Downstream', values: trace?.downstream ?? [] },
    { label: 'Files', values: trace?.files ?? [] },
    { label: 'Evidence', values: trace?.evidence ?? [] }
  ]);
  let relatedEvents = $derived(
    events
      .filter(
        (event) =>
          event.kind !== 'error' &&
          event.stage ===
          (selected === 'TESTING_RESULTS'
            ? 'testing'
            : fileArtifact
              ? 'implementation'
              : requirementsArtifactTypes.has(selected)
                ? 'requirements'
                : 'design')
      )
      .slice()
      .reverse()
  );

  let availableStages = $derived(
    Object.keys(document?.artifacts ?? {})
      .filter((key) => key in artifactLabels && artifactPresent(document?.artifacts?.[key]))
      .concat(Object.keys(fileArtifacts))
      .concat(liveSources ? ['LIVE_SOURCE'] : [])
      .concat(classPreview || classGenerating ? ['class_diagram'] : [])
      .concat(testingRun ? ['TESTING_RESULTS'] : [])
      .filter((stage, index, stages) => stages.indexOf(stage) === index)
  );

  $effect(() => {
    const currentSelection = selected;
    if (currentSelection !== previouslySelected) {
      previouslySelected = currentSelection;
      tab = 'artifact';
      diagramExpanded = false;
      sourceExpanded = false;
      deploymentView = 'runtime';
      deploymentTargetId = '';
      expandedSequence = null;
      indexOpen = false;
      traceRefs = [];
      selectedArtifactTraceRef = '';
    }
  });

  $effect(() => {
    if (selected !== 'deployment_diagram') return;
    const targets = deploymentTargets;
    const stored = deploymentMetadata?.selectedTarget?.id ?? '';
    if (!targets.some((target) => target.id === deploymentTargetId)) {
      deploymentTargetId = stored || targets[0]?.id || '';
    }
  });

  $effect(() => {
    const currentAppId = appId;
    const currentSelection = selected;
    const hasFileSelection = Boolean(fileArtifact);
    if (tab !== 'evidence' || !currentAppId || hasFileSelection) return;

    // 전체 ref 목록은 작은 문자열 목록이다. 문서 종류에 해당하는 prefix만 남겨
    // 사용자가 클래스·operation·API·resource 같은 실제 항목을 직접 고르게 한다.
    const prefixesByArtifact: Record<string, string[]> = {
      refined_requirements: ['requirement:'],
      usecase_spec: ['use_case:', 'use_case_spec:', 'step:'],
      usecase_diagram: ['use_case:'],
      class_diagram: ['class:', 'operation:', 'collaboration:', 'call:', 'data_type:'],
      sequence_diagram: ['sequence:', 'message:'],
      api_spec: ['api:', 'schema:'],
      erd: ['entity:'],
      deployment_diagram: [
        'planning_fact:',
        'workload:',
        'resource:',
        'external_dependency:',
        'connection:',
        'constraint:'
      ]
    };
    const prefixes = prefixesByArtifact[currentSelection] ?? [];
    getArtifactTrace(currentAppId)
      .then((result) => {
        if (currentAppId !== appId || currentSelection !== selected || tab !== 'evidence') return;
        const candidates = result.refs.filter((ref) => prefixes.some((prefix) => ref.startsWith(prefix)));
        traceRefs = candidates;
        if (!candidates.includes(selectedArtifactTraceRef)) {
          selectedArtifactTraceRef = candidates[0] ?? '';
        }
      })
      .catch((error) => {
        if (currentAppId !== appId || currentSelection !== selected || tab !== 'evidence') return;
        traceRefs = [];
        selectedArtifactTraceRef = '';
        traceError = 'Linked trace is unavailable.';
      });
  });

  $effect(() => {
    if (!appId || !selected) return;
    versions = [];
    versionsError = '';
    if (liveClassPreview || selected === 'LIVE_SOURCE' || selected === 'TESTING_RESULTS') return;
    const loader = fileArtifacts[selected] ? getFileArtifactVersions : getVersions;
    loader(appId, selected)
      .then((result) => (versions = result.versions))
      .catch(() => (versionsError = 'Version history is unavailable.'));
  });

  $effect(() => {
    const currentAppId = appId;
    const currentSelection = selected;
    const currentSequenceSource = sequenceArtifactSource;
    const loadVersion = ++sequenceLoadVersion;
    sequenceDiagrams = [];
    sequenceError = '';
    sequenceLoading = false;

    if (currentSelection !== 'sequence_diagram' || !currentAppId || !currentSequenceSource) return;

    sequenceLoading = true;
    getSequenceDiagrams(currentAppId)
      .then((result) => {
        if (loadVersion !== sequenceLoadVersion) return;
        sequenceDiagrams = result.diagrams;
        // Deliberately advance after every accepted fetch.  The list is keyed
        // by use_case_id, so without this token Svelte can retain an existing
        // <img> whose URL still points at a pre-feedback rendering.
        sequenceImageEpoch += 1;
      })
      .catch(() => {
        if (loadVersion !== sequenceLoadVersion) return;
        sequenceError = 'Sequence diagrams are unavailable right now.';
      })
      .finally(() => {
        if (loadVersion === sequenceLoadVersion) sequenceLoading = false;
      });
  });

  $effect(() => {
    const currentAppId = appId;
    const currentRef = selectedTraceRef;
    const requestVersion = ++traceRequestVersion;
    trace = null;
    traceError = '';
    traceLoading = false;

    if (tab !== 'evidence' || !currentAppId || !currentRef) return;

    traceLoading = true;
    getArtifactTrace(currentAppId, currentRef)
      .then((result) => {
        if (requestVersion !== traceRequestVersion) return;
        trace = result;
      })
      .catch(() => {
        if (requestVersion !== traceRequestVersion) return;
        traceError = 'Linked trace is unavailable.';
      })
      .finally(() => {
        if (requestVersion === traceRequestVersion) traceLoading = false;
      });
  });

  $effect(() => {
    const snapshot = fileArtifact;
    if (!snapshot) {
      fileRequestVersion += 1;
      selectedFile = '';
      fileContent = '';
      fileLoading = false;
      sourceExpanded = false;
      loadedFileKey = '';
      return;
    }
    const candidates = snapshot.files.filter((file) => !('exists' in file) || file.exists);
    const preferredCandidates = [
      preferredFile,
      preferredFile.startsWith('frontend/') ? preferredFile.slice('frontend/'.length) : ''
    ].filter(Boolean);
    const nextFile =
      candidates.find((file) => file.path === selectedFile) ??
      candidates.find((file) => preferredCandidates.includes(file.path)) ??
      candidates[0];
    if (!nextFile) {
      selectedFile = '';
      fileContent = '';
      loadedFileKey = '';
      return;
    }
    const nextKey = `${selected}:${nextFile.path}:${nextFile.sha256}`;
    if (loadedFileKey !== nextKey) void loadFile(nextFile.path, nextKey);
  });

  async function loadFile(path: string, expectedKey = '', reveal = false) {
    if (!path) return;
    const requestVersion = ++fileRequestVersion;
    if (expectedKey) loadedFileKey = expectedKey;
    selectedFile = path;
    onFileSelect?.(path);
    fileError = '';
    fileLoading = true;
    if (reveal) sourceExpanded = true;
    const requestSelection = selected;
    try {
      const response =
        requestSelection === 'LIVE_SOURCE' && liveSources
          ? await getLiveImplementationFile(appId, liveSources.job_id, path)
          : await getArtifactFile(appId, requestSelection, path);
      if (
        requestVersion !== fileRequestVersion ||
        requestSelection !== selected ||
        path !== selectedFile
      ) return;
      fileContent = response.content;
      loadedFileKey = expectedKey || `${requestSelection}:${path}:${response.sha256}`;
    } catch {
      if (
        requestVersion !== fileRequestVersion ||
        requestSelection !== selected ||
        path !== selectedFile
      ) return;
      fileError = 'Source preview is unavailable.';
      fileContent = '';
      loadedFileKey = '';
    } finally {
      if (requestVersion === fileRequestVersion) fileLoading = false;
    }
  }

  function openSourceFile(path: string) {
    const file = fileArtifact?.files.find((candidate) => candidate.path === path);
    const expectedKey = file ? `${selected}:${path}:${file.sha256}` : '';
    if (expectedKey && loadedFileKey === expectedKey && fileContent) {
      selectedFile = path;
      onFileSelect?.(path);
      fileError = '';
      sourceExpanded = true;
      return;
    }
    void loadFile(path, expectedKey, true);
  }

  function fileLanguage(path: string): string {
    const extension = path.split('.').pop()?.toLowerCase() ?? '';
    return {
      java: 'Java', kt: 'Kotlin', ts: 'TypeScript', tsx: 'TSX', js: 'JavaScript',
      svelte: 'Svelte', py: 'Python', yml: 'YAML', yaml: 'YAML', json: 'JSON',
      tf: 'Terraform', dockerfile: 'Dockerfile', xml: 'XML', gradle: 'Gradle',
      properties: 'Properties', sh: 'Shell', sql: 'SQL', md: 'Markdown'
    }[extension] ?? (path.endsWith('Dockerfile') ? 'Dockerfile' : 'Text');
  }

  async function copySelectedFile() {
    if (!fileContent || !navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(fileContent);
      copiedFile = true;
      window.setTimeout(() => (copiedFile = false), 1600);
    } catch {
      copiedFile = false;
    }
  }

  function rawContent(value: unknown) {
    return typeof value === 'string' ? value : JSON.stringify(value ?? {}, null, 2);
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      diagramExpanded = false;
      sourceExpanded = false;
      indexOpen = false;
    }
  }

  function selectFromIndex(stage: string) {
    onSelect(stage);
    indexOpen = false;
  }

  function sequenceImageUrl(diagram: SequenceDiagramSummary, extension: 'png' | 'svg') {
    return `/api/apps/${encodeURIComponent(appId)}/stages/sequence_diagram/diagrams/${encodeURIComponent(diagram.use_case_id)}/image.${extension}?revision=${sequenceImageEpoch}`;
  }

  function expandDiagram(diagram: SequenceDiagramSummary | null = null) {
    expandedSequence = diagram;
    diagramExpanded = true;
  }

  function diagramImageUrl(stage: string) {
    const encodedAppId = encodeURIComponent(appId);
    if (stage === 'class_diagram' && liveClassPreview) {
      const commandId = encodeURIComponent(liveClassPreview.command_id);
      return `/api/workspace/apps/${encodedAppId}/commands/${commandId}/previews/class_diagram/image.svg?revision=${liveClassPreview.revision}`;
    }
    if (stage === 'deployment_diagram') {
      const target = deploymentTargetId
        ? `?target=${encodeURIComponent(deploymentTargetId)}`
        : '';
      return `/api/apps/${encodedAppId}/stages/deployment_diagram/views/${deploymentView}/image.svg${target}`;
    }
    return `/api/apps/${encodedAppId}/stages/${encodeURIComponent(stage)}/image.svg`;
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<aside class="artifact-pane relative flex h-full min-h-0 min-w-0 flex-col overflow-hidden border-l border-[#deded7] bg-white">
  <header class="flex h-12 shrink-0 items-center justify-between gap-2 border-b border-[#e4e4de] px-3">
      <div class="flex min-w-0 items-center gap-2">
        <h2 class="truncate text-sm font-semibold">{artifactLabels[selected] ?? 'Development artifact'}</h2>
        <span class="shrink-0 text-[10px] text-[#85877e]">{availableStages.length} available</span>
      </div>
      <div class="flex shrink-0 items-center gap-1">
        <button
          class="focus-ring flex size-8 items-center justify-center rounded-lg text-[#60635a] hover:bg-[#f1f2ed]"
          onclick={() => (indexOpen = !indexOpen)}
          aria-label="Browse artifact index"
          aria-expanded={indexOpen}
        >
          <Layers3 size={15} />
        </button>
        {#if onClose}
          <button class="focus-ring flex size-8 items-center justify-center rounded-lg text-[#60635a] hover:bg-[#f1f2ed]" onclick={onClose} aria-label="Close artifact details">
            <X size={15} />
          </button>
        {/if}
      </div>
  </header>

  {#if indexOpen}
    <button
      class="absolute inset-x-0 bottom-0 top-12 z-20 cursor-default bg-black/5"
      onclick={() => (indexOpen = false)}
      aria-label="Close artifact index"
    ></button>
    <div class="absolute left-3 right-3 top-12 z-30 overflow-hidden rounded-xl border border-[#dfe1da] bg-white shadow-[0_12px_32px_rgba(31,45,37,.16)]">
      <div class="flex items-center justify-between border-b border-[#ecece6] px-3 py-2">
        <strong class="text-xs">Artifact index</strong>
        <span class="text-[10px] text-[#85877e]">Select an output</span>
      </div>
      <ArtifactNavigator {document} {fileArtifacts} liveSourceAvailable={Boolean(liveSources)} testingResultsAvailable={Boolean(testingRun)} {classPreview} {classGenerating} {selected} onSelect={selectFromIndex} />
    </div>
  {/if}

  {#if selected !== 'TESTING_RESULTS'}
  <!--<div class="flex shrink-0 border-b border-[#e6e6e0] px-2 pt-1" role="tablist">
    {#each [
      ['artifact', 'Artifact'],
      ['validation', 'Validation'],
      ['changes', 'Changes'],
      ['evidence', 'Evidence']
    ] as item}
      <button
        class="focus-ring flex-1 border-b-2 px-1 py-2.5 text-[11px] font-semibold {tab === item[0]
          ? 'border-[#1f5d45] text-[#1f5d45]'
          : 'border-transparent text-[#7c7e75]'}"
        onclick={() => (tab = item[0] as typeof tab)}
      >{item[1]}</button>
    {/each}
  </div>-->
  {/if}

  <div class="scrollbar-thin flex-1 overflow-auto bg-[#fbfbf8]">
    <div class="min-h-full w-full">
    {#if selected === 'TESTING_RESULTS'}
      <TestingResultsPanel run={testingRun} />
    {:else if classGenerating && selected === 'class_diagram' && !displayContent}
      <div class="mt-16 text-center text-[#5d7565]" role="status">
        <LoaderCircle class="mx-auto mb-3 animate-spin" size={25} strokeWidth={1.5} />
        <p class="text-xs font-semibold">Generating the class diagram</p>
        <p class="mt-1 text-[11px] text-[#898b83]">The first accepted snapshot will appear here.</p>
      </div>
    {:else if !displayContent && !fileArtifact}
      <div class="mt-16 text-center text-[#898b83]">
        <FileText class="mx-auto mb-3" size={25} strokeWidth={1.5} />
        <p class="text-xs">This artifact has not been generated yet.</p>
      </div>
    {:else if tab === 'artifact'}
      {#if liveClassPreview}
        <div class="border-b border-[#b8d7c5] bg-[#eef8f1] px-3 py-2 text-xs leading-5 text-[#24553d]" role="status">
          <strong>Generating the class diagram</strong>
          <span class="ml-1">{liveClassPreview.completed}/{liveClassPreview.total || '?'} · {liveClassPreview.unit || liveClassPreview.phase}</span>
        </div>
      {/if}
      {#if selected === 'deployment_diagram' && document?.artifact_metadata?.deployment_diagram?.readOnly}
        <div class="border-b border-[#e3c98b] bg-[#fff8e7] px-3 py-2 text-xs leading-5 text-[#755b24]" role="status">
          <strong class="block">Legacy deployment plan is read-only</strong>
          <span>{document.artifact_metadata.deployment_diagram.regeneration?.reason ?? 'Regenerate this artifact as WorkloadGraph v2 before editing or IaC generation.'}</span>
        </div>
      {/if}
      {#if fileArtifact}
        <section class="overflow-hidden bg-white" aria-label="Implementation source review">
          <header class="border-b border-[#e4e7e1] bg-[#f6f8f4] px-3 py-2">
            <div class="flex items-start justify-between gap-3">
              <div>
                <p class="text-[10px] font-bold uppercase tracking-[.13em] text-[#65806d]">{selected === 'LIVE_SOURCE' ? 'Writing now' : 'Implementation review'}</p>
                <h3 class="mt-0.5 text-sm font-semibold text-[#30362f]">{artifactLabels[selected]}</h3>
              </div>
              <span class="rounded-full border border-[#d4e5d9] bg-white px-2 py-1 text-[10px] font-semibold text-[#467055]">{fileArtifact.files.length} files</span>
            </div>
            <div class="mt-3 flex flex-wrap gap-1.5" role="tablist" aria-label="Implementation artifact categories">
              {#each implementationStages as stage}
                <button
                  class="focus-ring rounded-md border px-2 py-1 text-[10px] font-semibold transition {selected === stage ? 'border-[#78a88a] bg-[#eaf5ed] text-[#24553d]' : 'border-[#dfe3dc] bg-white text-[#656960] hover:bg-[#f3f6f2]'}"
                  role="tab"
                  aria-selected={selected === stage}
                  onclick={() => onSelect(stage)}
                >{artifactLabels[stage] ?? stage}</button>
              {/each}
            </div>
          </header>
          <div class="min-h-[26rem] bg-[#f5f7f3]">
            <SourceFileExplorer
              files={fileArtifact.files}
              {selectedFile}
              onSelect={openSourceFile}
            />
          </div>
        </section>
      {:else}
      {#if diagramArtifactTypes.has(selected)}
        <div class="overflow-hidden bg-white">
          <div class="flex items-center justify-between gap-2 border-b border-[#e4e4de] px-3 py-2 text-[11px] font-semibold text-[#676960]">
            <span class="flex items-center gap-2"><Image size={13} /> Rendered view</span>
            <span class="flex items-center gap-1 font-normal text-[#85877e]"><Maximize2 size={12} /> Click to expand</span>
          </div>
          {#if selected === 'deployment_diagram'}
            {#if deploymentTargets.length > 1}
              <div class="mb-2 flex gap-1.5 overflow-x-auto" role="tablist" aria-label="Deployment target preview">
                {#each deploymentTargets as target}
                  <button
                    class="focus-ring whitespace-nowrap rounded-md border px-2 py-1.5 text-[11px] font-semibold {deploymentTargetId === target.id ? 'border-[#86aa93] bg-[#eaf4ed] text-[#285b43]' : 'border-[#dde1da] bg-white text-[#74776f]'}"
                    role="tab"
                    aria-selected={deploymentTargetId === target.id}
                    onclick={() => (deploymentTargetId = target.id ?? '')}
                  >
                    {target.provider.toUpperCase()} · {target.region}
                  </button>
                {/each}
              </div>
            {/if}
            {#if deploymentTargetId}
              <DeploymentSizingPanel
                {appId}
                targetId={deploymentTargetId}
                onApplied={onDeploymentSizingApplied}
              />
            {/if}
            <div class="mb-3 grid grid-cols-2 rounded-lg bg-[#f0f1ed] p-1" role="tablist" aria-label="Deployment diagram view">
              {#each [['runtime', 'Runtime placement'], ['provisioning', 'Creation dependencies']] as view}
                <button
                  class="focus-ring rounded-md px-2 py-1.5 text-[11px] font-semibold {deploymentView === view[0] ? 'bg-white text-[#285b43] shadow-sm' : 'text-[#74776f]'}"
                  role="tab"
                  aria-selected={deploymentView === view[0]}
                  onclick={() => (deploymentView = view[0] as typeof deploymentView)}
                >{view[1]}</button>
              {/each}
            </div>
          {/if}
          {#if selected === 'sequence_diagram'}
            {#if sequenceLoading}
              <p class="rounded-lg bg-[#f8f8f5] p-6 text-center text-xs text-[#85877e]">Loading per-use-case diagrams...</p>
            {:else if sequenceError}
              <p class="rounded-lg bg-[#f8f8f5] p-6 text-center text-xs text-[#85877e]">{sequenceError}</p>
            {:else if sequenceDiagrams.length === 0}
              <p class="rounded-lg bg-[#f8f8f5] p-6 text-center text-xs text-[#85877e]">No use-case sequence diagrams are available.</p>
            {:else}
              {#if !automaticRepairActive && validation?.findings?.length}
                <div class="mb-3 rounded-lg border border-[#e3c98b] bg-[#fff8e7] px-3 py-2 text-xs leading-5 text-[#755b24]" role="status">
                  <strong>Review required.</strong> {validation.findings.length} semantic finding{validation.findings.length === 1 ? '' : 's'} remain; rendered cards are drafts, not approved sequence contracts.
                  Use a revision request to address the affected use cases.
                </div>
              {/if}
              {#if validation?.method_proposals?.length}
                <div class="mb-3 rounded-lg border border-[#b8d7c5] bg-[#eef8f1] px-3 py-2 text-xs leading-5 text-[#24553d]" role="status">
                  <strong>Class-method approval required.</strong> Review the proposed operations in Validation, then approve them to add every proposal and regenerate only the affected UC cards.
                  <button
                    class="focus-ring mt-2 rounded-md border border-[#78a88a] bg-white px-2.5 py-1 text-[11px] font-semibold text-[#24553d] disabled:cursor-not-allowed disabled:opacity-50"
                    onclick={() => onSequenceMethodApproval?.()}
                    disabled={!sequenceMethodApprovalAvailable || !onSequenceMethodApproval}
                  >
                    Approve all proposed methods
                  </button>
                </div>
              {/if}
              <div class="sequence-diagram-gallery grid grid-cols-1 gap-3 xl:grid-cols-2">
                {#each sequenceDiagrams as diagram (diagram.use_case_id)}
                  <article class="sequence-diagram-card min-w-0 overflow-hidden rounded-lg border border-[#e1e1db] bg-white">
                    <h3 class="truncate border-b border-[#ecece7] bg-[#f8f8f5] px-3 py-2 text-xs font-semibold">
                      {diagram.use_case_name && diagram.use_case_name !== diagram.use_case_id
                        ? `${diagram.use_case_id} · ${diagram.use_case_name}`
                        : diagram.use_case_id}
                    </h3>
                    <button
                      class="focus-ring block w-full cursor-zoom-in p-2"
                      onclick={() => expandDiagram(diagram)}
                      aria-label={`Expand ${diagram.use_case_name || diagram.use_case_id}`}
                    >
                      <img
                        class="sequence-diagram-image max-h-[52vh] w-full object-contain"
                        src={sequenceImageUrl(diagram, 'png')}
                        alt={`${diagram.use_case_name || diagram.use_case_id} sequence diagram`}
                        loading="lazy"
                      />
                    </button>
                  </article>
                {/each}
              </div>
            {/if}
          {:else}
            <button class="focus-ring block w-full cursor-zoom-in bg-[#f8f8f5]" onclick={() => expandDiagram()} aria-label={`Expand ${artifactLabels[selected]}`}>
              <img
                class="max-h-[68vh] w-full object-contain"
                src={diagramImageUrl(selected)}
                alt={artifactLabels[selected]}
              />
            </button>
          {/if}
        </div>
      {:else}
        <ArtifactVisualization stage={selected} value={displayContent} />
      {/if}
      <details class="border-t border-[#e1e1db] bg-white">
        <summary class="flex cursor-pointer items-center gap-2 px-3 py-2.5 text-[11px] font-semibold text-[#65675f]">
          <Braces size={13} /> Raw source
        </summary>
        <pre class="prose-json overflow-auto border-t border-[#ecece7] p-3">{rawContent(displayContent)}</pre>
      </details>
      {/if}
    {:else if tab === 'validation'}
      {#if automaticRepairActive}
        <div class="border-b border-[#e0e1da] bg-white p-3 text-xs text-[#5f6a61]" role="status">
          Validation is being repaired automatically.
        </div>
      {:else}
      <div class="border-b border-[#e0e1da] bg-white p-3">
        <div class="mb-4 flex items-center gap-2">
          <ShieldCheck size={17} class="text-[#2d7354]" />
          <strong class="text-sm">Artifact validation</strong>
        </div>
        <dl class="space-y-3 text-xs">
          <div class="flex justify-between"><dt>Syntax validation</dt><dd>{validation?.valid == null ? 'Not applicable' : validation.valid ? 'Passed' : 'Failed'}</dd></div>
          <div class="flex justify-between"><dt>Rule-check status</dt><dd>{validation?.check_status ?? 'Not run'}</dd></div>
          <div class="flex justify-between"><dt>Automatic repair attempts</dt><dd>{validation?.repair_iters ?? 0}</dd></div>
        </dl>
        {#if selected === 'sequence_diagram' && validation?.method_proposals?.length}
          <section class="mt-4 border-t border-[#ecece7] pt-3 text-xs">
            <strong class="text-[#24553d]">Class method additions awaiting approval</strong>
            <p class="mt-1 leading-5 text-[#65675f]">Each operation was proposed because the current class contract cannot ground the indicated sequence flow. No class is changed until you approve it.</p>
            <ul class="mt-3 space-y-2">
              {#each validation.method_proposals as proposal}
                <li class="rounded-lg border border-[#cfe2d6] bg-[#f5fbf7] p-3">
                  <code class="font-semibold text-[#24553d]">{proposal.class_name}.{proposal.method}</code>
                  <p class="mt-1 leading-5 text-[#55584f]">{proposal.reason}</p>
                  {#if proposal.step_ids?.length}
                    <p class="mt-1 text-[11px] text-[#777a70]">Flow steps: {proposal.step_ids.join(', ')}</p>
                  {/if}
                  <p class="mt-1 text-[11px] text-[#777a70]">Proposal ID: {proposal.id}</p>
                </li>
              {/each}
            </ul>
            <p class="mt-3 rounded-lg bg-[#eef8f1] px-3 py-2 leading-5 text-[#24553d]">Approve all: <code>approve all</code>. To approve selected items only, include their Proposal ID in your feedback.</p>
          </section>
        {/if}
        {#if validation?.errors?.length || validation?.findings?.length}
          <ul class="mt-4 space-y-2 border-t border-[#ecece7] pt-3 text-xs text-[#8b3d36]">
            {#each [...(validation.errors ?? []), ...(validation.findings ?? [])] as finding}
              <li>• {finding}</li>
            {/each}
          </ul>
        {:else}
          <p class="mt-4 flex items-center gap-2 border-t border-[#ecece7] pt-3 text-xs text-[#347154]">
            <CheckCircle2 size={14} /> No recorded errors.
          </p>
        {/if}
      </div>
      {/if}
    {:else if tab === 'changes'}
      <div class="divide-y divide-[#e1e1db]">
        {#if versionsError}<p class="text-xs text-[#9a4139]">{versionsError}</p>{/if}
        {#each versions as version}
          <div class="bg-white p-3 text-xs">
            <div class="flex items-center justify-between">
              <strong>Version {version.version_no ?? version.versionNo}</strong>
              <Badge tone={version.syntax_valid === false ? 'danger' : 'neutral'}>{version.origin ?? 'generated'}</Badge>
            </div>
            <p class="mt-2 text-[11px] text-[#85877e]">{version.created_at ?? ''}</p>
          </div>
        {:else}
          <p class="mt-12 text-center text-xs text-[#898b83]">No version history is available.</p>
        {/each}
      </div>
    {:else}
      {#if !fileArtifact && traceRefs.length}
        <label class="block border-b border-[#e1e1db] bg-white p-3 text-[11px] font-semibold text-[#454940]">
          Traced artifact item
          <select
            class="focus-ring mt-2 w-full rounded-md border border-[#dfe1da] bg-white px-2 py-2 font-mono text-[10px] font-normal text-[#555a52]"
            bind:value={selectedArtifactTraceRef}
          >
            {#each traceRefs as ref}
              <option value={ref}>{ref}</option>
            {/each}
          </select>
        </label>
      {/if}
      {#if selectedTraceRef && traceLoading}
        <div class="mt-16 text-center text-xs text-[#85877e]" role="status">Loading linked trace…</div>
      {:else if selectedTraceRef && trace}
        <div class="divide-y divide-[#e1e1db]">
          <div class="bg-[#f5fbf7] px-3 py-2.5 text-xs text-[#24553d]">
            <span class="text-[10px] font-bold uppercase tracking-[.12em]">Selected trace</span>
            <code class="mt-1 block break-all font-mono text-[11px]">{trace.ref || selectedTraceRef}</code>
          </div>
          {#each traceGroups as group}
            <section class="bg-white p-3" aria-label={group.label}>
              <h3 class="text-[11px] font-semibold text-[#454940]">{group.label}</h3>
              {#if group.values.length}
                <ul class="mt-2 space-y-1.5">
                  {#each group.values as value}
                    <li class="break-all rounded-md bg-[#f8f8f5] px-2 py-1.5 font-mono text-[10px] text-[#656960]">{value}</li>
                  {/each}
                </ul>
              {:else}
                <p class="mt-1.5 text-[11px] text-[#898b83]">None linked.</p>
              {/if}
            </section>
          {/each}
        </div>
      {:else if selectedTraceRef && traceError}
        <div class="border-b border-[#e1e1db] bg-[#f8f8f5] px-3 py-2 text-xs text-[#65675f]" role="status">
          {traceError}
        </div>
        <div class="divide-y divide-[#e1e1db]">
          {#each relatedEvents as event}
            <div class="bg-white p-3">
              <div class="flex items-center gap-2 text-[11px] font-semibold"><Clock3 size={12} />{event.kind}</div>
              <p class="mt-2 text-xs leading-5 text-[#66685f]">{event.text}</p>
            </div>
          {:else}
            <p class="mt-12 text-center text-xs text-[#898b83]">No related execution evidence is available.</p>
          {/each}
        </div>
      {:else}
        <div class="divide-y divide-[#e1e1db]">
          {#each relatedEvents as event}
            <div class="bg-white p-3">
              <div class="flex items-center gap-2 text-[11px] font-semibold"><Clock3 size={12} />{event.kind}</div>
              <p class="mt-2 text-xs leading-5 text-[#66685f]">{event.text}</p>
            </div>
          {:else}
            <p class="mt-12 text-center text-xs text-[#898b83]">No related execution evidence is available.</p>
          {/each}
        </div>
      {/if}
    {/if}
    </div>
  </div>
</aside>

<Dialog.Root bind:open={sourceExpanded}>
  {#if fileArtifact}
    <Dialog.Portal>
      <Dialog.Overlay class="fixed inset-0 z-[100] bg-[#101612]/55 backdrop-blur-sm" />
      <Dialog.Content class="fixed inset-0 z-[101] flex min-h-0 flex-col overflow-hidden bg-[#1e2420] text-[#d7dfd8] outline-none">
        <header class="flex h-16 shrink-0 items-center justify-between gap-4 border-b border-white/10 bg-[#262f29] px-4 sm:px-6">
          <div class="min-w-0 flex-1">
            <p class="text-[9px] font-bold uppercase tracking-[.14em] text-[#8fa296]">{artifactLabels[selected] ?? 'Implementation source'}</p>
            <Dialog.Title class="mt-0.5 truncate font-mono text-xs font-semibold">{selectedFile || 'Select a file'}</Dialog.Title>
            <Dialog.Description class="sr-only">Browse implementation files and inspect the selected source in Monaco.</Dialog.Description>
          </div>
          <div class="flex shrink-0 items-center gap-2">
            <span class="rounded bg-white/10 px-2 py-1 text-[9px] font-semibold text-[#d9e8dc]">{fileLanguage(selectedFile)}</span>
            <button
              class="focus-ring flex items-center gap-1.5 rounded-lg px-2.5 py-2 text-xs hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-50"
              onclick={copySelectedFile}
              disabled={!fileContent || fileLoading}
              aria-label="Copy source code"
            >
              {#if copiedFile}<Check size={13} /> Copied{:else}<Copy size={13} /> Copy{/if}
            </button>
            <Dialog.Close class="focus-ring flex items-center gap-1.5 rounded-lg px-2.5 py-2 text-xs hover:bg-white/10" aria-label="Close source detail">
              <X size={15} /> Close
            </Dialog.Close>
          </div>
        </header>
        <div class="flex min-h-0 flex-1 overflow-hidden">
          <aside class="h-full min-h-0 w-44 min-w-36 shrink-0 border-r border-white/10 bg-[#202722] sm:w-52 md:w-64 md:min-w-52" aria-label="Source file explorer">
            <SourceFileExplorer
              files={fileArtifact.files}
              {selectedFile}
              mode="modal"
              onSelect={openSourceFile}
            />
          </aside>
          <div class="min-h-0 min-w-0 flex-1 overflow-hidden">
            {#if fileLoading}
              <div class="flex h-full items-center justify-center gap-2 text-xs text-[#b9c8bd]" role="status">
                <LoaderCircle size={16} class="animate-spin" /> Loading source…
              </div>
            {:else if fileError}
              <p class="m-4 rounded-md bg-white/5 p-3 text-xs text-[#b9c8bd]">{fileError}</p>
            {:else}
              <ReadOnlySourceViewer path={selectedFile} value={fileContent} />
            {/if}
          </div>
        </div>
      </Dialog.Content>
    </Dialog.Portal>
  {/if}
</Dialog.Root>

{#if diagramExpanded && diagramArtifactTypes.has(selected)}
  <div
    class="fixed inset-0 z-50 bg-black/70 p-3 backdrop-blur-sm md:p-6"
    role="dialog"
    aria-modal="true"
    tabindex="-1"
    aria-label={`${artifactLabels[selected]} expanded view`}
    onkeydown={handleKeydown}
    onclick={(event) => {
      if (event.currentTarget === event.target) diagramExpanded = false;
    }}
  >
    <div class="flex h-full min-h-0 flex-col overflow-hidden rounded-2xl border border-white/20 bg-white shadow-2xl">
      <header class="flex shrink-0 items-center justify-between gap-4 border-b border-[#e2e2dc] px-4 py-3 md:px-6">
        <div class="min-w-0">
          <p class="text-[10px] font-bold uppercase tracking-[.14em] text-[#85877e]">Expanded diagram</p>
          <h2 class="truncate text-sm font-semibold">{artifactLabels[selected]}</h2>
        </div>
        <button class="focus-ring flex items-center gap-2 rounded-lg px-3 py-2 text-xs text-[#5f6259] hover:bg-[#f1f2ed]" onclick={() => (diagramExpanded = false)} aria-label="Close expanded diagram">
          <X size={16} /> Close
        </button>
      </header>
      <div class="min-h-0 flex-1 overflow-hidden">
        <DraggableDiagramViewport label={`${artifactLabels[selected]} movable diagram`}>
          {#if selected === 'sequence_diagram' && expandedSequence}
            <img
              class="mx-auto h-auto min-w-full max-w-none"
              src={sequenceImageUrl(expandedSequence, 'svg')}
              alt={`${expandedSequence.use_case_name || expandedSequence.use_case_id} expanded`}
              draggable="false"
            />
          {:else}
            <img
              class="mx-auto h-auto min-w-full max-w-none"
              src={diagramImageUrl(selected)}
              alt={`${artifactLabels[selected]} expanded`}
              draggable="false"
            />
          {/if}
        </DraggableDiagramViewport>
      </div>
    </div>
  </div>
{/if}
