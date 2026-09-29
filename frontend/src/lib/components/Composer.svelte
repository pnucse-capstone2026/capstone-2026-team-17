<script lang="ts">
  import { ArrowUp, Download, LoaderCircle, Paperclip, Square, Zap } from '@lucide/svelte';
  import { downloadImplementationArtifacts } from '$lib/api';
  import type { ActionOffer, WorkspaceCommand } from '$lib/types';
  import { Button } from '$lib/components/ui/button';
  import { isRecoveryAction } from '$lib/auto-mode';

  let {
    command,
    appId,
    busy,
    stopping,
    context,
    autoMode,
    onSend,
    onStop,
    onAction,
    onToggleAutoMode
  }: {
    command?: WorkspaceCommand | null;
    appId: string;
    busy: boolean;
    stopping: boolean;
    context?: { stage: string; artifact_stage?: string; element_ref?: string } | null;
    autoMode: boolean;
    onSend: (text: string, extra?: Record<string, unknown>) => Promise<void>;
    onStop: () => Promise<void>;
    onAction: (action: string, extra?: Record<string, unknown>) => Promise<void>;
    onToggleAutoMode: () => void;
  } = $props();
  let text = $state('');

  let result = $derived(command?.result ?? {});
  let actions = $derived(
    (Array.isArray(result?.actions) ? result.actions : []).filter(isActionOffer)
  );
  let messageChoices = $derived(
    actions.filter((offer) => offer.action === 'message' && hasMessageText(offer))
  );
  let resourceQuestion = $derived(result?.resource_question ?? result?.resource_questions?.[0] ?? null);
  let questionText = $derived(
    String(resourceQuestion?.question ?? result?.question ?? result?.questions?.[0]?.question ?? '').trim()
  );
  let messageInput = $derived(
    result?.requires_revision && !questionText
      ? undefined
      : actions.find((offer) => offer.action === 'message' && !hasMessageText(offer))
  );
  let buttonActions = $derived(
    actions.filter(
      (offer) =>
        offer.action !== 'message' &&
        !isRecoveryAction(offer.action, command)
    )
  );
  let implementationAction = $derived(
    command?.stage === 'implementation' &&
      command.action === 'start_implementation'
      ? command.action
      : null
  );
  let implementationResponse = $derived(
    implementationAction && ['QUEUED', 'RUNNING'].includes(command?.status ?? '')
      ? null
      : implementationAction && command?.status === 'COMPLETED'
        ? String(result?.message ?? 'Implementation request completed.')
        : implementationAction && command?.status === 'FAILED'
          ? null
          : null
  );
  let testingResponse = $derived(
    command?.stage === 'testing' && command.action === 'start_testing'
      ? command?.status === 'QUEUED'
        ? 'Testing requested. Preparing the test run…'
        : command?.status === 'COMPLETED'
            ? String(result?.message ?? 'Testing completed.')
            : null
      : null
  );
  let downloadError = $state('');
  let commandActive = $derived(['QUEUED', 'RUNNING'].includes(command?.status ?? ''));

  function isActionOffer(value: unknown): value is ActionOffer {
    if (!value || typeof value !== 'object') return false;
    const candidate = value as Partial<ActionOffer>;
    return (
      typeof candidate.action === 'string' &&
      typeof candidate.label === 'string' &&
      Boolean(candidate.payload) &&
      typeof candidate.payload === 'object' &&
      !Array.isArray(candidate.payload) &&
      typeof candidate.auto_selectable === 'boolean'
    );
  }

  function hasMessageText(offer: ActionOffer): boolean {
    return typeof offer.payload.text === 'string' && offer.payload.text.trim().length > 0;
  }

  async function downloadArtifacts() {
    downloadError = '';
    try {
      await downloadImplementationArtifacts(appId);
    } catch (reason) {
      downloadError = reason instanceof Error ? reason.message : 'Could not download implementation artifacts.';
    }
  }

  async function submit() {
    const value = text.trim();
    if (!value || busy || !messageInput) return;
    text = '';
    await onSend(value, messageInput.payload);
  }

  function keydown(event: KeyboardEvent) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }
</script>

