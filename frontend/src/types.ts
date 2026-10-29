/**
 * Контракт API (зеркалит pydantic-схемы backend'а).
 * При изменении схем на сервере — обновлять здесь (см. /api/v1/openapi.json).
 */

export type Role = 'OWNER' | 'ADMIN' | 'TEACHER'

/** код типа приказа из динамического справочника /meta/order-types */
export type OrderType = string
export type OrderStatus = 'DRAFT' | 'PUBLISHED' | 'ARCHIVED'
export type AckStatus = 'PENDING' | 'ACKNOWLEDGED'

/** Хранимые статусы задачи + вычисляемый OVERDUE (просрочена). */
export type TaskStatusStored = 'NEW' | 'IN_PROGRESS' | 'DONE'
export type EffectiveTaskStatus = TaskStatusStored | 'OVERDUE'
export type TaskPriority = 'LOW' | 'NORMAL' | 'HIGH' | 'URGENT'
/** Периодичность повторяющейся задачи. */
export type TaskRecurrence = 'NONE' | 'DAILY' | 'WEEKLY' | 'MONTHLY'

export type AuthSource = 'local' | 'ldap'

export interface User {
  id: string
  /** null/undefined — у учётки без e-mail: вход по логину */
  email: string | null
  /** логин локальной учётки (сгенерирован из ФИО, если e-mail не задан) */
  login?: string | null
  /** путь аватара в хранилище; изображение — GET /users/{id}/avatar */
  avatar?: string | null
  /** учётка удалена (скрыта, вход закрыт, история сохранена) */
  is_deleted?: boolean
  /** владелец потребовал сменить пароль при входе */
  password_must_change?: boolean
  /** грант доступа к журналу входящих писем (выдаёт Владелец) */
  incoming_journal_access?: boolean
  /** грант доступа к разделу «Движение обучающихся» (выдаёт Владелец) */
  movement_access?: boolean
  /** грант Владельца: журнал служебных записок целиком (v1.11) */
  notes_full_access?: boolean
  /** грант доступа к учёту оборудования (выдаёт Владелец) */
  equipment_access?: boolean
  /** грант «кадровик»: журнал замещений целиком + удаление строк замещений (выдаёт Владелец) */
  substitutions_access?: boolean
  /** грант «системный администратор»: очередь заявок Helpdesk (выдаёт Владелец, v1.12) */
  helpdesk_access?: boolean
  full_name: string
  /** должности-роли из справочника (может быть несколько) */
  positions: Position[]
  phone?: string | null
  /** дата рождения (необязательное поле): виджет «Дни рождения» на дашборде */
  birth_date?: string | null
  role: Role
  is_active: boolean
  created_at: string
  last_login_at?: string | null
  /** 'local' — пароль SchoolHub, 'ldap' — учётная запись домена (Active Directory) */
  auth_source: AuthSource
  domain_account?: string | null
  ad_groups?: string[] | null
  last_ad_sync_at?: string | null
  /** ПЭП: привязан и подтверждён аутентификатор (TOTP).
   *  Есть и в карточках сотрудников: Владелец видит, кому можно сбросить привязку. */
  totp_enabled?: boolean
}

export interface Me extends Omit<User, 'created_at' | 'last_login_at'> {
  capabilities: string[]
  /** ПЭП: установлено контрольное слово (уровень «код + слово») */
  recovery_word_set?: boolean
}

/** Должность-роль из справочника (назначается администрацией). */
export interface Position {
  id: string
  title: string
  aliases?: string
  sort_order?: number
}

/** Ответственный в плане/ВШК: сотрудник | должность целиком | администрация. */
export interface RespRef {
  kind: 'user' | 'pos' | 'admin'
  id?: string | null
  title: string
}

export interface SetupStatus {
  is_configured: boolean
  setup_required: boolean
  school_name?: string | null
  completed_by?: string | null
  hint?: string
}

export interface LoginOptions {
  ldap_enabled: boolean
  local_enabled: boolean
  login_hint: string
  password_managed_by: string
}

