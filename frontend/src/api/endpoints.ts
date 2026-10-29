import { http, downloadAuthorized, openDocumentInNewTab } from '@/api/client'
import type {
  Acknowledgement,
  ADImportResult,
  BulkImportResult,
  ADUser,
  AppNotification,
  AuditEntry,
  SetupStatus,
  Certification,
  CertKind,
  CertPage,
  CertState,
  Dashboard,
  LoginOptions,
  Me,
  OrderDetail,
  OrderListItem,
  OrderStatus,
  OrderType,
  Page,
  QueryParams,
  Role,
  Plan,
  PlanListItem,
  PlanStatus,
  Position,
  VshkItem,
  VshkPlan,
  VshkPlanList,
  Task,
  TaskComment,
  TaskDetail,
  TaskOccurrence,
  TaskPage,
  TaskPriority,
  TaskRecurrence,
  TaskStatusStored,
  TokenPair,
  User,
  PlanParseResult,
  IncomingLetter,
  MovementOrder,
  EquipmentItem,
  EquipmentMove,
  MonthSummary,
  SearchResults,
  ServiceNote,
  ServiceNoteDetail,
  Substitution,
  HelpArticle,
  HelpVisibility,
  Ticket,
  TicketCategory,
  TicketDirectory,
  TicketFilters,
  TicketMessage,
  TicketPriority,
  TicketStats,
  SysadminUser,
} from '@/types'

export interface OrderFilters {
  search?: string
  order_type?: OrderType | ''
  status?: OrderStatus | ''
  date_from?: string
  date_to?: string
  ack?: 'all' | 'pending' | 'acknowledged' | ''
  mine_pending?: boolean
  page?: number
  per_page?: number
}

export interface TaskFilters {
  /** mine — только где я исполнитель/постановщик (staff); school — общешкольный (staff) */
  scope?: 'mine' | 'school'
  /** ACTIVE — все незавершённые (лента «Все задачи»: выполненные скрыты) */
  status?: TaskStatusStored | 'OVERDUE' | 'ACTIVE' | ''
  assignee_id?: string
  creator_id?: string
  priority?: TaskPriority | ''
  search?: string
  due_before?: string
  due_after?: string
  sort?: 'deadline' | 'created' | 'priority' | 'title' | 'updated' | 'reg'
  order?: 'asc' | 'desc'
  /** true — только повторяющиеся задачи, false — только обычные */
  recurring?: boolean
  page?: number
  per_page?: number
}

export interface TaskPayload {
  title: string
  incoming_number?: string | null
  /** ручной № регистрации для журнала входящих; пусто — авто-номер по порядку за год */
  reg_no?: number | null
  description?: string | null
  assignee_ids?: string[]
  deadline?: string | null
  priority: TaskPriority
  status?: TaskStatusStored
  /** NONE | DAILY | WEEKLY | MONTHLY */
  recurrence?: TaskRecurrence
  /** для WEEKLY: дни недели ISO, 1=пн … 7=вс */
  recurrence_weekdays?: number[] | null
  /** для MONTHLY: число месяца 1..31 */
  recurrence_day?: number | null
}

export interface OrderPayload {
  number: string
  order_year: number
  title: string
  description?: string | null
  order_type: OrderType
  /** требуемый уровень подписи ознакомления (v1.11: выбор в форме) */
  signature_policy?: 'NONE' | 'CLICK' | 'TOTP' | 'CODE_WORD'
  issued_on: string
  ack_deadline?: string | null
  require_all?: boolean
  recipient_ids?: string[] | null
}

export interface PlanContentPayload {
  duty?: { klass?: string | null; teacher_id?: string | null }
  notes?: string[]
  month_items?: { text: string; place?: string | null; resp: string[] }[]
  week_items?: { text: string; place?: string | null; resp: string[] }[]
  days?: { date: string; events: { title: string; time?: string | null; place?: string | null; resp: string[] }[] }[]
}

export interface PlanPayload {
  week_start: string
  content?: PlanContentPayload
}

/** Данные для создания/правки записи об аттестации или КПК. */
export interface CertificationPayload {
  user_id: string
  kind: CertKind
  category?: string | null
  title?: string | null
  hours?: number | null
  provider?: string | null
  document_no?: string | null
  awarded_at?: string | null
  expires_at?: string | null
  note?: string | null
}

export interface CertImportResult {
  created: number
  updated: number
  failed: { row: number; reason: string }[]
  total: number
}

/** Публичные вызовы первоначальной настройки (до создания первого Владельца). */
export const setupApi = {
  status: () => http.public.get<SetupStatus>('/setup/status'),
  info: () => http.public.get<Record<string, unknown>>('/setup/info'),
}

