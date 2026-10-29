/** Task Manager: вкладки-статусы (включая вычисляемую «Просрочена»), фильтры, список/канбан. */
import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { tasksApi, usersApi, type TaskFilters } from '@/api/endpoints'
import { qk } from '@/api/query'
import { AvatarStack } from '@/components/AvatarStack'
import { TaskFormModal } from '@/components/TaskFormModal'
import {
  EmptyState,
  ErrorState,
  Pagination,
  PriorityBadge,
  Spinner,
  TaskStatusBadge,
} from '@/components/ui'
import { OverdueReportModal } from '@/components/OverdueReportModal'
import { CAP, can } from '@/lib/permissions'
import { PRIORITY_LABEL, deadlineLabel, formatDate, formatDateTime, plural, recurrenceLabel, shortName } from '@/lib/format'
import { usePinnedView } from '@/lib/pinnedView'
import { useAuthStore } from '@/store/auth'
import type { EffectiveTaskStatus, Task, TaskPriority } from '@/types'

const PER_PAGE = 12
const TABS: { value: EffectiveTaskStatus | ''; label: string }[] = [
  { value: '', label: 'Все' },
  { value: 'NEW', label: 'Новые' },
  { value: 'IN_PROGRESS', label: 'В работе' },
  { value: 'OVERDUE', label: 'Просрочены' },
  { value: 'DONE', label: 'Выполнены' },
]

