/** Базовые UI-примитивы без внешних библиотек. */
import type { ReactNode } from 'react'

import { useOrderTypes } from '@/lib/orderTypes'
import { plural } from '@/lib/format'
import type { AckStatus, EffectiveTaskStatus, OrderStatus, OrderType, Role, TaskPriority } from '@/types'
import {
  ORDER_STATUS_LABEL,
  PRIORITY_LABEL,
  TASK_STATUS_LABEL,
} from '@/lib/format'
import { ROLE_SHORT } from '@/lib/permissions'

export type Tone = 'neutral' | 'ok' | 'warn' | 'danger' | 'info' | 'brand' | 'muted'

export function Badge({ tone = 'neutral', children, dot = false }: { tone?: Tone; children: ReactNode; dot?: boolean }) {
  return (
    <span className={`badge badge--${tone}`}>
      {dot && <span className="badge__dot" />}
      {children}
    </span>
  )
}

export const TASK_TONE: Record<EffectiveTaskStatus, Tone> = {
  NEW: 'info',
  IN_PROGRESS: 'warn',
  DONE: 'ok',
  OVERDUE: 'danger',
}

export function TaskStatusBadge({ status }: { status: EffectiveTaskStatus }) {
  return (
    <Badge tone={TASK_TONE[status]} dot>
      {TASK_STATUS_LABEL[status]}
    </Badge>
  )
}

export const PRIORITY_TONE: Record<TaskPriority, Tone> = {
  LOW: 'muted',
  NORMAL: 'neutral',
  HIGH: 'warn',
  URGENT: 'danger',
}

export function PriorityBadge({ priority }: { priority: TaskPriority }) {
  return <Badge tone={PRIORITY_TONE[priority]}>{PRIORITY_LABEL[priority]}</Badge>
}

export function OrderStatusBadge({ status }: { status: OrderStatus }) {
  const tone: Record<OrderStatus, Tone> = { DRAFT: 'warn', PUBLISHED: 'ok', ARCHIVED: 'muted' }
  return <Badge tone={tone[status]} dot>{ORDER_STATUS_LABEL[status]}</Badge>
}

export function OrderTypeBadge({ type }: { type: OrderType }) {
  const { labels } = useOrderTypes()
  return <Badge tone="brand">{labels[type] ?? type}</Badge>
}

export function AckBadge({ status }: { status: AckStatus | null | undefined }) {
  if (!status) return <Badge tone="muted">Не в списке</Badge>
  return status === 'ACKNOWLEDGED' ? (
    <Badge tone="ok" dot>Ознакомлен</Badge>
  ) : (
    <Badge tone="danger" dot>Не ознакомлен</Badge>
  )
}

export const ROLE_TONE: Record<Role, Tone> = { OWNER: 'brand', ADMIN: 'info', TEACHER: 'neutral' }

export function RoleBadge({ role }: { role: Role }) {
  return <Badge tone={ROLE_TONE[role]}>{ROLE_SHORT[role]}</Badge>
}

export function Avatar({ name, size = 'md' }: { name?: string | null; size?: 'sm' | 'md' | 'lg' }) {
  const parts = (name ?? '?').trim().split(/\s+/)
  const letters = `${parts[0]?.[0] ?? ''}${parts[1]?.[0] ?? ''}`.toUpperCase()
  const cls = size === 'sm' ? 'avatar avatar--sm' : size === 'lg' ? 'avatar avatar--lg' : 'avatar'
  return <span className={cls} title={name ?? undefined}>{letters || '?'}</span>
}

// AvatarStack переехал в @/components/AvatarStack (теперь показывает фото-аватары)

export function Spinner({ large = false, label = 'Загрузка…' }: { large?: boolean; label?: string }) {
  return (
    <div className="loading-box" role="status" aria-live="polite">
      <div className={large ? 'spinner spinner--lg' : 'spinner'} />
      <span className="small muted">{label}</span>
    </div>
  )
}

export function EmptyState({ icon = '🗂', title, hint, action }: { icon?: string; title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty__icon">{icon}</div>
      <div className="empty__title">{title}</div>
      {hint && <div className="small">{hint}</div>}
      {action && <div style={{ marginTop: 12 }}>{action}</div>}
    </div>
  )
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : 'Неизвестная ошибка'
  return (
    <div className="empty">
      <div className="empty__icon">⚠️</div>
      <div className="empty__title">Не удалось загрузить данные</div>
      <div className="small">{message}</div>
      {onRetry && (
        <div style={{ marginTop: 12 }}>
          <button className="btn" onClick={onRetry}>Повторить</button>
        </div>
      )}
    </div>
  )
}

export function Progress({ value, tone }: { value: number; tone?: 'ok' | 'warn' }) {
  const pct = Math.max(0, Math.min(100, value))
  const cls = tone ? `progress__bar progress__bar--${tone}` : 'progress__bar'
  return (
    <div className="progress" title={`${pct.toFixed(0)}%`}>
      <div className={cls} style={{ width: `${pct}%` }} />
    </div>
  )
}

export function Pagination({
  page,
  pages,
  total,
  perPage,
  onPage,
}: {
  page: number
  pages: number
  total: number
  perPage: number
  onPage: (p: number) => void
}) {
  if (total === 0) return null
  const from = (page - 1) * perPage + 1
  const to = Math.min(total, page * perPage)
  return (
    <div className="pagination">
      <span className="pagination__info">
        {from}–{to} из {total} {plural(total, 'записи', 'записей', 'записей')}
      </span>
      <button className="btn btn--sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>← Назад</button>
      <span className="small muted nowrap">
        стр. {page} / {pages}
      </span>
      <button className="btn btn--sm" disabled={page >= pages} onClick={() => onPage(page + 1)}>Вперёд →</button>
    </div>
  )
}

export function Kpi({
  label,
  value,
  note,
  tone,
}: {
  label: string
  value: string | number
  note?: string
  tone?: 'danger' | 'warn' | 'ok' | 'brand'
}) {
  return (
    <div className={`card kpi${tone ? ` kpi--${tone}` : ''}`}>
      <div className="kpi__label">{label}</div>
      <div className="kpi__value">{value}</div>
      {note && <div className="kpi__note">{note}</div>}
    </div>
  )
}

export function Section({
  title,
  actions,
  children,
  flush = false,
}: {
  title?: ReactNode
  actions?: ReactNode
  children: ReactNode
  flush?: boolean
}) {
  return (
    <section className="card">
      {(title || actions) && (
        <header className="card__head">
          {title && <h3>{title}</h3>}
          {actions}
        </header>
      )}
      <div className={flush ? 'card__body card__body--flush' : 'card__body'}>{children}</div>
    </section>
  )
}