export const authApi = {
  /** Публичный эндпоинт: какие способы входа доступны (домен / локально). */
  loginOptions: () => http.public.get<LoginOptions>('/auth/login-options'),
  login: (email: string, password: string, remember = true) =>
    http.public.post<TokenPair>('/auth/login', { email, password, remember }),
  /** обновление access-токена по httpOnly-cookie (тело не нужно) */
  refresh: () => http.public.post<TokenPair>('/auth/refresh'),
  me: () => http.get<Me>('/auth/me'),
  logout: () => http.post<void>('/auth/logout'),
  adDiagnostics: () => http.get<Record<string, unknown>>('/auth/ad/diagnostics'),
  /** смена собственного пароля (локальные учётки); все сессии после этого отзываются */
  changePassword: (current_password: string, new_password: string) =>
    http.post<void>('/auth/change-password', { current_password, new_password }),
}

/** ПЭП: привязка аутентификатора (TOTP), контрольное слово, чувствительные операции. */
export interface TotpSetupResult {
  secret: string          // выдаётся ОДНОРАЗОВО — только в ответе setup
  otpauth_uri: string     // для QR-кода в приложении-аутентификаторе
  algorithm: string
  digits: number
  period: number
}

export const totpApi = {
  setup: () => http.post<TotpSetupResult>('/users/me/totp/setup'),
  confirm: (code: string) => http.post<void>('/users/me/totp/confirm', { code }),
  /** Отвязка ДЕЙСТВУЮЩЕЙ привязки подтверждается текущим кодом (без кода сервер
   *  вернёт 409 TOTP_CODE_REQUIRED): снятие аутентификатора обнулило бы ПЭП.
   *  Незавершённую привязку (секрет выдан, но не подтверждён) можно снять без кода. */
  remove: (code?: string) => http.delete<void>('/users/me/totp', code ? { code } : undefined),
  setWord: (word: string, code: string) =>
    http.put<void>('/users/me/totp/recovery-word', { word, code }),
  /**
   * РЕЗЕРВНЫЙ сброс привязки Владельца (v1.12): пароль + заранее заданное
   * контрольное слово, БЕЗ кода аутентификатора.
   *
   * Единственный путь, когда Владелец потерял телефон: штатный сброс
   * (`usersApi.totpReset`) подписывается кодом, а подписать нечем.
   * После успеха сервер отзывает ВСЕ сессии и снимает cookie, поэтому
   * вызывающий код обязан разлогинить пользователя и показать вход.
   */
  breakGlass: (password: string, word: string) =>
    http.post<void>('/users/me/totp/break-glass', { password, word }),
}

export const ordersApi = {
  list: (filters: OrderFilters = {}) => http.get<Page<OrderListItem>>('/orders', filters as QueryParams),
  detail: (id: string) => http.get<OrderDetail>(`/orders/${id}`),
  create: (payload: OrderPayload) => http.post<OrderDetail>('/orders', payload),
  update: (id: string, payload: Partial<OrderPayload> & { status?: OrderStatus }) =>
    http.patch<OrderDetail>(`/orders/${id}`, payload),
  publish: (id: string) => http.post<OrderDetail>(`/orders/${id}/publish`),
  archive: (id: string) => http.post<OrderDetail>(`/orders/${id}/archive`),
  remove: (id: string) => http.delete<void>(`/orders/${id}`),
  uploadFile: (id: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return http.upload<OrderDetail>(`/orders/${id}/files`, form)
  },
  deleteFile: (id: string, fileId: string) => http.delete<OrderDetail>(`/orders/${id}/files/${fileId}`),
  previewMeta: (id: string) =>
    http.get<{ order_id: string; file_id: string; original_name: string; mime_type: string; preview: 'inline' | 'download'; url: string; converter?: string | null }>(
      `/orders/${id}/preview`,
    ),
  acknowledge: (id: string, comment?: string, sign?: { code?: string; word?: string }) =>
    http.post<Acknowledgement>(`/orders/${id}/acknowledgements`, {
      comment: comment ?? null,
      code: sign?.code,
      word: sign?.word,
    }),
  acknowledgements: (id: string, status?: 'PENDING' | 'ACKNOWLEDGED') =>
    http.get<Acknowledgement[]>(`/orders/${id}/acknowledgements`, { status }),
  ackSummary: (id: string) =>
    http.get<{
      order_id: string
      total: number
      acknowledged: number
      pending: number
      progress: number
      ack_deadline: string | null
      pending_users: { id: string; full_name: string; email: string }[]
    }>(`/orders/${id}/ack-summary`),
  setRecipients: (id: string, user_ids: string[]) => http.put<OrderDetail>(`/orders/${id}/recipients`, { user_ids }),
  filePath: (id: string, fileId?: string, inline = true) =>
    `/orders/${id}/file?inline=${inline}${fileId ? `&file_id=${fileId}` : ''}`,
  exportAckSheet: () => '/orders/export/ack-sheet.csv',
}

