import type { WorkspaceCommand, WorkspaceEvent } from '$lib/types';

export type TestingStatus =
  | 'PENDING'
  | 'RUNNING'
  | 'PASS'
  | 'FAIL'
  | 'INCONCLUSIVE'
  | 'DEFERRED'
  | 'REUSED'
  | 'SKIPPED';

export interface TestingStepView {
  stepId: string;
  operationId?: string;
  label: string;
  status: TestingStatus;
  detail?: string;
  method?: string;
  path?: string;
  expectedStatus?: string;
  inputLinks: string[];
  statusCode?: number;
  control?: string;
  elapsedMs?: number;
  attempt?: number;
  contractStatus?: string;
  semanticStatus?: string;
  criteria: Array<{ condition: string; passed?: boolean }>;
  request?: unknown;
  response?: unknown;
  cleanup?: boolean;
}

export interface TestingWorkflowView {
  workflowId: string;
  label: string;
  status: TestingStatus;
  detail?: string;
  steps: TestingStepView[];
  totalSteps?: number;
  requirementIds: string[];
  useCaseIds: string[];
  reused?: boolean;
}

export interface TestingGateView {
  id: string;
  label: string;
  status: TestingStatus;
  detail?: string;
  issues: string[];
  files: string[];
  commands: string[];
  plannedChecks: string[];
  elapsedMs?: number;
  reused?: boolean;
}

export interface TestingRunView {
  available: boolean;
  commandId?: string;
  targetImplementationJobId?: string;
  startedAt?: string;
  status: TestingStatus;
  gateStatus: TestingStatus;
  phase?: string;
  currentLabel?: string;
  currentDetail?: string;
  updatedAt?: string;
  blockingReason?: string;
  failedWorkflowId?: string;
  failedStepId?: string;
  candidateDigest?: string;
  gateCounts?: { passed: number; failed: number; inconclusive: number };
  planTotal: number;
  workflowCounts: {
    total: number;
    completed?: number;
    passed: number;
    failed: number;
    running: number;
    pending: number;
  };
  workflows: TestingWorkflowView[];
  plans: Array<{ workflowId: string; useCaseId: string; name: string; status: TestingStatus; attempt?: number; detail?: string }>;
  gates: TestingGateView[];
  findings: Array<{
    code: string;
    message: string;
    severity?: string;
    defectClass?: string;
    repairOwner?: string;
  }>;
  repair?: { status?: string; attemptCount?: number; acceptedCount?: number };
  initialFailure?: {
    gateStatus: TestingStatus;
    reason?: string;
    failedWorkflowId?: string;
    failedStepId?: string;
    findings: Array<{ code: string; message: string }>;
  };
  systemError?: string;
  arazzoDocument?: Record<string, unknown>;
  rawReport?: Record<string, unknown>;
}

const gateLabels: Record<string, string> = {
  dynamicFunctional: 'Dynamic API tests',
  static: 'Deployment security',
  package: 'Deployment package',
  iac: 'Infrastructure code'
};

const statusValues = new Set<TestingStatus>([
  'PENDING',
  'RUNNING',
  'PASS',
  'FAIL',
  'INCONCLUSIVE',
  'DEFERRED',
  'REUSED',
  'SKIPPED'
]);

function record(value: unknown): Record<string, any> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, any>)
    : {};
}

function status(value: unknown, fallback: TestingStatus = 'PENDING'): TestingStatus {
  const normalized = String(value ?? '').trim().toUpperCase();
  if (normalized === 'PASSED' || normalized === 'COMPLETED') return 'PASS';
  if (normalized === 'FAILED' || normalized === 'ERROR') return 'FAIL';
  if (normalized === 'UNAVAILABLE' || normalized === 'NOT_APPLICABLE') return 'INCONCLUSIVE';
  return statusValues.has(normalized as TestingStatus)
    ? (normalized as TestingStatus)
    : fallback;
}

function strings(value: unknown): string[] {
  const values = Array.isArray(value) ? value : value == null ? [] : [value];
  return values
    .map((item) => {
      if (typeof item === 'string') return item;
      const candidate = record(item);
      return String(candidate.message ?? candidate.path ?? candidate.command ?? '').trim();
    })
    .filter(Boolean);
}

