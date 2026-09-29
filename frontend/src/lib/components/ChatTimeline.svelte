<script lang="ts">
  import { AlertTriangle, Bot, CheckCircle2, Circle, CircleHelp, LoaderCircle, UserRound } from '@lucide/svelte';
  import {
    hasPendingRevisionPlan,
    hasCompletedRevisionExecution,
    revisionPlanTargetLabel,
    type ArtifactDocument,
    type CloudProvider,
    type CloudRegionOption,
    type DeploymentPreferences,
    type FileArtifactSnapshot,
    type RevisionPlanTarget,
    type RevisionExecution,
    type WorkspaceCommand,
    type WorkspaceEvent
  } from '$lib/types';
  import { formatTime } from '$lib/utils';
  import { Badge } from '$lib/components/ui/badge';
  import ArtifactConversationCard from '$lib/components/ArtifactConversationCard.svelte';
  import { artifactPresent, fileArtifactTypes } from '$lib/artifacts';
  import DeploymentPreferencesCard from '$lib/components/DeploymentPreferencesCard.svelte';
  import ImplementationErrorPanel from '$lib/components/ImplementationErrorPanel.svelte';
  import LlmTimingHistory from '$lib/components/LlmTimingHistory.svelte';
  import { workspaceProgressCards, type ProgressCard } from '$lib/workspace-timeline';

  let {
    appId,
    events,
    command = null,
    document,
    fileArtifacts,
    implementationErrors = [],
    regions,
    showDeploymentPreferences = false,
    initialProvider,
    preferenceSaving = false,
    onDeploymentPreferencesSave,
    onArtifactSelect
  }: {
    appId: string;
    events: WorkspaceEvent[];
    command?: WorkspaceCommand | null;
    document?: ArtifactDocument | null;
    fileArtifacts: Record<string, FileArtifactSnapshot>;
    implementationErrors?: string[];
    regions: Record<CloudProvider, CloudRegionOption[]>;
    showDeploymentPreferences?: boolean;
    initialProvider?: CloudProvider;
    preferenceSaving?: boolean;
    onDeploymentPreferencesSave: (preferences: DeploymentPreferences) => Promise<void>;
    onArtifactSelect: (stage: string) => void;
  } = $props();
  let progressCards = $derived(workspaceProgressCards(events));
  let progressCardCommandIds = $derived(new Set(progressCards.map((card) => card.commandId)));
  let latestProgress = $derived(
    [...events]
      .reverse()
      .find(
        (event) =>
          event.kind === 'progress' &&
          event.metadata?.progress_event !== 'testingProgressUpdated' &&
          event.metadata?.progress_event !== 'durableProgressCards' &&
          event.metadata?.progress_event !== 'progressCardPatch'
      )
  );
  let latestImplementationError = $derived(
    [...events].reverse().find(
      (event) => event.stage === 'implementation' && event.kind === 'error'
    )?.event_id
  );
  let implementationCompletionEventId = $derived(
    [...events]
      .reverse()
      .find(
        (event) =>
          event.stage === 'implementation' &&
          event.kind === 'status' &&
          String(event.metadata?.status ?? '') === 'COMPLETED'
      )?.event_id ?? 0
  );
  let implementationTimelineResetId = $derived(
    [...events]
      .reverse()
      .find(
        (event) =>
          event.stage === 'implementation' &&
          event.metadata?.reset_implementation_timeline === true
      )?.event_id ?? 0
  );
  let implementationFocus = $derived.by(() => {
    if (latestProgress?.stage !== 'implementation') return null;
    const latestOwnerEvents = new Map<string, WorkspaceEvent>();
    for (const event of events) {
      const step = String(event.metadata?.step ?? '');
      if (
        event.stage !== 'implementation' ||
        event.kind !== 'progress' ||
        event.command_id !== latestProgress.command_id ||
        event.event_id < implementationTimelineResetId ||
        !['phase-backend', 'phase-frontend'].includes(step)
      ) {
        continue;
      }
      const previous = latestOwnerEvents.get(step);
      if (!previous || previous.event_id < event.event_id) latestOwnerEvents.set(step, event);
    }
    const activeOwnerEvent = [...latestOwnerEvents.values()]
      .filter((event) => String(event.metadata?.progress_status ?? '') === 'running')
      .sort((left, right) => right.event_id - left.event_id)[0];
    const metadata = activeOwnerEvent?.metadata ?? {};
    const file = String(metadata.current_file ?? '');
    const className = String(metadata.current_class ?? '');
    if (!file && !className) return null;
    return { file, className };
  });
  let activeSpecTasks = $derived(
    Array.isArray(latestProgress?.metadata?.active_spec_tasks)
      ? latestProgress.metadata.active_spec_tasks
      : []
  );
  let progressSteps = $derived.by(() => {
    if (!latestProgress) return [];
    const commandEvents = events.filter(
      (event) =>
        event.kind === 'progress' &&
        event.command_id === latestProgress?.command_id &&
        (event.stage !== 'implementation' || event.event_id >= implementationTimelineResetId)
    );
    const steps = new Map<
      string,
      { id: string; label: string; detail: string; status: string }
    >();
    for (const event of commandEvents) {
      const id = String(event.metadata?.analysis_step ?? event.metadata?.step ?? '');
      if (!id) continue;
      const previous = steps.get(id);
      const progressEvent = String(event.metadata?.progress_event ?? '');
      const status = String(
        event.metadata?.progress_status ??
          (progressEvent === 'analysisStepFinished'
            ? event.metadata?.status ?? 'completed'
            : progressEvent === 'analysisStepStarted'
              ? 'running'
              : previous?.status ?? 'running')
      );
      steps.set(id, {
        id,
        label: String(event.metadata?.progress_step_label ?? previous?.label ?? id),
        detail: String(event.metadata?.progress_detail ?? previous?.detail ?? ''),
        status
      });
    }
    const order = (id: string): number => {
      if (id === 'phase-backend') return 100;
      if (id === 'phase-frontend') return 200;
      if (id === 'phase-integration') return 300;
      return 400;
    };
    return [...steps.values()].sort((left, right) => order(left.id) - order(right.id));
  });
  let visibleProgressSteps = $derived.by(() => {
    if (latestProgress?.stage === 'implementation') {
      return progressSteps.filter((step) =>
        ['phase-backend', 'phase-frontend', 'phase-integration'].includes(step.id)
      );
    }
    return progressSteps.filter(
      (step) =>
        !['validate-input', 'generate-sources', 'prepare-build', 'verify-generated', 'plan-workflow'].includes(step.id)
    );
  });
  let visibleEvents = $derived.by(() => {
    const lastProgressId = latestProgress?.event_id;
    return events.filter(
      (event) =>
        !(
          event.stage === 'implementation' &&
          implementationTimelineResetId > 0 &&
          event.event_id < implementationTimelineResetId
        ) &&
        event.metadata?.progress_event !== 'testingProgressUpdated' &&
        event.metadata?.progress_event !== 'durableProgressCards' &&
        event.metadata?.progress_event !== 'progressCardPatch' &&
        !(
          event.stage === 'implementation' &&
          event.kind === 'progress' &&
          implementationCompletionEventId > 0 &&
          event.event_id < implementationCompletionEventId
        ) &&
        !(
          event.stage === 'implementation' &&
          event.metadata?.reset_implementation_timeline === true
        ) &&
        (event.kind !== 'status' || eventArtifactStages(event).length > 0) &&
        (event.kind !== 'progress' ||
          event.metadata?.progress_event === 'designLlmMetrics' ||
          (!progressCardCommandIds.has(String(event.command_id ?? '')) &&
            event.event_id === lastProgressId))
    );
  });
  type TimelineEntry =
    | { key: string; type: 'event'; eventId: number; order: number; event: WorkspaceEvent }
    | { key: string; type: 'progress-card'; eventId: number; order: number; card: ProgressCard };
  let timelineEntries = $derived.by((): TimelineEntry[] => [
    ...visibleEvents.map((event) => ({
      key: `event:${event.event_id}`,
      type: 'event' as const,
      eventId: event.event_id,
      order: 0,
      event
    })),
    ...progressCards.map((card) => ({
      key: `progress:${card.commandId}:${card.id}`,
      type: 'progress-card' as const,
      eventId: card.eventId,
      order: card.order,
      card
    }))
  ].sort((left, right) => left.eventId - right.eventId || left.order - right.order || left.key.localeCompare(right.key)));
  let artifactEventOwners = $derived.by(() => {
    const owners = new Map<string, number>();
    for (const event of events) {
      for (const stage of artifactCandidates(event).filter(available)) {
        owners.set(stage, event.event_id);
      }
    }
    return owners;
  });

  function available(stage: string): boolean {
    return Boolean(fileArtifacts[stage]) || artifactPresent(document?.artifacts?.[stage]);
  }

  function topLevelProgressSteps(card: ProgressCard) {
    const taskIds = new Set(card.tasks.map((task) => task.id));
    return card.tasks.filter((task) => !task.parentId || !taskIds.has(task.parentId));
  }

  function childProgressSteps(card: ProgressCard, parentId: string) {
    return card.tasks.filter((task) => task.parentId === parentId);
  }

  function artifactCandidates(event: WorkspaceEvent): string[] {
    const candidates: string[] = [];
    const phase = String(event.metadata?.phase ?? '');
    if (event.stage === 'requirements' && event.kind !== 'status') {
      const requirementStage = {
        requirements: 'refined_requirements',
        use_cases: 'usecase_spec',
        specs: 'usecase_spec',
        relationships: 'usecase_diagram',
        diagram: 'usecase_diagram'
      }[phase];
      if (requirementStage) candidates.push(requirementStage);
    }

    if (event.stage === 'design' && event.kind !== 'status') {
      const designStage = String(
        event.metadata?.design?.stage ?? event.metadata?.current_stage ?? ''
      );
      if (designStage) candidates.push(designStage);
    }

    if (
      event.stage === 'implementation' &&
      (event.kind === 'progress' ||
        (event.kind === 'status' &&
          String(event.metadata?.status ?? '') === 'COMPLETED') ||
        (event.actor === 'assistant' &&
          String(event.metadata?.status ?? '') === 'COMPLETED'))
    ) {
      candidates.push(...fileArtifactTypes);
    }

    return [...new Set(candidates)];
  }

  function eventArtifactStages(event: WorkspaceEvent): string[] {
    return artifactCandidates(event).filter(
      (stage) => available(stage) && artifactEventOwners.get(stage) === event.event_id
    );
  }

  function eventText(event: WorkspaceEvent): string {
    if (event.stage === 'implementation' && event.kind === 'error') {
      return 'An implementation error occurred. Review the detailed error log below.';
    }
    if (
      event.stage === 'implementation' &&
      event.kind === 'status' &&
      String(event.metadata?.status ?? '') === 'COMPLETED'
    ) {
      return 'Review the generated implementation artifacts below.';
    }
    return event.text;
  }

  function revisionPlanTargets(event: WorkspaceEvent, key: string): RevisionPlanTarget[] {
    const targets = event.metadata?.[key];
    return Array.isArray(targets) ? targets : [];
  }

  function revisionExecution(event: WorkspaceEvent): RevisionExecution {
    const execution = event.metadata?.revision_execution;
    return execution && typeof execution === 'object' ? (execution as RevisionExecution) : {};
  }

  function executionGroups(execution: RevisionExecution, key: keyof Pick<
    RevisionExecution,
    'touched_targets' | 'regenerated_targets' | 'stale_targets'
  >): Array<[string, string[]]> {
    const groups = execution[key];
    return groups && typeof groups === 'object'
      ? Object.entries(groups).filter(([, refs]) => Array.isArray(refs) && refs.length > 0)
      : [];
  }
