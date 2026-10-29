/** WebSocket-канал мгновенных уведомлений.
 *
 * Подключение — в два шага: `POST /realtime/ticket` с обычным Bearer-заголовком,
 * затем `WS /realtime/ws?ticket=<одноразовый>` (браузер не умеет ставить заголовки
 * в WebSocket, а access-токен в query утекал в логи прокси).
 *
 * Соединение открывается при входе и рвётся при выходе; обрывы переподключаются
 * с экспоненциальной задержкой — каждый раз с новым билетом. Поллинг счётчика
 * непрочитанных в шапке остаётся fallback'ом (сервер без WS, старый браузер,
 * обрыв сети). */
import { API_BASE, getAccessToken, onSessionChange } from '@/api/client'
import { realtimeApi } from '@/api/endpoints'

export interface RealtimeMessage {
  type: 'notification' | 'hello' | 'pong'
  id?: string
  notification_type?: string
  title?: string
  body?: string | null
  entity_type?: string | null
  entity_id?: string | null
}

type Handler = (msg: RealtimeMessage) => void
const handlers = new Set<Handler>()

let ws: WebSocket | null = null
let retry = 0
let stopped = true
let pingTimer: number | null = null

export function onRealtime(fn: Handler): () => void {
  handlers.add(fn)
  return () => {
    handlers.delete(fn)
  }
}

/** Адрес сокета с одноразовым билетом.

 *  Раньше здесь был `?token=<access>`: браузерный WebSocket не умеет ставить
 *  заголовки, поэтому access-токен приходилось передавать в query-строке, а она
 *  попадает в access-логи nginx и DSM Reverse Proxy. Токен живёт 30 минут и даёт
 *  полный доступ к API — утечка через лог была полноценной компрометацией сессии.
 *  Теперь подключение обменивает обычный Bearer-запрос на билет: он одноразовый,
 *  живёт минуту и привязан к цели `realtime:ws`, поэтому в логе бесполезен. */
function wsUrl(ticket: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}${API_BASE}/realtime/ws?ticket=${encodeURIComponent(ticket)}`
}

async function connect() {
  if (stopped) return
  if (!getAccessToken()) return

  let url: string
  try {
    const { ticket } = await realtimeApi.ticket()
    url = wsUrl(ticket)
  } catch {
    // нет сессии, сервер недоступен, refresh не сработал — пробуем позже
    scheduleRetry()
    return
  }
  if (stopped) return // за время обмена билетом пользователь мог выйти

  try {
    ws = new WebSocket(url)
  } catch {
    scheduleRetry()
    return
  }
  ws.onopen = () => {
    retry = 0
    // keep-alive: nginx/DSM рвут тихие соединения через ~60–300 c
    pingTimer = window.setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send('ping')
    }, 30_000)
  }
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(String(e.data)) as RealtimeMessage
      handlers.forEach((h) => h(msg))
    } catch {
      /* игнорируем не-JSON */
    }
  }
  ws.onclose = () => {
    if (pingTimer) window.clearInterval(pingTimer)
    pingTimer = null
    if (!stopped) scheduleRetry()
  }
  ws.onerror = () => ws?.close()
}

function scheduleRetry() {
  retry = Math.min(retry + 1, 6)
  window.setTimeout(() => void connect(), 1000 * 2 ** retry)
}

export function startRealtime() {
  if (!stopped) return
  stopped = false
  void connect()
}

export function stopRealtime() {
  stopped = true
  if (pingTimer) window.clearInterval(pingTimer)
  pingTimer = null
  ws?.close()
  ws = null
}

// вход/выход: поднимаем и рвём канал вместе с сессией
onSessionChange((event) => {
  if (event === 'login') startRealtime()
  else stopRealtime()
})
