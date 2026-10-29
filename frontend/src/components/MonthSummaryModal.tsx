/** Отчёт «Итоги месяца» (кнопка на дашборде, только Владелец/Администрация):
 *  счётчики за выбранный календарный месяц + печатная форма. */
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'

import { dashboardApi } from '@/api/endpoints'
import { qk } from '@/api/query'
import { Modal } from '@/components/Modal'
import { Spinner } from '@/components/ui'
import { useOrderTypes } from '@/lib/orderTypes'
import { useAuthStore } from '@/store/auth'

function currentMonth(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

function monthTitle(month: string): string {
  const [y, m] = month.split('-').map(Number)
  const names = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь']
  return `${names[(m || 1) - 1]} ${y}`
}

export function MonthSummaryModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const user = useAuthStore((s) => s.user)!
  const { labels: TYPE_LABELS } = useOrderTypes()
  const [month, setMonth] = useState(currentMonth)

  useEffect(() => {
    if (open) setMonth(currentMonth())
  }, [open])

  // пока отчёт открыт, печать отдаёт только печатную форму
  useEffect(() => {
    if (!open) return
    document.body.classList.add('print-sheet-active')
    return () => document.body.classList.remove('print-sheet-active')
  }, [open])

  const { data, isLoading } = useQuery({
    queryKey: qk('/dashboard/month-summary', { month }),
    queryFn: () => dashboardApi.monthSummary(month),
    enabled: open,
  })

  const rows: { label: string; value: string; hint?: string }[] = data
    ? [
        { label: 'Приказов опубликовано', value: String(data.orders_published) },
        {
          label: '— из них полностью ознакомлены',
          value: `${data.orders_ack_complete} из ${data.orders_published}`,
          hint: 'состояние на текущий момент',
        },
        ...data.orders_by_type.map((t) => ({
          label: `— приказы типа «${TYPE_LABELS[t.type] ?? t.type}»`,
          value: String(t.count),
        })),
        { label: 'Задач создано', value: String(data.tasks_created) },
        { label: 'Задач выполнено', value: String(data.tasks_done), hint: 'отметка «выполнено» в этом месяце' },
        { label: 'Задач с просрочкой', value: String(data.tasks_overdue), hint: 'дедлайн в месяце, не выполнены на сейчас' },
        { label: 'Писем в журнале входящих', value: String(data.incoming_count) },
        { label: 'Заявлений на движение', value: String(data.movement_count) },
      ]
    : []

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="📊 Итоги месяца"
      footer={
        <>
          <button className="btn" onClick={() => onClose()}>Закрыть</button>
          <button className="btn btn--primary" onClick={() => window.print()} disabled={!data}>
            🖨 Печать
          </button>
        </>
      }
    >
      <div className="stack" style={{ gap: 12 }}>
        <label className="field" style={{ maxWidth: 220 }}>
          <span className="field__label">Месяц отчёта</span>
          <input className="input" type="month" value={month} max={currentMonth()} onChange={(e) => setMonth(e.target.value)} />
        </label>

        {isLoading && <Spinner label="Считаем показатели…" />}
        {data && (
          <table className="table" style={{ width: '100%' }}>
            <tbody>
              {rows.map((r) => (
                <tr key={r.label}>
                  <td style={{ padding: '6px 8px' }}>
                    {r.label}
                    {r.hint && <div className="tiny muted">{r.hint}</div>}
                  </td>
                  <td className="strong" style={{ padding: '6px 8px', width: 140, textAlign: 'right' }}>
                    {r.value}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {createPortal(
        <div className="print-sheet">
          <h2 style={{ margin: '0 0 4px', fontSize: 18 }}>Итоги месяца</h2>
          <div style={{ marginBottom: 10 }}>
            {monthTitle(month)} · отчёт сформирован {new Date().toLocaleDateString('ru-RU')} · {user.full_name}
          </div>
          <table className="print-sheet__list">
            <thead>
              <tr>
                <th>Показатель</th>
                <th style={{ width: 120 }}>Значение</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.label}>
                  <td>{r.label}{r.hint ? ` (${r.hint})` : ''}</td>
                  <td style={{ textAlign: 'right' }}>{r.value}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
        document.body,
      )}
    </Modal>
  )
}