export const tasksApi = {
  list: (filters: TaskFilters = {}) => http.get<TaskPage>('/tasks', filters as QueryParams),
  detail: (id: string) => http.get<TaskDetail>(`/tasks/${id}`),
  create: (payload: TaskPayload) => http.post<Task>('/tasks', payload),
  update: (id: string, payload: Partial<TaskPayload>) => http.patch<Task>(`/tasks/${id}`, payload),
  changeStatus: (id: string, status: TaskStatusStored, note?: string) =>
    http.post<Task>(`/tasks/${id}/status`, { status, note }),
  comment: (id: string, body: string) => http.post<TaskComment>(`/tasks/${id}/comments`, { body }),
  comments: (id: string) => http.get<TaskComment[]>(`/tasks/${id}/comments`),
  remove: (id: string) => http.delete<void>(`/tasks/${id}`),
  counts: () => http.get<Record<string, number>>('/tasks/status-counts'),
  overdueReport: () => http.get<{ items: OverdueRow[] }>('/tasks/overdue-report'),

  /** отметка «выполнено» текущего пользователя (исполнителя задачи) */
  markMyDone: (id: string) => http.post<Task>(`/tasks/${id}/my-mark`),
  clearMyMark: (id: string) => http.delete<Task>(`/tasks/${id}/my-mark`),

  /** история повторов: закрытые периоды (выполнен / провален), новые первыми */
  occurrences: (id: string, limit = 60) =>
    http.get<TaskOccurrence[]>(`/tasks/${id}/occurrences`, { limit }),

  uploadFile: (id: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return http.upload<TaskDetail>(`/tasks/${id}/files`, form)
  },
  removeFile: (id: string, fileId: string) => http.delete<TaskDetail>(`/tasks/${id}/files/${fileId}`),
  fileDownloadPath: (id: string, fileId: string) => `/tasks/${id}/files/${fileId}/download`,
}

export interface OverdueRow {
  id: string
  title: string
  assignees: string[]
  assignee_ids: string[]
  deadline?: string | null
  overdue_by_days?: number | null
  status: string
  priority: string
}

export const plansApi = {
  list: (params: { status?: PlanStatus | ''; year?: number; page?: number; per_page?: number } = {}) =>
    http.get<Page<PlanListItem>>('/plans', params as QueryParams),
  detail: (id: string) => http.get<Plan>(`/plans/${id}`),
  current: () => http.get<Plan | null>('/plans/current'),
  create: (payload: PlanPayload) => http.post<Plan>('/plans', payload),
  update: (id: string, payload: Partial<PlanPayload>) => http.patch<Plan>(`/plans/${id}`, payload),
  publish: (id: string) => http.post<Plan>(`/plans/${id}/publish`),
  archive: (id: string) => http.post<Plan>(`/plans/${id}/archive`),
  remove: (id: string) => http.delete<void>(`/plans/${id}`),
  /** разбор DOCX школьного макета плана недели в структуру редактора */
  parseDocument: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return http.upload<PlanParseResult>('/plans/parse-document', form)
  },
}

export const certificationsApi = {
  list: (params: {
    kind?: CertKind | ''
    user_id?: string
    state?: CertState | ''
    search?: string
    sort?: 'expires' | 'user' | 'awarded'
    order?: 'asc' | 'desc'
    page?: number
    per_page?: number
  } = {}) => http.get<CertPage>('/certifications', params as QueryParams),
  create: (payload: CertificationPayload) => http.post<Certification>('/certifications', payload),
  update: (id: string, payload: Partial<CertificationPayload>) =>
    http.patch<Certification>(`/certifications/${id}`, payload),
  remove: (id: string) => http.delete<void>(`/certifications/${id}`),
  uploadFile: (id: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return http.upload<Certification>(`/certifications/${id}/file`, form)
  },
  removeFile: (id: string) => http.delete<Certification>(`/certifications/${id}/file`),
  /** путь для downloadAuthorized (префикс /api/v1 добавляет сам http-клиент) */
  fileUrl: (id: string, inline = false) => `/certifications/${id}/file?inline=${inline}`,
  exportCsv: () => '/certifications/export.csv',
  templateCsv: () => '/certifications/import/template.csv',
  importCsv: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return http.upload<CertImportResult>('/certifications/import', form)
  },
}