</script>

<div class="mx-auto w-full max-w-3xl px-5 pb-8 pt-6">
  {#if events.length === 0}
    <div class="mt-20 text-center text-[#74766e]">
      <Bot class="mx-auto mb-4" size={30} strokeWidth={1.5} />
      <p class="text-sm">Waiting for the first command.</p>
    </div>
  {/if}
  {#each timelineEntries as item (item.key)}
    {#if item.type === 'progress-card'}
      {@const card = item.card}
      <div
        class="mb-4 ml-11 rounded-xl border border-[#dfe3dc] bg-[#fafbf8] px-3 py-2.5 text-xs text-[#555950]"
        data-kind="progress"
        data-command-id={card.commandId}
        data-progress-card-id={card.id}
      >
        <div class="mb-2 flex items-center justify-between gap-3">
          <span class="font-semibold text-[#343831]">{card.label}</span>
          {#if card.createdAt}
            <time class="text-[10px] text-[#a0a29a]">{formatTime(card.createdAt)}</time>
          {/if}
        </div>
        <div class="space-y-2">
          {#if card.stage === 'implementation' && implementationFocus && card.commandId === latestProgress?.command_id}
            <div class="rounded-lg border border-[#dfe6dd] bg-[#f3f7f2] px-2.5 py-2 text-[10px] leading-5 text-[#3b453f]">
              <div class="font-medium text-[#2f3d33]">Current implementation target</div>
              <div class="mt-0.5 flex flex-wrap items-center gap-1.5">
                <span class="font-mono text-[9px] text-[#57615d]">{implementationFocus.file}</span>
                {#if implementationFocus.className}
                  <span class="rounded bg-[#dfeee2] px-1.5 py-0.5 text-[9px] font-semibold text-[#2d7354]">{implementationFocus.className}</span>
                {/if}
              </div>
            </div>
          {/if}
          {#each topLevelProgressSteps(card) as step (step.id)}
            <div class="flex items-start gap-2">
              {#if step.status === 'completed'}
                <CheckCircle2 size={13} class="mt-0.5 shrink-0 text-[#5d806c]" />
              {:else if step.status === 'failed'}
                <AlertTriangle size={13} class="mt-0.5 shrink-0 text-[#a8433a]" />
              {:else if step.status === 'waiting'}
                <Circle size={13} class="mt-0.5 shrink-0 text-[#b1b4ac]" />
              {:else}
                <LoaderCircle size={13} class="mt-0.5 shrink-0 animate-spin text-[#2d7354]" />
              {/if}
              <div class="min-w-0">
                <div class="leading-4">{step.label}</div>
                {#if step.detail && step.detail !== 'Started'}
                  <div class="mt-0.5 text-[10px] leading-4 text-[#85887f]">{step.detail}</div>
                {/if}
              </div>
            </div>
            {#if childProgressSteps(card, step.id).length}
              <div class="ml-5 space-y-2 border-l border-[#dfe6dd] pl-3">
                {#each childProgressSteps(card, step.id) as child (child.id)}
                  <div class="flex items-start gap-2">
                    {#if child.status === 'completed'}
                      <CheckCircle2 size={12} class="mt-0.5 shrink-0 text-[#5d806c]" />
                    {:else if child.status === 'failed'}
                      <AlertTriangle size={12} class="mt-0.5 shrink-0 text-[#a8433a]" />
                    {:else if child.status === 'waiting'}
                      <Circle size={12} class="mt-0.5 shrink-0 text-[#b1b4ac]" />
                    {:else}
                      <LoaderCircle size={12} class="mt-0.5 shrink-0 animate-spin text-[#2d7354]" />
                    {/if}
                    <div class="min-w-0">
                      <div class="leading-4">{child.label}</div>
                      {#if child.detail && child.detail !== 'Started'}
                        <div class="mt-0.5 text-[10px] leading-4 text-[#85887f]">{child.detail}</div>
                      {/if}
                    </div>
                  </div>
                {/each}
              </div>
            {/if}
          {/each}
        </div>
      </div>
    {:else}
      {@const event = item.event}
      {@const relatedArtifacts = eventArtifactStages(event)}
      {@const isLlmMetrics = event.metadata?.progress_event === 'designLlmMetrics'}
      {#if event.kind === 'progress'}
      <div
        class="mb-4 ml-11 rounded-xl border border-[#dfe3dc] bg-[#fafbf8] px-3 py-2.5 text-xs text-[#555950]"
        data-kind="progress"
      >
        <div class="mb-2 flex items-center justify-between gap-3">
          <span class="font-semibold text-[#343831]">
            {isLlmMetrics
              ? 'LLM run history'
              : event.stage === 'implementation'
                ? 'Implementation progress'
                : String(event.metadata?.progress_card_label ?? 'Requirements analysis')}
          </span>
          <time class="text-[10px] text-[#a0a29a]">{formatTime(event.created_at)}</time>
        </div>
        <div class="space-y-2">
          {#if isLlmMetrics}
            <LlmTimingHistory
              {appId}
              eventId={event.event_id}
              count={Number(event.metadata?.llm_timing_count ?? 0)}
            />
          {:else if visibleProgressSteps.length === 0}
            <div class="flex items-center gap-2">
              <LoaderCircle size={13} class="shrink-0 animate-spin text-[#2d7354]" />
              <span>{event.text}</span>
            </div>
          {:else}
            {#if implementationFocus}
              <div class="rounded-lg border border-[#dfe6dd] bg-[#f3f7f2] px-2.5 py-2 text-[10px] leading-5 text-[#3b453f]">
                <div class="font-medium text-[#2f3d33]">Current implementation target</div>
                <div class="mt-0.5 flex flex-wrap items-center gap-1.5">
                  <span class="font-mono text-[9px] text-[#57615d]">{implementationFocus.file}</span>
                  {#if implementationFocus.className}
                    <span class="rounded bg-[#dfeee2] px-1.5 py-0.5 text-[9px] font-semibold text-[#2d7354]">{implementationFocus.className}</span>
                  {/if}
                </div>
              </div>
            {/if}
            {#each visibleProgressSteps as step (step.id)}
              <div class="flex items-start gap-2">
                {#if step.status === 'completed'}
                  <CheckCircle2 size={13} class="mt-0.5 shrink-0 text-[#5d806c]" />
                {:else if step.status === 'failed' || step.status === 'timeout' || step.status === 'needs_review'}
                  <AlertTriangle size={13} class="mt-0.5 shrink-0 text-[#a8433a]" />
                {:else if latestProgress?.stage === 'implementation' && step.status === 'pending'}
                  <Circle size={13} class="mt-0.5 shrink-0 text-[#b1b4ac]" />
                {:else}
                  <LoaderCircle size={13} class="mt-0.5 shrink-0 animate-spin text-[#2d7354]" />
                {/if}
                <div class="min-w-0">
                  <div class="leading-4">{step.label}</div>
                  {#if step.detail && step.detail !== 'Started'}
                    <div class="mt-0.5 text-[10px] leading-4 text-[#85887f]">{step.detail}</div>
                  {/if}
                  {#if step.id === 'generate_specs' && activeSpecTasks.length}
                    <ul class="mt-1.5 space-y-1 border-l border-[#dce3dd] pl-2.5 text-[10px] leading-4 text-[#62675f]">
                      {#each activeSpecTasks as task (task.id)}
                        <li class="flex items-start gap-1.5">
                          <LoaderCircle size={10} class="mt-0.5 shrink-0 animate-spin text-[#2d7354]" />
                          <span><span class="font-mono">{task.id}</span> · {task.name}</span>
                        </li>
                      {/each}
                    </ul>
                  {/if}
                </div>
              </div>
            {/each}
          {/if}
        </div>
      </div>
    {:else}
    <article class="mb-5 flex gap-3" data-kind={event.kind}>
      <div
        class="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-full border border-[#dcddd6] bg-white text-[#5d6058]"
      >
        {#if event.actor === 'user'}
          <UserRound size={15} />
        {:else if event.kind === 'error'}
          <AlertTriangle size={15} class="text-[#a8433a]" />
        {:else if event.kind === 'question' || event.kind === 'action_required'}
          <CircleHelp size={15} class="text-[#7d5b13]" />
        {:else}
          <Bot size={15} />
        {/if}
      </div>
      <div class="min-w-0 flex-1">
        <div class="mb-1.5 flex items-center gap-2">
          <span class="text-xs font-semibold">{event.actor === 'user' ? 'You' : 'EasyDep'}</span>
          <Badge tone={event.kind === 'error' ? 'danger' : event.kind === 'action_required' ? 'warning' : 'neutral'}>
            {event.stage}
          </Badge>
          <time class="text-[10px] text-[#96988f]">{formatTime(event.created_at)}</time>
        </div>
        <div
          class="whitespace-pre-wrap rounded-2xl border px-4 text-sm leading-6 shadow-[0_1px_2px_rgba(0,0,0,.02)] {event.actor === 'user'
            ? 'py-3 border-[#d8e5dd] bg-[#edf5f0]'
            : event.kind === 'error' && event.stage === 'implementation'
              ? 'pb-1 pt-3 border-[#eccbc7] bg-[#fff7f6]'
              : event.kind === 'error'
                ? 'py-3 border-[#eccbc7] bg-[#fff7f6]'
                : 'py-3 border-[#e3e3dd] bg-white'}"
        >
          {eventText(event)}
          {#if hasCompletedRevisionExecution(event.kind, event.metadata)}
            {@const execution = revisionExecution(event)}
            {@const touchedTargets = executionGroups(execution, 'touched_targets')}
            {@const regeneratedTargets = executionGroups(execution, 'regenerated_targets')}
            {@const staleTargets = executionGroups(execution, 'stale_targets')}
            {@const targetRemap = Object.entries(execution.target_remap ?? {})}
            <section
              class="mt-3 border-t border-[#dce9df] pt-2.5 text-xs leading-5 text-[#536158]"
              aria-label="Revision execution"
            >
              <div class="mb-1.5 font-semibold text-[#34463a]">Revision execution</div>
              {#if execution.changed_stages?.length}
                <div>
                  <span class="font-medium text-[#34463a]">Changed stages:</span>
                  <span class="ml-1">{execution.changed_stages.join(', ')}</span>
                </div>
              {/if}
              {#if touchedTargets.length}
                <div class="mt-1">
                  <span class="font-medium text-[#34463a]">Touched targets:</span>
                  <span class="ml-1">{touchedTargets.map(([stage, refs]) => `${stage}: ${refs.join(', ')}`).join('; ')}</span>
                </div>
              {/if}
              {#if regeneratedTargets.length}
                <div class="mt-1">
                  <span class="font-medium text-[#34463a]">Regenerated targets:</span>
                  <span class="ml-1">{regeneratedTargets.map(([stage, refs]) => `${stage}: ${refs.join(', ')}`).join('; ')}</span>
                </div>
              {/if}
              {#if staleTargets.length}
                <div class="mt-1">
                  <span class="font-medium text-[#34463a]">Stale targets:</span>
                  <span class="ml-1">{staleTargets.map(([stage, refs]) => `${stage}: ${refs.join(', ')}`).join('; ')}</span>
                </div>
              {/if}
              {#if targetRemap.length}
                <div class="mt-1">
                  <span class="font-medium text-[#34463a]">Target remap:</span>
                  <span class="ml-1">{targetRemap.map(([source, target]) => `${source} → ${target}`).join('; ')}</span>
                </div>
              {/if}
            </section>
          {/if}
          {#if hasPendingRevisionPlan(event.kind, event.metadata)}
            {@const requestedTargets = revisionPlanTargets(event, 'requested_targets')}
            {@const authorityTargets = revisionPlanTargets(event, 'authority_targets')}
            {@const downstreamTargets = revisionPlanTargets(event, 'downstream_targets')}
            <section
              class="mt-3 border-t border-[#ece8dc] pt-2.5 text-xs leading-5 text-[#5d625a]"
              aria-label="Revision plan"
            >
              <div class="mb-1.5 font-semibold text-[#343831]">Revision plan</div>
              <dl class="space-y-1.5">
                <div>
                  <dt class="font-medium text-[#343831]">Requested change</dt>
                  {#if requestedTargets.length}
                    <dd class="ml-3">{requestedTargets.map(revisionPlanTargetLabel).filter(Boolean).join(', ')}</dd>
                  {/if}
                </div>
                <div>
                  <dt class="font-medium text-[#343831]">Will edit (authority)</dt>
                  {#if authorityTargets.length}
                    <dd class="ml-3">{authorityTargets.map(revisionPlanTargetLabel).filter(Boolean).join(', ')}</dd>
                  {/if}
                </div>
                <div>
                  <dt class="font-medium text-[#343831]">Will update (downstream)</dt>
                  {#if downstreamTargets.length}
                    <dd class="ml-3">{downstreamTargets.map(revisionPlanTargetLabel).filter(Boolean).join(', ')}</dd>
                  {/if}
                </div>
              </dl>
            </section>
          {/if}
          {#if event.event_id === latestImplementationError && implementationErrors.length}
            <ImplementationErrorPanel errors={implementationErrors} />
          {/if}
          {#if event.metadata?.resource_question?.why}
            <p class="mt-2 border-t border-[#ece8dc] pt-2 text-xs leading-5 text-[#777267]">
              {event.metadata.resource_question.why}
            </p>
          {/if}
          {#if Array.isArray(event.metadata?.blocking_findings) && event.metadata.blocking_findings.length}
            <ul class="mt-2 space-y-1 border-t border-[#ece8dc] pt-2 text-xs leading-5 text-[#76554f]">
              {#each event.metadata.blocking_findings as finding}
                <li><span class="font-mono text-[10px]">{finding.code}</span> · {finding.message}</li>
              {/each}
            </ul>
          {/if}
          {#if event.metadata?.repair_state?.attempt_count > 0}
            <p class="mt-2 text-[11px] leading-5 text-[#777267]">
              Repair attempts: {event.metadata.repair_state.attempt_count}
              · accepted: {event.metadata.repair_state.accepted_count}
              · status: {event.metadata.repair_state.status}
            </p>
          {/if}
        </div>
        {#if relatedArtifacts.length}
          <div class="mt-3 grid gap-2 sm:grid-cols-2" aria-label="Generated artifacts">
            {#each relatedArtifacts as stage}
              <ArtifactConversationCard
                {appId}
                {stage}
                value={document?.artifacts?.[stage]}
                fileArtifact={fileArtifacts[stage]}
                validation={document?.validation?.[stage]}
                onOpen={onArtifactSelect}
              />
            {/each}
          </div>
        {/if}
      </div>
    </article>
    {/if}
    {/if}
  {/each}
  {#if showDeploymentPreferences && Object.values(regions).some((items) => items.length)}
    {#key appId}
      <DeploymentPreferencesCard
        {regions}
        {initialProvider}
        saving={preferenceSaving}
        onSave={onDeploymentPreferencesSave}
      />
    {/key}
  {/if}
</div>
