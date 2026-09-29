<script lang="ts">
  import { AlertTriangle, CheckCircle2, CircleSlash2, FlaskConical, LoaderCircle } from '@lucide/svelte';
  import type {
    TestingGateReport,
    TestingGateStatus,
    TestingResultResponse
  } from '$lib/types';
  import { Badge } from '$lib/components/ui/badge';

  let { result }: { result?: TestingResultResponse | null } = $props();

  let report = $derived(result?.report ?? null);
  let verification = $derived(report?.verification ?? null);
  let reports = $derived(verification?.reports ?? {});
  let staticReport = $derived(reports.static ?? null);
  let gateRows = $derived([
    {
      id: 'static',
      label: 'Static configuration',
      description: 'Trivy checks generated deployment and configuration files.',
      value: staticReport?.trivyScan ?? staticReport
    },
    {
      id: 'package',
      label: 'Deployment package',
      description: 'Checks that generated deployment files agree with the selected topology.',
      value: staticReport?.deploymentPackage ?? null
    },
    {
      id: 'iac',
      label: 'Infrastructure as code',
      description: 'Validates the generated OpenTofu configuration.',
      value: reports.iac ?? null
    },
    {
      id: 'dynamic',
      label: 'Functional API',
      description: 'Runs use-case flows against the generated application through its OpenAPI contract.',
      value: reports.dynamicFunctional ?? null
    }
  ]);
  let overallStatus = $derived(
    normalizedStatus(report?.gateStatus ?? verification?.gateStatus ?? result?.command_status ?? '')
  );
  let counts = $derived(Object.entries(report?.gateCounts ?? verification?.gateCounts ?? {}));
  let diagnostics = $derived(report?.diagnostics ?? verification?.diagnostics ?? []);
  let blockers = $derived(report?.blocking_findings ?? []);
  let dynamicReport = $derived(reports.dynamicFunctional ?? null);

  function normalizedStatus(value: unknown): TestingGateStatus {
    const status = String(value ?? '').toUpperCase();
    if (status === 'COMPLETED') return 'PASS';
    return status || 'PENDING';
  }

  function tone(status: unknown): 'neutral' | 'success' | 'warning' | 'danger' | 'accent' {
    const value = normalizedStatus(status);
    if (value === 'PASS') return 'success';
    if (value === 'FAIL' || value === 'FAILED') return 'danger';
    if (value === 'INCONCLUSIVE' || value === 'AWAITING_INPUT') return 'warning';
    if (value === 'RUNNING' || value === 'QUEUED') return 'accent';
    return 'neutral';
  }

  function gateStatus(value: TestingGateReport | null | undefined): TestingGateStatus {
    return normalizedStatus(value?.gateStatus ?? value?.status ?? 'NOT_APPLICABLE');
  }

  function readable(value: unknown): string {
    if (value === null || value === undefined || value === '') return '';
    if (typeof value === 'string') return value;
    return JSON.stringify(value, null, 2);
  }

  function attempt(value: Record<string, unknown>) {
    return {
      stage: String(value.stage ?? 'testing'),
      outcome: String(value.outcome ?? 'recorded'),
      detail: String(value.detail ?? '')
    };
  }
</script>