export const equipmentApi = {
  list: (params: { search?: string; location?: string; page?: number; per_page?: number } = {}) =>
    http.get<Page<EquipmentItem>>('/equipment', params as QueryParams),
  create: (payload: Record<string, unknown>) => http.post<EquipmentItem>('/equipment', payload),
  update: (id: string, payload: Record<string, unknown>) => http.patch<EquipmentItem>(`/equipment/${id}`, payload),
  remove: (id: string) => http.delete<void>(`/equipment/${id}`),
  moveItem: (id: string, payload: { to_location: string; move_date?: string | null; quantity?: number; note?: string | null }) =>
    http.post<EquipmentMove>(`/equipment/${id}/move`, payload),
  moves: (month: string) => http.get<EquipmentMove[]>('/equipment/moves', { month }),
  scan: (code: string) => http.get<EquipmentItem>('/equipment/scan', { code }),
  movesCsv: (month: string) => `/equipment/moves/export.csv?month=${month}`,
  importCsv: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return http.upload<{ created: number; updated: number; total: number; failed: { row: number; reason: string }[] }>(
      '/equipment/import',
      form,
    )
  },
}

export const movementApi = {
  list: (params: { status?: string; order_type?: string; search?: string; page?: number; per_page?: number } = {}) =>
    http.get<Page<MovementOrder>>('/movement', params as QueryParams),
  create: (payload: Record<string, unknown>) => http.post<MovementOrder>('/movement', payload),
  update: (id: string, payload: Record<string, unknown>) => http.patch<MovementOrder>(`/movement/${id}`, payload),
  remove: (id: string) => http.delete<void>(`/movement/${id}`),
  exportCsv: (ids: string[]) => `/movement/export.csv?ids=${ids.join(',')}`,
  template: () => http.get<{ template: string | null; default: string }>('/movement/template'),
  saveTemplate: (template: string | null) => http.put<{ template: string | null; default: string }>('/movement/template', { template }),
}

export const incomingApi = {
  list: (params: { search?: string; year?: number; page?: number; per_page?: number } = {}) =>
    http.get<Page<IncomingLetter>>('/incoming', params as QueryParams),
  create: (payload: {
    incoming_number: string
    incoming_date?: string | null
    sent_date?: string | null
    addressee?: string | null
    subject: string
    note?: string | null
    responsible_id?: string | null
  }) => http.post<IncomingLetter>('/incoming', payload),
  update: (
    id: string,
    payload: Partial<{
      incoming_number: string
      incoming_date: string | null
      sent_date: string | null
      addressee: string | null
      subject: string
      note: string | null
      responsible_id: string | null
    }>,
  ) => http.patch<IncomingLetter>(`/incoming/${id}`, payload),
  remove: (id: string) => http.delete<void>(`/incoming/${id}`),
  exportCsv: (year?: number) => `/incoming/export.csv${year ? `?year=${year}` : ''}`,
}

export const usersApi = {
  list: (params: { search?: string; role?: Role | ''; is_active?: boolean; include_deleted?: boolean; page?: number; per_page?: number } = {}) =>
    http.get<Page<User>>('/users', params as QueryParams),
  options: (role?: Role) => http.get<User[]>('/users/options', { role }),
  detail: (id: string) => http.get<User>(`/users/${id}`),
  create: (payload: {
    email?: string
    login?: string
    password: string
    full_name: string
    role: Role
    position_ids?: string[]
    phone?: string
    birth_date?: string
  }) => http.post<User>('/users', payload),
  bulkUpdate: (payload: {
    user_ids: string[]
    position_ids_add?: string[]
    position_ids_remove?: string[]
    incoming_journal_access?: boolean
    movement_access?: boolean
    equipment_access?: boolean
    notes_full_access?: boolean
    substitutions_access?: boolean
    helpdesk_access?: boolean
    is_active?: boolean
  }) => http.post<{ updated: number; skipped: { id: string; reason: string }[] }>('/users/bulk-update', payload),
  listViews: () => http.get<Record<string, Record<string, string>>>('/users/me/list-views'),
  saveListView: (key: string, params: Record<string, string>) =>
    http.put<{ list_key: string; params: Record<string, string> }>(`/users/me/list-views/${key}`, { params }),
  deleteListView: (key: string) => http.delete<void>(`/users/me/list-views/${key}`),
  update: (
    id: string,
    payload: Partial<{
      full_name: string
      position: string
      phone: string
      birth_date: string | null
      email: string
      role: Role
      is_active: boolean
      password: string
      login: string
      password_must_change: boolean
      // персональные гранты Владельца
      incoming_journal_access: boolean
      movement_access: boolean
      equipment_access: boolean
      notes_full_access: boolean
      substitutions_access: boolean
      /** v1.12: грант «системный администратор» — очередь заявок Helpdesk */
      helpdesk_access: boolean
    }>,
  ) => http.patch<User>(`/users/${id}`, payload),
  /** удаление с сохранением истории (только Владелец); ПЭП «код + слово» ОБЯЗАТЕЛЬНА */
  remove: (id: string, sign: { code: string; word: string }) =>
    http.delete<void>(`/users/${id}`, sign),
  restore: (id: string) => http.post<User>(`/users/${id}/restore`),
  /** Аварийный сброс чужого аутентификатора (только Владелец); ПЭП «код + слово».
   *  Нужен, когда сотрудник потерял телефон и не может отвязать TOTP сам. */
  resetTotp: (id: string, sign: { code: string; word: string }) =>
    http.post<void>(`/users/${id}/totp-reset`, sign),

  /** Массовое создание: строки «ФИО, ПАРОЛЬ[, e-mail[, роль]]» уже распарсены клиентом. */
  bulkCreate: (rows: { full_name: string; password: string; email?: string | null; role?: Role; position?: string | null }[]) =>
    http.post<BulkImportResult>('/users/bulk', { rows }),

  /** Аватары: загрузка/удаление своего, получение чужого (blob с авторизацией). */
  uploadMyAvatar: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return http.upload<User>('/users/me/avatar', form)
  },
  deleteMyAvatar: () => http.delete<void>('/users/me/avatar'),
  avatarBlob: (id: string) => http.blob(`/users/${id}/avatar`),

  /** Active Directory: поиск в каталоге и импорт учётных записей. */
  adSearch: (q: string, limit = 50) => http.get<ADUser[]>('/users/ad/search', { q, limit }),
  adImport: (usernames: string[], role?: Role | null) =>
    http.post<ADImportResult>('/users/ad/import', { usernames, role: role ?? null }),
}

