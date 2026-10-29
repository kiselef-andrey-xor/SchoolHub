/** Отчёт по просроченным задачам в разрезе исполнителей (GET /tasks/overdue-report). */
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { tasksApi } from '@/api/endpoints'
import { qk } from '@/api/query'
import { Modal } from '@/components/Modal'
import { Avatar, EmptyState, Kpi, Spinner } from '@/components/ui'
import { deadlineLabel } from '@/lib/format'

interface Props {
  open: boolean
  onClose: () => void
}

export function OverdueReportModal({ open, onClose }: Props) {
  const { data, isLoading } = useQuery({
    queryKey: qk('/tasks/overdue-report'),
    queryFn: tasksApi.overdueReport,
    enabled: open,
  })

  const items = data?.items ?? []
  // группируем по исполнителю: у задачи может быть несколько ответственных
  const byAssignee = new Map<string, { name: string; rows: typeof items }>()
  for (const row of items) {
    const names = row.assignees.length ? row.assignees : ['Не назначен']
    for (const name of names) {
      const entry = byAssignee.get(name) ?? { name, rows: [] }
      entry.rows.push(row)
      byAssignee.set(name, entry)
    }
  }
  const groups = Array.from(byAssignee.values()).sort((a, b) => b.rows.length - a.rows.length)

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Отчёт по просроченным задачам"
      wide
      footer={<button className="btn" onClick={onClose}>Закрыть</button>}
    >
      {isLoading ? (
        <Spinner label="Считаем просрочки…" />
      ) : items.length === 0 ? (
        <EmptyState icon="🎉" title="Просроченных задач нет" />
      ) : (
        <div className="stack" style={{ gap: 14 }}>
          <Kpi label="Просрочено задач" value={items.length} note="по всем видимым задачам" tone="danger" />
          <table className="table">
            <thead>
              <tr>
                <th>Исполнитель</th>
                <th style={{ width: 100 }}>Просрочено</th>
                <th>Задачи</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((g) => (
                <tr key={g.name}>
                  <td>
                    <div className="user-chip">
                      <Avatar name={g.name} size="sm" />
                      <span className="small strong">{g.name}</span>
                    </div>
                  </td>
                  <td><strong style={{ color: 'var(--danger)' }}>{g.rows.length}</strong></td>
                  <td>
                    {g.rows.map((r, i) => (
                      <div key={r.id} className="small">
                        {i > 0 && <br />}
                        <Link to={`/tasks/${r.id}`} onClick={onClose}>{r.title}</Link>{' '}
                        <span className="tiny muted">({deadlineLabel(r.deadline)})</span>
                      </div>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  )
}
