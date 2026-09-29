import type {
  ArtifactDocument,
  CommandPayload,
  FileArtifactSnapshot,
  WorkspaceApp,
  WorkspaceEvent,
  WorkspaceCommand,
  WorkspaceSnapshot,
  CloudProvider,
  CloudRegionOption,
  DeploymentPreferences,
  DeploymentSizingResponse,
  CapacityOverride,
  SequenceDiagramSummary,
  LiveDiagramPreview,
  LiveSourceSnapshot,
  LlmTimingPage,
  ArtifactTraceResponse,
  TestingResultResponse
} from '$lib/types';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // JSON API의 공통 처리 지점이다. HTTP 오류 응답도 가능한 경우 JSON으로 읽어 백엔드가
  // 보낸 detail을 사용자에게 보여 주고, JSON이 아니면 상태 코드를 포함한 기본 오류를 만든다.
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) }
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail?.message ?? body.detail ?? `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export async function listApps() {
  const data = await request<{ apps: WorkspaceApp[] }>('/api/workspace/apps');
  return data.apps;
}

export function createApp(input: {
  message: string;
}) {
  return request<{ app_id: string }>('/api/workspace/apps', {
    method: 'POST',
    body: JSON.stringify(input)
  });
}

export function getCloudOptions() {
  return request<{
    regions: Record<CloudProvider, CloudRegionOption[]>;
    currencies: string[];
  }>('/api/workspace/cloud-options');
}

export function saveDeploymentPreferences(appId: string, input: DeploymentPreferences) {
  return request<{ preferences: DeploymentPreferences }>(
    `/api/workspace/apps/${appId}/deployment-preferences`,
    { method: 'PUT', body: JSON.stringify(input) }
  );
}

export function getWorkspace(appId: string) {
  return request<WorkspaceSnapshot>(`/api/workspace/apps/${appId}`);
}

export function getTestingResult(appId: string) {
  return request<TestingResultResponse>(
    `/api/workspace/apps/${encodeURIComponent(appId)}/testing-result`
  );
}

export function sendCommand(appId: string, payload: CommandPayload) {
  return request(`/api/workspace/apps/${appId}/commands`, {
    method: 'POST',
    body: JSON.stringify(payload)
  });
}

export function stopCommand(appId: string, commandId: string) {
  return request<{ app_id: string; command: WorkspaceCommand }>(
    `/api/workspace/apps/${encodeURIComponent(appId)}/commands/${encodeURIComponent(commandId)}/stop`,
    { method: 'POST' }
  );
}

export function getArtifacts(appId: string) {
  return request<{ app_id: string } & ArtifactDocument>(`/api/apps/${appId}`);
}

export function getDeploymentSizing(
  appId: string,
  targetId: string,
  capacity?: CapacityOverride[]
) {
  const capacityQuery = capacity?.length ? `&capacity=${encodeURIComponent(JSON.stringify(capacity))}` : '';
  return request<DeploymentSizingResponse>(
    `/api/workspace/apps/${encodeURIComponent(appId)}/deployment-sizing?target=${encodeURIComponent(targetId)}${capacityQuery}`
  );
}

export function applyDeploymentSizing(
  appId: string,
  input: {
    targetId: string;
    structureDigest: string;
    capacityOverrides: CapacityOverride[];
    selections: Array<{
      computeUnitId: string;
      sku: string;
      replicaCount: number;
      replicationConfirmed: boolean;
    }>;
  }
) {
  return request(`/api/workspace/apps/${encodeURIComponent(appId)}/deployment-sizing`, {
    method: 'PUT',
    body: JSON.stringify(input)
  });
}

export function getEventLlmTimings(
  appId: string,
  eventId: number,
  offset = 0,
  limit = 20
) {
  return request<LlmTimingPage>(
    `/api/workspace/apps/${encodeURIComponent(appId)}/events/${eventId}/llm-timings?offset=${offset}&limit=${limit}`
  );
}

export function getClassDiagramPreview(appId: string, commandId: string) {
  return request<LiveDiagramPreview>(
    `/api/workspace/apps/${encodeURIComponent(appId)}/commands/${encodeURIComponent(commandId)}/previews/class_diagram`
  );
}

export function getVersions(appId: string, stage: string) {
  return request<{ versions: Array<Record<string, any>> }>(
    `/api/apps/${appId}/stages/${stage}/versions`
  );
}

export function getSequenceDiagrams(appId: string) {
  return request<{ diagrams: SequenceDiagramSummary[] }>(
    `/api/apps/${encodeURIComponent(appId)}/stages/sequence_diagram/diagrams`
  );
}

export function getFileArtifact(appId: string, artifactType: string) {
  return request<FileArtifactSnapshot>(
    `/api/implementation/apps/${appId}/artifacts/${artifactType}`
  );
}

export function getFileArtifactVersions(appId: string, artifactType: string) {
  return request<{ versions: Array<Record<string, any>> }>(
    `/api/implementation/apps/${appId}/artifacts/${artifactType}/versions`
  );
}

export function getArtifactFile(appId: string, artifactType: string, path: string) {
  return request<{ path: string; content: string; sha256: string }>(
    `/api/implementation/apps/${appId}/artifacts/${artifactType}/files/${path
      .split('/')
      .map(encodeURIComponent)
      .join('/')}`
  );
}

export function getArtifactTrace(appId: string, ref = '') {
  const query = ref ? `?ref=${encodeURIComponent(ref)}` : '';
  return request<ArtifactTraceResponse>(
    `/api/apps/${encodeURIComponent(appId)}/trace${query}`
  );
}

export function getLiveImplementationSources(appId: string, jobId: string) {
  return request<LiveSourceSnapshot>(
    `/api/implementation/apps/${encodeURIComponent(appId)}/jobs/${encodeURIComponent(jobId)}/live`
  );
}

export function getLiveImplementationFile(appId: string, jobId: string, path: string) {
  return request<{ path: string; content: string; sha256: string; size: number }>(
    `/api/implementation/apps/${encodeURIComponent(appId)}/jobs/${encodeURIComponent(jobId)}/live/files/${path
      .split('/')
      .map(encodeURIComponent)
      .join('/')}`
  );
}

export async function downloadImplementationArtifacts(appId: string) {
  const response = await fetch(`/api/implementation/apps/${encodeURIComponent(appId)}/download`);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail?.message ?? body.detail ?? `Request failed (${response.status})`);
  }
  const blob = await response.blob();
  // 서버가 만든 ZIP을 브라우저 memory URL로 연결해 다운로드한다. 클릭이 끝나면 DOM과
  // object URL을 정리해 여러 번 다운로드해도 메모리가 계속 남지 않게 한다.
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `easydep-${appId}-implementation.zip`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export function connectEvents(
  appId: string,
  after: number,
  onEvent: (event: WorkspaceEvent) => void,
  onError: () => void
) {
  // EventSource는 연결이 끊기면 브라우저가 자동으로 다시 연결한다. after에는 마지막으로
  // 화면에 반영한 event ID를 전달하며 서버는 그 다음 이벤트부터 보내 중복을 줄인다.
  const source = new EventSource(`/api/workspace/apps/${appId}/events?after=${after}`);
  source.addEventListener('workspace', (raw) => {
    onEvent(JSON.parse((raw as MessageEvent).data));
  });
  source.onerror = onError;
  return source;
}