export const notificationsApi = {
  list: (params: { unread_only?: boolean; page?: number; per_page?: number } = {}) =>
    http.get<Page<AppNotification>>('/notifications', params as QueryParams),
  unreadCount: () => http.get<{ unread: number }>('/notifications/unread-count'),
  markRead: (id: string) => http.post<void>(`/notifications/${id}/read`),
  markAllRead: () => http.post<void>('/notifications/read-all'),
  remove: (id: string) => http.delete<void>(`/notifications/${id}`),
  clearAll: () => http.delete<void>('/notifications'),
}

export const auditApi = {
  list: (
    params: {
      action?: string
      entity_type?: string
      actor_id?: string
      date_from?: string
      date_to?: string
      page?: number
      per_page?: number
    } = {},
  ) => http.get<Page<AuditEntry>>('/audit', params as QueryParams),
  actions: () => http.get<string[]>('/audit/actions'),
  /** полная очистка журнала; доступно только Владельцу; ПЭП «код + слово» */
  clear: (sign?: { code: string; word: string }) => http.delete<void>('/audit', sign),
}

export const dashboardApi = {
  summary: (scope?: 'mine' | 'school') => http.get<Dashboard>('/dashboard', scope ? { scope } : undefined),
  /** отчёт «Итоги месяца»: month в формате ГГГГ-ММ */
  monthSummary: (month: string) => http.get<MonthSummary>('/dashboard/month-summary', { month }),
}

export const searchApi = {
  global: (q: string) => http.get<SearchResults>('/search', { q }),
}

export const systemApi = {
  /** полная замена БД из .sql-дампа (только Владелец); все сессии сбрасываются.
   *  ПЭП максимального уровня: одноразовый код аутентификатора + контрольное слово. */
  restore: (file: File, sign?: { code: string; word: string }) => {
    const form = new FormData()
    form.append('file', file)
    if (sign?.code) form.append('pep_code', sign.code)
    if (sign?.word) form.append('pep_word', sign.word)
    return http.upload<{ ok: boolean; detail: string }>('/system/restore', form)
  },
  /** статус режима «Локдаун» и маскируется ли ответ для текущего пользователя */
  lockdownStatus: () => http.get<{ enabled: boolean; masked_for_you: boolean }>('/system/lockdown'),
  /** локдаун требует подпись «код + слово» (ПЭП максимального уровня) */
  lockdownSet: (enabled: boolean, sign?: { code?: string; word?: string }) =>
    http.post<{ enabled: boolean }>('/system/lockdown', { enabled, code: sign?.code, word: sign?.word }),
  /** сводка по файловому хранилищу: файлы на диске, ссылки БД, битые ссылки, сироты */
  filesSummary: () =>
    http.get<{
      files_on_disk: number
      bytes_on_disk: number
      db_references: number
      broken_links: { storage_path: string; kind: string; original_name: string | null }[]
      orphan_files: { storage_path: string; size_bytes: number }[]
      orphan_files_total: number
      manifest_version: number
    }>('/system/files-summary'),
  /**
   * Подпись выгрузки данных (ПЭП «код + слово») → одноразовый билет.
   *
   * Дамп БД и архив всех файлов школы раскрывают ВСЕ персональные данные, поэтому
   * скачиваются только по билету. GET-запрос не может нести тело, а код+слово
   * в query-строке попали бы в access-логи nginx — отсюда двухшаговый обмен.
   */
  exportTicket: (target: 'db_backup' | 'files_zip', sign: { code: string; word: string }) =>
    http.post<{ ticket: string; target: string; url: string; expires_in: number }>(
      '/system/export-ticket', { target, pep_code: sign.code, pep_word: sign.word }),
  /** путь дампа БД; билет подставляется параметром `ticket` */
  backupPath: '/system/backup',
  /** ZIP всех файлов хранилища с manifest.json (скачивается через downloadAuthorized) */
  filesZipPath: '/system/files-zip',
  /** восстановление файлов из ZIP с manifest.json; ПЭП «код + слово» */
  filesRestore: (file: File, sign?: { code: string; word: string }) => {
    const form = new FormData()
    form.append('file', file)
    if (sign?.code) form.append('pep_code', sign.code)
    if (sign?.word) form.append('pep_word', sign.word)
    return http.upload<{
      restored: string[]
      replaced: string[]
      already: string[]
      orphans: string[]
      errors: { item: string; reason: string }[]
    }>('/system/files-restore', form)
  },
}