function commandText(command: unknown): string {
  if (Array.isArray(command)) return command.map(String).join(' ');
  const value = record(command);
  if (Array.isArray(value.command)) return value.command.map(String).join(' ');
  return String(value.command ?? value.name ?? '').trim();
}

function gate(id: string, value: unknown, progressValue: unknown): TestingGateView {
  const item = record(value);
  const progress = record(progressValue);
  const commands = Array.isArray(item.commands)
    ? item.commands.map(commandText).filter(Boolean)
    : [];
  return {
    id,
    label: gateLabels[id] ?? id,
    status: status(
      item.reused ? 'REUSED' : item.deferred ? 'DEFERRED' : item.gateStatus ?? item.status ?? progress.status,
      'PENDING'
    ),
    detail:
      String(
        item.message ??
          item.reason ??
          progress.progress_detail ??
          progress.progress_step_label ??
          ''
      ).trim() || undefined,
    issues: strings(item.issues),
    files: strings(item.targets ?? item.files),
    commands,
    plannedChecks: strings(item.plannedChecks ?? item.planned_checks ?? item.checks ?? item.checkNames),
    elapsedMs:
      typeof item.elapsedMs === 'number'
        ? item.elapsedMs
        : typeof progress.elapsed_ms === 'number'
          ? progress.elapsed_ms
          : undefined,
    reused: Boolean(item.reused)
  };
}

function criteria(value: unknown): Array<{ condition: string; passed?: boolean }> {
  return (Array.isArray(value) ? value : [])
    .map((raw) => {
      const item = record(raw);
      const criterion = record(item.criterion);
      return {
        condition: String(criterion.condition ?? item.condition ?? '').trim(),
        passed: typeof item.passed === 'boolean' ? item.passed : undefined
      };
    })
    .filter((item) => item.condition);
}

function reportStep(
  raw: unknown,
  planned: Record<string, any> = {},
  cleanup = false
): TestingStepView {
  const step = record(raw);
  const finding = record(step.finding);
  const code = typeof step.statusCode === 'number' ? step.statusCode : undefined;
  const successCriteria = criteria(step.successCriteria ?? step.criteria);
  const expectedStatus = successCriteria
    .map((item) => item.condition.match(/\b([1-5]\d\d)\b/)?.[1])
    .find(Boolean) ?? (Array.isArray(step.responses)
      ? step.responses.map((response) => String(record(response).status ?? '')).find(Boolean)
      : undefined);
  const parameters = Array.isArray(step.parameters) ? step.parameters : [];
  const inputLinks = parameters
    .map((raw) => {
      const parameter = record(raw);
      const name = String(parameter.name ?? '').trim();
      const value = String(parameter.value ?? '').trim();
      return name && value ? `${name}=${value}` : '';
    })
    .filter(Boolean);
  if (record(step.requestBody).payload !== undefined) inputLinks.push('request body payload');
  const failed = Boolean(finding.code) || step.contractStatus === 'FAIL' || step.semanticStatus === 'FAIL';
  return {
    stepId: String(step.stepId ?? planned.stepId ?? 'step'),
    label: String(step.summary ?? step.operationId ?? planned.operationId ?? step.stepId ?? planned.stepId ?? 'Step'),
    status: status(step.status ?? (failed ? 'FAIL' : code == null ? 'PENDING' : 'PASS')),
    detail: String(finding.message ?? '').trim() || undefined,
    operationId: String(step.operationId ?? planned.operationId ?? '').trim() || undefined,
    method: String(step.method ?? step.httpMethod ?? '').trim().toUpperCase() || undefined,
    path: String(step.path ?? step.operationPath ?? '').trim() || undefined,
    expectedStatus,
    inputLinks,
    statusCode: code,
    control: String(step.control ?? '').trim() || undefined,
    elapsedMs: typeof step.elapsedMs === 'number' ? step.elapsedMs : undefined,
    attempt: typeof step.attempt === 'number' ? step.attempt : undefined,
    contractStatus: String(step.contractStatus ?? '').trim() || undefined,
    semanticStatus: String(step.semanticStatus ?? '').trim() || undefined,
    criteria: successCriteria,
    request: step.request,
    response: step.responseBody,
    cleanup
  };
}

function count(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0;
}

function terminalVerificationStatus(value: unknown): TestingStatus | undefined {
  const normalized = String(value ?? '').trim().toUpperCase();
  if (normalized === 'PASS' || normalized === 'PASSED') return 'PASS';
  if (normalized === 'FAIL' || normalized === 'FAILED') return 'FAIL';
  return undefined;
}

