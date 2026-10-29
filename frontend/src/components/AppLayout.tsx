/** Каркас приложения: боковая навигация (по правам роли), топбар, уведомления, меню профиля. */
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { authApi, helpApi, helpdeskApi, notificationsApi, systemApi } from '@/api/endpoints'
import { clearTokens } from '@/api/client'
import { qk } from '@/api/query'
import { Badge } from '@/components/ui'
import { Modal } from '@/components/Modal'
import { UserAvatar } from '@/components/UserAvatar'
import { SearchModal } from '@/components/SearchModal'
import { useToast } from '@/components/Toast'
import { CAP, ROLE_LABEL, can, type Capability } from '@/lib/permissions'
import { relativeTime } from '@/lib/format'
import { startIdleWatch } from '@/lib/idle'
import { onRealtime, startRealtime, stopRealtime } from '@/lib/realtime'
import { currentTheme, toggleTheme } from '@/lib/theme'
import { authStore, useAuthStore } from '@/store/auth'

interface NavItem {
  to?: string
  /** внешняя ссылка (открывается в новой вкладке) — например, школьное расписание */
  href?: string
  label: string
  icon: string
  cap?: Capability
  /** пункт виден, если есть ЛЮБАЯ из перечисленных возможностей */
  anyCaps?: Capability[]
  end?: boolean
  group: string
}

const NAV: NavItem[] = [
  { to: '/', label: 'Дашборд', icon: '📊', group: 'Обзор', end: true },
  {
    href: 'https://raspisanie.nikasoft.ru/23011494.html',
    label: 'Расписание',
    icon: '🕒',
    group: 'Обзор',
  },
  { to: '/orders', label: 'Приказы', icon: '📜', group: 'Документы', cap: CAP.ordersRead },
  // ВАЖНО: «Задачи» видны всем ролям — у учителя tasks:read-own, у администрации/владельца tasks:read-all
  { to: '/tasks', label: 'Задачи', icon: '✅', group: 'Работа', anyCaps: [CAP.tasksReadOwn, CAP.tasksReadAll] },
  { to: '/plans', label: 'План работы', icon: '🗓', group: 'Работа', cap: CAP.plansRead },
  { to: '/vshk', label: 'ВШК', icon: '🔎', group: 'Работа' },
  { to: '/movement', label: 'Движение', icon: '🎓', group: 'Документы', cap: CAP.movementRead },
  { to: '/equipment', label: 'Инвентарь', icon: '🖥', group: 'Документы', cap: CAP.equipmentRead },
  { to: '/certifications', label: 'Аттестация', icon: '🏅', group: 'Документы', cap: CAP.certificationsRead },
  { to: '/notes', label: 'Служебные записки', icon: '📝', group: 'Документы', cap: CAP.notesRead },
  { to: '/substitutions', label: 'Замещения', icon: '🔁', group: 'Работа', cap: CAP.substitutionsRead },
  // Техподдержка видна всем (учитель подаёт заявку), но управлять очередью может
  // только системный администратор — грант выдаёт Владелец (capabilities: helpdesk:*)
  { to: '/helpdesk', label: 'Техподдержка', icon: '🛠', group: 'Работа', cap: CAP.helpdeskRead },
  { to: '/help', label: 'Справка', icon: '❔', group: 'Обзор' },
  { to: '/users', label: 'Пользователи', icon: '👥', group: 'Администрирование', cap: CAP.usersRead },
  { to: '/audit', label: 'Журнал событий', icon: '🧾', group: 'Администрирование', cap: CAP.auditRead },
]

const TITLES: Record<string, string> = {
  '/': 'Дашборд',
  '/orders': 'Приказы',
  '/tasks': 'Задачи',
  '/plans': 'План работы',
  '/vshk': 'Внутришкольный контроль',
  '/notifications': 'Уведомления',
  '/movement': 'Движение обучающихся',
  '/equipment': 'Оборудование и инвентарь',
  '/certifications': 'Аттестация и КПК',
  '/users': 'Пользователи',
  '/audit': 'Журнал событий',
  '/profile': 'Мой профиль',
  '/notes': 'Служебные записки',
  '/substitutions': 'Замещения',
  '/help': 'Справка и инструкции',
  '/helpdesk': 'Техническая поддержка',
}