export const metaApi = {
  /** название школы + версия установленной сборки (версия публична: её отдаёт и /health) */
  school: () => http.get<{ school_name: string | null; version?: string }>('/meta/school'),
  orderTypes: () => http.get<{ code: string; title: string }[]>('/meta/order-types'),
  addOrderType: (title: string, code?: string) => http.post<{ code: string; title: string }>('/meta/order-types', { title, code }),
  removeOrderType: (code: string) => http.delete<void>(`/meta/order-types/${code}`),
  updateSchool: (school_name: string) => http.patch<{ school_name: string | null }>('/meta/school', { school_name }),
  enums: () =>
    http.get<Record<string, { value: string; label: string }[]>>('/meta/enums'),
  permissions: () =>
    http.get<{ roles: string[]; capabilities: { capability: string; grants: Record<string, boolean> }[] }>(
      '/meta/permissions',
    ),
}

export const positionsApi = {
  list: () => http.get<Position[]>('/meta/positions'),
  create: (title: string, aliases?: string) => http.post<Position>('/meta/positions', { title, aliases }),
  update: (id: string, payload: { title?: string; aliases?: string; sort_order?: number }) =>
    http.patch<Position>(`/meta/positions/${id}`, payload),
  remove: (id: string) => http.delete<void>(`/meta/positions/${id}`),
}

export const vshkApi = {
  list: () => http.get<VshkPlanList[]>('/vshk'),
  detail: (id: string) => http.get<VshkPlan>(`/vshk/${id}`),
  create: (year_start?: number) => http.post<VshkPlan>('/vshk', { year_start }),
  importDocx: (file: File, year_start?: number) => {
    const form = new FormData()
    form.append('file', file)
    if (year_start) form.append('year_start', String(year_start))
    return http.upload<{ plan_id: string; year_label: string; items_created: number; sections: number; warnings: string[] }>(
      '/vshk/import',
      form,
    )
  },
  publish: (id: string) => http.post<VshkPlan>(`/vshk/${id}/publish`),
  addItem: (planId: string, payload: Record<string, unknown>) => http.post<VshkItem>(`/vshk/${planId}/items`, payload),
  updateItem: (itemId: string, payload: Record<string, unknown>) => http.patch<VshkItem>(`/vshk/items/${itemId}`, payload),
  removeItem: (itemId: string) => http.delete<void>(`/vshk/items/${itemId}`),
  setResult: (itemId: string, result_text: string | null, file?: File) => {
    const form = new FormData()
    if (result_text !== null) form.append('result_text', result_text)
    if (file) form.append('file', file)
    return http.upload<VshkItem>(`/vshk/items/${itemId}/result`, form)
  },
  clearResult: (itemId: string) => http.delete<void>(`/vshk/items/${itemId}/result`),
  fileUrl: (itemId: string) => `/api/v1/vshk/items/${itemId}/file`,
}

// ==================== Модуль 2: служебные записки ====================
export interface NoteFilters {
  status?: string | ''
  author_id?: string
  executor_id?: string
  year?: number
  q?: string
}

export interface NotePayload {
  subject: string
  body: string
  executor_id?: string | null
  /** кого просят утвердить (Владелец или администрация); пусто — входящий пул администрации */
  approver_id?: string | null
  deadline?: string | null
}

