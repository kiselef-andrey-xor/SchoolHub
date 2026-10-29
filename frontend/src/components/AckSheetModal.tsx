/** Лист ознакомления (для Администрации/Владельца): кто подписал, кто нет, экспорт в CSV. */
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'

import { downloadAuthorized } from '@/api/client'
import { ordersApi } from '@/api/endpoints'
import { qk } from '@/api/query'
import { Modal } from '@/components/Modal'
import { UserAvatar } from '@/components/UserAvatar'
import { AckBadge, EmptyState, Progress, Spinner } from '@/components/ui'
import { formatDate, formatDateTime } from '@/lib/format'
import { useOrderTypes } from '@/lib/orderTypes'
import { SIGN_METHOD_LABELS } from '@/types'
import type { OrderDetail } from '@/types'

export function AckSheetModal({ open, order, onClose }: { open: boolean; order: OrderDetail | null; onClose: () => void }) {
  const [filter, setFilter] = useState<'all' | 'PENDING' | 'ACKNOWLEDGED'>('all')
  const { labels: TYPE_LABELS } = useOrderTypes()

  const { data, isLoading } = useQuery({
    queryKey: qk(`/orders/${order?.id}/acknowledgements`, { status: filter === 'all' ? undefined : filter }),
    queryFn: () => ordersApi.acknowledgements(order!.id, filter === 'all' ? undefined : filter),
    enabled: open && Boolean(order),
  })

  const { data: summary } = useQuery({
    queryKey: qk(`/orders/${order?.id}/ack-summary`),
    queryFn: () => ordersApi.ackSummary(order!.id),
    enabled: open && Boolean(order),
  })

  // полный список (без фильтра) — для печатной формы листа
  const { data: full } = useQuery({
    queryKey: qk(`/orders/${order?.id}/acknowledgements`, {}),
    queryFn: () => ordersApi.acknowledgements(order!.id),
    enabled: open && Boolean(order),
  })

  // пока модалка открыта, печать должна отдавать ТОЛЬКО печатную форму
  useEffect(() => {
    if (!open) return
    document.body.classList.add('print-sheet-active')
    return () => document.body.classList.remove('print-sheet-active')
  }, [open])

  if (!order) return null

  const printSheet = (
        <div className="print-sheet">
          <h2 style={{ margin: '0 0 4px', fontSize: 18 }}>Лист ознакомления с приказом</h2>
          <div style={{ marginBottom: 10 }}>
            Приказ № {order.number} · {order.order_year} год · {TYPE_LABELS[order.order_type] ?? order.order_type}
          </div>
          <table className="print-sheet__req">
            <tbody>
              <tr><td>Название приказа</td><td>{order.title}</td></tr>
              <tr><td>Дата приказа</td><td>{formatDate(order.issued_on)}</td></tr>
              <tr><td>Опубликован</td><td>{order.published_at ? formatDateTime(order.published_at) : '—'}</td></tr>
              <tr><td>Срок ознакомления</td><td>{order.ack_deadline ? formatDate(order.ack_deadline) : 'не установлен'}</td></tr>
              <tr><td>Издал(а)</td><td>{order.created_by?.full_name ?? '—'}</td></tr>
              <tr>
                <td>Ознакомление</td>
                <td>
                  {summary ? `${summary.acknowledged} из ${summary.total} (${summary.progress.toFixed(0)}%)` : '—'}
                </td>
              </tr>
            </tbody>
          </table>
          <table className="print-sheet__list">
            <thead>
              <tr>
                <th style={{ width: 28 }}>№</th>
                <th>ФИО</th>
                <th style={{ width: 160 }}>Должность</th>
                <th style={{ width: 150 }}>Дата и время ознакомления</th>
                <th style={{ width: 110 }}>Подпись</th>
              </tr>
            </thead>
            <tbody>
              {(full ?? []).map((a, i) => (
                <tr key={a.id}>
                  <td>{i + 1}</td>
                  <td>{a.user.full_name}</td>
                  <td>{(a.user.positions || []).map((p) => p.title).join(', ') || '—'}</td>
                  <td>{a.acknowledged_at ? formatDateTime(a.acknowledged_at) : 'не ознакомлен(а)'}</td>
                  <td>{a.sign_method ? (SIGN_METHOD_LABELS[a.sign_method] ?? a.sign_method) : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ marginTop: 12, fontSize: 11 }}>
            Лист сформирован {formatDateTime(new Date().toISOString())} · SchoolHub
          </div>
        </div>
  )

  return (
    <>
    <Modal
      open={open}
      onClose={onClose}
      wide
      title={`Лист ознакомления — приказ №${order.number}`}
      footer={
        <>
          <button className="btn" onClick={onClose}>Закрыть</button>
          <button className="btn" onClick={() => window.print()} title="Печать листа с реквизитами приказа и списком ознакомившихся">
            🖨 Печать
          </button>
          <button
            className="btn btn--primary"
            onClick={() => void downloadAuthorized(ordersApi.exportAckSheet(), 'ack_sheet.csv')}
            title="Выгрузка по всем опубликованным приказам"
          >
            ⬇ Экспорт CSV
          </button>
        </>
      }
    >
      <div className="stack" style={{ gap: 14 }}>
        {summary && (
          <div className="row" style={{ gap: 16 }}>
            <div className="grow">
              <div className="row small muted">
                <span>
                  Подписали {summary.acknowledged} из {summary.total}
                </span>
                <span className="grow" />
                <span>{summary.progress.toFixed(0)}%</span>
              </div>
              <Progress value={summary.progress} tone={summary.progress === 100 ? 'ok' : undefined} />
            </div>
            {summary.ack_deadline && <div className="small muted nowrap">срок до {summary.ack_deadline}</div>}
          </div>
        )}

        <div className="row">
          {(['all', 'ACKNOWLEDGED', 'PENDING'] as const).map((value) => (
            <button
              key={value}
              className={filter === value ? 'chip chip--active' : 'chip'}
              onClick={() => setFilter(value)}
            >
              {value === 'all' ? 'Все' : value === 'ACKNOWLEDGED' ? 'Ознакомлены' : 'Не ознакомлены'}
            </button>
          ))}
        </div>

        {isLoading ? (
          <Spinner />
        ) : !data || data.length === 0 ? (
          <EmptyState icon="🗒" title="Нет записей" hint="Измените фильтр или круг ознакомления" />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Сотрудник</th>
                <th style={{ width: 200 }}>Должность</th>
                <th style={{ width: 150 }}>Статус</th>
                <th style={{ width: 170 }}>Дата и время</th>
                <th>Комментарий</th>
              </tr>
            </thead>
            <tbody>
              {data.map((a) => (
                <tr key={a.id}>
                  <td>
                    <div className="user-chip">
                      <UserAvatar user={a.user} size="sm" />
                      <div>
                        <div className="user-chip__name">{a.user.full_name}</div>
                        <div className="tiny muted">{a.user.email}</div>
                      </div>
                    </div>
                  </td>
                  <td className="small">{(a.user.positions || []).map((p) => p.title).join(', ') || '—'}</td>
                  <td><AckBadge status={a.status} /></td>
                  <td className="small nowrap">
                    {a.acknowledged_at ? formatDateTime(a.acknowledged_at) : '—'}
                    {a.ip_address && <div className="tiny muted mono">{a.ip_address}</div>}
                  </td>
                  <td className="small">{a.comment ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {summary && summary.pending_users.length > 0 && (
          <div className="alert alert--warn small">
            <strong>Напомнить ({summary.pending_users.length}):</strong>{' '}
            {summary.pending_users.map((u) => u.full_name).join(', ')}
          </div>
        )}
      </div>
    </Modal>
    {open && createPortal(printSheet, document.body)}
    </>
  )
}