export function AppLayout() {
  const user = useAuthStore((s) => s.user)
  const navigate = useNavigate()
  const location = useLocation()
  const toast = useToast()
  const qc = useQueryClient()
  const [menuOpen, setMenuOpen] = useState(false)
  const [bellOpen, setBellOpen] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  const [darkTheme, setDarkTheme] = useState(() => currentTheme() === 'dark')
  const menuRef = useRef<HTMLDivElement>(null)
  const bellRef = useRef<HTMLDivElement>(null)

  const authed = user !== null

  // счётчик непрочитанных: лёгкий поллинг, чтобы бейдж «оживал» без перезагрузки
  const { data: unread = 0 } = useQuery({
    queryKey: qk('/notifications/unread-count'),
    queryFn: () => notificationsApi.unreadCount().then((r) => r.unread),
    enabled: authed,
    refetchInterval: 45_000,
  })

  // бейдж «обязательно к прочтению»: сколько инструкций ещё не подтверждены
  const { data: helpPending = 0 } = useQuery({
    queryKey: qk('/help/mine/pending'),
    queryFn: () => helpApi.pendingCount().then((r) => r.count),
    enabled: authed,
    refetchInterval: 60_000,
  })

  // счётчик новых заявок в очереди — бейдж на «Техподдержке» у системных администраторов.
  // Обычному сотрудному очередь не видна, поэтому запрос для него не выполняем вовсе.
  const seesQueue = can(user?.capabilities, CAP.helpdeskManage) || user?.role === 'ADMIN'
  const { data: queueNew = 0 } = useQuery({
    queryKey: qk('/helpdesk/stats'),
    queryFn: () => helpdeskApi.stats().then((r) => r.new),
    enabled: seesQueue,
    refetchInterval: 60_000,
  })

  // «Локдаун»: предупреждающая плашка, пока режим включён
  const { data: lockdown } = useQuery({
    queryKey: qk('/system/lockdown'),
    queryFn: systemApi.lockdownStatus,
    enabled: authed,
    refetchInterval: 60_000,
  })

  // последние уведомления — подгружаем только когда колокольчик открыт
  const { data: latest = [] } = useQuery({
    queryKey: qk('/notifications/latest'),
    queryFn: () => notificationsApi.list({ per_page: 6 }).then((r) => r.items),
    enabled: authed && bellOpen,
    refetchInterval: bellOpen ? 30_000 : false,
  })

  // WebSocket-уведомления: тост и обновление счётчика без перезагрузки страницы
  useEffect(() => {
    if (!authed) return
    startRealtime()
    const off = onRealtime((msg) => {
      if (msg.type !== 'notification') return
      toast.show(msg.title ?? 'Новое уведомление', 'info')
      void qc.invalidateQueries({ queryKey: ['/notifications'] })
    })
    return () => {
      off()
      stopRealtime()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authed])

  // v1.11: вылет на экран авторизации после 3 часов бездействия (см. lib/idle.ts).
  // Таймер общий для всех вкладок (метка в localStorage), поэтому работа в одной
  // вкладке продлевает остальные; срабатывание выполняет обычный выход.
  useEffect(() => {
    if (!authed) return
    return startIdleWatch(() => {
      void authStore.logout()
    })
  }, [authed])

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false)
      if (bellRef.current && !bellRef.current.contains(e.target as Node)) setBellOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [])

  useEffect(() => setSidebarOpen(false), [location.pathname])

  // Ctrl+K (Cmd+K) — глобальный поиск
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k' && authed) {
        e.preventDefault()
        setSearchOpen((v) => !v)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [authed])

  const items = useMemo(
    () =>
      user
        ? NAV.filter((item) => {
            // пункт виден, если достаточно ЛЮБОЙ из перечисленных возможностей:
            // «Задачи» есть у всех ролей — у учителя tasks:read-own, у администрации tasks:read-all
            if (item.anyCaps) return item.anyCaps.some((c) => can(user.capabilities, c))
            return !item.cap || can(user.capabilities, item.cap)
          })
        : [],
    [user],
  )

  const title = useMemo(() => {
    const path = location.pathname
    if (path.startsWith('/orders/')) return 'Приказ'
    if (path.startsWith('/tasks/')) return 'Задача'
    if (path.startsWith('/plans/')) return path.endsWith('/edit') ? 'Редактор плана' : 'План работы на неделю'
    return TITLES[path] ?? 'SchoolHub'
  }, [location.pathname])

  if (!user) return null

  const groups = Array.from(new Set(items.map((i) => i.group)))

  return (
    <div className="app-shell">
      <aside className={sidebarOpen ? 'sidebar sidebar--open' : 'sidebar'}>
        <div className="sidebar__brand">
          <div className="sidebar__logo">SH</div>
          <div>
            <div className="sidebar__title">SchoolHub</div>
            <div className="sidebar__subtitle">цифровая среда школы</div>
          </div>
        </div>

        <nav className="sidebar__nav">
          {groups.map((group) => (
            <div key={group}>
              <div className="sidebar__group">{group}</div>
              {items
                .filter((i) => i.group === group)
                .map((item) =>
                  item.href ? (
                    <a
                      key={item.href}
                      className="nav-link"
                      href={item.href}
                      target="_blank"
                      rel="noopener noreferrer"
                      title="Откроется в новой вкладке"
                    >
                      <span className="nav-link__icon" aria-hidden>{item.icon}</span>
                      <span>{item.label}</span>
                      <span className="nav-link__badge" style={{ background: 'transparent', color: 'var(--ink-400)' }}>↗</span>
                    </a>
                  ) : (
                  <NavLink
                    key={item.to}
                    to={item.to!}
                    end={item.end}
                    className={({ isActive }) => (isActive ? 'nav-link nav-link--active' : 'nav-link')}
                  >
                    <span className="nav-link__icon" aria-hidden>{item.icon}</span>
                    <span>{item.label}</span>
                    {item.to === '/help' && helpPending > 0 ? (
                      <span className="nav-link__badge" title="Обязательные к прочтению инструкции без вашей подписи">
                        {helpPending > 99 ? '99+' : helpPending}
                      </span>
                    ) : null}
                    {item.to === '/helpdesk' && queueNew > 0 ? (
                      <span className="nav-link__badge nav-link__badge--warn" title="Новые заявки в очереди техподдержки">
                        {queueNew > 99 ? '99+' : queueNew}
                      </span>
                    ) : null}
                  </NavLink>
                  ),
                )}
            </div>
          ))}
        </nav>

      </aside>

      <div className="main">
        <header className="topbar">
          <button className="btn btn--ghost btn--icon burger" onClick={() => setSidebarOpen((v) => !v)} aria-label="Меню">
            ☰
          </button>
          <div className="topbar__title">{title}</div>
          <div className="topbar__spacer" />

          <button
            className="btn btn--ghost btn--icon no-print"
            onClick={() => setDarkTheme(toggleTheme() === 'dark')}
            title={darkTheme ? 'Включить светлую тему' : 'Включить тёмную тему'}
            aria-label="Переключить тему"
          >
            <span aria-hidden>{darkTheme ? '☀️' : '🌙'}</span>
          </button>

          <button
            className="btn btn--ghost no-print"
            onClick={() => setSearchOpen(true)}
            title="Глобальный поиск: приказы, задачи, письма, сотрудники"
          >
            🔍 <span className="nowrap hide-sm">Поиск</span>
            <span className="tiny muted nowrap hide-sm">Ctrl+K</span>
          </button>

          <div className="menu" ref={bellRef}>
            <button className="btn btn--ghost row" onClick={() => setBellOpen((v) => !v)} aria-label="Уведомления">
              <span aria-hidden>🔔</span>
              {unread > 0 && <Badge tone="danger">{unread}</Badge>}
            </button>
            {bellOpen && (
              <div className="menu__panel" style={{ minWidth: 330 }}>
                <div className="row row--between" style={{ padding: '4px 8px 8px' }}>
                  <strong className="small">Последние уведомления</strong>
                  <button
                    className="btn btn--sm btn--ghost"
                    onClick={async () => {
                      try {
                        await notificationsApi.markAllRead()
                        void qc.invalidateQueries({ queryKey: ['notifications'] })
                        void qc.invalidateQueries({ queryKey: qk('/notifications/unread-count') })
                        toast.success('Все уведомления отмечены прочитанными')
                      } catch (e) {
                        toast.error(e)
                      }
                    }}
                  >
                    Прочитать все
                  </button>
                </div>
                {latest.length === 0 ? (
                  <div className="small muted" style={{ padding: '10px 8px' }}>Уведомлений пока нет</div>
                ) : (
                  latest.slice(0, 6).map((n) => (
                    <button
                      key={n.id}
                      className="menu__item"
                      style={{ flexDirection: 'column', alignItems: 'flex-start', gap: 2 }}
                      onClick={() => {
                        setBellOpen(false)
                        if (n.entity_type === 'task') navigate(`/tasks/${n.entity_id}`)
                        else if (n.entity_type === 'order') navigate(`/orders/${n.entity_id}`)
                        else if (n.entity_type === 'certification') navigate('/certifications')
                        // заявка техподдержки открывается сразу карточкой (?t=…)
                        else if (n.entity_type === 'helpdesk_ticket' && n.entity_id) navigate(`/helpdesk?t=${n.entity_id}`)
                        else navigate('/notifications')
                      }}
                    >
                      <span style={{ fontWeight: n.is_read ? 500 : 700 }}>{n.title}</span>
                      <span className="tiny muted">{relativeTime(n.created_at)}</span>
                    </button>
                  ))
                )}
                <div className="menu__sep" />
                <button className="menu__item" onClick={() => { setBellOpen(false); navigate('/notifications') }}>
                  Все уведомления →
                </button>
              </div>
            )}
          </div>

          <div className="menu" ref={menuRef}>
            <button className="btn btn--ghost row" onClick={() => setMenuOpen((v) => !v)}>
              <UserAvatar user={user} size="sm" />
              <span className="nowrap">{user.full_name.split(' ')[0]}</span>
              <span aria-hidden>▾</span>
            </button>
            {menuOpen && (
              <div className="menu__panel">
                <div style={{ padding: '8px 10px' }}>
                  <div className="strong">{user.full_name}</div>
                  <div className="tiny muted">{user.email ?? user.login ?? ''}</div>
                  <div style={{ marginTop: 6 }}>
                    <Badge tone="brand">{ROLE_LABEL[user.role]}</Badge>
                  </div>
                </div>
                <div className="menu__sep" />
                <button className="menu__item" onClick={() => { setMenuOpen(false); navigate('/profile') }}>
                  🪪 Мой профиль
                </button>
                <button
                  className="menu__item"
                  onClick={async () => {
                    setMenuOpen(false)
                    await authStore.logout()
                    navigate('/login', { replace: true })
                  }}
                >
                  🚪 Выйти
                </button>
              </div>
            )}
          </div>
        </header>

        {lockdown?.enabled && (
          <div className="lockdown-banner" role="status">
            🔒 Режим «Локдаун»: персональные данные скрыты
            {lockdown.masked_for_you
              ? ' — вам видны только обезличенные метки.'
              : ' от других ролей; вы видите их как Владелец.'}{' '}
            {user?.role === 'OWNER' && <Link to="/profile">Выключить в профиле →</Link>}
          </div>
        )}

        <main className="content">
          <Outlet />
        </main>
      </div>

      <ForcePasswordChange />
      <SearchModal open={searchOpen} onClose={() => setSearchOpen(false)} />
    </div>
  )
}