export const notesApi = {
  list: (filters: NoteFilters = {}) => http.get<ServiceNote[]>('/notes', filters as QueryParams),
  detail: (id: string) => http.get<ServiceNoteDetail>(`/notes/${id}`),
  create: (payload: NotePayload) => http.post<ServiceNoteDetail>('/notes', payload),
  update: (id: string, payload: Partial<NotePayload>) => http.patch<ServiceNoteDetail>(`/notes/${id}`, payload),
  submit: (id: string) => http.post<ServiceNoteDetail>(`/notes/${id}/submit`),
  /** утверждение — подпись кодом TOTP всегда; word задаётся, если привязано контрольное слово */
  approve: (id: string, sign: { code: string; word?: string }) =>
    http.post<ServiceNoteDetail>(`/notes/${id}/approve`, sign),
  reject: (id: string, comment: string) => http.post<ServiceNoteDetail>(`/notes/${id}/reject`, { comment }),
  addFile: (id: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return http.upload<ServiceNoteDetail>(`/notes/${id}/files`, form)
  },
  removeFile: (id: string, fileId: string) => http.delete<ServiceNoteDetail>(`/notes/${id}/files/${fileId}`),
  downloadFile: (id: string, fileId: string, name: string) =>
    downloadAuthorized(`/notes/${id}/files/${fileId}/download`, name),
  /** v1.12: открыть документ В НОВОЙ ВКЛАДКЕ (сервер отдаёт автоматическую PDF-копию) */
  openFile: (id: string, fileId: string) =>
    openDocumentInNewTab(`/notes/${id}/files/${fileId}/download`, { inline: 'true' }),
  /** печатная форма: только авторизованный запрос (прямая ссылка отдаёт 401) */
  printHtml: (id: string, autoPrint = false) =>
    http.blob(`/notes/${id}/print`, autoPrint ? { auto_print: 'true' } : undefined),
  remove: (id: string) => http.delete<void>(`/notes/${id}`),
  exportCsv: () => downloadAuthorized('/notes/export.csv', 'service_notes.csv'),
}

// ==================== Модуль 3: замещения ====================
export interface SubstitutionPayload {
  absent_user_id: string
  substitute_user_id: string
  reason: string
  start_date: string
  end_date?: string | null
  transfer_tasks?: boolean
  transfer_plans?: boolean
  transfer_vshk?: boolean
}

export const substitutionsApi = {
  list: (params: { status?: string | ''; user_id?: string; ongoing?: boolean } = {}) =>
    http.get<Substitution[]>('/substitutions', params as QueryParams),
  create: (payload: SubstitutionPayload) => http.post<Substitution>('/substitutions', payload),
  confirm: (id: string) => http.post<Substitution>(`/substitutions/${id}/confirm`),
  end: (id: string) => http.post<Substitution>(`/substitutions/${id}/end`),
  cancel: (id: string, comment?: string) => http.post<Substitution>(`/substitutions/${id}/cancel`, { comment: comment ?? null }),
  /** продлить период (больничный продлили): новая дата окончания + необязательный комментарий */
  extend: (id: string, endDate: string, comment?: string) =>
    http.post<Substitution>(`/substitutions/${id}/extend`, { end_date: endDate, comment: comment ?? null }),
  /** удалить строку замещения: Владелец или назначенный им кадровик */
  remove: (id: string) => http.delete<void>(`/substitutions/${id}`),
  exportCsv: (params: { status?: string; ongoing?: boolean } = {}) =>
    downloadAuthorized('/substitutions/export.csv', 'substitutions.csv', params as QueryParams),
}

// ==================== Модуль 4: справка / FAQ ====================
export interface HelpArticlePayload {
  title: string
  category?: string
  body?: string | null
  mandatory_read?: boolean
  /** ALL (по умолчанию) / ADMIN — только администрации / OWNER — только Владельцу.
   *  Администрация может задать ALL или ADMIN; OWNER — любое значение. */
  visibility?: HelpVisibility
}

/** Мгновенные уведомления: одноразовый билет для WebSocket-подключения.
 *  Access-токен в query больше не передаётся — он попадал в логи nginx (§5.6). */
export const realtimeApi = {
  ticket: () => http.post<{ ticket: string; expires_in: number; url: string }>('/realtime/ticket'),
}

export const helpApi = {
  categories: () => http.get<string[]>('/help/categories'),
  list: (params: { q?: string; category?: string; kind?: string; visibility?: HelpVisibility } = {}) =>
    http.get<HelpArticle[]>('/help', params as QueryParams),
  detail: (id: string) => http.get<HelpArticle>(`/help/${id}`),
  pendingCount: () => http.get<{ count: number }>('/help/mine/pending'),
  create: (payload: HelpArticlePayload) => http.post<HelpArticle>('/help', payload),
  update: (id: string, payload: Partial<HelpArticlePayload>) => http.patch<HelpArticle>(`/help/${id}`, payload),
  remove: (id: string) => http.delete<void>(`/help/${id}`),
  /** УСТАРЕВШИЙ маршрут (единственный файл статьи). Сохранён для совместимости;
   *  новый код использует `uploadFiles` — вложений у инструкции может быть несколько. */
  uploadFile: (id: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return http.upload<HelpArticle>(`/help/${id}/file`, form)
  },
  fileDownloadPath: (id: string) => `/help/${id}/file/download`,
  downloadFile: (id: string, name: string) => downloadAuthorized(`/help/${id}/file/download`, name),
  /** v1.12: прикрепить документ к инструкции (можно несколько, с подписью).
   *  `caption` уходит query-параметром: тело запроса занято multipart-формой. */
  uploadFiles: (id: string, file: File, caption?: string) => {
    const form = new FormData()
    form.append('file', file)
    const suffix = caption?.trim() ? `?caption=${encodeURIComponent(caption.trim())}` : ''
    return http.upload<HelpArticle>(`/help/${id}/files${suffix}`, form)
  },
  removeFile: (id: string, fileId: string) => http.delete<HelpArticle>(`/help/${id}/files/${fileId}`),
  downloadArticleFile: (id: string, fileId: string, name: string) =>
    downloadAuthorized(`/help/${id}/files/${fileId}/download`, name),
  /** открыть документ инструкции в новой вкладке (PDF-копия) */
  openArticleFile: (id: string, fileId: string) =>
    openDocumentInNewTab(`/help/${id}/files/${fileId}/download`, { inline: 'true' }),
  ack: (id: string, sign?: { code?: string; word?: string }) =>
    http.post<HelpArticle>(`/help/${id}/ack`, sign ?? {}),
}

