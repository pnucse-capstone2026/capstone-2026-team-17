<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { goto } from '$app/navigation';
  import { GitBranch, PanelRightClose, PanelRightOpen, RefreshCw, Wifi, WifiOff } from '@lucide/svelte';
  import AppSidebar from '$lib/components/AppSidebar.svelte';
  import ArtifactPane from '$lib/components/ArtifactPane.svelte';
  import ChatTimeline from '$lib/components/ChatTimeline.svelte';
  import Composer from '$lib/components/Composer.svelte';
  import StageRail from '$lib/components/StageRail.svelte';
  import ResizableWorkspace from '$lib/components/ResizableWorkspace.svelte';
  import { connectEvents, getArtifacts, getClassDiagramPreview, getCloudOptions, getFileArtifact, getLiveImplementationSources, getWorkspace, listApps, saveDeploymentPreferences, sendCommand, stopCommand } from '$lib/api';
  import type { ArtifactDocument, CloudProvider, CloudRegionOption, DeploymentPreferences, FileArtifactSnapshot, LiveDiagramPreview, LiveSourceSnapshot, Stage, WorkspaceApp, WorkspaceCommand, WorkspaceEvent } from '$lib/types';
  import { errorMessage } from '$lib/utils';
  import { Badge } from '$lib/components/ui/badge';
  import { Button } from '$lib/components/ui/button';
  import {
    artifactPresent,
    artifactDevelopmentStage,
    fileArtifactTypes,
    implementationCompletionArtifactLoadKey,
    internalArtifactTypes,
    shouldLoadFileArtifactsInitially
  } from '$lib/artifacts';
  import { nextAutoAction } from '$lib/auto-mode';
  import { projectTestingRun } from '$lib/testing-results';
  import { optimisticCommandEvents, reconcileWorkspaceEvents } from '$lib/workspace-timeline';

  const AUTO_MODE_STORAGE_KEY = 'easydep:auto-mode';

  let appId = $state('');
  let apps = $state.raw<WorkspaceApp[]>([]);
  let events = $state.raw<WorkspaceEvent[]>([]);
  let progressCursor = 0;
  let command = $state.raw<WorkspaceCommand | null>(null);
  let currentStage = $state<Stage>('requirements');
  let artifacts = $state.raw<ArtifactDocument | null>(null);
  let fileArtifacts = $state.raw<Record<string, FileArtifactSnapshot>>({});
  let selectedArtifact = $state('refined_requirements');
  let sidebarCollapsed = $state(false);
  let artifactOpen = $state(true);
  let connected = $state(false);
  let loading = $state(true);
  let actionBusy = $state(false);
  let stopping = $state(false);
  let stopRequestedCommandId = $state('');
  let error = $state('');
  let source: EventSource | null = null;
  let stateRefreshTimer: ReturnType<typeof setTimeout> | null = null;
  const completedArtifactLoads = new Set<string>();
  let classPreview = $state<LiveDiagramPreview | null>(null);
  let liveSources = $state<LiveSourceSnapshot | null>(null);
  let selectedSourcePath = $state('');
  let liveOpenedForJob = '';
  let previewOpenedForCommand = '';
  let timelineScroller = $state<HTMLDivElement>();
  let followTimeline = $state(true);
  let initialized = false;
  let artifactSignatures = $state<Record<string, string>>({});
  let cloudRegions = $state<Record<CloudProvider, CloudRegionOption[]>>({ aws: [], azure: [], gcp: [] });
  let deploymentPreferences = $state<DeploymentPreferences | null>(null);
  let preferenceSaving = $state(false);
  let autoMode = $state(false);
  let autoActionKey = '';
  let checkpointMenuOpen = $state(false);
  let checkpointMode = $state<'branch' | 'rerun'>('branch');
  let checkpointStage = $state<Stage>('requirements');
  let openedCheckpointCommand = '';
  let testingOpenedForCommand = '';

  let implementationErrors = $derived.by(() => {
    const messages = events
      .filter((event) => event.stage === 'implementation' && event.kind === 'error')
      .map((event) => event.text.trim())
      .filter(Boolean);
    if (command?.stage === 'implementation' && command.error?.trim()) {
      messages.push(command.error.trim());
    }
    if (currentStage === 'implementation' && error.trim()) {
      messages.push(error.trim());
    }
    return [...new Set(messages)];
  });

  let sequenceMethodApprovalOffer = $derived(
    command?.result?.actions?.find(
      (offer) =>
        offer.action === 'advance' && offer.payload.auto_approve_method_proposals === true
    ) ?? null
  );
  let canApproveSequenceMethodProposals = $derived(Boolean(sequenceMethodApprovalOffer));
  let testingRun = $derived(projectTestingRun({ command, events }));

  let busy = $derived(actionBusy || ['QUEUED', 'RUNNING'].includes(command?.status ?? ''));
  let classGenerating = $derived(
    command?.stage === 'design' &&
      ['QUEUED', 'RUNNING'].includes(command?.status ?? '') &&
      !artifactPresent(artifacts?.artifacts?.class_diagram)
  );
  let implementationJobId = $derived(findImplementationJobId(command, events));
  let implementationActive = $derived(
    command?.stage === 'implementation' &&
      ['QUEUED', 'RUNNING', 'AWAITING_INPUT'].includes(command?.status ?? '')
  );
  let cloudPreferencesPending = $derived.by(() => {
    if (command?.stage !== 'requirements' || command.status !== 'AWAITING_INPUT') return false;
    const result = command.result ?? {};
    const questions = [
      ...(Array.isArray(result.resource_questions) ? result.resource_questions : []),
      ...(result.resource_question ? [result.resource_question] : [])
    ];
    return questions.some((question) => {
      if (!question || typeof question !== 'object') return false;
      const item = question as Record<string, unknown>;
      const field = String(item.field ?? '');
      return item.kind !== 'suggested' &&
        (field === 'provider' || field === 'region' || field === 'deploymentTargets');
    });
  });
  let cloudQuestionProvider = $derived.by(() => {
    if (!cloudPreferencesPending) return undefined;
    const result = command?.result ?? {};
    const questions = [
      ...(Array.isArray(result.resource_questions) ? result.resource_questions : []),
      ...(result.resource_question ? [result.resource_question] : [])
    ];
    const question = questions.find((item) => {
      if (!item || typeof item !== 'object') return false;
      const value = item as Record<string, unknown>;
      return value.kind !== 'suggested' && value.field === 'region';
    });
    const ui = question && typeof question === 'object'
      ? (question as Record<string, unknown>).ui
      : null;
    const knownProvider = ui && typeof ui === 'object'
      ? String((ui as Record<string, unknown>).knownProvider ?? '').toLowerCase()
      : '';
    return ['aws', 'azure', 'gcp'].includes(knownProvider)
      ? (knownProvider as CloudProvider)
      : undefined;
  });
  let selectedStage = $derived(artifactDevelopmentStage(selectedArtifact));

  onMount(() => {
    initialized = true;
    autoMode = window.localStorage.getItem(AUTO_MODE_STORAGE_KEY) === 'true';
    const syncLocation = () => {
      appId = new URL(window.location.href).searchParams.get('app') ?? '';
    };
    syncLocation();
    window.addEventListener('popstate', syncLocation);
    if (window.innerWidth < 900) {
      sidebarCollapsed = true;
      artifactOpen = false;
    }
    void refreshApps();
    getCloudOptions()
      .then((options) => {
        cloudRegions = options.regions;
      })
      .catch(() => undefined);
    return () => {
      source?.close();
      if (stateRefreshTimer) clearTimeout(stateRefreshTimer);
      window.removeEventListener('popstate', syncLocation);
    };
  });

  $effect(() => {
    if (!initialized) return;
    const id = appId;
    source?.close();
    if (id) void loadApp(id);
  });

  $effect(() => {
    const id = appId;
    const jobId = implementationJobId;
    if (!initialized || !artifactOpen || !implementationActive || !id || !jobId) return;
    void refreshLiveSources(id, jobId);
    const timer = window.setInterval(() => void refreshLiveSources(id, jobId), 2000);
    return () => window.clearInterval(timer);
  });

  $effect(() => {
    events.length;
    void tick().then(() => {
      if (timelineScroller && followTimeline) {
        timelineScroller.scrollTop = timelineScroller.scrollHeight;
      }
    });
  });

  $effect(() => {
    const run = testingRun;
    const commandId = run?.commandId ?? '';
    if (!initialized || loading || !commandId || testingOpenedForCommand === commandId) return;
    if (window.innerWidth < 900) return;
    testingOpenedForCommand = commandId;
    selectedArtifact = 'TESTING_RESULTS';
    artifactOpen = true;
  });

  $effect(() => {
    const current = command;
    if (!autoMode || busy || !current) return;
    const next = nextAutoAction(current);
    if (!next) return;
    const key = `${current.command_id}:${current.status}:${next.action}:${JSON.stringify(next.extra ?? {})}`;
    if (key === autoActionKey) return;
    autoActionKey = key;
    queueMicrotask(() => {
      if (autoMode) void act(next.action, next.extra ?? {});
    });
  });

  async function refreshApps() {
    apps = await listApps();
  }

  function scheduleStateRefresh(id: string) {
    if (stateRefreshTimer) return;
    stateRefreshTimer = setTimeout(() => {
      stateRefreshTimer = null;
      void refreshState(id).catch(() => undefined);
    }, 800);
  }

  async function loadApp(id: string) {
    loading = true;
    error = '';
    try {
      const [snapshot, document] = await Promise.all([getWorkspace(id), getArtifacts(id)]);
      events = snapshot.events;
      progressCursor = snapshot.progress_cursor;
      classPreview = null;
      previewOpenedForCommand = '';
      command = snapshot.command ?? null;
      deploymentPreferences = snapshot.deployment_preferences ?? null;
      currentStage = (command?.stage ?? snapshot.current_stage ?? 'requirements') as Stage;
      const loadedFileArtifacts = shouldLoadFileArtifactsInitially(command)
        ? await loadFileArtifacts(id)
        : {};
      if (command?.stage === 'implementation' && command.status === 'COMPLETED') {
        completedArtifactLoads.add(`${id}:${command.command_id}`);
      }
      applyArtifactSnapshot(document, loadedFileArtifacts, true);
      const liveJobId = findImplementationJobId(command, events);
      if (
        command?.stage === 'implementation' &&
        ['QUEUED', 'RUNNING', 'AWAITING_INPUT'].includes(command.status) &&
        liveJobId
      ) {
        await refreshLiveSources(id, liveJobId);
      }
      const previewEvent = [...events].reverse().find(
        (event) => event.metadata?.progress_event === 'classDiagramPreviewUpdated'
      );
      if (previewEvent?.command_id) void refreshClassPreview(id, previewEvent.command_id, false);
      connect(id);
    } catch (reason) {
      error = errorMessage(reason);
    } finally {
      loading = false;
    }
  }

  function connect(id: string) {
    source?.close();
    source = connectEvents(
      id,
      progressCursor,
      (event) => {
        connected = true;
        progressCursor = Math.max(progressCursor, event.event_id);
        if (event.metadata?.progress_event === 'commandStateChanged') {
          void refreshState(id);
          return;
        }
        events = reconcileWorkspaceEvents(events, [event]);
        if (
          event.metadata?.progress_event === 'classDiagramPreviewUpdated' &&
          event.command_id
        ) {
          void refreshClassPreview(id, event.command_id, true);
        }
        if (event.kind !== 'progress') void refreshState(id);
        else if (event.stage === 'implementation' || event.stage === 'testing') {
          scheduleStateRefresh(id);
        }
      },
      () => (connected = false)
    );
    source.onopen = () => (connected = true);
  }

  async function refreshClassPreview(id: string, commandId: string, reveal: boolean) {
    try {
      const preview = await getClassDiagramPreview(id, commandId);
      if (id !== appId) return;
      if (
        classPreview?.command_id === preview.command_id &&
        classPreview.revision >= preview.revision
      ) return;
      classPreview = preview;
      if (reveal && previewOpenedForCommand !== commandId) {
        previewOpenedForCommand = commandId;
        selectedArtifact = 'class_diagram';
        if (window.innerWidth >= 900) artifactOpen = true;
      }
    } catch {
      // A terminal or restarted command may have already released its preview.
    }
  }

  async function refreshState(id = appId) {
    if (!id) return;
    const previousCommand = command;
    const [snapshot, document] = await Promise.all([getWorkspace(id), getArtifacts(id)]);
    const nextCommand = snapshot.command ?? null;
    events = reconcileWorkspaceEvents(
      events,
      snapshot.events,
      nextCommand && !['QUEUED', 'RUNNING'].includes(nextCommand.status)
        ? nextCommand.command_id
        : ''
    );
    progressCursor = Math.max(progressCursor, snapshot.progress_cursor);
    command = nextCommand;
    deploymentPreferences = snapshot.deployment_preferences ?? null;
    currentStage = (command?.stage ?? snapshot.current_stage ?? currentStage) as Stage;
    const targetAppId = nextCommand?.result?.target_app_id;
    if (
      nextCommand?.status === 'COMPLETED' &&
      typeof targetAppId === 'string' &&
      targetAppId &&
      openedCheckpointCommand !== nextCommand.command_id
    ) {
      openedCheckpointCommand = nextCommand.command_id;
      await refreshApps();
      chooseApp(targetAppId);
      return;
    }
    let nextFileArtifacts = fileArtifacts;
    const completionKey = implementationCompletionArtifactLoadKey(previousCommand, nextCommand);
    const artifactLoadKey = completionKey ? `${id}:${completionKey}` : null;
    if (artifactLoadKey && !completedArtifactLoads.has(artifactLoadKey)) {
      completedArtifactLoads.add(artifactLoadKey);
      nextFileArtifacts = await loadFileArtifacts(id);
    }
    applyArtifactSnapshot(document, nextFileArtifacts);
    const liveJobId = findImplementationJobId(nextCommand, snapshot.events);
    if (
      nextCommand?.stage === 'implementation' &&
      ['QUEUED', 'RUNNING', 'AWAITING_INPUT'].includes(nextCommand.status) &&
      liveJobId
    ) {
      await refreshLiveSources(id, liveJobId);
    } else if (Object.keys(nextFileArtifacts).length) {
      transitionFromLiveSnapshot(nextFileArtifacts);
    }
    await refreshApps();
  }

  function findImplementationJobId(
    current: WorkspaceCommand | null,
    history: WorkspaceEvent[]
  ): string {
    const payloadJobId = current?.payload?.job_id;
    if (typeof payloadJobId === 'string' && payloadJobId) return payloadJobId;
    const resultJobId = current?.result?.job_id;
    if (typeof resultJobId === 'string' && resultJobId) return resultJobId;
    const event = [...history]
      .reverse()
      .find((item) => item.stage === 'implementation' && typeof item.metadata?.job_id === 'string');
    return typeof event?.metadata?.job_id === 'string' ? event.metadata.job_id : '';
  }

  async function refreshLiveSources(id: string, jobId: string) {
    try {
      const snapshot = await getLiveImplementationSources(id, jobId);
      if (id !== appId || jobId !== findImplementationJobId(command, events)) return;
      liveSources = snapshot;
      if (snapshot.files.length && liveOpenedForJob !== jobId) {
        liveOpenedForJob = jobId;
        selectedArtifact = 'LIVE_SOURCE';
        if (window.innerWidth >= 900) artifactOpen = true;
      }
    } catch {
      // run_root가 만들어지기 전의 짧은 404와 task 교체 중의 읽기 경쟁은 다음 event/poll에서
      // 다시 확인한다. 이미 받은 파일 목록은 완료 snapshot으로 전환할 때까지 유지한다.
    }
  }

  function transitionFromLiveSnapshot(files: Record<string, FileArtifactSnapshot>) {
    if (!liveSources) return;
    if (selectedArtifact === 'LIVE_SOURCE') {
      const selectedLive = liveSources.files.find((file) => file.path === selectedSourcePath);
      const targetType = selectedLive?.artifact_type;
      const targetPath = selectedLive?.artifact_path;
      if (
        targetType &&
        targetPath &&
        files[targetType]?.files.some((file) => file.path === targetPath)
      ) {
        selectedArtifact = targetType;
        selectedSourcePath = targetPath;
      } else {
        selectedArtifact = Object.keys(files).at(-1) ?? selectedArtifact;
      }
    }
    liveSources = null;
  }

  function artifactSnapshotSignatures(
    document: ArtifactDocument,
    files: Record<string, FileArtifactSnapshot>
  ) {
    const signatures: Record<string, string> = {};
    for (const [stage, value] of Object.entries(document.artifacts)) {
      if (!internalArtifactTypes.has(stage) && artifactPresent(value)) {
        signatures[stage] = JSON.stringify(value);
      }
    }
    for (const [stage, value] of Object.entries(files)) {
      signatures[stage] = `${value.version_no}:${value.files.map((file) => `${file.path}:${file.sha256}`).join('|')}`;
    }
    return signatures;
  }

  function applyArtifactSnapshot(
    document: ArtifactDocument,
    files: Record<string, FileArtifactSnapshot>,
    initial = false
  ) {
    const nextSignatures = artifactSnapshotSignatures(document, files);
    const nextStages = Object.keys(nextSignatures);
    const changedArtifacts = nextStages.filter(
      (stage) => nextSignatures[stage] && nextSignatures[stage] !== artifactSignatures[stage]
    );

    artifacts = document;
    fileArtifacts = files;
    artifactSignatures = nextSignatures;
    if (artifactPresent(document.artifacts.class_diagram)) classPreview = null;

    if (initial) {
      if (!nextSignatures[selectedArtifact]) {
        selectedArtifact = nextStages.at(-1) ?? selectedArtifact;
      }
      return;
    }

    const latestArtifact = changedArtifacts.at(-1);
    if (latestArtifact) {
      selectedArtifact = latestArtifact;
      if (window.innerWidth >= 900) artifactOpen = true;
    }
  }

  function reviewArtifact(stage: string) {
    selectedArtifact = stage;
    artifactOpen = true;
  }

  async function approveSequenceMethodProposals() {
    const offer = sequenceMethodApprovalOffer;
    if (!offer) return;
    await act(offer.action, offer.payload);
  }

  async function loadFileArtifacts(id: string) {
    const entries = await Promise.all(
      fileArtifactTypes.map(async (type) => {
        try {
          return [type, await getFileArtifact(id, type)] as const;
        } catch {
          return null;
        }
      })
    );
    return Object.fromEntries(entries.filter((entry) => entry !== null)) as Record<string, FileArtifactSnapshot>;
  }

  async function act(action: string, extra: Record<string, unknown> = {}) {
    if (!appId || busy) return;
    actionBusy = true;
    error = '';
    try {
      const accepted = await sendCommand(appId, { action, ...extra }) as { command: WorkspaceCommand };
      command = accepted.command;
      currentStage = accepted.command.stage;
      events = [
        ...events,
        ...optimisticCommandEvents(accepted.command, String(extra.text ?? ''))
      ];
      await refreshState();
    } catch (reason) {
      error = errorMessage(reason);
    } finally {
      actionBusy = false;
    }
  }

  async function send(text: string, extra: Record<string, unknown> = {}) {
    await act('message', {
      context: {
        stage: selectedStage,
        artifact_stage:
          selectedArtifact === 'TESTING_RESULTS' ? undefined : selectedArtifact
      },
      ...extra,
      text,
    });
  }

  async function stopCurrentCommand() {
    if (
      !appId || !command || stopping || stopRequestedCommandId === command.command_id ||
      !['QUEUED', 'RUNNING'].includes(command.status)
    ) return;
    stopping = true;
    stopRequestedCommandId = command.command_id;
    const targetAppId = appId;
    try {
      const response = await stopCommand(targetAppId, command.command_id);
      if (targetAppId === appId) command = response.command;
    } catch {
      // The command may have completed between rendering the Stop button and the request.
      // Refreshing state resolves that race without adding a synthetic chat error.
    } finally {
      try {
        if (targetAppId === appId) await refreshState(targetAppId);
      } catch {
        // SSE and the normal reconnect refresh will recover the latest workspace state.
      }
      stopping = false;
    }
  }

  async function saveCloudPreferences(preferences: DeploymentPreferences) {
    if (!appId || preferenceSaving) return;
    preferenceSaving = true;
    error = '';
    try {
      const response = await saveDeploymentPreferences(appId, preferences);
      deploymentPreferences = response.preferences;
      await refreshState();
    } catch (reason) {
      error = errorMessage(reason);
      throw reason;
    } finally {
      preferenceSaving = false;
    }
  }

  function toggleAutoMode() {
    autoMode = !autoMode;
    autoActionKey = '';
    window.localStorage.setItem(AUTO_MODE_STORAGE_KEY, String(autoMode));
  }

  async function runCheckpointTool() {
    checkpointMenuOpen = false;
    if (checkpointMode === 'branch') {
      const stage = checkpointStage === 'testing' ? 'implementation' : checkpointStage;
      await act('branch_checkpoint', { checkpoint_stage: stage });
      return;
    }
    await act('rerun_from_stage', { restart_stage: checkpointStage });
  }

  function chooseApp(id: string) {
    if (id !== appId) {
      appId = id;
      void goto(`/workspace/?app=${id}`, { replaceState: false, noScroll: true });
    }
  }

  function trackTimelineScroll() {
    if (!timelineScroller) return;
    followTimeline =
      timelineScroller.scrollHeight - timelineScroller.scrollTop - timelineScroller.clientHeight < 96;
  }