export interface ADUser {
  username: string
  full_name: string
  email: string
  position?: string | null
  phone?: string | null
  dn?: string | null
  disabled: boolean
  groups: string[]
}

export interface BulkImportResult {
  created: { full_name: string; login: string | null; email: string | null; role: Role }[]
  failed: { row: number; full_name: string; error: string }[]
  total: number
}

export interface ADImportResult {
  created: string[]
  skipped: string[]
  failed: { username: string; error: string }[]
  total: number
}

export interface OrderFile {
  id: string
  original_name: string
  mime_type: string
  size_bytes: number
  version: number
  is_current: boolean
  checksum?: string | null
  created_at: string
  uploaded_by?: User | null
}

export interface Acknowledgement {
  id: string
  status: AckStatus
  acknowledged_at?: string | null
  comment?: string | null
  ip_address?: string | null
  sign_method?: 'NONE' | 'CLICK' | 'TOTP' | 'CODE_WORD' | null // способ подписи (ПЭП)
  user: User
}

/** Политика усиленной квалифицированной подписи приказа (ПЭП). */
export type SignaturePolicy = 'NONE' | 'CLICK' | 'TOTP' | 'CODE_WORD'

export const SIGN_POLICY_LABELS: Record<SignaturePolicy, string> = {
  NONE: 'Без подписи (только аудит)',
  CLICK: 'Клик «Ознакомлен»',
  TOTP: 'Код из аутентификатора',
  CODE_WORD: 'Код + контрольное слово',
}

export const SIGN_METHOD_LABELS: Record<string, string> = {
  NONE: 'без подписи',
  CLICK: 'клик «Ознакомлен»',
  TOTP: 'код аутентификатора',
  CODE_WORD: 'код + слово',
}

export interface OrderListItem {
  id: string
  number: string
  order_year: number
  title: string
  order_type: OrderType
  status: OrderStatus
  issued_on: string
  ack_deadline?: string | null
  published_at?: string | null
  created_by?: User | null
  current_file?: OrderFile | null
  ack_total: number
  ack_done: number
  signature_policy?: SignaturePolicy | null // политика подписи ознакомления (ПЭП)
  my_ack?: Acknowledgement | null
  can_manage: boolean
}

export interface OrderDetail extends OrderListItem {
  description?: string | null
  require_all: boolean
  recipient_ids: string[]
  created_at: string
  updated_at: string
  files: OrderFile[]
}

export interface TaskAssignee {
  id: string
  user: User
  /** личная отметка «выполнено» (null — отметки нет) */
  done_at?: string | null
}

export interface TaskFile {
  id: string
  original_name: string
  mime_type: string
  size_bytes: number
  checksum?: string | null
  created_at: string
  uploaded_by?: User | null
}

export interface Task {
  id: string
  title: string
  incoming_number?: string | null
  /** «12/2026» — номер регистрации из журнала входящих */
  incoming_reg?: string | null
  description?: string | null
  status: TaskStatusStored
  effective_status: EffectiveTaskStatus
  priority: TaskPriority
  deadline?: string | null
  /** NONE | DAILY | WEEKLY | MONTHLY — периодичность повторяющейся задачи */
  recurrence: TaskRecurrence
  /** дни недели ISO (1=пн … 7=вс) для WEEKLY */
  recurrence_weekdays?: number[] | null
  /** число месяца (1..31) для MONTHLY */
  recurrence_day?: number | null
  /** когда задача выполнялась в последний раз (сохраняется между периодами) */
  last_done_at?: string | null
  /** повторяющаяся задача выполнена в текущем периоде */
  done_this_period?: boolean
  /** сколько периодов повторения подряд провалено (v1.10) */
  missed_streak?: number
  /** начало последнего закрытого периода повторения (локальная дата, «ГГГГ-ММ-ДД») */
  last_occurrence_start?: string | null
  /** DONE | MISSED — чем закрыт последний период */
  last_occurrence_status?: 'DONE' | 'MISSED' | null
  created_at: string
  updated_at: string
  completed_at?: string | null
  creator?: User | null
  assignees: TaskAssignee[]
  is_overdue: boolean
  days_left: number | null
  can_edit: boolean
  can_mark: boolean
  my_done: boolean
  marks_done: number
  files_count: number
}

