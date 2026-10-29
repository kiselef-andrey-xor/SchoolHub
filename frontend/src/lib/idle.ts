/** Таймер неактивности (v1.11).
 *
 * Требование Владельца: сессия не должна жить вечно — после 3 часов без
 * активности пользователя выбрасываем на экран авторизации.
 *
 * Реализация КЛИЕНТСКАЯ: серверные сроки жизни токенов не менялись (access 30 мин,
 * refresh 14 дней с ротацией). При срабатывании таймера выполняется обычный выход
 * (`authStore.logout()`), который отзывает refresh-сессию на сервере, — иначе
 * следующая загрузка страницы молча восстановила бы сессию и вылет не сработал.
 *
 * Метка последней активности хранится в localStorage, поэтому таймер общий для
 * всех вкладок одного браузера: работа в одной вкладке продлевает остальные.
 * Метка пишется не чаще раза в 20 секунд (иначе mousemove засорял бы storage).
 */

const STORAGE_KEY = 'schoolhub.last-activity'
/** Флаг «вышли по неактивности» — его читает экран входа, чтобы объяснить причину. */
export const IDLE_REASON_KEY = 'schoolhub.logout-reason'
const WRITE_THROTTLE_MS = 20_000
const CHECK_INTERVAL_MS = 30_000
const ACTIVITY_EVENTS = ['pointerdown', 'keydown', 'wheel', 'touchstart', 'mousemove'] as const

/** Лимит неактивности в минутах: 180 (3 часа) по умолчанию, можно задать
 * переменной окружения `VITE_IDLE_LIMIT_MIN` при сборке фронтенда. */
export const IDLE_LIMIT_MIN: number = (() => {
  const raw = Number((import.meta.env.VITE_IDLE_LIMIT_MIN as string | undefined) ?? '')
  return Number.isFinite(raw) && raw > 0 ? raw : 180
})()

export const IDLE_LIMIT_MS = IDLE_LIMIT_MIN * 60_000

let lastWrite = 0

function readMark(): number {
  try {
    const raw = Number(localStorage.getItem(STORAGE_KEY))
    return Number.isFinite(raw) && raw > 0 ? raw : 0
  } catch {
    return 0 // приватный режим / заблокированный storage — считаем, что активность была
  }
}

/** Отметить активность пользователя (пишется в storage не чаще раза в 20 секунд). */
export function markActivity(force = false): void {
  const now = Date.now()
  if (!force && now - lastWrite < WRITE_THROTTLE_MS) return
  lastWrite = now
  try {
    localStorage.setItem(STORAGE_KEY, String(now))
  } catch {
    /* приватный режим — таймер просто будет работать от памяти вкладки */
  }
}

/** Сколько миллисекунд прошло с последней активности (0 — метки нет). */
export function idleMs(): number {
  const mark = readMark()
  if (!mark) return 0
  return Math.max(0, Date.now() - mark)
}

/** Причина выхода, установленная таймером неактивности (читается один раз). */
export function takeIdleReason(): 'idle' | null {
  try {
    const value = sessionStorage.getItem(IDLE_REASON_KEY)
    if (value === 'idle') {
      sessionStorage.removeItem(IDLE_REASON_KEY)
      return 'idle'
    }
  } catch {
    /* storage недоступен */
  }
  return null
}

/**
 * Запустить наблюдение за неактивностью. Возвращает функцию остановки.
 * `onExpire` вызывается РОВНО ОДИН раз — дальше таймер останавливается сам.
 */
export function startIdleWatch(onExpire: () => void): () => void {
  let fired = false
  let timer = 0

  const stop = () => {
    ACTIVITY_EVENTS.forEach((ev) => window.removeEventListener(ev, onActivity))
    window.removeEventListener('focus', check)
    document.removeEventListener('visibilitychange', onVisibility)
    if (timer) window.clearInterval(timer)
    timer = 0
  }

  function check() {
    if (fired) return
    if (idleMs() >= IDLE_LIMIT_MS) {
      fired = true
      stop()
      try {
        sessionStorage.setItem(IDLE_REASON_KEY, 'idle')
      } catch {
        /* причина не покажется — не критично */
      }
      onExpire()
    }
  }

  function onActivity() {
    if (!fired) markActivity()
  }

  function onVisibility() {
    // вкладку вернули на передний план — сразу проверяем, не истекло ли время
    if (document.visibilityState === 'visible') check()
  }

  // старт отсчёта: метку ставим только если её нет (первый вход / очистили storage).
  // Существующую метку НЕ перезаписываем — иначе возврат в браузер спустя сутки
  // обнулял бы таймер и сессия молча восстанавливалась.
  if (!readMark()) markActivity(true)
  ACTIVITY_EVENTS.forEach((ev) => window.addEventListener(ev, onActivity, { passive: true }))
  window.addEventListener('focus', check)
  document.addEventListener('visibilitychange', onVisibility)
  timer = window.setInterval(check, CHECK_INTERVAL_MS)
  // проверяем сразу: пользователь мог открыть страницу после долгого перерыва
  check()

  return stop
}

/** Человекочитаемый лимит для подписей в интерфейсе: «3 часа». */
export function idleLimitLabel(): string {
  const hours = Math.floor(IDLE_LIMIT_MIN / 60)
  const mins = IDLE_LIMIT_MIN % 60
  if (hours && mins) return `${hours} ч ${mins} мин`
  if (hours) return `${hours} ${hours === 1 ? 'час' : hours < 5 ? 'часа' : 'часов'}`
  return `${mins} мин`
}
