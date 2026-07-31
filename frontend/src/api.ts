import type {
  AuthStatus,
  Collection,
  DownloadJob,
  DownloadMode,
  DownloadSchedule,
  Health,
  HubModel,
  HubModelDetails,
  HardwareRig,
  HardwareRigInput,
  LocalModel,
  LocalModelDetails,
  OwnedRepository,
  RuntimeJob,
  RuntimeTarget,
  SavedModel,
  SelectionPath,
  User,
} from './types'

let csrfToken: string | null = null

function applyAuth(status: AuthStatus): AuthStatus {
  csrfToken = status.csrf_token
  return status
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (init?.body && typeof init.body === 'string' && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  if (csrfToken && init?.method && !['GET', 'HEAD'].includes(init.method)) {
    headers.set('X-CSRF-Token', csrfToken)
  }
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers,
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event('hugginghack:unauthorized'))
    throw new Error(payload.detail || `Request failed with status ${response.status}`)
  }
  return payload as T
}

const repoPath = (repoId: string) =>
  repoId
    .split('/')
    .map((part) => encodeURIComponent(part))
    .join('/')

export const api = {
  authStatus: () => request<AuthStatus>('/api/auth/status').then(applyAuth),
  setup: (payload: { username: string; display_name: string; password: string }) =>
    request<AuthStatus>('/api/auth/setup', {
      method: 'POST',
      body: JSON.stringify(payload),
    }).then(applyAuth),
  login: (payload: { username: string; password: string }) =>
    request<AuthStatus>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify(payload),
    }).then(applyAuth),
  logout: () =>
    request<{ status: string; logout_url: string | null }>('/api/auth/logout', { method: 'POST' }).finally(() => {
      csrfToken = null
    }),
  users: () => request<{ items: User[] }>('/api/users'),
  createUser: (payload: { username: string; display_name: string; password: string }) =>
    request<User>('/api/users', { method: 'POST', body: JSON.stringify(payload) }),
  changePassword: (payload: { current_password: string; new_password: string }) =>
    request<{ status: string }>('/api/account/password', {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  hardwareRigs: () => request<{ items: HardwareRig[] }>('/api/hardware/rigs'),
  createHardwareRig: (payload: HardwareRigInput) =>
    request<HardwareRig>('/api/hardware/rigs', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateHardwareRig: (rigId: string, payload: HardwareRigInput) =>
    request<HardwareRig>(`/api/hardware/rigs/${encodeURIComponent(rigId)}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  deleteHardwareRig: (rigId: string) =>
    request<{ status: string }>(`/api/hardware/rigs/${encodeURIComponent(rigId)}`, {
      method: 'DELETE',
    }),
  health: () => request<Health>('/api/health'),
  searchModels: (params: URLSearchParams) =>
    request<{ items: HubModel[]; count: number }>(`/api/hub/models?${params.toString()}`),
  modelDetails: (repoId: string, revision = 'main') =>
    request<HubModelDetails>(
      `/api/hub/models/${repoPath(repoId)}?revision=${encodeURIComponent(revision)}`,
    ),
  downloads: () => request<{ items: DownloadJob[]; active: number }>('/api/downloads'),
  startDownload: (payload: {
    repo_id: string
    revision?: string
    allow_patterns?: string[]
    ignore_patterns?: string[]
    mode?: DownloadMode
    selection?: { paths: SelectionPath[]; include_metadata: boolean }
  }) =>
    request<DownloadJob>('/api/downloads', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  cancelDownload: (downloadId: string) =>
    request<DownloadJob>(`/api/downloads/${encodeURIComponent(downloadId)}/cancel`, {
      method: 'POST',
    }),
  pauseDownload: (downloadId: string) =>
    request<DownloadJob>(`/api/downloads/${encodeURIComponent(downloadId)}/pause`, {
      method: 'POST',
    }),
  resumeDownload: (downloadId: string) =>
    request<DownloadJob>(`/api/downloads/${encodeURIComponent(downloadId)}/resume`, {
      method: 'POST',
    }),
  cleanupDownload: (downloadId: string) =>
    request<DownloadJob>(`/api/downloads/${encodeURIComponent(downloadId)}/data`, {
      method: 'DELETE',
    }),
  downloadSettings: () => request<DownloadSchedule>('/api/download-settings'),
  updateDownloadSettings: (payload: Omit<DownloadSchedule, 'window_open' | 'updated_by' | 'updated_at'>) =>
    request<DownloadSchedule>('/api/download-settings', {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  localModels: (query = '') =>
    request<{ items: LocalModel[]; count: number; total_bytes: number }>(
      `/api/local-models?query=${encodeURIComponent(query)}`,
    ),
  scanLocalModels: () =>
    request<{ count: number; models: LocalModel[]; scanned_at: string }>(
      '/api/local-models/scan',
      { method: 'POST' },
    ),
  localModelDetails: (repoId: string) =>
    request<LocalModelDetails>(`/api/local-models/${repoPath(repoId)}`),
  restoreLocalModel: (repoId: string) =>
    request<LocalModelDetails>(`/api/local-models/${repoPath(repoId)}/restore`, {
      method: 'POST',
    }),
  evictLocalModelCache: (repoId: string) =>
    request<{ status: string; model: LocalModel }>(
      `/api/local-models/${repoPath(repoId)}/cache`,
      { method: 'DELETE' },
    ),
  runtimeTargets: () => request<{ items: RuntimeTarget[] }>('/api/runtimes'),
  runtimeJobs: (limit = 100) =>
    request<{ items: RuntimeJob[]; active: number }>(
      `/api/runtime-jobs?limit=${encodeURIComponent(limit)}`,
    ),
  runtimeJob: (jobId: string) =>
    request<RuntimeJob>(`/api/runtime-jobs/${encodeURIComponent(jobId)}`),
  loadRuntime: (
    targetId: string,
    payload: {
      repo_id: string
      runtime_model_name?: string
      source_file?: string
    },
  ) =>
    request<RuntimeJob>(`/api/runtimes/${encodeURIComponent(targetId)}/load`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  collections: () => request<{ items: Collection[] }>('/api/collections'),
  createCollection: (payload: { name: string; description?: string }) =>
    request<Collection>('/api/collections', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  deleteCollection: (collectionId: string) =>
    request<{ status: string }>(`/api/collections/${encodeURIComponent(collectionId)}`, {
      method: 'DELETE',
    }),
  savedModels: (query = '', collectionId = '') => {
    const params = new URLSearchParams()
    if (query) params.set('query', query)
    if (collectionId) params.set('collection_id', collectionId)
    return request<{ items: SavedModel[]; count: number }>(
      `/api/saved-models?${params.toString()}`,
    )
  },
  saveModel: (payload: {
    repo_id: string
    note?: string
    collection_ids?: string[]
    metadata?: Record<string, unknown>
  }) =>
    request<SavedModel>('/api/saved-models', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  unsaveModel: (repoId: string) =>
    request<{ status: string }>(`/api/saved-models/${repoPath(repoId)}`, {
      method: 'DELETE',
    }),
  uploadRepositories: () =>
    request<{ items: OwnedRepository[] }>('/api/uploads/repositories'),
  createUploadRepository: (payload: {
    slug: string
    description?: string
    visibility?: 'private' | 'shared'
  }) =>
    request<OwnedRepository>('/api/uploads/repositories', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateUploadRepository: (
    repoId: string,
    payload: { description: string; visibility: 'private' | 'shared' },
  ) =>
    request<OwnedRepository>(
      `/api/uploads/repositories?repo_id=${encodeURIComponent(repoId)}`,
      { method: 'PATCH', body: JSON.stringify(payload) },
    ),
  finalizeUploadRepository: (repoId: string) =>
    request<OwnedRepository>(
      `/api/uploads/repositories/finalize?repo_id=${encodeURIComponent(repoId)}`,
      { method: 'POST' },
    ),
  deleteUploadRepository: (repoId: string, confirmation: string) =>
    request<{ status: string }>(
      `/api/uploads/repositories?repo_id=${encodeURIComponent(repoId)}`,
      {
        method: 'DELETE',
        body: JSON.stringify({ confirmation }),
      },
    ),
  uploadFile: async (
    repoId: string,
    filePath: string,
    file: File,
    chunkBytes: number,
    onProgress: (uploaded: number) => void,
  ) => {
    const params = new URLSearchParams({ repo_id: repoId, path: filePath })
    const status = await request<{ offset: number; complete: boolean }>(
      `/api/uploads/repositories/files/status?${params.toString()}`,
    )
    if (status.complete && status.offset === file.size) {
      onProgress(file.size)
      return
    }
    if (status.complete || status.offset > file.size) {
      throw new Error(`A different completed or partial file already exists at ${filePath}.`)
    }
    let offset = status.offset
    do {
      const chunk = file.slice(offset, Math.min(file.size, offset + chunkBytes))
      const headers = new Headers({
        'Content-Type': 'application/octet-stream',
        'Upload-Offset': String(offset),
        'Upload-Length': String(file.size),
      })
      if (csrfToken) headers.set('X-CSRF-Token', csrfToken)
      const response = await fetch(
        `/api/uploads/repositories/files?${params.toString()}`,
        {
          method: 'PUT',
          credentials: 'same-origin',
          headers,
          body: chunk,
        },
      )
      const result = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(result.detail || `Upload failed with status ${response.status}`)
      }
      offset = result.offset
      onProgress(offset)
      if (file.size === 0) break
    } while (offset < file.size)
  },
}