function terminalCommandStatus(value: unknown): boolean {
  return ['COMPLETED', 'FAILED'].includes(String(value ?? '').trim().toUpperCase());
}

function terminalWorkflowStatus(
  result: Record<string, any>,
  finalReportTerminal: boolean
): TestingStatus | undefined {
  const value = terminalVerificationStatus(result.gateStatus ?? result.status);
  // Candidate/profile reports contain planned workflow steps and can mark
  // their generation PASSED before any request runs. Only a terminal final
  // report makes report status authoritative; live workflow terminal states
  // come from testingProgressUpdated and are merged separately.
  return value && finalReportTerminal ? value : undefined;
}

function workflowAliases(workflowId: string, useCaseIds: string[]): Set<string> {
  return new Set([workflowId, ...useCaseIds].map((value) => value.trim().toLowerCase()).filter(Boolean));
}

function sameWorkflow(
  left: Pick<TestingWorkflowView, 'workflowId' | 'useCaseIds'>,
  right: Pick<TestingWorkflowView, 'workflowId' | 'useCaseIds'>
): boolean {
  const leftAliases = workflowAliases(left.workflowId, left.useCaseIds);
  return [...workflowAliases(right.workflowId, right.useCaseIds)].some((alias) => leftAliases.has(alias));
}

function workflowsFromProgress(progress: Record<string, any>): TestingWorkflowView[] {
  return Object.values(record(progress.workflows)).map((raw) => {
    const item = record(raw);
    const steps = Object.values(record(item.steps)).map((stepRaw) => {
      const step = record(stepRaw);
      return {
        stepId: String(step.step_id ?? step.stepId ?? 'step'),
        label: String(step.progress_step_label ?? step.label ?? step.step_id ?? step.stepId ?? 'Step'),
        status: status(step.status),
        detail: String(step.progress_detail ?? '').trim() || undefined,
        operationId: String(step.operation_id ?? step.operationId ?? '').trim() || undefined,
        method: String(step.method ?? '').trim().toUpperCase() || undefined,
        path: String(step.path ?? '').trim() || undefined,
        expectedStatus: undefined,
        inputLinks: [],
        statusCode: typeof step.status_code === 'number' ? step.status_code : undefined,
        control: String(step.control ?? '').trim() || undefined,
        elapsedMs: typeof step.elapsed_ms === 'number' ? step.elapsed_ms : undefined,
        attempt: typeof step.attempt === 'number' ? step.attempt : undefined,
        contractStatus: String(step.contract_status ?? '').trim() || undefined,
        semanticStatus: String(step.semantic_status ?? '').trim() || undefined,
      criteria: []
      };
    });
    return {
      workflowId: String(item.workflow_id ?? item.workflowId ?? 'workflow'),
      label: String(item.progress_step_label ?? item.label ?? item.workflow_id ?? item.workflowId ?? 'Workflow'),
      status: status(item.status),
      detail: String(item.progress_detail ?? '').trim() || undefined,
      steps,
      totalSteps: typeof item.total_steps === 'number' ? item.total_steps : undefined,
      requirementIds: [],
      useCaseIds: strings(item.use_case_id ?? item.useCaseId),
      reused: item.status === 'REUSED'
    };
  });
}

