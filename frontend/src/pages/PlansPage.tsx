/** Список планов работы по неделям: текущая неделя, архив, черновики (администрация). */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { plansApi } from '@/api/endpoints'
import { qk } from '@/api/query'
import { Modal } from '@/components/Modal'
import { Badge, EmptyState, ErrorState, Spinner } from '@/components/ui'
import { useToast } from '@/components/Toast'
import { CAP, can } from '@/lib/permissions'
import { PLAN_STATUS_LABEL, formatWeekRange, plural, relativeTime, toDateInput } from '@/lib/format'
import { useAuthStore } from '@/store/auth'
import type { PlanListItem, PlanStatus } from '@/types'

const PLAN_TONE: Record<PlanStatus, 'ok' | 'warn' | 'muted'> = {
  PUBLISHED: 'ok',
  DRAFT: 'warn',
  ARCHIVED: 'muted',
}

function isoDate(d: Date): string {
  return toDateInput(d)
}

export function PlansPage() {
  const user = useAuthStore((s) => s.user)!
  const canManage = can(user.capabilities, CAP.plansManage)
  const navigate = useNavigate()
  const toast = useToast()
  const [createOpen, setCreateOpen] = useState(false)

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: qk('/plans'),
    queryFn: () => plansApi.list({ per_page: 100 }),
  })

  const today = isoDate(new Date())

  async function publish(p: PlanListItem) {
    try {
      await plansApi.publish(p.id)
      toast.success('План опубликован, ответственные уведомлены')
      void refetch()
    } catch (err) {
      toast.error(err)
    }
  }

  async function archive(p: PlanListItem) {
    try {
      await plansApi.archive(p.id)
      toast.success('План архивирован')
      void refetch()
    } catch (err) {
      toast.error(err)
    }
  }

  async function removeArchived(p: PlanListItem) {
    if (!window.confirm(`Удалить АРХИВНЫЙ план на неделю ${formatWeekRange(p.week_start, p.week_end)} безвозвратно? Отменить действие нельзя.`)) return
    try {
      await plansApi.remove(p.id)
      toast.success('Архивный план удалён')
      void refetch()
    } catch (err) {
      toast.error(err)
    }
  }

  async function remove(p: PlanListItem) {
    if (!window.confirm(`Удалить черновик плана на неделю ${formatWeekRange(p.week_start, p.week_end)}?`)) return
    try {
      await plansApi.remove(p.id)
      toast.success('Черновик удалён')
      void refetch()
    } catch (err) {
      toast.error(err)
    }
  }

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div className="page-head__text">
          <h1>План работы на неделю</h1>
          <div className="page-head__hint">
            {canManage
              ? 'Составляйте план недели: дежурство, мероприятия по дням, ответственные из числа сотрудников. Публикация отправляет уведомления всем ответственным.'
              : 'Планы работы школы по неделям. Нажмите «Печать», чтобы получить документ как в Word.'}
          </div>
        </div>
        <div className="page-head__actions">
          {canManage && (
            <button className="btn btn--primary" onClick={() => setCreateOpen(true)}>+ Создать план</button>
          )}
        </div>
      </div>

      {isLoading ? (
        <Spinner large label="Загружаем планы…" />
      ) : error ? (
        <ErrorState error={error} onRetry={() => void refetch()} />
      ) : !data || data.items.length === 0 ? (
        <div className="card">
          <EmptyState
            icon="🗓"
            title="Планов пока нет"
            hint={canManage ? 'Создайте план на текущую неделю' : 'Как только администрация опубликует план, он появится здесь'}
          />
        </div>
      ) : (
        <section className="card">
          {data.items.map((p) => {
            const isCurrent = p.status === 'PUBLISHED' && p.week_start <= today && today <= p.week_end
            return (
              <div
                key={p.id}
                className="week-card"
                onClick={() => navigate(`/plans/${p.id}`)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => e.key === 'Enter' && navigate(`/plans/${p.id}`)}
              >
                <div style={{ fontSize: 24 }}>🗓</div>
                <div className="week-card__side">
                  <div className="week-card__dates">{formatWeekRange(p.week_start, p.week_end)}</div>
                  <div className="list-item__meta">
                    <span>мероприятий: {p.events_total}</span>
                    <span>ответственных: {p.responsibles_total}</span>
                    {p.duty_class && <span>дежурный класс: {p.duty_class}</span>}
                    {p.my_items > 0 && (
                      <span className="strong" style={{ color: 'var(--brand-700)' }}>
                        моих пунктов: {p.my_items}
                      </span>
                    )}
                    {p.published_at && <span>опубликован {relativeTime(p.published_at)}</span>}
                  </div>
                </div>
                <div className="row" style={{ gap: 6, flexShrink: 0 }} onClick={(e) => e.stopPropagation()}>
                  {isCurrent && <Badge tone="brand">текущая неделя</Badge>}
                  <Badge tone={PLAN_TONE[p.status]} dot>{PLAN_STATUS_LABEL[p.status]}</Badge>
                  {canManage && p.status === 'DRAFT' && (
                    <>
                      <button className="btn btn--sm" onClick={() => navigate(`/plans/${p.id}/edit`)}>✎ Редактировать</button>
                      <button className="btn btn--sm btn--primary" onClick={() => void publish(p)}>📢 Опубликовать</button>
                      <button className="btn btn--sm btn--danger" onClick={() => void remove(p)}>🗑</button>
                    </>
                  )}
                  {canManage && p.status === 'PUBLISHED' && (
                    <>
                      <button className="btn btn--sm" onClick={() => navigate(`/plans/${p.id}/edit`)}>✎</button>
                      <button className="btn btn--sm" onClick={() => void archive(p)}>🗄 В архив</button>
                    </>
                  )}
                  {user.role === 'OWNER' && p.status === 'ARCHIVED' && (
                    <button
                      className="btn btn--sm btn--danger"
                      title="Удалить архивный план безвозвратно (только Владелец)"
                      onClick={() => void removeArchived(p)}
                    >
                      🗑
                    </button>
                  )}
                  <button className="btn btn--sm" onClick={() => navigate(`/plans/${p.id}?print=1`)}>🖨</button>
                  <button className="btn btn--sm" onClick={() => navigate(`/plans/${p.id}`)}>Открыть →</button>
                </div>
              </div>
            )
          })}
        </section>
      )}

      <CreatePlanModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(id) => navigate(`/plans/${id}/edit`)}
      />
    </div>
  )
}

