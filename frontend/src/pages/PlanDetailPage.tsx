/** Просмотр плана работы: документ в стиле Word-макета + печать/PDF, действия администрации. */
import { useEffect } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { metaApi, plansApi } from '@/api/endpoints'
import { qk } from '@/api/query'
import { Badge, EmptyState, ErrorState, Spinner } from '@/components/ui'
import { useToast } from '@/components/Toast'
import { useAuthStore } from '@/store/auth'
import { PLAN_STATUS_LABEL, formatDateTime, formatDateIso, formatWeekRange, shortName } from '@/lib/format'
import type { Plan, PlanItem, PlanEvent, RespRef } from '@/types'

function respNames(resp: RespRef[]): string {
  return resp.map((r) => (r.kind === 'user' ? shortName(r.title) : r.title)).join(', ')
}

export function PlanDetailPage() {
  const { id = '' } = useParams()
  const [searchParams] = useSearchParams()
  const toast = useToast()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const user = useAuthStore((s) => s.user)!

  const { data: plan, isLoading, error, refetch } = useQuery({
    queryKey: qk(`/plans/${id}`),
    queryFn: () => plansApi.detail(id),
  })

  // авто-печать по ?print=1 (из кнопки 🖨 в списке планов)
  useEffect(() => {
    if (plan && searchParams.get('print') === '1') {
      const t = window.setTimeout(() => window.print(), 250)
      return () => window.clearTimeout(t)
    }
  }, [plan, searchParams])

  if (isLoading) return <Spinner large label="Открываем план…" />
  if (error) return <ErrorState error={error} onRetry={() => void refetch()} />
  if (!plan) return null

  const canManage = plan.can_manage

  async function run(action: () => Promise<unknown>, successText: string) {
    try {
      await action()
      toast.success(successText)
      void qc.invalidateQueries({ queryKey: ['/plans'] })
      void qc.invalidateQueries({ queryKey: qk(`/plans/${id}`) })
      void qc.invalidateQueries({ queryKey: ['/dashboard'] })
      void qc.invalidateQueries({ queryKey: qk('/notifications/unread-count') })
      void refetch()
    } catch (err) {
      toast.error(err)
    }
  }

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="page-head no-print">
        <div className="page-head__text">
          <div className="row" style={{ gap: 8, marginBottom: 6 }}>
            <Link className="tiny" to="/plans">← Все планы</Link>
            <Badge tone={plan.status === 'PUBLISHED' ? 'ok' : plan.status === 'DRAFT' ? 'warn' : 'muted'} dot>
              {PLAN_STATUS_LABEL[plan.status]}
            </Badge>
            <Badge tone="muted">мероприятий: {plan.events_total}</Badge>
            <Badge tone="muted">ответственных: {plan.responsibles_total}</Badge>
            {plan.my_items > 0 && <Badge tone="brand">моих пунктов: {plan.my_items}</Badge>}
          </div>
          <h1>План работы: {formatWeekRange(plan.week_start, plan.week_end)}</h1>
          <div className="page-head__hint">
            {plan.created_by ? `составил(а) ${plan.created_by.full_name}` : ''}
            {plan.published_at ? ` · опубликован ${formatDateTime(plan.published_at)}` : ''}
          </div>
        </div>
        <div className="page-head__actions">
          {canManage && plan.status === 'DRAFT' && (
            <>
              <button className="btn btn--primary" onClick={() => void run(() => plansApi.publish(plan.id), 'План опубликован, ответственные уведомлены')}>
                📢 Опубликовать
              </button>
              <Link className="btn" to={`/plans/${plan.id}/edit`}>✎ Редактировать</Link>
              <button
                className="btn btn--danger"
                onClick={() => void run(async () => {
                  if (!window.confirm('Удалить черновик плана?')) return
                  await plansApi.remove(plan.id)
                  window.history.back()
                }, 'Черновик удалён')}
              >
                🗑 Удалить
              </button>
            </>
          )}
          {canManage && plan.status === 'PUBLISHED' && (
            <>
              <Link className="btn" to={`/plans/${plan.id}/edit`}>✎ Редактировать</Link>
              <button className="btn" onClick={() => void run(() => plansApi.archive(plan.id), 'План архивирован')}>🗄 В архив</button>
            </>
          )}
          {canManage && plan.status === 'ARCHIVED' && user.role === 'OWNER' && (
            <button
              className="btn btn--danger"
              onClick={() => {
                if (!window.confirm(`Удалить АРХИВНЫЙ план на неделю ${formatWeekRange(plan.week_start, plan.week_end)} безвозвратно? Отменить действие нельзя.`)) return
                void run(async () => {
                  await plansApi.remove(plan.id)
                  navigate('/plans')
                }, 'Архивный план удалён')
              }}
            >
              🗑 Удалить архивный
            </button>
          )}
          <button className="btn" onClick={() => window.print()}>🖨 Печать / PDF</button>
        </div>
      </div>

      {plan.status === 'DRAFT' && (
        <div className="alert alert--warn no-print">
          Это черновик — он виден только администрации. После публикации план увидят все сотрудники,
          а ответственные получат уведомления.
        </div>
      )}

      <div className="doc-frame">
        <PlanPaper plan={plan} />
      </div>
    </div>
  )
}