export interface TaskComment {
  id: string
  body: string
  created_at: string
  author?: User | null
}

/** Закрытый период повторяющейся задачи: выполнен (DONE) или провален (MISSED). */
export interface TaskOccurrence {
  /** локальная дата начала периода, «ГГГГ-ММ-ДД» */
  period_start: string
  status: 'DONE' | 'MISSED'
  completed_at?: string | null
  /** полные имена тех, кто закрыл период */
  completed_by: string[]
  closed_at: string
}

export interface TaskHistoryItem {
  id: string
  from_status: string | null
  to_status: string
  note?: string | null
  created_at: string
  actor?: User | null
}

export interface TaskDetail extends Task {
  comments: TaskComment[]
  history: TaskHistoryItem[]
  files: TaskFile[]
}

// ------------------------------------------------- план работы на неделю
export type PlanStatus = 'DRAFT' | 'PUBLISHED' | 'ARCHIVED'

export interface PlanDuty {
  klass?: string | null
  teacher?: User | null
}

export interface PlanItem {
  text: string
  place?: string | null
  resp: RespRef[]
}

export interface PlanEvent {
  title: string
  time?: string | null
  place?: string | null
  resp: RespRef[]
}

export interface PlanDay {
  date: string
  weekday: string
  events: PlanEvent[]
}

export interface PlanContent {
  duty: PlanDuty
  notes: string[]
  month_items: PlanItem[]
  week_items: PlanItem[]
  days: PlanDay[]
}

export interface Plan {
  id: string
  week_start: string
  week_end: string
  status: PlanStatus
  content: PlanContent
  created_by?: User | null
  published_at?: string | null
  created_at: string
  updated_at: string
  events_total: number
  responsibles_total: number
  my_items: number
  can_manage: boolean
}

export interface PlanListItem {
  id: string
  week_start: string
  week_end: string
  status: PlanStatus
  duty_class?: string | null
  events_total: number
  responsibles_total: number
  my_items: number
  published_at?: string | null
  created_by?: User | null
  can_manage: boolean
}

export interface Page<T> {
  items: T[]
  total: number
  page: number
  per_page: number
  pages: number
}

export interface TaskPage extends Page<Task> {
  status_counts: Record<EffectiveTaskStatus | 'all' | 'ACTIVE', number>
}

export interface AppNotification {
  id: string
  type: string
  title: string
  body?: string | null
  entity_type?: 'order' | 'task' | 'plan' | 'certification' | 'helpdesk_ticket' | null
  entity_id?: string | null
  is_read: boolean
  created_at: string
}

export interface AuditEntry {
  id: string
  actor_id?: string | null
  actor_email?: string | null
  actor_name?: string | null
  actor_role?: Role | null
  action: string
  entity_type?: string | null
  entity_id?: string | null
  changes?: Record<string, unknown> | null
  ip_address?: string | null
  created_at: string
  /** Человекопонятная фраза события («Создан приказ № 433 «О зачислении…»»). */
  summary?: string
  /** Строки деталей («Номер: 432 → 433», «Круг ознакомления (3): …»). */
  lines?: string[]
}

export interface MyPlanEvent {
  kind: 'duty' | 'event' | 'week' | 'month'
  date?: string | null
  weekday?: string | null
  time?: string | null
  place?: string | null
  title: string
}

export interface MyVshkItem {
  id: string
  plan_id: string
  month: number
  section: string
  title: string
  control_type: string
  summary: string
  status: 'PLANNED' | 'IN_WORK' | 'DONE'
  result_text?: string | null
}

export interface VshkItem {
  id: string
  month: number
  section: string
  title: string
  control_type: string
  resp: string[]
  resp_titles: string[]
  summary: string
  status: 'PLANNED' | 'IN_WORK' | 'DONE'
  result_text?: string | null
  result_file?: string | null
  result_file_name?: string | null
  result_at?: string | null
  sort_order: number
}