function workflowsFromReport(
  report: Record<string, any>,
  verificationFinal: boolean
): TestingWorkflowView[] {
  const dynamicStatus = verificationFinal
    ? terminalVerificationStatus(report.gateStatus ?? report.status)
    : undefined;
  const finalDynamicPass = dynamicStatus === 'PASS';
  const results: TestingWorkflowView[] = (
    Array.isArray(report.workflows) ? report.workflows : []
  ).map((raw) => {
    const item = record(raw);
    const result = record(item.result);
    const workflowStatus = terminalWorkflowStatus(result, verificationFinal);
    const workflow = record(item.workflow);
    const trace = record(workflow['x-easydep-trace'] ?? item['x-easydep-trace']);
    const operations = new Map(
      (Array.isArray(item.operations) ? item.operations : [])
        .map((raw) => record(raw))
        .map((operation) => [String(operation.operationId ?? ''), operation] as const)
        .filter(([operationId]) => operationId)
    );
    const withOperation = (raw: unknown): Record<string, any> => {
      const step = record(raw);
      return { ...record(operations.get(String(step.operationId ?? ''))), ...step };
    };
    const executedSteps = Array.isArray(result.steps) ? result.steps : [];
    const executedIds = new Set(executedSteps.map((step) => String(record(step).stepId ?? '')));
    const planned = Array.isArray(workflow.steps) ? workflow.steps : [];
    let cleanupLane = false;
    const executedViews = executedSteps.map((step) => {
      const actual = record(step);
      const definition = withOperation(
        planned.find((plannedStep) => String(record(plannedStep).stepId ?? '') === String(actual.stepId ?? ''))
      );
      const view = reportStep({ ...definition, ...actual }, definition, cleanupLane);
      if (String(record(step).control ?? '').startsWith('cleanup-')) cleanupLane = true;
      return view;
    });
    const unexecutedViews = planned
      .filter((plannedStep) => !executedIds.has(String(record(plannedStep).stepId ?? '')))
      .map((plannedStep) => {
        const definition = withOperation(plannedStep);
        return reportStep(
          {
            ...definition,
            status: (workflowStatus === 'PASS' || (verificationFinal && dynamicStatus === 'PASS'))
              ? 'SKIPPED'
              : 'PENDING'
          },
          definition
        );
      });
    const steps = [...executedViews, ...unexecutedViews];
    return {
      workflowId: String(item.workflowId ?? result.workflowId ?? workflow.workflowId ?? 'workflow'),
      label: String(workflow.summary ?? workflow.description ?? item.summary ?? item.workflowId ?? 'Workflow'),
      status: workflowStatus ?? (verificationFinal ? dynamicStatus ?? 'PENDING' : 'PENDING'),
      detail: String(result.reason ?? '').trim() || undefined,
      steps,
      totalSteps: planned.length || steps.length || undefined,
      requirementIds: strings(item.requirementIds ?? trace.requirementIds),
      useCaseIds: strings(item.useCaseIds ?? trace.useCaseIds),
      reused: Boolean(result.reused)
    };
  });
  const pendingIds = new Set(strings(report.pendingWorkflowIds));
  const plannedWorkflows = Array.isArray(record(report.candidatePlan).workflows)
    ? record(report.candidatePlan).workflows
    : [];
  for (const raw of plannedWorkflows) {
    const workflow = record(raw);
    const workflowId = String(workflow.workflowId ?? 'workflow');
    const trace = record(workflow['x-easydep-trace']);
    const planned: TestingWorkflowView = {
      workflowId,
      label: String(workflow.summary ?? workflow.description ?? workflowId),
      status: finalDynamicPass
        ? 'PASS'
        : dynamicStatus === 'FAIL' && !pendingIds.has(workflowId)
          ? 'SKIPPED'
          : 'PENDING',
      steps: (Array.isArray(workflow.steps) ? workflow.steps : []).map((step) =>
        reportStep({ ...record(step), status: finalDynamicPass ? 'PASS' : 'PENDING' }, record(step))
      ),
      totalSteps: Array.isArray(workflow.steps) ? workflow.steps.length : undefined,
      requirementIds: strings(trace.requirementIds),
      useCaseIds: strings(trace.useCaseIds)
    };
    // A final execution entry is authoritative. Match on the durable workflow
    // ID and on trace-linked use-case IDs so a pending candidate cannot add a
    // second row or replace a completed row.
    if (!results.some((result) => sameWorkflow(result, planned))) results.push(planned);
  }
  return results;
}

function finalReport(value: unknown): Record<string, any> {
  const root = record(value);
  if (root.verification || root.gateStatus) return root;
  const jobReport = record(record(root.job).result);
  if (Object.keys(jobReport).length) return jobReport;
  const nested = record(root.result);
  return nested.verification || nested.gateStatus ? nested : {};
}

function latestProgressEvent(
  events: WorkspaceEvent[],
  commandId: string | undefined
): WorkspaceEvent | undefined {
  return [...events].reverse().find(
    (event) =>
      event.metadata?.progress_event === 'testingProgressUpdated' &&
      (!commandId || event.command_id === commandId)
  );
}