/** «Бумажный» вид плана — повторяет школьный Word-макет. */
export function PlanPaper({ plan }: { plan: Plan }) {
  const c = plan.content
  const { data: schoolInfo } = useQuery({ queryKey: qk('/meta/school'), queryFn: metaApi.school })
  const schoolName = schoolInfo?.school_name ?? null
  const hasEvents = c.days.some((d) => d.events.length > 0)

  return (
    <div className="paper plan-paper">
      {schoolName && <div className="p-school">{schoolName}</div>}
      <hr />
      <div className="p-kind" style={{ fontSize: 17 }}>ПЛАН РАБОТЫ</div>
      <div className="p-meta"><strong>{formatWeekRange(plan.week_start, plan.week_end)}</strong></div>
      {c.duty.klass && (
        <div className="p-meta">
          Дежурный класс: <strong>{c.duty.klass}</strong>
          {c.duty.teacher && <>
            , кл. рук. {shortName(c.duty.teacher.full_name)}
          </>}
        </div>
      )}
      {c.notes.map((n, i) => (
        <div className="p-meta" key={i}>{n}</div>
      ))}

      {c.month_items.length > 0 && (
        <>
          <div className="p-sec">В течение месяца:</div>
          <ol>
            {c.month_items.map((it: PlanItem, i) => (
              <li key={i}>
                {it.text}
                {it.resp.length > 0 && <em> Отв.: {respNames(it.resp)}</em>}
              </li>
            ))}
          </ol>
        </>
      )}

      {c.week_items.length > 0 && (
        <>
          <div className="p-sec">В течение недели:</div>
          <ol>
            {c.week_items.map((it: PlanItem, i) => (
              <li key={i}>
                {it.text}
                {it.place && <em> Место: {it.place}.</em>}
                {it.resp.length > 0 && <em> Отв.: {respNames(it.resp)}</em>}
              </li>
            ))}
          </ol>
        </>
      )}

      {hasEvents ? (
        <table className="plan-table">
          <thead>
            <tr>
              <th style={{ width: 118 }}>Дата</th>
              <th>Мероприятие</th>
              <th style={{ width: 98 }}>Время</th>
              <th style={{ width: 130 }}>Место проведения</th>
              <th style={{ width: 170 }}>Ответственный</th>
            </tr>
          </thead>
          <tbody>
            {c.days.map((d) =>
              d.events.length === 0 ? (
                <tr key={d.date}>
                  <td className="c">
                    <strong>{formatDateIso(d.date)}</strong>
                    <div className="tiny muted">{d.weekday}</div>
                  </td>
                  <td colSpan={4} className="c muted">—</td>
                </tr>
              ) : (
                d.events.map((e: PlanEvent, i) => (
                  <tr key={`${d.date}-${i}`}>
                    {i === 0 && (
                      <td className="c" rowSpan={d.events.length}>
                        <strong>{formatDateIso(d.date)}</strong>
                        <div className="tiny">({d.weekday})</div>
                      </td>
                    )}
                    <td>{d.events.length > 1 ? `${i + 1}. ` : ''}{e.title}</td>
                    <td className="c">{e.time || '—'}</td>
                    <td>{e.place || '—'}</td>
                    <td>{respNames(e.resp) || '—'}</td>
                  </tr>
                ))
              ),
            )}
          </tbody>
        </table>
      ) : (
        <div className="p-meta" style={{ marginTop: 14 }}>Мероприятия на неделю не запланированы.</div>
      )}

      <div className="p-note">
        План сформирован в электронной системе SchoolHub · ответственные сотрудники уведомлены при публикации
        {plan.published_at ? ` · опубликован ${formatDateTime(plan.published_at)}` : ''}.
      </div>
    </div>
  )
}

/** Пустое состояние для страниц, где план не найден. */
export function PlanNotFound() {
  return <EmptyState icon="🗓" title="План не найден" />
}