<section class="min-h-full bg-[#f7f8f5]" aria-label="Testing results">
  {#if !result?.available}
    <div class="px-5 py-16 text-center text-[#85877e]">
      <FlaskConical class="mx-auto mb-3" size={26} strokeWidth={1.5} />
      <p class="text-sm font-semibold text-[#555950]">No testing run yet</p>
      <p class="mt-1 text-xs">Complete implementation and start Testing to see the report.</p>
    </div>
  {:else if !report}
    <div class="px-5 py-16 text-center text-[#5d7565]" role="status">
      {#if ['QUEUED', 'RUNNING'].includes(result.command_status ?? '')}
        <LoaderCircle class="mx-auto mb-3 animate-spin" size={26} strokeWidth={1.5} />
        <p class="text-sm font-semibold">Testing is running</p>
        <p class="mt-1 text-xs text-[#85877e]">The detailed report will appear after the verification gates finish.</p>
      {:else}
        <CircleSlash2 class="mx-auto mb-3" size={26} strokeWidth={1.5} />
        <p class="text-sm font-semibold">No completed report is available</p>
      {/if}
    </div>
  {:else}
    <header class="border-b border-[#dde2dc] bg-white px-4 py-4">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p class="text-[10px] font-bold uppercase tracking-[.13em] text-[#688071]">Latest testing run</p>
          <h3 class="mt-1 text-base font-semibold text-[#30362f]">
            {overallStatus === 'PASS' ? 'All required checks passed' : overallStatus === 'FAIL' ? 'Testing found blocking failures' : 'Testing needs attention'}
          </h3>
          {#if verification?.blockingReason}
            <p class="mt-2 max-w-3xl text-xs leading-5 text-[#76554f]">{verification.blockingReason}</p>
          {/if}
        </div>
        <Badge tone={tone(overallStatus)}>{overallStatus.replaceAll('_', ' ')}</Badge>
      </div>
      {#if counts.length}
        <div class="mt-3 flex flex-wrap gap-2" aria-label="Gate counts">
          {#each counts as [status, count]}
            <span class="rounded-md border border-[#e0e3dc] bg-[#f8f9f6] px-2 py-1 text-[10px] text-[#60645c]">
              {status.replaceAll('_', ' ')} <strong>{count}</strong>
            </span>
          {/each}
        </div>
      {/if}
    </header>

    {#if verification?.applicationLaunchError}
      <div class="m-3 rounded-lg border border-[#eccbc7] bg-[#fff7f6] p-3 text-xs leading-5 text-[#85524c]">
        <div class="mb-1 flex items-center gap-2 font-semibold"><AlertTriangle size={14} /> Application launch failed</div>
        {verification.applicationLaunchError}
      </div>
    {/if}

    <div class="space-y-3 p-3">
      {#each gateRows as gate}
        {@const status = gateStatus(gate.value)}
        <details class="overflow-hidden rounded-xl border border-[#dfe3dc] bg-white" open={status === 'FAIL' || status === 'INCONCLUSIVE'}>
          <summary class="cursor-pointer list-none px-3 py-3">
            <div class="flex items-start justify-between gap-3">
              <div class="flex min-w-0 gap-2.5">
                {#if status === 'PASS'}
                  <CheckCircle2 class="mt-0.5 shrink-0 text-[#3c7656]" size={15} />
                {:else if status === 'FAIL' || status === 'INCONCLUSIVE'}
                  <AlertTriangle class="mt-0.5 shrink-0 text-[#a8433a]" size={15} />
                {:else}
                  <CircleSlash2 class="mt-0.5 shrink-0 text-[#85877e]" size={15} />
                {/if}
                <div class="min-w-0">
                  <h4 class="text-xs font-semibold text-[#343831]">{gate.label}</h4>
                  <p class="mt-0.5 text-[11px] leading-4 text-[#777b72]">{gate.description}</p>
                </div>
              </div>
              <Badge tone={tone(status)}>{status.replaceAll('_', ' ')}</Badge>
            </div>
          </summary>

          {#if gate.value}
            <div class="space-y-3 border-t border-[#eceee9] px-3 py-3 text-xs leading-5">
              {#if gate.value.message || gate.value.reason}
                <p class="text-[#555a52]">{gate.value.reason ?? gate.value.message}</p>
              {/if}
              {#if gate.value.issues?.length}
                <div>
                  <p class="mb-1 font-semibold text-[#454a43]">Issues</p>
                  <ul class="space-y-1 text-[#7b4d47]">
                    {#each gate.value.issues as issue}<li>• {readable(issue)}</li>{/each}
                  </ul>
                </div>
              {/if}
              {#if gate.value.targets?.length}
                <div>
                  <p class="mb-1 font-semibold text-[#454a43]">Related files</p>
                  <div class="flex flex-wrap gap-1.5">
                    {#each gate.value.targets as target}<code class="rounded bg-[#f0f2ed] px-1.5 py-0.5 text-[10px]">{target}</code>{/each}
                  </div>
                </div>
              {/if}
              {#if gate.value.allowedFindings?.length}
                <p class="rounded-md bg-[#f2f7f3] px-2.5 py-2 text-[11px] text-[#42614e]">
                  {gate.value.allowedFindings.length} topology-specific finding(s) were reviewed and allowed.
                </p>
              {/if}
              {#if gate.value.commands?.length}
                <details class="rounded-md bg-[#f6f7f4] px-2.5 py-2">
                  <summary class="cursor-pointer font-semibold">Command evidence ({gate.value.commands.length})</summary>
                  <div class="mt-2 space-y-2">
                    {#each gate.value.commands as command}
                      <pre class="overflow-auto whitespace-pre-wrap rounded bg-white p-2 text-[10px]">{readable(command)}</pre>
                    {/each}
                  </div>
                </details>
              {/if}
            </div>
          {/if}
        </details>
      {/each}
    </div>

    {#if dynamicReport?.cases?.length}
      <section class="border-t border-[#dde2dc] bg-white px-3 py-4" aria-label="Functional test cases">
        <div class="mb-3 flex items-center justify-between gap-3">
          <h3 class="text-xs font-semibold">Use-case API checks</h3>
          <span class="text-[10px] text-[#85877e]">{dynamicReport.cases.length} executed</span>
        </div>
        <div class="space-y-2">
          {#each dynamicReport.cases as testCase}
            {@const caseStatus = gateStatus(testCase.result)}
            <details class="rounded-lg border border-[#e2e4de] bg-[#fafbf8]" open={caseStatus !== 'PASS'}>
              <summary class="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5">
                <div class="min-w-0">
                  <strong class="text-xs">{testCase.useCaseId ?? testCase.caseId ?? 'Use case'}</strong>
                  {#if testCase.requirementIds?.length}
                    <span class="ml-2 font-mono text-[9px] text-[#777b72]">{testCase.requirementIds.join(', ')}</span>
                  {/if}
                </div>
                <Badge tone={tone(caseStatus)}>{caseStatus.replaceAll('_', ' ')}</Badge>
              </summary>
              <div class="border-t border-[#e8eae5] px-3 py-2.5">
                {#if testCase.result?.steps?.length}
                  <div class="space-y-1.5">
                    {#each testCase.result.steps as step}
                      <div class="grid gap-1 rounded-md bg-white px-2.5 py-2 text-[10px] sm:grid-cols-[4rem_minmax(0,1fr)_auto]">
                        <strong>{step.method ?? 'HTTP'}</strong>
                        <span class="min-w-0 break-all font-mono">{step.path ?? step.operationId ?? step.stepId}</span>
                        <span class={step.statusCode && step.statusCode >= 400 ? 'text-[#9a4139]' : 'text-[#3c7656]'}>{step.statusCode ?? '—'}</span>
                      </div>
                    {/each}
                  </div>
                {/if}
                {#if testCase.result?.finding}
                  <div class="mt-2 rounded-md border border-[#edd7d3] bg-[#fff8f7] p-2.5 text-[11px] leading-5 text-[#784b45]">
                    <strong>{testCase.result.finding.code ?? 'Failure'}</strong>
                    <p>{testCase.result.finding.message}</p>
                    {#if testCase.result.finding.request || testCase.result.finding.responseBody}
                      <details class="mt-2">
                        <summary class="cursor-pointer font-semibold">Request and response</summary>
                        <pre class="mt-1 overflow-auto whitespace-pre-wrap rounded bg-white p-2 text-[10px]">{readable({ request: testCase.result.finding.request, response: testCase.result.finding.responseBody })}</pre>
                      </details>
                    {/if}
                  </div>
                {/if}
              </div>
            </details>
          {/each}
        </div>
        {#if dynamicReport.pendingCaseIds?.length}
          <p class="mt-3 text-[11px] text-[#74520c]">Pending after the first blocker: {dynamicReport.pendingCaseIds.join(', ')}</p>
        {/if}
        {#if dynamicReport.reusedCaseIds?.length}
          <p class="mt-1 text-[11px] text-[#5f6e64]">Reused passing results: {dynamicReport.reusedCaseIds.join(', ')}</p>
        {/if}
      </section>
    {/if}

    {#if blockers.length || diagnostics.length || report.repair_state?.attempt_count}
      <section class="space-y-3 border-t border-[#dde2dc] bg-[#f8f9f6] px-3 py-4">
        {#if blockers.length}
          <div>
            <h3 class="mb-2 text-xs font-semibold">Blocking findings</h3>
            <div class="space-y-2">
              {#each blockers as blocker}
                <article class="rounded-lg border border-[#ead4d0] bg-white p-2.5 text-[11px] leading-5">
                  <div class="flex flex-wrap items-center gap-2">
                    <Badge tone="danger">{blocker.code ?? 'FAILURE'}</Badge>
                    {#if blocker.repair_owner}<span>Owner: {blocker.repair_owner}</span>{/if}
                    {#if blocker.repairable === false}<span class="text-[#85524c]">Environment action required</span>{/if}
                  </div>
                  <p class="mt-1.5 text-[#674b47]">{blocker.message}</p>
                  {#if blocker.file_hints?.length}
                    <p class="mt-1 font-mono text-[10px] text-[#6d7169]">{blocker.file_hints.join(', ')}</p>
                  {/if}
                </article>
              {/each}
            </div>
          </div>
        {/if}
        {#if diagnostics.length}
          <div>
            <h3 class="mb-2 text-xs font-semibold">Diagnostics</h3>
            <ul class="space-y-1 text-[11px] leading-5 text-[#62675f]">
              {#each diagnostics as diagnostic}<li><strong>{diagnostic.code ?? 'INFO'}</strong> · {diagnostic.message}</li>{/each}
            </ul>
          </div>
        {/if}
        {#if report.repair_state?.attempt_count}
          <div>
            <div class="flex flex-wrap items-center gap-2 text-xs">
              <strong>Automatic repair</strong>
              <Badge tone={tone(report.repair_state.status)}>{report.repair_state.status}</Badge>
              <span>{report.repair_state.attempt_count} attempt(s), {report.repair_state.accepted_count} improved</span>
            </div>
            {#if report.repair_state.recent_attempts?.length}
              <ul class="mt-2 space-y-1 text-[11px] leading-5 text-[#62675f]">
                {#each report.repair_state.recent_attempts as rawAttempt}
                  {@const item = attempt(rawAttempt)}
                  <li><strong>{item.stage}</strong> · {item.outcome}{#if item.detail} — {item.detail}{/if}</li>
                {/each}
              </ul>
            {/if}
          </div>
        {/if}
      </section>
    {/if}
  {/if}
</section>