<div class="mx-auto w-full max-w-3xl shrink-0 px-5 pb-5">
  {#if implementationResponse}
    <div
      class="mb-2 rounded-xl border p-2.5 text-xs {command?.status === 'FAILED'
        ? 'border-[#eccbc7] bg-[#fff7f6] text-[#85524c]'
        : command?.status === 'COMPLETED'
          ? 'border-[#cfe3d5] bg-[#f1f8f3] text-[#2d7354]'
          : 'border-[#cfe3d5] bg-[#f1f8f3] text-[#2d7354]'}"
      role="status"
      aria-live="polite"
    >
      <span class="flex items-center gap-2">
        {#if ['QUEUED', 'RUNNING'].includes(command?.status ?? '')}
          <LoaderCircle size={13} class="shrink-0 animate-spin text-[#2d7354]" />
        {/if}
        {implementationResponse}
      </span>
    </div>
  {/if}
  {#if testingResponse}
    <div
      class="mb-2 rounded-xl border border-[#cfe3d5] bg-[#f1f8f3] p-2.5 text-xs text-[#2d7354]"
      role="status"
      aria-live="polite"
    >
      <span class="flex items-center gap-2">
        {#if ['QUEUED', 'RUNNING'].includes(command?.status ?? '')}
          <LoaderCircle size={13} class="shrink-0 animate-spin text-[#2d7354]" />
        {/if}
        {testingResponse}
      </span>
    </div>
  {/if}
  {#if questionText}
    <div class="mb-2 rounded-xl border border-[#e5ddc9] bg-[#fffaf0] p-2.5 text-xs text-[#74520c]">
      <p class="text-sm font-medium text-[#5f4610]">{questionText}</p>
    </div>
  {/if}

  {#if messageChoices.length || buttonActions.length || messageInput?.description}
    <div class="mb-2 flex flex-wrap items-center gap-2 rounded-xl border border-[#e5ddc9] bg-[#fffaf0] p-2.5">
      {#if messageChoices.length}
        <div class="grid w-full gap-2 sm:grid-cols-2">
          {#each messageChoices as offer}
            <button
              type="button"
              class="rounded-lg border border-[#d9caa4] bg-white px-3 py-2 text-left transition hover:border-[#86ad98] hover:bg-[#f6fbf7] disabled:cursor-not-allowed disabled:opacity-60"
              onclick={() => onAction(offer.action, offer.payload)}
              disabled={busy}
            >
              <span class="block text-xs font-semibold text-[#37433b]">{offer.label}</span>
              {#if offer.description}
                <span class="mt-1 block text-[11px] leading-4 text-[#6d7068]">{offer.description}</span>
              {/if}
            </button>
          {/each}
        </div>
      {/if}
      {#each buttonActions as offer}
        <div class="flex items-center gap-2">
          <Button size="sm" onclick={() => onAction(offer.action, offer.payload)} disabled={busy}>
            {offer.label}
          </Button>
          {#if offer.description}
            <span class="text-[11px] leading-4 text-[#6d7068]">{offer.description}</span>
          {/if}
        </div>
      {/each}
      {#if messageInput?.description}
        <span class="w-full px-1 text-[11px] leading-4 text-[#6d7068]">{messageInput.description}</span>
      {/if}
    </div>
  {/if}

  {#if command?.status === 'COMPLETED' && command.stage === 'testing'}
    <div class="mb-2 flex flex-wrap items-center gap-2">
      <Button size="sm" onclick={downloadArtifacts} disabled={busy}>
        <Download size={13} /> Download implementation ZIP
      </Button>
      {#if downloadError}<span class="text-xs text-[#85524c]">{downloadError}</span>{/if}
    </div>
  {/if}

  {#if context}
    <div class="mb-2 flex items-center gap-2 px-1 text-[11px] text-[#5e6159]">
      <Paperclip size={12} />
      {#if context.element_ref}
        Feedback targets <code>{context.element_ref}</code> only; trace-linked artifacts may be updated.
      {:else}
        Feedback references the {context.artifact_stage ?? context.stage} artifact
      {/if}
    </div>
  {/if}
  <div class="rounded-2xl border border-[#d8d9d2] bg-white p-2 shadow-[0_8px_30px_rgba(31,35,29,.08)] focus-within:border-[#86ad98]">
    <textarea
      bind:value={text}
      onkeydown={keydown}
      rows="2"
      class="max-h-40 min-h-14 w-full resize-none border-0 bg-transparent px-2 py-2 text-sm leading-6 outline-none placeholder:text-[#999b93]"
      placeholder={messageInput
          ? questionText || messageInput.label || 'Enter a response'
          : messageChoices.length
            ? 'Choose one of the answers above'
            : 'No message action is currently available'}
      disabled={busy || !messageInput}
    ></textarea>
    <div class="flex items-center justify-between px-1 pb-1">
      <span class="text-[10px] text-[#a0a199]">Enter to send · Shift+Enter for a new line</span>
      <div class="flex items-center gap-1">
        <Button
          size="icon"
          variant={autoMode ? 'default' : 'ghost'}
          class="size-7 rounded-lg"
          onclick={onToggleAutoMode}
          aria-label={autoMode ? 'Disable automatic progress' : 'Enable automatic progress'}
          aria-pressed={autoMode}
          title={autoMode
            ? 'Automatic progress is enabled. Required questions and failures still pause the run.'
            : 'Automatically continue when no answer is required.'}
        >
          <Zap size={14} />
        </Button>
        {#if commandActive}
          <Button size="icon" variant="ghost" onclick={onStop} disabled={stopping} aria-label="Stop current command">
            {#if stopping}<LoaderCircle size={15} class="animate-spin" />{:else}<Square size={14} />{/if}
          </Button>
        {:else}
          <Button size="icon" onclick={submit} disabled={busy || !messageInput || !text.trim()} aria-label="Send message">
            <ArrowUp size={16} />
          </Button>
        {/if}
      </div>
    </div>
  </div>
</div>