export interface VshkPlan {
  id: string
  year_start: number
  year_label: string
  status: 'DRAFT' | 'PUBLISHED' | 'ARCHIVED'
  published_at?: string | null
  created_by?: User | null
  can_manage: boolean
  items: VshkItem[]
}

export interface VshkPlanList {
  id: string
  year_start: number
  year_label: string
  status: 'DRAFT' | 'PUBLISHED' | 'ARCHIVED'
  items_total: number
  published_at?: string | null
}

export interface Dashboard {
  current_plan?: Plan | null
  my_plan_events?: MyPlanEvent[]
  my_vshk?: MyVshkItem[]
  summary: {
    scope: 'personal' | 'global'
    orders: { total: number; published: number; drafts: number; pending_ack: number }
    tasks: { total: number; new: number; in_progress: number; done: number; overdue: number }
    users?: { total: number; teachers: number; admins: number; active: number } | null
  }
  pending_acks: OrderListItem[]
  my_tasks: Task[]
  recent_orders: OrderListItem[]
  recent_tasks: Task[]
  overdue_acks?: OverdueAck[]
  birthdays?: BirthdayItem[]
}

export interface TokenPair {
  access_token: string
  token_type: string
  expires_in?: number
  /** refresh-токен в теле НЕ отдаётся — живёт в httpOnly-cookie */
}

export type QueryParams = Record<string, string | number | boolean | undefined | null>

/** Результат разбора DOCX-плана недели (POST /plans/parse-document). */
export interface PlanParseResult {
  week_start: string | null
  content: {
    duty: { klass: string | null; teacher_id: string | null }
    notes: string[]
    month_items: { text: string; place: string | null; resp: string[] }[]
    week_items: { text: string; place: string | null; resp: string[] }[]
    days: { date: string; events: { title: string; time: string | null; place: string | null; resp: string[] }[] }[]
  }
  warnings: string[]
}

/** Запись журнала входящих писем. */
export interface IncomingLetter {
  id: string
  reg_year: number
  reg_no: number
  incoming_date: string
  incoming_number: string
  sent_date?: string | null
  addressee?: string | null
  subject: string
  note?: string | null
  responsible?: User | null
  task_id?: string | null
  created_at: string
  updated_at: string
}

export type MovementType = 'TRANSFER' | 'ENROLLMENT' | 'EXPULSION'
export type MovementStatus = 'NEW' | 'DONE' | 'ERROR'

/** Обучающийся в приказе по движению. */
export interface MovementChild {
  id?: string
  student: string
  birth_date?: string | null
  from_place?: string | null
  to_place?: string | null
}

/** Приказ по движению обучающихся (перевод / зачисление / отчисление). */
export interface MovementOrder {
  id: string
  order_type: MovementType
  status: MovementStatus
  guardian?: string | null
  children: MovementChild[]
  from_place?: string | null
  to_place?: string | null
  order_date: string
  order_number: string
  signatory?: string | null
  note?: string | null
  created_at: string
  updated_at: string
}

/** Перемещение позиции инвентаря. */
export interface EquipmentMove {
  id: string
  item_id: string
  item_name?: string | null
  inventory_number?: string | null
  from_location?: string | null
  to_location: string
  quantity: number
  move_date: string
  note?: string | null
  actor_name?: string | null
}

/** Позиция учёта оборудования и инвентаря. */
export interface EquipmentItem {
  id: string
  name: string
  inventory_number?: string | null
  serial_number?: string | null
  barcode?: string | null
  quantity: number
  location?: string | null
  note?: string | null
  created_at: string
  updated_at: string
}

/** Ближайший день рождения сотрудника (виджет дашборда администрации). */
export interface BirthdayItem {
  user_id: string
  full_name: string
  birth_date: string
  days_until: number
  turns_age: number
}

/** Просроченное ознакомление (сводка для администрации). */
export interface OverdueAck {
  order_id: string
  number: string
  title: string
  user_id: string
  user_name: string
  deadline: string
  days_overdue: number
}

/** Глобальный поиск (Ctrl+K): секции с ссылками внутри приложения. */
export interface SearchHit {
  id: string
  title: string
  subtitle: string
  url: string
}