</script>

<svelte:head><title>EasyDep · Development workspace</title></svelte:head>

<div class="flex h-dvh min-h-0 overflow-hidden bg-[#f4f4f0]">
  <AppSidebar
    {apps}
    currentAppId={appId}
    collapsed={sidebarCollapsed}
    onSelect={chooseApp}
    onNew={() => goto('/')}
    onToggle={() => (sidebarCollapsed = !sidebarCollapsed)}
  />

  <main class="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
    <header class="relative z-40 flex min-h-16 shrink-0 items-center justify-between gap-4 overflow-visible border-b border-[#deded7] bg-white/90 px-5 backdrop-blur">
      <div class="min-w-0">
        <h1 class="truncate text-sm font-semibold">{apps.find((app) => app.app_id === appId)?.title ?? 'Development workspace'}</h1>
        <div class="mt-1 flex items-center gap-2 text-[10px] text-[#85877e]">
          {#if connected}<Wifi size={11} class="text-[#2d7354]" /> Live{:else}<WifiOff size={11} /> Reconnecting{/if}
          <span>·</span><span class="font-mono">{appId.slice(0, 8)}</span>
        </div>
      </div>
      <div class="hidden md:block"><StageRail current={currentStage} {command} /></div>
      <div class="flex items-center gap-2">
        <Badge tone={command?.status === 'FAILED' ? 'danger' : busy ? 'warning' : 'success'}>{command?.status ?? 'READY'}</Badge>
        <div class="relative">
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onclick={() => (checkpointMenuOpen = !checkpointMenuOpen)}
            aria-label="Branch or rerun a stage"
          ><GitBranch size={14} /></Button>
          {#if checkpointMenuOpen}
            <div class="absolute right-0 top-10 z-50 w-64 rounded-lg border border-[#deded7] bg-white p-3 shadow-lg">
              <label class="mb-1 block text-[10px] font-semibold uppercase tracking-wide text-[#777970]" for="checkpoint-mode">Action</label>
              <select id="checkpoint-mode" bind:value={checkpointMode} class="mb-3 w-full rounded border border-[#d5d5cd] bg-white px-2 py-1.5 text-xs">
                <option value="branch">Create branch after stage</option>
                <option value="rerun">Rerun stage in new branch</option>
              </select>
              <label class="mb-1 block text-[10px] font-semibold uppercase tracking-wide text-[#777970]" for="checkpoint-stage">Stage</label>
              <select id="checkpoint-stage" bind:value={checkpointStage} class="mb-3 w-full rounded border border-[#d5d5cd] bg-white px-2 py-1.5 text-xs">
                <option value="requirements">Requirements</option>
                <option value="design">Design</option>
                <option value="implementation">Implementation</option>
                {#if checkpointMode === 'rerun'}<option value="testing">Testing</option>{/if}
              </select>
              <p class="mb-3 text-[10px] leading-4 text-[#777970]">The original app stays unchanged. Rerun starts the selected stage in the new branch.</p>
              <Button size="sm" class="w-full" onclick={runCheckpointTool}>{checkpointMode === 'branch' ? 'Create branch' : 'Create and rerun'}</Button>
            </div>
          {/if}
        </div>
        <Button size="icon" variant="ghost" onclick={() => (artifactOpen = !artifactOpen)} aria-label="Toggle artifact panel">
          {#if artifactOpen}<PanelRightClose size={17} />{:else}<PanelRightOpen size={17} />{/if}
        </Button>
      </div>
    </header>

    {#if !appId}
      <div class="flex flex-1 items-center justify-center text-sm text-[#777970]">Select an application on the left or start a new one.</div>
    {:else}
      <ResizableWorkspace sidebarOpen={artifactOpen}>
        <section class="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-[#f7f7f4]">
          <div
            class="scrollbar-thin min-h-0 flex-1 overflow-y-auto overscroll-contain"
            bind:this={timelineScroller}
            onscroll={trackTimelineScroll}
          >
            {#if loading}
              <div class="mt-24 text-center text-sm text-[#85877e]">Loading workspace history…</div>
            {:else}
              <ChatTimeline
                {appId}
                {events}
                {command}
                document={artifacts}
                {fileArtifacts}
                implementationErrors={implementationErrors}
                regions={cloudRegions}
                showDeploymentPreferences={cloudPreferencesPending}
                initialProvider={cloudQuestionProvider}
                preferenceSaving={preferenceSaving}
                onDeploymentPreferencesSave={saveCloudPreferences}
                onArtifactSelect={reviewArtifact}
              />
            {/if}
          </div>
          {#if error}<div class="mx-auto mb-2 w-full max-w-3xl px-5 text-xs text-[#a24037]">{error}</div>{/if}
          <Composer
            {appId}
            {command}
            {busy}
            stopping={stopping || stopRequestedCommandId === command?.command_id}
            {autoMode}
            context={{
              stage: selectedStage,
              artifact_stage:
                selectedArtifact === 'TESTING_RESULTS' ? undefined : selectedArtifact
            }}
            onSend={send}
            onStop={stopCurrentCommand}
            onAction={act}
            onToggleAutoMode={toggleAutoMode}
          />
        </section>
        {#snippet sidebar()}
          <ArtifactPane
            {appId}
            {command}
            document={artifacts}
            {fileArtifacts}
            {liveSources}
            preferredFile={selectedSourcePath}
            {events}
            {classPreview}
            {classGenerating}
            selected={selectedArtifact}
            onSelect={reviewArtifact}
            sequenceMethodApprovalAvailable={canApproveSequenceMethodProposals}
            onSequenceMethodApproval={approveSequenceMethodProposals}
            onFileSelect={(path) => (selectedSourcePath = path)}
            onDeploymentSizingApplied={refreshState}
            onClose={() => (artifactOpen = false)}
          />
        {/snippet}
      </ResizableWorkspace>
    {/if}
  </main>
</div>
