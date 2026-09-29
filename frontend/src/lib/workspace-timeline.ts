import type { WorkspaceCommand, WorkspaceEvent } from '$lib/types';

let nextOptimisticEventId = -1;

function optimisticEventId(): number {
  return nextOptimisticEventId--;
}

function stageLabel(stage: WorkspaceCommand['stage']): string {
  return {
    requirements: 'requirements analysis',
    design: 'system design',
    implementation: 'implementation',
    testing: 'system testing'
  }[stage];
}

function isOptimistic(event: WorkspaceEvent): boolean {
  return event.metadata?.optimistic === true;
}

function isDurableUserMessage(event: WorkspaceEvent, commandId: string): boolean {
  return event.command_id === commandId && event.actor === 'user' && event.kind === 'message' && !isOptimistic(event);
}

function isRealActivity(event: WorkspaceEvent, commandId: string): boolean {
  return event.command_id === commandId && event.kind === 'progress' && !isOptimistic(event);
}

export type ProgressStatus = 'waiting' | 'running' | 'completed' | 'failed';

export interface ProgressRow {
  id: string;
  label: string;
  detail: string;
  status: ProgressStatus;
  order: number;
  parentId?: string;
}

export interface ProgressCard {
  id: string;
  commandId: string;
  stage: string;
  order: number;
  label: string;
  status: ProgressStatus;
  tasks: ProgressRow[];
  // Kept from the original card/snapshot event so live patches do not move the card.
  eventId: number;
  createdAt?: string | null;
}

type UnknownRecord = Record<string, unknown>;