export interface SearchResults {
  orders: SearchHit[]
  tasks: SearchHit[]
  incoming: SearchHit[]
  users: SearchHit[]
  vshk?: SearchHit[]
  certifications?: SearchHit[]
  equipment?: SearchHit[]
  movement?: SearchHit[]
  /** служебные записки (v1.11+), инструкции (v1.12) и заявки техподдержки (v1.12) */
  notes?: SearchHit[]
  help?: SearchHit[]
  helpdesk?: SearchHit[]
}

// ------------------------------------------------- аттестация и КПК
export type CertKind = 'CATEGORY' | 'CPK'
/** expiring — истекает в пределах горизонта напоминаний; indefinite — бессрочно */
export type CertState = 'valid' | 'expiring' | 'expired' | 'indefinite'

/** Запись об аттестации (квалификационная категория) или курсе повышения квалификации. */
export interface Certification {
  id: string
  user: User
  kind: CertKind
  category?: string | null
  title?: string | null
  hours?: number | null
  provider?: string | null
  document_no?: string | null
  awarded_at?: string | null
  expires_at?: string | null
  file_name?: string | null
  has_file: boolean
  note?: string | null
  state: CertState
  days_left: number | null
  created_at: string
  updated_at: string
}

export interface CertPage extends Page<Certification> {
  state_counts: Record<CertState | 'all', number>
}

/** Отчёт «Итоги месяца» (кнопка на дашборде, только администрация). */
export interface MonthSummary {
  month: string
  orders_published: number
  orders_by_type: { type: string; count: number }[]
  orders_ack_complete: number
  tasks_created: number
  tasks_done: number
  tasks_overdue: number
  incoming_count: number
  movement_count: number
}

// ==================== Модуль 2: служебные записки (ТЗ) ====================
export type NoteStatus = 'DRAFT' | 'ON_APPROVAL' | 'APPROVED' | 'REJECTED' | 'COMPLETED'

export const NOTE_STATUS_LABELS: Record<NoteStatus, string> = {
  DRAFT: 'Черновик',
  ON_APPROVAL: 'На согласовании',
  APPROVED: 'Утверждена',
  REJECTED: 'Отклонена',
  COMPLETED: 'Выполнена',
}

/**
 * Состояние автоматической PDF-копии документа (v1.12).
 *
 *  NA      — формат не конвертируется (архив) или оригинал уже PDF: открывается как есть;
 *  PENDING — копия ещё готовится (конвертация идёт фоном после загрузки);
 *  READY   — копия готова, документ можно открыть в новой вкладке;
 *  FAILED  — конвертер не справился (нет LibreOffice, таймаут): доступен оригинал.
 */
export type PdfStatus = 'NA' | 'PENDING' | 'READY' | 'FAILED'

export interface PdfFileMixin {
  pdf_status: PdfStatus
  /** пояснение: почему копия не нужна / не получилась — показываем подсказкой
   *  вместо пустого места, чтобы пользователь не ждал «готовится» вечно */
  pdf_note?: string | null
  /** копия готова — можно рисовать «Открыть в новой вкладке» */
  pdf_ready: boolean
}

export interface NoteFile extends PdfFileMixin {
  id: string
  original_name: string
  mime_type: string
  size_bytes: number
  created_at: string
}

export interface ServiceNote {
  id: string
  number: number | null
  year: number | null
  subject: string
  status: NoteStatus
  author?: User | null
  executor?: User | null
  approver?: User | null
  deadline?: string | null
  approved_at?: string | null
  approved_sign_method?: string | null
  reject_comment?: string | null
  task_id?: string | null
  created_at: string
  files: NoteFile[]
}

export interface ServiceNoteDetail extends ServiceNote {
  body: string
}

// ==================== Модуль 3: замещения (ТЗ) ====================
export type SubstitutionStatus = 'ACTIVE' | 'CONFIRMED' | 'ENDED' | 'CANCELLED'

export const SUBSTITUTION_STATUS_LABELS: Record<SubstitutionStatus, string> = {
  ACTIVE: 'Активно (ждёт подтверждения)',
  CONFIRMED: 'Подтверждено замещающим',
  ENDED: 'Завершено',
  CANCELLED: 'Отменено',
}

