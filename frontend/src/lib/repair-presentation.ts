import type { WorkspaceCommand } from '$lib/types';

/** The public command result is authoritative for an in-flight automatic repair. */
export function isAutomaticRepairActive(command?: WorkspaceCommand | null): boolean {
  return String(command?.result?.repair_state?.status ?? '').toUpperCase() === 'ACTIVE';
}