export function TasksPage() {
  const user = useAuthStore((s) => s.user)!
  const canCreate = can(user.capabilities, CAP.tasksCreate)
  const canSeeAll = can(user.capabilities, CAP.tasksReadAll)
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const [tab, setTab] = useState<EffectiveTaskStatus | ''>((searchParams.get('status') as EffectiveTaskStatus) ?? '')
  const [search, setSearch] = useState('')
  const [debounced, setDebounced] = useState('')
  const [priority, setPriority] = useState<TaskPriority | ''>('')
  const [assignee, setAssignee] = useState(searchParams.get('assignee_id') ?? '')
  const [recurringOnly, setRecurringOnly] = useState(false)
  const [page, setPage] = useState(1)
  const [view, setView] = useState<'list' | 'board'>(searchParams.get('view') === 'board' ? 'board' : 'list')
  const [scope, setScope] = useState<'school' | 'mine'>(searchParams.get('scope') === 'mine' ? 'mine' : 'school')
  const [sort, setSort] = useState<'reg' | 'deadline' | 'created'>('reg')
  const [formOpen, setFormOpen] = useState(false)
  const [quickAssignee, setQuickAssignee] = useState<string | null>(null)
  const [reportOpen, setReportOpen] = useState(false)

  // закреплённый вид списка: «Запомнить этот вид» сохраняет фильтры в профиле
  const pin = usePinnedView(
    'tasks',
    () => ({
      ...(tab ? { status: tab } : {}),
      ...(priority ? { priority } : {}),
      ...(assignee ? { assignee_id: assignee } : {}),
      ...(recurringOnly ? { recurring: 'true' } : {}),
      ...(scope !== 'school' ? { scope } : {}),
      ...(sort !== 'reg' ? { sort } : {}),
      ...(view !== 'list' ? { view } : {}),
    }),
    (p) => {
      setTab((p.status as EffectiveTaskStatus | '') ?? '')
      setPriority((p.priority as TaskPriority | '') ?? '')
      setAssignee(p.assignee_id ?? '')
      setRecurringOnly(p.recurring === 'true')
      setScope(p.scope === 'mine' ? 'mine' : 'school')
      setSort((p.sort as 'reg' | 'deadline' | 'created') ?? 'reg')
      setView(p.view === 'board' ? 'board' : 'list')
      setPage(1)
    },
  )

  useEffect(() => {
    const id = window.setTimeout(() => { setDebounced(search); setPage(1) }, 350)
    return () => window.clearTimeout(id)
  }, [search])

  const filters = useMemo<TaskFilters>(
    () => ({
      // вкладка «Все» показывает НЕЗАВЕРШЁННЫЕ задачи (выполненные — в отдельной вкладке);
      // канбан не фильтруем: у него своя колонка «Выполнены»
      status: tab || (view === 'board' ? undefined : 'ACTIVE'),
      search: debounced || undefined,
      priority: priority || undefined,
      assignee_id: canSeeAll && assignee ? assignee : undefined,
      recurring: recurringOnly ? true : undefined,
      sort,
      order: sort === 'reg' ? 'desc' : 'asc',
      page,
      per_page: PER_PAGE,
      scope: canSeeAll ? scope : undefined,
    }),
    [tab, debounced, priority, assignee, recurringOnly, canSeeAll, page, scope, sort, view],
  )

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: qk('/tasks', filters as never),
    queryFn: () => tasksApi.list(filters),
  })

  const { data: people = [] } = useQuery({
    queryKey: qk('/users/options'),
    queryFn: () => usersApi.options(),
    enabled: canSeeAll,
  })

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['/tasks'] })
    void qc.invalidateQueries({ queryKey: ['/dashboard'] })
    void qc.invalidateQueries({ queryKey: ['/notifications'] })
  }

  useEffect(() => {
    const next: Record<string, string> = {}
    if (tab) next.status = tab
    if (assignee && canSeeAll) next.assignee_id = assignee
    if (view === 'board') next.view = 'board'
    if (page > 1) next.page = String(page)
    if (canSeeAll && scope === 'mine') next.scope = 'mine'
    setSearchParams(next, { replace: true })
  }, [tab, assignee, canSeeAll, view, page, scope, setSearchParams])

  const counts = data?.status_counts

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div className="page-head__text">
          <h1>Задачи</h1>
          <div className="page-head__hint">
            {canSeeAll
              ? 'Все задачи школы: назначение исполнителей, контроль сроков, отчёт по просрочкам.'
              : 'Ваши задачи. Берите в работу, отмечайте выполнение и оставляйте комментарии.'}
          </div>
        </div>
        <div className="page-head__actions">
          <div className="row">
            <button className={view === 'list' ? 'chip chip--active' : 'chip'} onClick={() => setView('list')}>☰ Список</button>
            <button className={view === 'board' ? 'chip chip--active' : 'chip'} onClick={() => setView('board')}>▦ Доска</button>
          </div>
          {canSeeAll && (
            <select
              className="select"
              style={{ width: 210 }}
              value={sort}
              onChange={(e) => { setSort(e.target.value as 'reg' | 'deadline' | 'created'); setPage(1) }}
              title="Сортировка списка"
            >
              <option value="reg">Сначала с регистрацией (№ убыв.)</option>
              <option value="deadline">По сроку</option>
              <option value="created">По дате создания</option>
            </select>
          )}
          {canSeeAll && (
            <div className="row" style={{ gap: 6 }}>
              <button
                className={scope === 'school' ? 'chip chip--active' : 'chip'}
                onClick={() => { setScope('school'); setPage(1) }}
                title="Все задачи школы"
              >
                По школе
              </button>
              <button
                className={scope === 'mine' ? 'chip chip--active' : 'chip'}
                onClick={() => { setScope('mine'); setPage(1) }}
                title="Задачи, где я исполнитель или постановщик"
              >
                Только мои
              </button>
            </div>
          )}
          {canSeeAll && <button className="btn" onClick={() => setReportOpen(true)}>⚠ Отчёт по просрочкам</button>}
          {can(user.capabilities, CAP.incomingRead) && (
            <button className="btn" onClick={() => navigate('/incoming')} title="Журнал входящих писем">
              ✉ Журнал входящих
            </button>
          )}
          {canCreate && (
            <button
              className="btn btn--primary"
              onClick={() => { setQuickAssignee(null); setFormOpen(true) }}
            >
              + Новая задача
            </button>
          )}
        </div>
      </div>

      <div className="tabs">
        {TABS.map((t) => (
          <button
            key={t.value || 'all'}
            className={tab === t.value ? 'tab tab--active' : 'tab'}
            onClick={() => { setTab(t.value); setPage(1) }}
          >
            {t.label}
            {counts && t.value && counts[t.value] !== undefined ? (
              <span className="chip__count"> · {counts[t.value]}</span>
            ) : null}
            {counts && t.value === '' ? (
              <span className="chip__count"> · {counts.ACTIVE ?? counts.all}</span>
            ) : null}
          </button>
        ))}
      </div>

      <section className="card">
        <div className="card__body">
          <div className="filters">
            <label className="field field--grow">
              <span className="field__label">Поиск</span>
              <input className="input" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Название или описание задачи…" />
            </label>

            {canSeeAll && (
              <label className="field">
                <span className="field__label">Исполнитель</span>
                <select className="select" value={assignee} onChange={(e) => { setAssignee(e.target.value); setPage(1) }}>
                  <option value="">Все сотрудники</option>
                  {people.map((p) => (
                    <option key={p.id} value={p.id}>{p.full_name}</option>
                  ))}
                </select>
              </label>
            )}

            <label className="field">
              <span className="field__label">Приоритет</span>
              <select className="select" value={priority} onChange={(e) => { setPriority(e.target.value as TaskPriority | ''); setPage(1) }}>
                <option value="">Любой</option>
                {(Object.entries(PRIORITY_LABEL) as [TaskPriority, string][]).map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
            </label>

            <div className="field">
              <span className="field__label">Повторение</span>
              <button
                className={recurringOnly ? 'chip chip--active' : 'chip'}
                onClick={() => { setRecurringOnly((v) => !v); setPage(1) }}
                title="Показать только повторяющиеся задачи (дежурства, ежедневные проверки и т.п.)"
              >
                🔁 Только повторяющиеся
              </button>
            </div>

            <div className="field">
              <span className="field__label">Вид списка</span>
              <div className="row" style={{ gap: 6 }}>
                <button
                  className="btn btn--sm btn--ghost"
                  title="Сохранить текущие фильтры в профиле: список откроется с ними при следующем входе"
                  onClick={() => void pin.save()}
                >
                  📌 Запомнить этот вид
                </button>
                {pin.isPinned && (
                  <button className="btn btn--sm btn--ghost" title="Открепить сохранённый вид" onClick={() => void pin.reset()}>
                    ✖ Открепить
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>
      </section>

      {isLoading ? (
        <Spinner large label="Загружаем задачи…" />
      ) : error ? (
        <ErrorState error={error} onRetry={() => void refetch()} />
      ) : !data || data.items.length === 0 ? (
        <div className="card">
          <EmptyState
            icon="✅"
            title="Задач не найдено"
            hint={canCreate ? 'Создайте задачу и назначьте исполнителя' : 'Как только администрация назначит вам задачу, она появится здесь'}
            action={canCreate ? <button className="btn btn--primary btn--sm" onClick={() => setFormOpen(true)}>+ Новая задача</button> : undefined}
          />
        </div>
      ) : view === 'list' ? (
        <section className="card">
          <table className="table table--clickable">
            <thead>
              <tr>
                <th>Задача</th>
                {canSeeAll && <th style={{ width: 120 }}>Входящий №</th>}
                {canSeeAll && <th style={{ width: 100 }}>№ рег.</th>}
                {canSeeAll && <th style={{ width: 230 }}>Исполнители</th>}
                <th style={{ width: 130 }}>Приоритет</th>
                <th style={{ width: 170 }}>Срок</th>
                <th style={{ width: 140 }}>Статус</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((t) => (
                <tr key={t.id} onClick={() => navigate(`/tasks/${t.id}`)}>
                  <td>
                    <div className="strong">
                      {t.recurrence !== 'NONE' && <span title="Повторяющаяся задача">🔁 </span>}
                      {t.title}
                    </div>
                    <div className="tiny muted">создана {formatDate(t.created_at)}{t.creator ? ` · ${shortName(t.creator.full_name)}` : ''}</div>
                  </td>
                  {canSeeAll && <td className="small mono">{t.incoming_number ?? '—'}</td>}
                  {canSeeAll && <td className="small mono">{t.incoming_reg ?? '—'}</td>}
                  {canSeeAll && (
              <td>
                {t.assignees.length > 0 ? (
                  <div className="row" style={{ gap: 8 }}>
                    <AvatarStack users={t.assignees.map((a) => a.user)} />
                    <span className="tiny muted">
                      {t.assignees.map((a) => shortName(a.user.full_name)).join(', ').slice(0, 40)}
                      {t.assignees.length > 1 ? ` · ${t.marks_done}/${t.assignees.length} ✓` : ''}
                    </span>
                  </div>
                ) : (
                  <span className="small muted">не назначен</span>
                )}
              </td>
            )}
                  <td><PriorityBadge priority={t.priority} /></td>
                  <td>
                    {t.recurrence !== 'NONE' ? (
                      <>
                        <div className="small nowrap">🔁 {recurrenceLabel(t)}</div>
                        <div className="tiny muted">
                          {t.done_this_period
                            ? 'в этом периоде выполнено ✓'
                            : t.last_done_at
                              ? `последний раз: ${formatDate(t.last_done_at)}`
                              : 'срок — период повторения'}
                        </div>
                        {(t.missed_streak ?? 0) > 0 && (
                          <div className="tiny nowrap" style={{ color: 'var(--danger)' }}>
                            ⚠ пропущено {t.missed_streak} {plural(t.missed_streak ?? 0, 'период', 'периода', 'периодов')}
                          </div>
                        )}
                      </>
                    ) : (
                      <>
                        <div className="small nowrap">{t.deadline ? formatDateTime(t.deadline) : 'без срока'}</div>
                        <div className={`tiny ${t.is_overdue ? '' : 'muted'}`} style={t.is_overdue ? { color: 'var(--danger)' } : undefined}>
                          {deadlineLabel(t.deadline, t.status === 'DONE')}
                        </div>
                      </>
                    )}
                  </td>
                  <td><TaskStatusBadge status={t.effective_status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination page={data.page} pages={data.pages} total={data.total} perPage={data.per_page} onPage={setPage} />
        </section>
      ) : (
        <Board tasks={data.items} onOpen={(id) => navigate(`/tasks/${id}`)} canSeeAll={canSeeAll} />
      )}

      {isFetching && !isLoading && <div className="tiny muted" style={{ textAlign: 'center' }}>обновляем…</div>}

      <TaskFormModal
        open={formOpen}
        defaultAssigneeId={quickAssignee}
        onClose={() => setFormOpen(false)}
        onSaved={() => { invalidate(); void refetch() }}
      />

      <OverdueReportModal open={reportOpen} onClose={() => setReportOpen(false)} />
    </div>
  )
}

const COLUMNS: { status: EffectiveTaskStatus; title: string }[] = [
  { status: 'NEW', title: 'Новые' },
  { status: 'IN_PROGRESS', title: 'В работе' },
  { status: 'OVERDUE', title: 'Просрочены' },
  { status: 'DONE', title: 'Выполнены' },
]

function Board({ tasks, onOpen, canSeeAll }: { tasks: Task[]; onOpen: (id: string) => void; canSeeAll: boolean }) {
  return (
    <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(250px, 1fr))' }}>
      {COLUMNS.map((col) => {
        const items = tasks.filter((t) => t.effective_status === col.status)
        return (
          <section className="card" key={col.status}>
            <header className="card__head">
              <h3>{col.title}</h3>
              <span className="badge badge--muted">{items.length}</span>
            </header>
            <div className="card__body card__body--flush">
              {items.length === 0 ? (
                <div className="empty small" style={{ padding: 20 }}>Пусто</div>
              ) : (
                items.map((t) => (
                  <div key={t.id} className="list-item" style={{ cursor: 'pointer' }} onClick={() => onOpen(t.id)}>
                    <div className="list-item__main">
                      <div className="list-item__title" style={{ fontSize: 13.5 }}>{t.title}</div>
                      <div className="list-item__meta" style={{ marginTop: 4 }}>
                        <PriorityBadge priority={t.priority} />
                        {t.recurrence !== 'NONE' ? (
                          <>
                            <span className="muted">🔁 {recurrenceLabel(t)}</span>
                            {(t.missed_streak ?? 0) > 0 && (
                              <span className="tiny" style={{ color: 'var(--danger)' }}
                                title={`Пропущено периодов подряд: ${t.missed_streak}`}>
                                ⚠ {t.missed_streak}
                              </span>
                            )}
                          </>
                        ) : (
                          <span className={t.is_overdue ? '' : 'muted'} style={t.is_overdue ? { color: 'var(--danger)' } : undefined}>
                            {deadlineLabel(t.deadline, t.status === 'DONE')}
                          </span>
                        )}
                      </div>
                      {canSeeAll && t.assignees.length > 0 && (
                        <div className="row" style={{ marginTop: 8, gap: 8 }}>
                          <AvatarStack users={t.assignees.map((a) => a.user)} />
                          <span className="tiny muted">{t.marks_done}/{t.assignees.length} ✓</span>
                        </div>
                      )}
                      {t.files_count > 0 && (
                        <span className="tiny muted"> 📎 {t.files_count}</span>
                      )}
                    </div>
                  </div>
                ))
              )}
            </div>
          </section>
        )
      })}
    </div>
  )
}
