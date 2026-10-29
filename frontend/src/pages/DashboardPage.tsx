import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { dashboardApi, usersApi } from '@/api/endpoints'
import { qk } from '@/api/query'
import {
  AckBadge,
  Badge,
  EmptyState,
  ErrorState,
  Kpi,
  Section,
  Spinner,
  TaskStatusBadge,
} from '@/components/ui'
import { MonthSummaryModal } from '@/components/MonthSummaryModal'
import { CAP, ROLE_LABEL, can } from '@/lib/permissions'
import { PLAN_STATUS_LABEL, deadlineLabel, formatDate, formatWeekRange, greeting, plural, recurrenceLabel, relativeTime } from '@/lib/format'
import { useAuthStore } from '@/store/auth'

/** Подписи и цвета для пунктов плана в виджете «Мои мероприятия». */
const PLAN_EVENT_LABEL: Record<string, string> = {
  duty: 'Дежурство',
  event: 'Мероприятие',
  week: 'На неделе',
  month: 'На месяц',
}
const PLAN_EVENT_TONE: Record<string, 'brand' | 'ok' | 'warn' | 'muted' | 'neutral'> = {
  duty: 'brand',
  event: 'ok',
  week: 'warn',
  month: 'muted',
}

export function DashboardPage() {
  const user = useAuthStore((s) => s.user)!
  const canGlobal = can(user.capabilities, CAP.dashboardGlobal)
  const [scope, setScope] = useState<'school' | 'mine'>('school')
  const [monthOpen, setMonthOpen] = useState(false)

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: qk('/dashboard', { scope }),
    queryFn: () => dashboardApi.summary(canGlobal ? scope : undefined),
    refetchInterval: 60_000,
  })

  // --- визуальный конструктор: порядок виджетов хранится в профиле (list-views 'dashboard')
  const WIDGET_ORDER_DEFAULT = ['kpi', 'main', 'plan', 'myplan', 'vshk', 'recent']
  const [savedOrder, setSavedOrder] = useState<string[] | null>(null)
  const [editLayout, setEditLayout] = useState(false)
  const dragId = useRef<string | null>(null)
  const { data: savedViews } = useQuery({ queryKey: qk('/users/me/list-views'), queryFn: usersApi.listViews })
  useEffect(() => {
    const raw = savedViews?.['dashboard']?.order
    if (raw) setSavedOrder(raw.split(',').filter((k) => WIDGET_ORDER_DEFAULT.includes(k)))
  }, [savedViews])

  if (isLoading) return <Spinner large label="Собираем сводку…" />
  if (error) return <ErrorState error={error} onRetry={() => void refetch()} />
  if (!data) return null

  const { summary, pending_acks, my_tasks, recent_orders, recent_tasks, current_plan, my_plan_events, my_vshk, overdue_acks, birthdays } = data
  // фактический режим вида сообщает сервер: global (по школе) или personal
  const isStaff = summary.scope === 'global'

  const ordered = savedOrder
    ? [...savedOrder, ...WIDGET_ORDER_DEFAULT.filter((k) => !savedOrder.includes(k))]
    : WIDGET_ORDER_DEFAULT

  function persistOrder(next: string[]) {
    setSavedOrder(next)
    void usersApi.saveListView('dashboard', { order: next.join(',') })
  }

  function onDropWidget(target: string) {
    const from = dragId.current
    dragId.current = null
    if (!from || from === target) return
    const cur = ordered.filter((k) => k !== from)
    const idx = cur.indexOf(target)
    cur.splice(idx < 0 ? cur.length : idx, 0, from)
    persistOrder(cur)
  }

  const WIDGET_TITLES: Record<string, string> = {
    kpi: 'показатели', main: 'ознакомление и задачи', plan: 'план недели',
    myplan: 'мои мероприятия', vshk: 'мой ВШК', recent: 'последние приказы и задачи',
  }

  // обычная функция (не компонент): не создаёт новый тип элемента и не ремount'ит блоки
  const widget = (id: string, children: ReactNode) => (
    <div
      draggable={editLayout}
      onDragStart={() => (dragId.current = id)}
      onDragOver={(e) => { if (editLayout) e.preventDefault() }}
      onDrop={(e) => { e.preventDefault(); if (editLayout) onDropWidget(id) }}
      style={editLayout ? { outline: '2px dashed var(--border-strong)', borderRadius: 12, padding: 6, cursor: 'grab' } : undefined}
    >
      {editLayout && (
        <div className="tiny muted" style={{ marginBottom: 4 }}>⠿ {WIDGET_TITLES[id] ?? id} — перетащите, чтобы поменять порядок</div>
      )}
      {children}
    </div>
  )

  const blocks: Record<string, ReactNode> = {
    kpi: (
      <>
          <div className="grid grid--kpi">
            <Kpi
              label={isStaff ? 'Приказов опубликовано' : 'Приказов доступно'}
              value={isStaff ? summary.orders.published : summary.orders.total}
              note={isStaff ? `всего в системе ${summary.orders.total}` : 'только опубликованные'}
              tone="brand"
            />
            <Kpi
              label={isStaff ? 'Ждут ознакомления' : 'Нужно ознакомиться'}
              value={summary.orders.pending_ack}
              note={isStaff ? 'подписей по всем приказам' : plural(summary.orders.pending_ack, 'приказ', 'приказа', 'приказов')}
              tone={summary.orders.pending_ack > 0 ? 'warn' : 'ok'}
            />
            <Kpi
              label={isStaff ? 'Задач в работе' : 'Мои задачи в работе'}
              value={summary.tasks.in_progress + summary.tasks.new}
              note={`новых: ${summary.tasks.new}, в работе: ${summary.tasks.in_progress}`}
            />
            <Kpi
              label="Просрочено"
              value={summary.tasks.overdue}
              note={summary.tasks.overdue ? 'требуется реакция' : 'просрочек нет'}
              tone={summary.tasks.overdue > 0 ? 'danger' : 'ok'}
            />
            {isStaff && summary.users && (
              <Kpi
                label="Сотрудников"
                value={summary.users.total}
                note={`учителей: ${summary.users.teachers} · администрация: ${summary.users.admins} · активных: ${summary.users.active}`}
              />
            )}
          </div>
      </>
    ),
    main: (
      <>
          <div className="grid grid--2">
            <Section
              title={isStaff ? 'Ознакомление идёт' : 'Требуется ваше ознакомление'}
              actions={<Link className="btn btn--sm" to="/orders?mine_pending=true">Все приказы →</Link>}
              flush
            >
              {pending_acks.length === 0 ? (
                <EmptyState
                  icon="🎉"
                  title={isStaff ? 'Все подписи собраны' : 'Всё прочитано'}
                  hint={isStaff ? 'Нет приказов, ожидающих ознакомления' : 'Новых приказов на ознакомление нет'}
                />
              ) : (
                pending_acks.map((o) => (
                  <Link key={o.id} to={`/orders/${o.id}`} className="list-item" style={{ color: 'inherit' }}>
                    <div className="list-item__main">
                      <div className="list-item__title">
                        №{o.number} · {o.title}
                      </div>
                      <div className="list-item__meta">
                        <span>от {formatDate(o.issued_on)}</span>
                        {o.ack_deadline && <span>ознакомиться до {formatDate(o.ack_deadline)}</span>}
                        {isStaff && (
                          <span>
                            подписей {o.ack_done}/{o.ack_total}
                          </span>
                        )}
                      </div>
                    </div>
                    <div className="list-item__actions">
                      <AckBadge status={o.my_ack?.status} />
                    </div>
                  </Link>
                ))
              )}
            </Section>

            {isStaff && (overdue_acks?.length ?? 0) > 0 && (
              <Section title="Просроченные ознакомления">
                <div className="alert alert--error small">
                  Просрочено ознакомлений: {overdue_acks!.length}. Напоминания сотрудникам уходят
                  автоматически, администрации каждое утро приходит сводка.
                </div>
                <table className="table">
                  <thead>
                    <tr>
                      <th style={{ width: 120 }}>Приказ</th>
                      <th>Название</th>
                      <th style={{ width: 200 }}>Сотрудник</th>
                      <th style={{ width: 130 }}>Срок</th>
                      <th style={{ width: 110 }}>Просрочка</th>
                    </tr>
                  </thead>
                  <tbody>
                    {overdue_acks!.slice(0, 15).map((a) => (
                      <tr key={`${a.order_id}-${a.user_id}`}>
                        <td className="mono small">
                          <Link to={`/orders/${a.order_id}`}>№ {a.number}</Link>
                        </td>
                        <td className="small">{a.title}</td>
                        <td className="small">{a.user_name}</td>
                        <td className="small nowrap">{formatDate(a.deadline)}</td>
                        <td><Badge tone="danger">{a.days_overdue} дн.</Badge></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Section>
            )}

            {isStaff && (birthdays?.length ?? 0) > 0 && (
              <Section title="🎂 Дни рождения">
                <div className="stack" style={{ gap: 6 }}>
                  {birthdays!.map((b) => (
                    <div key={b.user_id} className="row small" style={{ gap: 10 }}>
                      <span className="grow">{b.full_name}</span>
                      <span className="muted">{formatDate(b.birth_date)}</span>
                      {b.days_until === 0 ? (
                        <Badge tone="ok">сегодня! 🎉</Badge>
                      ) : (
                        <Badge tone="brand">через {b.days_until} дн. · {b.turns_age} лет</Badge>
                      )}
                    </div>
                  ))}
                </div>
                <div className="tiny muted" style={{ marginTop: 8 }}>
                  Дата рождения — необязательное поле карточки сотрудника (Пользователи → ✎).
                </div>
              </Section>
            )}

            <Section
              title={isStaff ? 'Просроченные и ближайшие задачи' : 'Мои ближайшие задачи'}
              actions={<Link className="btn btn--sm" to="/tasks">Все задачи →</Link>}
              flush
            >
              {my_tasks.length === 0 ? (
                <EmptyState icon="🌤" title="Задач нет" hint={isStaff ? 'Незавершённых задач нет — всё выполнено' : 'Вам пока ничего не назначено'} />
              ) : (
                my_tasks.map((t) => (
                  <Link key={t.id} to={`/tasks/${t.id}`} className="list-item" style={{ color: 'inherit' }}>
                    <div className="list-item__main">
                      <div className="list-item__title">{t.title}</div>
                      <div className="list-item__meta">
                        <span>
                          {t.recurrence && t.recurrence !== 'NONE'
                            ? `🔁 ${recurrenceLabel(t)}`
                            : deadlineLabel(t.deadline, t.status === 'DONE')}
                        </span>
                        {isStaff && t.assignees.length > 0 && (
                          <span>· {t.assignees.map((a) => a.user.full_name).join(', ')}{t.assignees.length > 1 ? ` (${t.marks_done}/${t.assignees.length} ✓)` : ''}</span>
                        )}
                      </div>
                    </div>
                    <div className="list-item__actions">
                      <TaskStatusBadge status={t.effective_status} />
                    </div>
                  </Link>
                ))
              )}
            </Section>
          </div>
      </>
    ),
    plan: (
      <>
          {current_plan && (
            <Section
              title="План работы на неделю"
              actions={<Link className="btn btn--sm" to="/plans">Все планы →</Link>}
              flush
            >
              <Link to={`/plans/${current_plan.id}`} className="list-item" style={{ color: 'inherit' }}>
                <div style={{ fontSize: 22 }}>🗓</div>
                <div className="list-item__main">
                  <div className="list-item__title">
                    {formatWeekRange(current_plan.week_start, current_plan.week_end)}
                    {current_plan.content.duty.klass && ` · дежурный класс ${current_plan.content.duty.klass}`}
                  </div>
                  <div className="list-item__meta">
                    <span>мероприятий: {current_plan.events_total}</span>
                    <span>ответственных: {current_plan.responsibles_total}</span>
                    {current_plan.my_items > 0 && (
                      <span className="strong" style={{ color: 'var(--brand-700)' }}>
                        вы отвечаете за {current_plan.my_items} {plural(current_plan.my_items, 'пункт', 'пункта', 'пунктов')}
                      </span>
                    )}
                  </div>
                </div>
                <div className="list-item__actions">
                  <Badge tone={current_plan.status === 'PUBLISHED' ? 'ok' : 'warn'} dot>
                    {PLAN_STATUS_LABEL[current_plan.status]}
                  </Badge>
                </div>
              </Link>
            </Section>
          )}
      </>
    ),
    myplan: (
      <>
          {current_plan && (
            <Section
              title="Мои мероприятия на этой неделе"
              actions={<Link className="btn btn--sm" to={`/plans/${current_plan.id}`}>Открыть план →</Link>}
              flush
            >
              {(my_plan_events?.length ?? 0) === 0 ? (
                <EmptyState
                  icon="🗓"
                  title="Вы не указаны ответственным в плане этой недели"
                  hint="Здесь появятся дежурства, мероприятия и пункты плана, за которые вы отвечаете"
                />
              ) : (
                my_plan_events!.map((ev, i) => (
                  <Link key={`${ev.kind}-${i}`} to={`/plans/${current_plan.id}`} className="list-item" style={{ color: 'inherit' }}>
                    <div className="list-item__main">
                      <div className="list-item__title">{ev.title}</div>
                      <div className="list-item__meta">
                        {ev.date && (
                          <span>
                            {formatDate(ev.date)}
                            {ev.weekday ? `, ${ev.weekday}` : ''}
                          </span>
                        )}
                        {ev.time && <span>🕐 {ev.time}</span>}
                        {ev.place && <span>📍 {ev.place}</span>}
                      </div>
                    </div>
                    <div className="list-item__actions">
                      <Badge tone={PLAN_EVENT_TONE[ev.kind] ?? 'neutral'}>{PLAN_EVENT_LABEL[ev.kind] ?? ev.kind}</Badge>
                    </div>
                  </Link>
                ))
              )}
            </Section>
          )}
      </>
    ),
    vshk: (
      <>
          {(my_vshk?.length ?? 0) > 0 && (
            <Section
              title="Мой ВШК в этом месяце"
              actions={<Link className="btn btn--sm" to="/vshk">Открыть ВШК →</Link>}
              flush
            >
              {my_vshk!.map((it) => (
                <Link key={it.id} to="/vshk" className="list-item" style={{ color: 'inherit' }}>
                  <div className="list-item__main">
                    <div className="list-item__title">{it.title}</div>
                    <div className="list-item__meta">
                      {it.section && <span>{it.section}</span>}
                      {it.control_type && <span>вид: {it.control_type}</span>}
                      {it.summary && <span>итог: {it.summary}</span>}
                    </div>
                  </div>
                  <div className="list-item__actions">
                    <span className={`vshk-status vshk-status--${it.status}`}>
                      {it.status === 'PLANNED' ? 'запланировано' : it.status === 'IN_WORK' ? 'проводится' : 'выполнено'}
                    </span>
                  </div>
                </Link>
              ))}
            </Section>
          )}
      </>
    ),
    recent: (
      <>
          <div className="grid grid--2">
            <Section title="Последние приказы" actions={<Link className="btn btn--sm" to="/orders">Открыть →</Link>} flush>
              {recent_orders.length === 0 ? (
                <EmptyState icon="📜" title="Приказов пока нет" />
              ) : (
                recent_orders.map((o) => (
                  <Link key={o.id} to={`/orders/${o.id}`} className="list-item" style={{ color: 'inherit' }}>
                    <div className="list-item__main">
                      <div className="list-item__title">
                        №{o.number} · {o.title}
                      </div>
                      <div className="list-item__meta">
                        <span>от {formatDate(o.issued_on)}</span>
                        <span>{o.ack_done}/{o.ack_total} подписей</span>
                        {o.published_at && <span>опубликован {relativeTime(o.published_at)}</span>}
                      </div>
                    </div>
                    <div className="list-item__actions">
                      <Badge tone={o.status === 'PUBLISHED' ? 'ok' : o.status === 'DRAFT' ? 'warn' : 'muted'}>
                        {o.status === 'PUBLISHED' ? 'Опубликован' : o.status === 'DRAFT' ? 'Черновик' : 'Архив'}
                      </Badge>
                    </div>
                  </Link>
                ))
              )}
            </Section>

            <Section title="Последние задачи" actions={<Link className="btn btn--sm" to="/tasks">Открыть →</Link>} flush>
              {recent_tasks.length === 0 ? (
                <EmptyState icon="✅" title="Задач пока нет" />
              ) : (
                recent_tasks.map((t) => (
                  <Link key={t.id} to={`/tasks/${t.id}`} className="list-item" style={{ color: 'inherit' }}>
                    <div className="list-item__main">
                      <div className="list-item__title">{t.title}</div>
                      <div className="list-item__meta">
                        <span>создана {relativeTime(t.created_at)}</span>
                        {t.assignees.length > 0 && (
                          <span>· {t.assignees.map((a) => a.user.full_name).join(', ')}</span>
                        )}
                      </div>
                    </div>
                    <div className="list-item__actions">
                      <TaskStatusBadge status={t.effective_status} />
                    </div>
                  </Link>
                ))
              )}
            </Section>
          </div>
      </>
    ),
  }

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div className="page-head__text">
          <h1>{greeting(user.full_name)}</h1>
          <div className="page-head__hint">
            {ROLE_LABEL[user.role]}
            {(user.positions || []).length ? ` · ${(user.positions || []).map((p) => p.title).join(', ')}` : ''} · {isStaff ? 'сводка по всей школе' : 'ваша личная сводка'}
          </div>
        </div>
        <div className="page-head__actions">
          <button
            className={editLayout ? 'chip chip--active' : 'chip'}
            title="Перетаскивайте блоки, как плитки: порядок сохранится в вашем профиле"
            onClick={() => setEditLayout((v) => !v)}
          >
            ⠿ Раскладка
          </button>
          {editLayout && savedOrder && (
            <button
              className="btn btn--sm"
              onClick={() => { setSavedOrder(null); void usersApi.deleteListView('dashboard') }}
            >
              ✖ Сбросить порядок
            </button>
          )}
          {canGlobal && (
            <div className="row" style={{ gap: 6 }}>
              <button
                className={scope === 'school' ? 'chip chip--active' : 'chip'}
                onClick={() => setScope('school')}
                title="Сводка по всей школе"
              >
                По школе
              </button>
              <button
                className={scope === 'mine' ? 'chip chip--active' : 'chip'}
                onClick={() => setScope('mine')}
                title="Только мои задачи и ознакомления"
              >
                Только моё
              </button>
            </div>
          )}
          {canGlobal && (
            <button className="btn" onClick={() => setMonthOpen(true)} title="Отчёт за календарный месяц: приказы, задачи, журнал, движение">
              📊 Итоги месяца
            </button>
          )}
          <Link className="btn" to="/orders">📜 Приказы</Link>
          <Link className="btn" to="/tasks">✅ Задачи</Link>
          <Link className="btn" to="/plans">🗓 План работы</Link>
        </div>
      </div>
    {ordered.map((k) => (
      <div key={k}>{widget(k, blocks[k])}</div>
    ))}

            <MonthSummaryModal open={monthOpen} onClose={() => setMonthOpen(false)} />
    </div>
  )
}