function progressFields(event: Record<string, any>): Record<string, any> {
  return Object.fromEntries(
    [
      'status', 'progress_step_label', 'progress_detail', 'updated_at', 'elapsed_ms',
      'operation_id', 'method', 'path', 'status_code', 'contract_status',
      'semantic_status', 'control', 'attempt', 'use_case_id', 'use_case_name'
    ].filter((key) => event[key] !== undefined && event[key] !== '').map((key) => [key, event[key]])
  );
}

type ProgressWorkflowRecord = Record<string, unknown> & {
  workflow_id: string;
  steps: Record<string, unknown>;
  total_steps?: unknown;
};

function foldTestingProgress(
  checkpoint: Record<string, any>,
  events: WorkspaceEvent[],
  commandId: string | undefined
): Record<string, any> {
  const relevantEvents = events.filter(
    (event) =>
      event.command_id === commandId && event.metadata?.progress_event === 'testingProgressUpdated'
  );
  if (!Object.keys(checkpoint).length && !relevantEvents.length) return {};
  const result: Record<string, any> = {
    ...checkpoint,
    plan_total: Math.max(
      count(checkpoint.plan_total),
      count(record(checkpoint.plan_counts).total),
      count(record(checkpoint.workflow_counts).total)
    ),
    plans: { ...record(checkpoint.plans) },
    workflows: { ...record(checkpoint.workflows) },
    gates: { ...record(checkpoint.gates) }
  };
  const checkpointUpdatedAt = String(checkpoint.updated_at ?? '');
  const ordered = relevantEvents
    .map((event, index) => ({ event, index }))
    .sort((left, right) => (left.event.event_id ?? left.index) - (right.event.event_id ?? right.index));

  for (const { event } of ordered) {
    const value = record(event.metadata);
    const updatedAt = String(value.updated_at ?? event.created_at ?? '');
    if (checkpointUpdatedAt && updatedAt && updatedAt < checkpointUpdatedAt) continue;
    const phase = String(value.phase ?? '');
    const scope = String(value.scope ?? '');
    const workflowId = String(value.workflow_id ?? '');
    const fields = progressFields(value);
    result.phase = phase;
    result.status = value.status;
    result.updated_at = updatedAt || result.updated_at;
    result.last_event = value;
    if (count(value.total_workflows)) {
      result.plan_total = Math.max(count(result.plan_total), count(value.total_workflows));
    }

    if (phase === 'planning' && scope === 'workflow' && workflowId) {
      result.plans[workflowId] = { workflow_id: workflowId, ...fields };
      continue;
    }
    if ((scope === 'workflow' || scope === 'step') && workflowId) {
      const existing = record(result.workflows[workflowId]);
      const workflow: ProgressWorkflowRecord = {
        ...existing,
        workflow_id: workflowId,
        steps: { ...record(existing.steps) }
      };
      if (scope === 'workflow') {
        Object.assign(workflow, fields);
        if (value.total_steps != null) workflow.total_steps = value.total_steps;
      } else {
        const stepId = String(value.step_id ?? '');
        if (stepId) workflow.steps[stepId] = { step_id: stepId, ...fields };
      }
      result.workflows[workflowId] = workflow;
    }
    if (scope === 'gate' && value.gate) {
      result.gates[String(value.gate)] = { gate: String(value.gate), ...fields };
    }
  }
  return result;
}

function mergeWorkflows(
  reportWorkflows: TestingWorkflowView[],
  progressWorkflows: TestingWorkflowView[]
): TestingWorkflowView[] {
  const merged = [...reportWorkflows];
  for (const progress of progressWorkflows) {
    const index = merged.findIndex((report) => sameWorkflow(report, progress));
    if (index < 0) {
      merged.push(progress);
      continue;
    }
    const report = merged[index];
    if (terminalVerificationStatus(report.status)) continue;
    merged[index] = {
      ...report,
      status: progress.status,
      detail: progress.detail ?? report.detail,
      steps: progress.steps.length ? progress.steps : report.steps,
      totalSteps: progress.totalSteps ?? report.totalSteps
    };
  }
  return merged;
}