/** Владелец потребовал смену пароля: модальное окно без возможности закрыть,
 *  пока пароль не сменён (или пользователь не вышел). */
function ForcePasswordChange() {
  const user = useAuthStore((s) => s.user)!
  const toast = useToast()
  const navigate = useNavigate()
  const [pwd, setPwd] = useState({ current: '', next: '', confirm: '' })
  const [busy, setBusy] = useState(false)

  if (!user.password_must_change || user.auth_source === 'ldap') return null

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (pwd.next !== pwd.confirm) {
      toast.error('Новый пароль и повтор не совпадают')
      return
    }
    setBusy(true)
    try {
      await authApi.changePassword(pwd.current, pwd.next)
      toast.success('Пароль изменён. Войдите заново с новым паролем.')
      clearTokens()
      authStore.set({ status: 'guest', user: null })
      navigate('/login')
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      onClose={() => undefined}
      title="Требуется сменить пароль"
      footer={
        <>
          <button className="btn" disabled={busy} onClick={() => void authStore.logout()}>Выйти</button>
          <button className="btn btn--primary" form="force-pwd-form" type="submit" disabled={busy}>
            {busy ? 'Меняем…' : 'Сменить пароль'}
          </button>
        </>
      }
    >
      <form id="force-pwd-form" onSubmit={(e) => void submit(e)} className="stack" style={{ gap: 10 }}>
        <div className="alert alert--warn small">
          Администратор потребовал сменить пароль для учётной записи <strong>{user.email ?? user.login}</strong>.
          До смены пароля другие разделы недоступны.
        </div>
        <label className="field">
          <span className="field__label">Текущий пароль</span>
          <input className="input" type="password" autoComplete="current-password" required value={pwd.current} onChange={(e) => setPwd({ ...pwd, current: e.target.value })} />
        </label>
        <label className="field">
          <span className="field__label">Новый пароль</span>
          <input className="input" type="password" autoComplete="new-password" minLength={8} required value={pwd.next} onChange={(e) => setPwd({ ...pwd, next: e.target.value })} />
          <span className="field__hint">Минимум 8 символов</span>
        </label>
        <label className="field">
          <span className="field__label">Повторите новый пароль</span>
          <input className="input" type="password" autoComplete="new-password" minLength={8} required value={pwd.confirm} onChange={(e) => setPwd({ ...pwd, confirm: e.target.value })} />
        </label>
      </form>
    </Modal>
  )
}
