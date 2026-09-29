import type { TestingStatus } from '$lib/testing-results';

export type PublicTestingStatus = 'PASS' | 'FAIL' | 'RUNNING' | 'PENDING';

/**
 * Restrict the sidebar to statuses users can act on without making planned or
 * reused work look like completed verification. Demo skips are terminal PASS
 * in the result contract, so legacy SKIPPED values remain a public success.
 */
export function publicTestingStatus(value: TestingStatus | string | null | undefined): PublicTestingStatus {
  switch (String(value ?? '').trim().toUpperCase()) {
    case 'PASS':
    case 'PASSED':
    case 'SKIPPED':
      return 'PASS';
    case 'FAIL':
    case 'FAILED':
      return 'FAIL';
    case 'RUNNING':
      return 'RUNNING';
    default:
      return 'PENDING';
  }
}