export function projectTestingRun(input: {
  command?: WorkspaceCommand | null;
  events?: WorkspaceEvent[];
  result?: Record<string, any> | null;
}): TestingRunView | null {
  const command = input.command ?? null;
  const outerResult = record(input.result ?? command?.result);
  const checkpoint = record(command?.payload?.testing_checkpoint);
  const checkpointResult = record(checkpoint.result);
  const report = finalReport(input.result ?? command?.result);
  const source = Object.keys(report).length ? report : checkpointResult;
  const verification = record(source.verification);
  const reports = record(verification.reports ?? source.reports);
  const event = latestProgressEvent(input.events ?? [], command?.command_id);
  const checkpointProgress = record(checkpoint.testing_progress);
  const progress = foldTestingProgress(
    checkpointProgress,
    input.events ?? [],
    command?.command_id
  );
  const lastProgress = record(progress.last_event);
  const currentProgress = Object.keys(lastProgress).length ? lastProgress : record(event?.metadata);
  const plans: TestingRunView['plans'] = Object.values(record(progress.plans)).map((raw) => {
    const item = record(raw);
    const lifecycleStatus = status(item.status);
    return {
      workflowId: String(item.workflow_id),
      useCaseId: String(item.use_case_id ?? item.workflow_id),
      name: String(item.use_case_name ?? item.progress_step_label ?? item.workflow_id),
      // Profile generation is preparation, not a verification result. Keep
      // non-failure plan rows pending even if an older event calls it passed.
      status: lifecycleStatus === 'FAIL' ? 'FAIL' : 'PENDING',
      attempt: item.attempt == null ? undefined : Number(item.attempt),
      detail: String(item.progress_detail ?? '') || undefined
    };
  });
  const dynamicReport = record(reports.dynamicFunctional);
  const candidatePlan = record(dynamicReport.candidatePlan);
  const candidatePlanWorkflows = Array.isArray(candidatePlan.workflows)
    ? candidatePlan.workflows
    : [];
  const counts = record(progress.workflow_counts);
  const planTotal = Math.max(
    count(progress.plan_total),
    count(record(progress.plan_counts).total),
    count(counts.total),
    candidatePlanWorkflows.length,
    plans.length
  );
  const commandTerminal = terminalCommandStatus(command?.status);
  const verificationFinal = Boolean(
    terminalVerificationStatus(dynamicReport.gateStatus ?? dynamicReport.status) &&
      commandTerminal
  );
  const reportWorkflows = workflowsFromReport(dynamicReport, verificationFinal);
  const progressWorkflows = workflowsFromProgress(progress);
  const workflows = mergeWorkflows(reportWorkflows, progressWorkflows);
  const progressGates = record(progress.gates);
  const aggregateGates = record(verification.gates ?? source.gates);
  const staticReport = record(reports.static);
  const gates = [
    gate(
      'dynamicFunctional',
      Object.keys(dynamicReport).length
        ? dynamicReport
        : { gateStatus: aggregateGates.dynamicFunctional },
      progressGates.dynamicFunctional
    ),
    gate(
      'static',
      Object.keys(record(staticReport.trivyScan)).length
        ? staticReport.trivyScan
        : { gateStatus: aggregateGates.static },
      progressGates.static
    ),
    gate('package', staticReport.deploymentPackage, progressGates.package),
    gate(
      'iac',
      Object.keys(record(reports.iac)).length
        ? reports.iac
        : { gateStatus: aggregateGates.iac },
      progressGates.iac
    )
  ];
  const blockers = Array.isArray(source.blocking_findings)
    ? source.blocking_findings
    : Array.isArray(outerResult.blocking_findings)
      ? outerResult.blocking_findings
      : [];
  const findings = blockers
    .map((raw) => {
      const item = record(raw);
      return {
        code: String(item.code ?? 'TESTING'),
        message: String(item.message ?? ''),
        severity: String(item.severity ?? '').trim() || undefined,
        defectClass: String(item.defect_class ?? '').trim() || undefined,
        repairOwner: String(item.repair_owner ?? '').trim() || undefined
      };
    })
    .filter((item) => item.message);
  const repairState = record(source.repair_state ?? outerResult.repair_state);
  const initialFailure = record(command?.payload?.initial_testing_failure);
  const hasTestingEvent = Boolean(event);
  const isTestingCommand = command?.stage === 'testing' || command?.action === 'start_testing';
  if (!Object.keys(source).length && !Object.keys(progress).length && !hasTestingEvent && !isTestingCommand) {
    return null;
  }
  const gateCounts = record(verification.gateCounts ?? source.gateCounts);
  const finalGateStatus = verificationFinal
    ? terminalVerificationStatus(source.gateStatus ?? verification.gateStatus ?? dynamicReport.gateStatus) ?? 'PENDING'
    : 'PENDING';
  const commandStatus = String(command?.status ?? '').toUpperCase();
  const runStatus: TestingStatus = verificationFinal
    ? finalGateStatus
    : commandStatus === 'RUNNING' || commandStatus === 'QUEUED'
      ? 'RUNNING'
      : commandStatus === 'FAILED'
        ? 'FAIL'
        : 'PENDING';
  const blockingReason =
    String(source.blockingReason ?? verification.blockingReason ?? findings[0]?.message ?? '').trim() ||
    undefined;

  const completedWorkflows = workflows.filter(
    (item) => !['PENDING', 'RUNNING'].includes(item.status)
  ).length;
  const passedWorkflows = workflows.filter(
    (item) => ['PASS', 'REUSED', 'SKIPPED'].includes(item.status)
  ).length;
  const failedWorkflows = workflows.filter((item) => item.status === 'FAIL').length;
  const workflowTotal = Math.max(workflows.length, planTotal);

  return {
    available: true,
    commandId: command?.command_id ?? event?.command_id ?? undefined,
    targetImplementationJobId:
      String(
        record(outerResult.job).implementation_job_id ??
          checkpoint.implementation_job_id ??
          command?.payload?.implementation_job_id ??
          ''
      ).trim() || undefined,
    startedAt: command?.created_at ?? undefined,
    status: runStatus,
    gateStatus: Object.keys(report).length ? finalGateStatus : runStatus,
    phase: String(progress.phase ?? currentProgress.phase ?? '').trim() || undefined,
    currentLabel: String(currentProgress.progress_step_label ?? '').trim() || undefined,
    currentDetail: String(currentProgress.progress_detail ?? '').trim() || undefined,
    updatedAt:
      String(progress.updated_at ?? currentProgress.updated_at ?? event?.created_at ?? '').trim() ||
      undefined,
    blockingReason,
    failedWorkflowId:
      String(dynamicReport.failedWorkflowId ?? source.failedWorkflowId ?? '').trim() || undefined,
    failedStepId: String(dynamicReport.failedStepId ?? source.failedStepId ?? '').trim() || undefined,
    candidateDigest:
      String(dynamicReport.candidateDigest ?? source.candidateDigest ?? '').trim() || undefined,
    gateCounts: Object.keys(gateCounts).length
      ? {
          passed: Number(gateCounts.passed ?? gateCounts.PASS ?? 0),
          failed: Number(gateCounts.failed ?? gateCounts.FAIL ?? 0),
          inconclusive: Number(
            gateCounts.inconclusive ?? gateCounts.INCONCLUSIVE ?? 0
          )
        }
      : undefined,
    planTotal,
    workflowCounts: {
      total: workflowTotal,
      completed: completedWorkflows,
      passed: passedWorkflows,
      failed: failedWorkflows,
      running: workflows.filter((item) => item.status === 'RUNNING').length,
      pending: Math.max(0, workflowTotal - completedWorkflows - workflows.filter((item) => item.status === 'RUNNING').length)
    },
    workflows,
    plans,
    gates,
    findings,
    repair: Object.keys(repairState).length
      ? {
          status: String(repairState.status ?? ''),
          attemptCount: Number(repairState.attempt_count ?? 0),
          acceptedCount: Number(repairState.accepted_count ?? 0)
      }
      : undefined,
    initialFailure: Object.keys(initialFailure).length
      ? {
          gateStatus: status(initialFailure.gateStatus, 'FAIL'),
          reason: String(initialFailure.blockingReason ?? '').trim() || undefined,
          failedWorkflowId:
            String(initialFailure.failedWorkflowId ?? '').trim() || undefined,
          failedStepId: String(initialFailure.failedStepId ?? '').trim() || undefined,
          findings: (Array.isArray(initialFailure.blocking_findings)
            ? initialFailure.blocking_findings
            : []
          ).map((raw) => {
            const item = record(raw);
            return {
              code: String(item.code ?? 'TESTING'),
              message: String(item.message ?? '')
            };
          }).filter((item) => item.message)
        }
      : undefined,
    systemError: command?.error?.trim() || undefined,
    arazzoDocument: Object.keys(record(dynamicReport.candidatePlan)).length
      ? record(dynamicReport.candidatePlan)
      : undefined,
    rawReport: Object.keys(source).length ? source : undefined
  };
}