function isRecord(value: unknown): value is UnknownRecord {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function hasOwn(value: UnknownRecord, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function normalizeProgressStatus(status: unknown): ProgressStatus {
  const value = String(status ?? '').toLowerCase();
  if (['running', 'in_progress', 'active'].includes(value)) return 'running';
  if (['waiting', 'pending', 'queued', 'not_started'].includes(value)) return 'waiting';
  if (['failed', 'fail', 'error', 'timeout', 'needs_review'].includes(value)) return 'failed';
  return 'completed';
}

function cardPayloads(event: WorkspaceEvent): UnknownRecord[] {
  const metadata = event.metadata ?? {};
  const progressEvent = metadata.progress_event;
  if (progressEvent !== 'durableProgressCards' && progressEvent !== 'progressCardPatch') return [];
  const wrapped = metadata.progress_cards;
  if (isRecord(wrapped) && Array.isArray(wrapped.cards)) return wrapped.cards.filter(isRecord);
  if (progressEvent === 'progressCardPatch') {
    if (isRecord(metadata.progress_card)) return [metadata.progress_card];
    if (isRecord(metadata.card)) return [metadata.card];
  }
  return [];
}

function cardEventId(card: UnknownRecord, fallback: number): number {
  const candidate = Number(card.event_id);
  return Number.isFinite(candidate) ? candidate : fallback;
}

/** Projects persisted snapshots and live card patches into a card for every stable command/card id. */
export function workspaceProgressCards(events: WorkspaceEvent[]): ProgressCard[] {
  const cards = new Map<string, ProgressCard>();
  const orderedEvents = [...events].sort((left, right) => left.event_id - right.event_id);
  for (const event of orderedEvents) {
    if (event.kind !== 'progress' || isOptimistic(event)) continue;
    const commandId = String(event.command_id ?? '').trim();
    if (!commandId) continue;
    for (const value of cardPayloads(event)) {
      const cardId = String(value.id ?? '').trim();
      if (!cardId) continue;
      const key = `${commandId}:${cardId}`;
      const previous = cards.get(key);
      const rawTasks = Array.isArray(value.tasks) ? value.tasks.filter(isRecord) : [];
      const tasks = new Map(previous?.tasks.map((task) => [task.id, task]) ?? []);
      for (const task of rawTasks) {
        const taskId = String(task.id ?? '').trim();
        if (!taskId) continue;
        const priorTask = tasks.get(taskId);
        const parentId = hasOwn(task, 'parent_id')
          ? String(task.parent_id ?? '').trim()
          : (priorTask?.parentId ?? '');
        tasks.set(taskId, {
          id: taskId,
          label: hasOwn(task, 'label') ? String(task.label ?? taskId) : (priorTask?.label ?? taskId),
          detail: hasOwn(task, 'detail') ? String(task.detail ?? '') : (priorTask?.detail ?? ''),
          status: hasOwn(task, 'status') ? normalizeProgressStatus(task.status) : (priorTask?.status ?? 'waiting'),
          order: Number.isFinite(Number(task.order)) ? Number(task.order) : (priorTask?.order ?? tasks.size),
          ...(parentId ? { parentId } : {})
        });
      }
      cards.set(key, {
        id: cardId,
        commandId,
        stage: hasOwn(value, 'stage') ? String(value.stage ?? event.stage) : (previous?.stage ?? event.stage),
        order: Number.isFinite(Number(value.order)) ? Number(value.order) : (previous?.order ?? 0),
        label: hasOwn(value, 'label') ? String(value.label ?? cardId) : (previous?.label ?? cardId),
        status: hasOwn(value, 'status') ? normalizeProgressStatus(value.status) : (previous?.status ?? 'waiting'),
        tasks: [...tasks.values()].sort((left, right) => left.order - right.order || left.id.localeCompare(right.id)),
        eventId: previous?.eventId ?? cardEventId(value, event.event_id),
        createdAt: hasOwn(value, 'created_at')
          ? String(value.created_at ?? '') || null
          : (previous?.createdAt ?? event.created_at ?? null)
      });
    }
  }
  return [...cards.values()].sort(
    (left, right) => left.eventId - right.eventId || left.order - right.order || left.id.localeCompare(right.id)
  );
}

export function optimisticCommandEvents(
  command: WorkspaceCommand,
  text: string,
  createdAt = new Date().toISOString()
): WorkspaceEvent[] {
  const events: WorkspaceEvent[] = [];
  const userText = text.trim();
  if (userText) {
    events.push({
      event_id: optimisticEventId(), app_id: command.app_id, command_id: command.command_id,
      stage: command.stage, kind: 'message', actor: 'user', text: userText,
      metadata: { optimistic: true }, created_at: createdAt
    });
  }
  events.push({
    event_id: optimisticEventId(), app_id: command.app_id, command_id: command.command_id,
    stage: command.stage, kind: 'progress', actor: 'system',
    text: `Starting ${stageLabel(command.stage)}...`,
    metadata: { optimistic: true, progress_event: 'commandStarting', status: command.status }, created_at: createdAt
  });
  return events;
}

/** Merges a refresh snapshot or SSE batch while replacing superseded local optimistic events. */
export function reconcileWorkspaceEvents(
  existing: WorkspaceEvent[],
  incoming: WorkspaceEvent[],
  terminalCommandId = ''
): WorkspaceEvent[] {
  const durableById = new Map<number, WorkspaceEvent>();
  for (const event of [...existing, ...incoming]) {
    if (!isOptimistic(event)) durableById.set(event.event_id, event);
  }
  const durableEvents = [...durableById.values()];
  const durableUserCommands = new Set(
    durableEvents
      .filter((event) => isDurableUserMessage(event, String(event.command_id ?? '')))
      .map((event) => String(event.command_id))
  );
  const activeCommands = new Set(
    durableEvents
      .filter((event) => isRealActivity(event, String(event.command_id ?? '')))
      .map((event) => String(event.command_id))
  );
  const retainedOptimistic = existing.filter((event) => {
    if (!isOptimistic(event)) return false;
    const commandId = String(event.command_id ?? '');
    return event.actor === 'user'
      ? !durableUserCommands.has(commandId)
      : commandId !== terminalCommandId && !activeCommands.has(commandId);
  });
  return [...durableEvents, ...retainedOptimistic].sort((left, right) => left.event_id - right.event_id);
}