// ==================== Helpdesk: заявки в техподдержку (v1.12) ====================
export interface TicketPayload {
  title: string
  description: string
  category: TicketCategory
  priority: TicketPriority
  location?: string | null
  contact_phone?: string | null
  equipment_id?: string | null
}

/**
 * Заявки сотрудников в техническую поддержку школы.
 *
 * Заявку подаёт ЛЮБОЙ сотрудник; обслуживает её очередь системных администраторов —
 * сотрудников, которым Владелец выдал грант «системный администратор»
 * (`usersApi.update({ helpdesk_access: true })`). Администрация видит очередь
 * чтением, но закрывать заявки не может.
 */
export const helpdeskApi = {
  /** справочники формы: категории, приоритеты, статусы и нормативы реакции */
  directory: () => http.get<TicketDirectory>('/helpdesk/directory'),
  stats: () => http.get<TicketStats>('/helpdesk/stats'),
  list: (filters: TicketFilters = {}) => http.get<Ticket[]>('/helpdesk', filters as QueryParams),
  detail: (id: string) => http.get<Ticket>(`/helpdesk/${id}`),
  create: (payload: TicketPayload) => http.post<Ticket>('/helpdesk', payload),
  update: (id: string, payload: Partial<TicketPayload>) => http.patch<Ticket>(`/helpdesk/${id}`, payload),
  /** взять в работу; assignee_id — переназначить другому системному администратору */
  take: (id: string, assigneeId?: string, comment?: string) =>
    http.post<Ticket>(`/helpdesk/${id}/take`, { assignee_id: assigneeId ?? null, comment: comment ?? null }),
  resolve: (id: string, resolution: string, comment?: string) =>
    http.post<Ticket>(`/helpdesk/${id}/resolve`, { resolution, comment: comment ?? null }),
  /** автор подтвердил выполнение → CLOSED */
  confirm: (id: string, comment?: string) =>
    http.post<Ticket>(`/helpdesk/${id}/confirm`, { comment: comment ?? null }),
  /** автор вернул «выполненную» заявку на доработку */
  reopen: (id: string, comment: string) => http.post<Ticket>(`/helpdesk/${id}/reopen`, { comment }),
  reject: (id: string, reason: string) => http.post<Ticket>(`/helpdesk/${id}/reject`, { reason }),
  cancel: (id: string, comment?: string) =>
    http.post<Ticket>(`/helpdesk/${id}/cancel`, { comment: comment ?? null }),
  remove: (id: string) => http.delete<void>(`/helpdesk/${id}`),
  message: (id: string, body: string) => http.post<TicketMessage>(`/helpdesk/${id}/messages`, { body }),
  removeMessage: (id: string, messageId: string) => http.delete<Ticket>(`/helpdesk/${id}/messages/${messageId}`),
  addFile: (id: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return http.upload<Ticket>(`/helpdesk/${id}/files`, form)
  },
  removeFile: (id: string, fileId: string) => http.delete<Ticket>(`/helpdesk/${id}/files/${fileId}`),
  downloadFile: (id: string, fileId: string, name: string) =>
    downloadAuthorized(`/helpdesk/${id}/files/${fileId}/download`, name),
  /** открыть вложение (фото поломки) в новой вкладке — сервер отдаёт PDF-копию */
  openFile: (id: string, fileId: string) =>
    openDocumentInNewTab(`/helpdesk/${id}/files/${fileId}/download`, { inline: 'true' }),
  /** круг исполнителей: Владелец + сотрудники с грантом (для переназначения) */
  staff: () => http.get<SysadminUser[]>('/helpdesk/staff'),
  exportCsv: (params: { status?: string; year?: number } = {}) =>
    downloadAuthorized('/helpdesk/export.csv', 'helpdesk_tickets.csv', params as QueryParams),
}
