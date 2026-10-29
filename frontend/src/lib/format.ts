/** Форматирование дат, чисел и производных статусов. Все даты храним в UTC, показываем в локали. */
import type { CertKind, CertState, EffectiveTaskStatus, OrderStatus, AckStatus, PlanStatus, Role, TaskPriority, TaskRecurrence } from '@/types'

const LOCALE = 'ru-RU'

/** Бэкенд отдаёт наивные datetime'ы (UTC) — нормализуем, чтобы JS не счёл их локальными. */
export function parseDate(value?: string | null): Date | null {
  if (!value) return null
  const iso = /Z$|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

export function formatDate(value?: string | null): string {
  const d = parseDate(value)
  return d ? d.toLocaleDateString(LOCALE) : '—'
}

export function formatDateTime(value?: string | null): string {
  const d = parseDate(value)
  return d
    ? d.toLocaleString(LOCALE, { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' })
    : '—'
}

/** Дата без времени («2026-10-12») → «12.10.2026» — без сдвига часовых поясов. */
export function formatDateIso(iso?: string | null): string {
  if (!iso) return '—'
  const [y, m, d] = String(iso).slice(0, 10).split('-')
  return d && m && y ? `${d}.${m}.${y}` : '—'
}

export function formatTime(value?: string | null): string {
  const d = parseDate(value)
  return d ? d.toLocaleTimeString(LOCALE, { hour: '2-digit', minute: '2-digit' }) : '—'
}

export function relativeTime(value?: string | null): string {
  const d = parseDate(value)
  if (!d) return '—'
  const diff = Date.now() - d.getTime()
  const min = Math.round(diff / 60000)
  if (Math.abs(min) < 1) return 'только что'
  if (Math.abs(min) < 60) return min > 0 ? `${min} мин назад` : `через ${-min} мин`
  const hours = Math.round(min / 60)
  if (Math.abs(hours) < 24) return hours > 0 ? `${hours} ч назад` : `через ${-hours} ч`
  const days = Math.round(hours / 24)
  if (Math.abs(days) < 30) return days > 0 ? `${days} дн назад` : `через ${-days} дн`
  return formatDate(value)
}

/** «осталось N дн» / «просрочено на N дн» — для дедлайнов задач.
 *
 * Считаем КАЛЕНДАРНЫЕ сутки (разница локальных полуночей), а не «округление
 * вверх» от остатка в миллисекундах: дедлайн сегодня в 15:02 утром — это
 * «сегодня», а не «завтра». Просрочка меньше суток показывается в часах. */
export function deadlineLabel(deadline?: string | null, done = false): string {
  const d = parseDate(deadline)
  if (!d) return 'без срока'
  if (done) return `завершено ${formatDate(deadline)}`
  const now = new Date()
  const ms = d.getTime() - now.getTime()
  const midnight = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const dayDiff = Math.round((midnight(d) - midnight(now)) / 86_400_000)
  if (ms < 0) {
    if (dayDiff === 0) {
      const hours = Math.floor(-ms / 3_600_000)
      if (hours < 1) return 'просрочено менее часа назад'
      return `просрочено на ${hours} ${plural(hours, 'час', 'часа', 'часов')}`
    }
    const days = Math.abs(dayDiff)
    return `просрочено на ${days} ${plural(days, 'день', 'дня', 'дней')}`
  }
  if (dayDiff === 0) return 'сегодня'
  if (dayDiff === 1) return 'завтра'
  return `осталось ${dayDiff} ${plural(dayDiff, 'день', 'дня', 'дней')}`
}

/** Имя из ФИО в формате «Фамилия Имя Отчество» (в приветствии — только имя, v1.11). */
export function firstName(fullName?: string | null): string {
  const words = (fullName || '').trim().split(/\s+/).filter(Boolean)
  if (words.length === 0) return ''
  const name = words.length > 1 ? words[1] : words[0]
  return name.charAt(0).toUpperCase() + name.slice(1)
}

/** Формы приветствия: нейтральные (подходят в любое время суток) и «по времени». */
const GREET_NEUTRAL = [
  'Здравствуйте', 'Привет', 'Приветствуем', 'Добрый день', 'Доброго дня', 'Всех благ',
  'Hello', 'Bonjour', 'Salut', 'Guten Tag', 'Buongiorno', 'Hola',
]
const GREET_MORNING = ['Доброе утро', 'С добрым утром', 'Удачного дня', 'Good morning', 'Bonjour']
const GREET_EVENING = ['Добрый вечер', 'Доброго вечера', 'Good evening', 'Bonsoir']
const GREET_NIGHT = ['Доброй ночи', 'Спокойной ночи', 'Good night']

/** Формы, уместные в этот час (днём — пустой список: берём нейтральные). */
function timedPool(hour: number): string[] {
  if (hour >= 5 && hour < 12) return GREET_MORNING
  if (hour >= 18 && hour < 23) return GREET_EVENING
  if (hour >= 23 || hour < 5) return GREET_NIGHT
  return []
}

/** Детерминированный «случайный» выбор: одинаковый результат для одного зерна,
 * поэтому приветствие не мигает при перерисовках, но меняется день ото дня
 * (пользовательское требование v1.11 — разные формы приветствия). */
function pickIndex(seed: string, length: number): number {
  // FNV-1a + финальное перемешивание: соседние зёрна (дата отличается на день)
  // не должны давать соседние варианты — иначе «случайность» выглядит счётчиком
  let h = 2166136261
  for (let i = 0; i < seed.length; i += 1) {
    h ^= seed.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  h ^= h >>> 15
  h = Math.imul(h, 2246822507)
  h ^= h >>> 13
  return Math.abs(h) % length
}

/** «Добрый день, Андрей» — обращение по имени и случайная форма приветствия.
 *
 * Форма выбирается раз в сутки на человека (зерно — «имя + дата»): утром чаще
 * «Доброе утро», вечером — «Добрый вечер», в остальное время — нейтральные и
 * шутливые («Привет», «Bonjour», «Guten Tag»). */
export function greeting(fullName?: string | null, now: Date = new Date()): string {
  const name = firstName(fullName)
  const day = `${now.getFullYear()}-${now.getMonth() + 1}-${now.getDate()}`
  const seed = `${name}|${day}`
  const timed = timedPool(now.getHours())
  // 6 из 10 — форма по времени суток, иначе нейтральная
  const pool = timed.length > 0 && pickIndex(`${seed}#timed`, 10) < 6 ? timed : GREET_NEUTRAL
  const word = pool[pickIndex(seed, pool.length)]
  return name ? `${word}, ${name}` : word
}

export function plural(n: number, one: string, few: string, many: string): string {
  const mod10 = n % 10
  const mod100 = n % 100
  if (mod10 === 1 && mod100 !== 11) return one
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few
  return many
}

export function formatSize(bytes?: number | null): string {
  if (!bytes && bytes !== 0) return '—'
  const units = ['Б', 'КБ', 'МБ', 'ГБ']
  let value = bytes
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i += 1
  }
  return `${i === 0 ? value : value.toFixed(1)} ${units[i]}`
}

/**
 * Длительность в минутах → «2 ч 15 мин» / «45 мин» / «3 дн 4 ч».
 *
 * Нужна Helpdesk (v1.12): норматив реакции и время в работе приходят с сервера
 * в минутах, а показывать «135» в журнале заявок бесполезно.
 */
export function formatDuration(minutes?: number | null): string {
  if (minutes === null || minutes === undefined || Number.isNaN(minutes)) return '—'
  const total = Math.max(0, Math.round(minutes))
  if (total < 60) return `${total} мин`
  const days = Math.floor(total / 1440)
  const hours = Math.floor((total % 1440) / 60)
  const mins = total % 60
  const parts: string[] = []
  if (days) parts.push(`${days} ${plural(days, 'день', 'дня', 'дней')}`)
  if (hours) parts.push(`${hours} ч`)
  if (!days && mins) parts.push(`${mins} мин`)
  return parts.join(' ')
}

/** Сколько времени прошло с момента (для «ждёт реакции 3 ч»). */
export function sinceLabel(value?: string | null): string {
  const d = parseDate(value)
  if (!d) return '—'
  return formatDuration((Date.now() - d.getTime()) / 60000)
}

export function toDateInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

export function toDateTimeLocal(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** datetime-local → ISO в UTC (то, что ждёт backend). */
export function fromDateTimeLocal(value: string): string | null {
  if (!value) return null
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d.toISOString()
}

export function initials(fullName?: string | null): string {
  if (!fullName) return '?'
  const parts = fullName.trim().split(/\s+/)
  return ((parts[0]?.[0] ?? '') + (parts[1]?.[0] ?? '')).toUpperCase()
}

export function shortName(fullName?: string | null): string {
  if (!fullName) return '—'
  const [last, first, middle] = fullName.trim().split(/\s+/)
  return [last, first?.[0] ? `${first[0]}.` : '', middle?.[0] ? `${middle[0]}.` : ''].filter(Boolean).join(' ')
}

// ------------------------------------------------------------------- словари
export const ORDER_TYPE_LABEL: Record<string, string> = {
  HR: 'По личному составу',
  STUDENTS: 'По обучающимся',
  EDUCATION: 'Учебная деятельность',
  SAFETY: 'Охрана труда и безопасность',
  ADMIN: 'Административно-хозяйственный',
  OTHER: 'Прочее',
}

export const ORDER_STATUS_LABEL: Record<OrderStatus, string> = {
  DRAFT: 'Черновик',
  PUBLISHED: 'Опубликован',
  ARCHIVED: 'Архив',
}

export const ACK_LABEL: Record<AckStatus, string> = {
  PENDING: 'Не ознакомлен',
  ACKNOWLEDGED: 'Ознакомлен',
}

export const PLAN_STATUS_LABEL: Record<PlanStatus, string> = {
  DRAFT: 'Черновик',
  PUBLISHED: 'Опубликован',
  ARCHIVED: 'Архив',
}

/** «12.10.2026 – 17.10.2026» — диапазон недели плана. */
export function formatWeekRange(start: string, end: string): string {
  const f = (iso: string) => {
    const [y, m, d] = iso.split('-')
    return `${d}.${m}.${y}`
  }
  return `${f(start)} – ${f(end)}`
}

export const TASK_STATUS_LABEL: Record<EffectiveTaskStatus, string> = {
  NEW: 'Новая',
  IN_PROGRESS: 'В работе',
  DONE: 'Выполнена',
  OVERDUE: 'Просрочена',
}

export const PRIORITY_LABEL: Record<TaskPriority, string> = {
  LOW: 'Низкий',
  NORMAL: 'Обычный',
  HIGH: 'Высокий',
  URGENT: 'Срочный',
}

export const ROLE_LABEL_FULL: Record<Role, string> = {
  OWNER: 'Владелец',
  ADMIN: 'Администрация',
  TEACHER: 'Учитель',
}

export const ACTION_LABEL: Record<string, string> = {
  LOGIN: 'Вход в систему',
  LOGIN_FAILED: 'Неудачная попытка входа',
  LOGOUT: 'Выход из системы',
  PASSWORD_CHANGE: 'Смена пароля',
  TOKEN_REUSE_DETECTED: 'Сброс сессий (повторный токен)',
  SETUP_COMPLETE: 'Первичная настройка',
  SYSTEM_RESTORE: 'Восстановление из резервной копии',
  SYSTEM_SEED: 'Инициализация демо-данных',
  AUDIT_CLEAR: 'Очистка журнала событий',
  SCHOOL_UPDATE: 'Изменено название школы',
  ORDER_TYPE_ADD: 'Добавлен тип приказов',
  ORDER_TYPE_DELETE: 'Удалён тип приказов',
  ORDER_CREATE: 'Создан приказ',
  ORDER_UPDATE: 'Изменён приказ',
  ORDER_PUBLISH: 'Опубликован приказ',
  ORDER_ARCHIVE: 'Приказ перемещён в архив',
  ORDER_DELETE: 'Удалён приказ',
  ORDER_FILE_UPLOAD: 'Загружен документ',
  ORDER_FILE_DELETE: 'Удалён документ',
  ORDER_ACK: 'Подтверждено ознакомление',
  ORDER_RECIPIENTS: 'Изменён круг ознакомления',
  TASK_CREATE: 'Создана задача',
  TASK_UPDATE: 'Изменена задача',
  TASK_STATUS: 'Сменён статус задачи',
  TASK_MARK: 'Отметка исполнителя по задаче',
  TASK_FILE: 'Прикреплён файл к задаче',
  TASK_FILE_DELETE: 'Удалён файл задачи',
  TASK_DELETE: 'Удалена задача',
  PLAN_CREATE: 'Создан план работы',
  PLAN_UPDATE: 'Изменён план работы',
  PLAN_PUBLISH: 'Опубликован план работы',
  PLAN_ARCHIVE: 'План работы перемещён в архив',
  PLAN_DELETE: 'Удалён черновик плана',
  USER_CREATE: 'Создан пользователь',
  USER_UPDATE: 'Изменён пользователь',
  USER_DELETE: 'Удалён пользователь',
  USER_RESTORE: 'Восстановлен пользователь',
  USER_DEACTIVATE: 'Заблокирован пользователь',
  USER_BULK_CREATE: 'Массовое создание пользователей',
  AD_IMPORT: 'Импорт пользователей из AD',
  AD_PROVISION: 'Создан пользователь из AD',
  AD_ROLE_UPGRADE: 'Повышена роль пользователя из AD',
  AVATAR_UPDATE: 'Обновлена фотография профиля',
  AVATAR_DELETE: 'Удалена фотография профиля',
  INCOMING_CREATE: 'Зарегистрирован входящий документ',
  INCOMING_UPDATE: 'Изменён входящий документ',
  INCOMING_DELETE: 'Удалён входящий документ',
  VSHK_CREATE: 'Создан черновик плана ВШК',
  VSHK_IMPORT: 'Импорт плана ВШК',
  VSHK_PUBLISH: 'Опубликован план ВШК',
  VSHK_ITEM_CREATE: 'Добавлено мероприятие ВШК',
  VSHK_ITEM_UPDATE: 'Изменено мероприятие ВШК',
  VSHK_ITEM_DELETE: 'Удалено мероприятие ВШК',
  VSHK_RESULT: 'Подведён итог мероприятия ВШК',
  VSHK_RESULT_CLEAR: 'Очищен итог мероприятия ВШК',
  POSITION_CREATE: 'Создана должность',
  POSITION_UPDATE: 'Изменена должность',
  POSITION_DELETE: 'Удалена должность',
  EQUIPMENT_IMPORT: 'Импорт инвентаря',
  EQUIPMENT_CREATE: 'Добавлена позиция инвентаря',
  EQUIPMENT_UPDATE: 'Изменена позиция инвентаря',
  EQUIPMENT_MOVE: 'Перемещение инвентаря',
  EQUIPMENT_DELETE: 'Удалена позиция инвентаря',
  MOVEMENT_TEMPLATE_UPDATE: 'Изменён шаблон приказа о движении',
  MOVEMENT_CREATE: 'Создан приказ о движении',
  MOVEMENT_UPDATE: 'Изменён приказ о движении',
  MOVEMENT_DELETE: 'Удалён приказ о движении',
  CERT_CREATE: 'Добавлена запись об аттестации/КПК',
  CERT_UPDATE: 'Изменена запись об аттестации/КПК',
  CERT_DELETE: 'Удалена запись об аттестации/КПК',
  CERT_IMPORT: 'Импорт данных об аттестации/КПК',
  CERT_FILE: 'Приложен скан документа об аттестации/КПК',
  CERT_FILE_DELETE: 'Удалён скан документа об аттестации/КПК',

  // -------------------------------------------------- служебные записки
  NOTE_CREATE: 'Создана служебная записка',
  NOTE_UPDATE: 'Изменена служебная записка',
  NOTE_SUBMIT: 'Служебная записка подана на рассмотрение',
  NOTE_APPROVE: 'Служебная записка согласована',
  NOTE_REJECT: 'Служебная записка отклонена',
  NOTE_COMPLETE: 'Служебная записка исполнена',
  NOTE_DELETE: 'Удалена служебная записка',
  NOTE_FILE: 'Приложен документ к служебной записке',
  NOTE_FILE_DELETE: 'Удалён документ служебной записки',
  NOTE_FILES_TO_TASK: 'Документы служебной записки переданы в задачу',

  // -------------------------------------------------- справка и инструкции
  HELP_CREATE: 'Добавлена инструкция',
  HELP_UPDATE: 'Изменена инструкция',
  HELP_DELETE: 'Удалена инструкция',
  HELP_FILE: 'Приложен документ к инструкции',
  HELP_FILE_DELETE: 'Удалён документ инструкции',
  HELP_ACK: 'Подтверждено ознакомление с инструкцией',
  HELP_SHEET_OPEN: 'Открыт лист ознакомления инструкции',
  HELP_SHEET_RESCOPE: 'Изменён круг ознакомления инструкции',

  // -------------------------------------------------- замещения
  SUBSTITUTION_CREATE: 'Добавлено замещение',
  SUBSTITUTION_UPDATE: 'Изменено замещение',
  SUBSTITUTION_CONFIRM: 'Замещение подтверждено',
  SUBSTITUTION_CANCEL: 'Замещение отменено',
  SUBSTITUTION_EXTEND: 'Замещение продлено',
  SUBSTITUTION_END: 'Замещение закрыто',
  SUBSTITUTION_DELETE: 'Удалено замещение',

  // -------------------------------------------------- техподдержка (v1.12)
  TICKET_CREATE: 'Подана заявка в техподдержку',
  TICKET_UPDATE: 'Изменена заявка техподдержки',
  TICKET_TAKE: 'Заявка взята в работу',
  TICKET_RESOLVE: 'Заявка выполнена',
  TICKET_CLOSE: 'Заявка закрыта',
  TICKET_REOPEN: 'Заявка возвращена на доработку',
  TICKET_REJECT: 'Заявка отклонена',
  TICKET_CANCEL: 'Заявка отозвана автором',
  TICKET_DELETE: 'Удалена заявка техподдержки',
  TICKET_MESSAGE: 'Сообщение по заявке техподдержки',
  TICKET_MESSAGE_DELETE: 'Удалено сообщение по заявке',
  TICKET_FILE: 'Приложен документ к заявке техподдержки',
  TICKET_FILE_DELETE: 'Удалён документ заявки техподдержки',
  TICKET_SLA_BREACH: 'Просрочен норматив реакции по заявке',

  // -------------------------------------------------- электронная подпись (ПЭП)
  TOTP_SETUP_START: 'Начата привязка аутентификатора',
  TOTP_CONFIRMED: 'Аутентификатор привязан',
  TOTP_REMOVED: 'Аутентификатор отвязан',
  TOTP_RESET: 'Аутентификатор сброшен Владельцем',
  TOTP_WORD_SET: 'Задано контрольное слово',
  TOTP_BREAK_GLASS: 'Резервный сброс аутентификатора Владельца',
  TOTP_BREAK_GLASS_DONE: 'Резервный сброс выполнен, сессии отозваны',
  SIGNATURE_FAILED: 'Проверка подписи не пройдена',

  // -------------------------------------------------- система
  LOCKDOWN_ON: 'Включён режим блокировки',
  SYSTEM_EXPORT_TICKET: 'Выдан билет на выгрузку данных',
  SYSTEM_FILES_ZIP: 'Выгружен архив файлов школы',
  SYSTEM_FILES_RESTORE: 'Файлы школы восстановлены из архива',
  USER_BULK_UPDATE: 'Массовое изменение сотрудников',
}

// ------------------------------------------------- повторяющиеся задачи
/** Дни недели ISO: 1=пн … 7=вс. */
export const WEEKDAY_SHORT: Record<number, string> = {
  1: 'пн', 2: 'вт', 3: 'ср', 4: 'чт', 5: 'пт', 6: 'сб', 7: 'вс',
}

export const RECURRENCE_LABEL: Record<TaskRecurrence, string> = {
  NONE: 'Не повторять',
  DAILY: 'Каждый день',
  WEEKLY: 'Каждую неделю',
  MONTHLY: 'Каждый месяц',
}

/** Короткая человеческая подпись периодичности: «🔁 каждый день», «🔁 пн, ср, пт», «🔁 5-го числа». */
export function recurrenceLabel(task: {
  recurrence?: TaskRecurrence
  recurrence_weekdays?: number[] | null
  recurrence_day?: number | null
}): string {
  switch (task.recurrence ?? 'NONE') {
    case 'DAILY':
      return 'каждый день'
    case 'WEEKLY': {
      const days = [...(task.recurrence_weekdays ?? [])].sort((a, b) => a - b)
      const isWorkdays = days.length === 5 && [1, 2, 3, 4, 5].every((d) => days.includes(d))
      if (isWorkdays) return 'по будням'
      return days.map((d) => WEEKDAY_SHORT[d] ?? d).join(', ') || 'еженедельно'
    }
    case 'MONTHLY':
      return task.recurrence_day ? `${task.recurrence_day}-го числа` : 'ежемесячно'
    default:
      return ''
  }
}

// ------------------------------------------------- аттестация и КПК
export const CERT_KIND_LABEL: Record<CertKind, string> = {
  CATEGORY: 'Категория',
  CPK: 'КПК',
}

export const CERT_STATE_LABEL: Record<CertState, string> = {
  valid: 'Действует',
  expiring: 'Скоро истекает',
  expired: 'Срок истёк',
  indefinite: 'Бессрочно',
}

/** «осталось 45 дней» / «истекло 3 дня назад» — для срока действия категории/КПК. */
export function certDaysLabel(daysLeft?: number | null): string {
  if (daysLeft === null || daysLeft === undefined) return 'бессрочно'
  if (daysLeft < 0) {
    const n = Math.abs(daysLeft)
    return `истекло ${n} ${plural(n, 'день', 'дня', 'дней')} назад`
  }
  if (daysLeft === 0) return 'истекает сегодня'
  return `осталось ${daysLeft} ${plural(daysLeft, 'день', 'дня', 'дней')}`
}