export interface Substitution {
  id: string
  absent_user: User
  substitute_user: User
  initiator?: User | null
  reason: string
  start_date: string
  end_date?: string | null
  transfer_tasks: boolean
  transfer_plans: boolean
  transfer_vshk: boolean
  status: SubstitutionStatus
  confirmed_at?: string | null
  ended_at?: string | null
  /** не null — объекты фактически переданы замещающему (и ещё не возвращены).
   *  Передача выполняется после подтверждения, возврат — при отмене/завершении/удалении. */
  transferred_at?: string | null
  note?: string | null
  created_at: string
}

// ==================== Модуль 4: справка / FAQ (ТЗ) ====================
export interface HelpAckRow {
  id: string
  user: User
  acked_at: string
  sign_method?: string | null
}

/** Кто видит статью справки: всем сотрудникам / только администрации / только Владельцу. */
export type HelpVisibility = 'ALL' | 'ADMIN' | 'OWNER'

/** Подписи значений видимости для интерфейса. */
export const VISIBILITY_LABELS: Record<HelpVisibility, string> = {
  ALL: 'всем сотрудникам',
  ADMIN: 'только администрации',
  OWNER: 'только Владельцу',
}

/** Короткая метка для бейджа в списке статей. */
export const VISIBILITY_BADGE: Record<HelpVisibility, string> = {
  ALL: 'всем',
  ADMIN: 'служебная',
  OWNER: 'для Владельца',
}

/** Вложение инструкции (v1.12): документов у одной статьи может быть несколько. */
export interface HelpArticleFile extends PdfFileMixin {
  id: string
  original_name: string
  mime_type: string
  size_bytes: number
  /** подпись к файлу: «Приложение 1», «Форма заявления» */
  caption?: string | null
  created_at: string
  uploaded_by?: User | null
}

export interface HelpArticle {
  id: string
  kind: 'SYSTEM' | 'UPLOAD'
  slug?: string | null
  title: string
  category: string
  body?: string | null
  /** список вложений (v1.12) */
  files?: HelpArticleFile[]
  /** НАСЛЕДИЕ до v1.12: данные первого вложения. Новый код читает `files`. */
  file_name?: string | null
  file_mime?: string | null
  file_size?: number | null
  mandatory_read: boolean
  /** ALL — общая, ADMIN — служебная (администрация), OWNER — только для Владельца */
  visibility: HelpVisibility
  created_by?: User | null
  updated_at: string
  my_ack_at?: string | null
  acks?: HelpAckRow[] | null
}

// ==================== Helpdesk: заявки в техподдержку (v1.12) ====================
/**
 * Жизненный цикл заявки (state machine на сервере, app/enums.py::TICKET_TRANSITIONS):
 *
 *   NEW ──► IN_PROGRESS ──► RESOLVED ──► CLOSED
 *    │           ▲              │
 *    │           └──────────────┘  (автор вернул на доработку)
 *    ├─► REJECTED   (системный администратор отклонил с причиной)
 *    └─► CANCELLED  (автор отозвал)
 */
export type TicketStatus = 'NEW' | 'IN_PROGRESS' | 'RESOLVED' | 'CLOSED' | 'REJECTED' | 'CANCELLED'

export const TICKET_STATUS_LABELS: Record<TicketStatus, string> = {
  NEW: 'Новая',
  IN_PROGRESS: 'В работе',
  RESOLVED: 'Выполнена',
  CLOSED: 'Закрыта',
  REJECTED: 'Отклонена',
  CANCELLED: 'Отозвана',
}

export type TicketPriority = 'LOW' | 'NORMAL' | 'HIGH' | 'URGENT'

export const TICKET_PRIORITY_LABELS: Record<TicketPriority, string> = {
  LOW: 'Низкий',
  NORMAL: 'Обычный',
  HIGH: 'Высокий',
  URGENT: 'Срочный',
}