/** Модалка создания: выбираем понедельник недели; можно скопировать пункты месяца из последнего плана. */
function CreatePlanModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean
  onClose: () => void
  onCreated: (id: string) => void
}) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const [copy, setCopy] = useState(true)

  const monday = new Date()
  monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7))
  const defaultStart = isoDate(monday)
  const [weekStart, setWeekStart] = useState(defaultStart)

  const { data: lastPublished } = useQuery({
    queryKey: qk('/plans', { status: 'PUBLISHED' }),
    queryFn: () => plansApi.list({ status: 'PUBLISHED', per_page: 1 }),
    enabled: open && copy,
  })

  async function create() {
    setBusy(true)
    try {
      let content = {}
      if (copy && lastPublished?.items[0]) {
        const full = await plansApi.detail(lastPublished.items[0].id)
        content = {
          duty: { klass: full.content.duty.klass ?? null, teacher_id: full.content.duty.teacher?.id ?? null },
          notes: full.content.notes,
          month_items: full.content.month_items.map((i) => ({
            text: i.text,
            place: i.place ?? null,
            resp: i.resp.map((u) => u.id),
          })),
        }
      }
      const plan = await plansApi.create({ week_start: weekStart, content })
      toast.success('Черновик плана создан — заполните мероприятия')
      onCreated(plan.id)
      onClose()
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Новый план работы на неделю"
      footer={
        <>
          <button className="btn" onClick={onClose} disabled={busy}>Отмена</button>
          <button className="btn btn--primary" onClick={() => void create()} disabled={busy}>
            {busy ? 'Создаём…' : 'Создать и редактировать'}
          </button>
        </>
      }
    >
      <div className="stack" style={{ gap: 14 }}>
        <label className="field">
          <span className="field__label">Понедельник недели *</span>
          <input className="input" type="date" value={weekStart} onChange={(e) => setWeekStart(e.target.value)} />
          <span className="field__hint">План охватывает пн–сб выбранной недели · один план на неделю</span>
        </label>
        <label className="row small" style={{ gap: 8, alignItems: 'flex-start' }}>
          <input type="checkbox" checked={copy} onChange={(e) => setCopy(e.target.checked)} style={{ marginTop: 3 }} />
          <span>
            Скопировать дежурный класс и пункты «в течение месяца» из последнего опубликованного плана
            (мероприятия по дням не копируются)
          </span>
        </label>
        <div className="tiny muted">
          Создаётся черновик — он виден только администрации. После заполнения опубликуйте: ответственные
          ({plural(0, 'сотрудник', 'сотрудника', 'сотрудников')}) получат уведомления.
        </div>
      </div>
    </Modal>
  )
}