/** Что сломалось — формулировки из школьной практики, а не «прочее». */
export type TicketCategory =
  | 'PRINTER' | 'COMPUTER' | 'NETWORK' | 'BOARD' | 'PROJECTOR' | 'DOC_CAMERA'
  | 'AUDIO' | 'SOFTWARE' | 'ACCOUNT' | 'PHONE' | 'OTHER'

export const TICKET_CATEGORY_LABELS: Record<TicketCategory, string> = {
  PRINTER: 'Принтер / МФУ / сканер',
  COMPUTER: 'Компьютер / ноутбук',
  NETWORK: 'Интернет / сеть',
  BOARD: 'Интерактивная доска',
  PROJECTOR: 'Проектор / экран',
  DOC_CAMERA: 'Документ-камера',
  AUDIO: 'Звук / микрофон',
  SOFTWARE: 'Программы',
  ACCOUNT: 'Доступы и пароли',
  PHONE: 'Телефон',
  OTHER: 'Прочее',
}

/** Иконка категории: журнал заявок читается глазами, а не по подписям. */
export const TICKET_CATEGORY_ICONS: Record<TicketCategory, string> = {
  PRINTER: '🖨',
  COMPUTER: '🖥',
  NETWORK: '🌐',
  BOARD: '📺',
  PROJECTOR: '📽',
  DOC_CAMERA: '📷',
  AUDIO: '🔊',
  SOFTWARE: '💿',
  ACCOUNT: '🔑',
  PHONE: '☎',
  OTHER: '🧰',
}

export interface TicketFile extends PdfFileMixin {
  id: string
  original_name: string
  mime_type: string
  size_bytes: number
  /** AUTHOR — приложил автор заявки, STAFF — системный администратор (акт, счёт) */
  kind: 'AUTHOR' | 'STAFF'
  created_at: string
  uploaded_by?: User | null
}

export interface TicketMessage {
  id: string
  /** MESSAGE — реплика участника, SYSTEM — служебная отметка о смене статуса */
  kind: 'MESSAGE' | 'SYSTEM'
  body: string
  author?: User | null
  created_at: string
}

export interface Ticket {
  id: string
  number: number | null
  year: number | null
  title: string
  description: string
  category: TicketCategory
  priority: TicketPriority
  status: TicketStatus
  location?: string | null
  contact_phone?: string | null
  equipment_id?: string | null
  equipment_name?: string | null

  author?: User | null
  assignee?: User | null
  closed_by?: User | null
  auto_closed: boolean

  taken_at?: string | null
  resolved_at?: string | null
  closed_at?: string | null
  resolution?: string | null
  rejected_reason?: string | null

  /** норматив реакции в часах, зафиксированный на момент создания */
  sla_hours?: number | null
  /** вычисляемые на сервере: крайний срок реакции, просрочка и фактические минуты */
  sla_deadline?: string | null
  sla_breached: boolean
  reaction_minutes?: number | null
  work_minutes?: number | null

  created_at: string
  updated_at: string
  files: TicketFile[]
  messages: TicketMessage[]
  last_message_at?: string | null
}

export interface TicketStats {
  total: number
  new: number
  in_progress: number
  resolved: number
  closed: number
  rejected: number
  cancelled: number
  mine: number
  sla_breached: number
  resolved_today: number
  median_reaction_minutes: number | null
  /** видит ли текущий пользователь всю очередь (сисадмин, администрация, Владелец) */
  is_staff: boolean
}

export interface TicketDirectory {
  categories: { value: TicketCategory; label: string }[]
  priorities: { value: TicketPriority; label: string }[]
  statuses: { value: TicketStatus; label: string }[]
  sla_hours: Record<string, number>
}

/** Системный администратор школы: Владелец + сотрудники с грантом Владельца. */
export interface SysadminUser {
  id: string
  full_name: string
  role: Role
  positions: string[]
}

export interface TicketFilters {
  status?: TicketStatus | ''
  category?: TicketCategory | ''
  priority?: TicketPriority | ''
  mine?: boolean
  unassigned?: boolean
  overdue?: boolean
  author_id?: string
  q?: string
  year?: number
  limit?: number
}
