/**
 * Helpdesk — заявки сотрудников в техническую поддержку школы (v1.12).
 *
 * Модуль спроектирован с нуля под школьную реальность: «зажевало принтер»,
 * «не включается компьютер», «нет интернета на интерактивной доске»,
 * «не работает документ-камера».
 *
 * Две принципиальные вещи в интерфейсе:
 *
 *  1. ОЧЕРЕДЬ, А НЕ НАЗНАЧЕНИЕ. Заявку видят все системные администраторы
 *     (грант Владельца) и сами берут её в работу — кнопка «Взять в работу».
 *     В школе сисадмин один-два, и «назначенная, но не взятая» заявка просто
 *     потерялась бы. Переназначение — отдельное действие (уехал, заболел).
 *
 *  2. ПОДТВЕРЖДЕНИЕ АВТОРА. Выполнение переводит заявку в RESOLVED, а закрывает
 *     её автор: «починили» должно означать «у учителя действительно работает».
 *     Автор может вернуть заявку на доработку одной кнопкой.
 *
 * Обычный сотрудник видит здесь только СВОИ обращения и форму подачи;
 * администрация — всю очередь чтением (без кнопок закрытия);
 * системные администраторы и Владелец — очередь целиком со всеми действиями.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { helpdeskApi } from '@/api/endpoints'
import { qk } from '@/api/query'
import { DocLink, DocList } from '@/components/DocLink'
import { Modal } from '@/components/Modal'
import { useToast } from '@/components/Toast'
import { Avatar, Badge, EmptyState, ErrorState, Kpi, Section, Spinner, type Tone } from '@/components/ui'
import { formatDateTime, formatDuration, plural, shortName } from '@/lib/format'
import { CAP, can } from '@/lib/permissions'
import { useAuthStore } from '@/store/auth'
import type { QueryParams } from '@/types'
import {
  TICKET_CATEGORY_ICONS,
  TICKET_CATEGORY_LABELS,
  TICKET_PRIORITY_LABELS,
  TICKET_STATUS_LABELS,
  type SysadminUser,
  type Ticket,
  type TicketCategory,
  type TicketFilters,
  type TicketPriority,
  type TicketStatus,
} from '@/types'

const STATUS_TONE: Record<TicketStatus, Tone> = {
  NEW: 'warn',
  IN_PROGRESS: 'info',
  RESOLVED: 'brand',
  CLOSED: 'ok',
  REJECTED: 'muted',
  CANCELLED: 'muted',
}

const PRIORITY_TONE: Record<TicketPriority, Tone> = {
  LOW: 'muted',
  NORMAL: 'neutral',
  HIGH: 'warn',
  URGENT: 'danger',
}

/** Заявка «живая»: по ней ещё возможны действия. */
const ACTIVE: TicketStatus[] = ['NEW', 'IN_PROGRESS', 'RESOLVED']

export function HelpdeskPage() {
  const toast = useToast()
  const qc = useQueryClient()
  const me = useAuthStore((st) => st.user)
  const [params, setParams] = useSearchParams()

  const isManager = can(me?.capabilities, CAP.helpdeskManage)
  const seesQueue = isManager || me?.role === 'ADMIN'

  const [status, setStatus] = useState<TicketStatus | ''>(
    (params.get('status') as TicketStatus) ?? '',
  )
  const [category, setCategory] = useState<TicketCategory | ''>(
    (params.get('category') as TicketCategory) ?? '',
  )
  const [priority, setPriority] = useState<TicketPriority | ''>('')
  const [mine, setMine] = useState(false)
  const [unassigned, setUnassigned] = useState(false)
  const [overdue, setOverdue] = useState(params.get('overdue') === '1')
  const [q, setQ] = useState('')
  const [createOpen, setCreateOpen] = useState(params.get('new') === '1')
  const [openId, setOpenId] = useState<string | null>(params.get('t'))

  const filters: TicketFilters = useMemo(
    () => ({
      status: status || undefined,
      category: category || undefined,
      priority: priority || undefined,
      mine: mine || undefined,
      unassigned: unassigned || undefined,
      overdue: overdue || undefined,
      q: q.trim() || undefined,
      limit: 300,
    }),
    [status, category, priority, mine, unassigned, overdue, q],
  )

  const listQ = useQuery({
    queryKey: qk('/helpdesk', filters as QueryParams),
    queryFn: () => helpdeskApi.list(filters),
  })
  const statsQ = useQuery({
    queryKey: qk('/helpdesk/stats'),
    queryFn: () => helpdeskApi.stats(),
    refetchInterval: 60_000,   // очередь живая: сводка сама себя обновляет
  })

  // глубокую ссылку из уведомления (/helpdesk?t=<id>) отражаем в адресной строке,
  // чтобы обновление страницы не теряло открытую карточку
  useEffect(() => {
    const next = new URLSearchParams(params)
    if (openId) next.set('t', openId)
    else next.delete('t')
    if (overdue) next.set('overdue', '1')
    else next.delete('overdue')
    if (next.toString() !== params.toString()) setParams(next, { replace: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openId, overdue])

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['/helpdesk'] })
    void qc.invalidateQueries({ queryKey: ['/notifications'] })
    void qc.invalidateQueries({ queryKey: ['/dashboard'] })
  }

  const rows = listQ.data ?? []
  const stats = statsQ.data

  return (
    <div className="page">
      <div className="page__head">
        <div>
          <h1>Техническая поддержка</h1>
          <p className="muted small">
            {seesQueue
              ? 'Очередь заявок сотрудников: берите в работу, выполняйте, закрывайте.'
              : 'Сообщите о неисправной технике — системный администратор увидит заявку в общей очереди.'}
          </p>
        </div>
        <div className="row" style={{ gap: 8 }}>
          {seesQueue && (
            <button
              className="btn"
              onClick={() => void helpdeskApi.exportCsv().catch((e: Error) => toast.show(e.message, 'error'))}
            >
              Экспорт CSV
            </button>
          )}
          <button className="btn btn--primary" onClick={() => setCreateOpen(true)}>
            + Подать заявку
          </button>
        </div>
      </div>

      {stats && (
        <div className="grid grid--kpi" style={{ marginBottom: 12 }}>
          <Kpi
            label={seesQueue ? 'В очереди' : 'Мои заявки'}
            value={seesQueue ? stats.new + stats.in_progress : stats.total}
            note={seesQueue ? `${stats.new} новых` : `${stats.closed + stats.rejected} завершено`}
          />
          {seesQueue && (
            <>
              <Kpi
                label="У меня в работе"
                value={stats.mine}
                tone={stats.mine ? 'brand' : undefined}
                note={stats.median_reaction_minutes != null
                  ? `медиана реакции: ${formatDuration(stats.median_reaction_minutes)}`
                  : undefined}
              />
              <Kpi
                label="Просрочена реакция"
                value={stats.sla_breached}
                tone={stats.sla_breached ? 'danger' : 'ok'}
                note={stats.sla_breached ? 'нужно взять в работу' : 'нормативы соблюдаются'}
              />
              <Kpi
                label="Ожидают подтверждения"
                value={stats.resolved}
                tone={stats.resolved ? 'warn' : undefined}
                note={`выполнено сегодня: ${stats.resolved_today}`}
              />
            </>
          )}
        </div>
      )}

      <div className="filters row" style={{ gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
        <select className="input input--sm" value={status}
                onChange={(e) => setStatus(e.target.value as TicketStatus | '')}>
          <option value="">Все статусы</option>
          {(Object.keys(TICKET_STATUS_LABELS) as TicketStatus[]).map((s) => (
            <option key={s} value={s}>{TICKET_STATUS_LABELS[s]}</option>
          ))}
        </select>
        <select className="input input--sm" value={category}
                onChange={(e) => setCategory(e.target.value as TicketCategory | '')}>
          <option value="">Все категории</option>
          {(Object.keys(TICKET_CATEGORY_LABELS) as TicketCategory[]).map((c) => (
            <option key={c} value={c}>{TICKET_CATEGORY_ICONS[c]} {TICKET_CATEGORY_LABELS[c]}</option>
          ))}
        </select>
        <select className="input input--sm" value={priority}
                onChange={(e) => setPriority(e.target.value as TicketPriority | '')}>
          <option value="">Любой приоритет</option>
          {(Object.keys(TICKET_PRIORITY_LABELS) as TicketPriority[]).map((p) => (
            <option key={p} value={p}>{TICKET_PRIORITY_LABELS[p]}</option>
          ))}
        </select>
        <input className="input input--sm" placeholder="Поиск по теме, описанию, кабинету…"
               value={q} onChange={(e) => setQ(e.target.value)} style={{ minWidth: 220 }} />
        {seesQueue && (
          <>
            <label className="chip">
              <input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} />
              У меня в работе
            </label>
            <label className="chip">
              <input type="checkbox" checked={unassigned} onChange={(e) => setUnassigned(e.target.checked)} />
              Без исполнителя
            </label>
            <label className="chip">
              <input type="checkbox" checked={overdue} onChange={(e) => setOverdue(e.target.checked)} />
              Просрочена реакция
            </label>
          </>
        )}
      </div>

      {listQ.isLoading ? <Spinner /> : listQ.isError ? <ErrorState error={listQ.error} onRetry={() => void listQ.refetch()} /> : rows.length === 0 ? (
        <EmptyState
          icon="🛠"
          title={seesQueue ? 'Заявок нет' : 'Вы ещё не подавали заявок'}
          hint={seesQueue
            ? 'Как только сотрудник сообщит о неисправности, заявка появится здесь и у всех системных администраторов.'
            : 'Сломался принтер, не включается компьютер, нет интернета на доске — нажмите «Подать заявку».'}
          action={<button className="btn btn--primary" onClick={() => setCreateOpen(true)}>+ Подать заявку</button>}
        />
      ) : (
        <Section flush>
          <table className="table">
            <thead>
              <tr>
                <th>№</th>
                <th>Что случилось</th>
                <th>Категория</th>
                <th>Приоритет</th>
                <th>Статус</th>
                {seesQueue && <th>Исполнитель</th>}
                <th>{seesQueue ? 'Автор' : 'Подана'}</th>
                <th>Реакция</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => (
                <tr key={t.id} className={t.sla_breached ? 'row--danger' : undefined}>
                  <td className="muted">{t.number ? `${t.number}/${t.year}` : '—'}</td>
                  <td>
                    <button className="link" onClick={() => setOpenId(t.id)}>{t.title}</button>
                    {t.location && <div className="small muted">📍 {t.location}</div>}
                  </td>
                  <td>
                    <span title={TICKET_CATEGORY_LABELS[t.category]}>
                      {TICKET_CATEGORY_ICONS[t.category]} <span className="small">{TICKET_CATEGORY_LABELS[t.category]}</span>
                    </span>
                  </td>
                  <td><Badge tone={PRIORITY_TONE[t.priority]}>{TICKET_PRIORITY_LABELS[t.priority]}</Badge></td>
                  <td>
                    <Badge tone={STATUS_TONE[t.status]}>{TICKET_STATUS_LABELS[t.status]}</Badge>
                    {t.sla_breached && <div className="small danger">реакция просрочена</div>}
                    {t.auto_closed && <div className="small muted">закрыта автоматически</div>}
                  </td>
                  {seesQueue && (
                    <td>{t.assignee ? shortName(t.assignee.full_name) : <span className="muted">— не назначен —</span>}</td>
                  )}
                  <td>{seesQueue ? shortName(t.author?.full_name) : formatDateTime(t.created_at)}</td>
                  <td className="small">
                    {t.reaction_minutes != null
                      ? formatDuration(t.reaction_minutes)
                      : t.status === 'NEW' && t.sla_hours
                        ? <span className={t.sla_breached ? 'danger' : 'muted'}>норматив {t.sla_hours} ч</span>
                        : '—'}
                  </td>
                  <td className="row" style={{ gap: 6 }}>
                    {isManager && t.status === 'NEW' && (
                      <QuickTake id={t.id} onDone={invalidate} />
                    )}
                    {isManager && t.status === 'IN_PROGRESS' && t.assignee?.id !== me?.id && (
                      <QuickTake id={t.id} onDone={invalidate} label="Перенять" />
                    )}
                    <button className="btn btn--sm" onClick={() => setOpenId(t.id)}>Открыть</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}

      {createOpen && (
        <TicketCreateModal
          onClose={() => {
            setCreateOpen(false)
            const next = new URLSearchParams(params)
            next.delete('new')
            setParams(next, { replace: true })
          }}
          onDone={invalidate}
        />
      )}
      {openId && <TicketDetailModal id={openId} onClose={() => setOpenId(null)} onChanged={invalidate} />}
    </div>
  )
}

/** «Взять в работу» прямо из журнала — одно действие вместо открытия карточки. */
function QuickTake({ id, onDone, label = 'Взять в работу' }: { id: string; onDone: () => void; label?: string }) {
  const toast = useToast()
  const m = useMutation({
    mutationFn: () => helpdeskApi.take(id),
    onSuccess: () => { onDone(); toast.show('Заявка у вас в работе', 'success') },
    onError: (e: Error) => toast.show(e.message, 'error'),
  })
  return (
    <button className="btn btn--sm btn--success" disabled={m.isPending} onClick={() => m.mutate()}>
      {m.isPending ? '…' : label}
    </button>
  )
}

// ------------------------------------------------------------------ подача заявки
function TicketCreateModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const toast = useToast()
  const me = useAuthStore((st) => st.user)
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [category, setCategory] = useState<TicketCategory>('PRINTER')
  const [priority, setPriority] = useState<TicketPriority>('NORMAL')
  const [location, setLocation] = useState('')
  const [phone, setPhone] = useState(me?.phone ?? '')
  const fileRef = useRef<HTMLInputElement>(null)

  const directoryQ = useQuery({ queryKey: qk('/helpdesk/directory'), queryFn: () => helpdeskApi.directory() })
  const directory = directoryQ.data

  const createM = useMutation({
    mutationFn: async () => {
      const ticket = await helpdeskApi.create({
        title: title.trim(),
        description: description.trim(),
        category,
        priority,
        location: location.trim() || null,
        contact_phone: phone.trim() || null,
      })
      // фото поломки резко повышает шансы починить с первого раза — грузим следом
      const files = Array.from(fileRef.current?.files ?? [])
      for (const f of files) await helpdeskApi.addFile(ticket.id, f)
      return ticket
    },
    onSuccess: (ticket) => {
      onDone()
      onClose()
      toast.show(`Заявка №${ticket.number}/${ticket.year} отправлена системным администраторам`, 'success')
    },
    onError: (e: Error) => toast.show(e.message, 'error'),
  })

  const slaHours = directory?.sla_hours[priority]
  const valid = title.trim().length >= 3 && description.trim().length >= 5

  return (
    <Modal
      open
      wide
      onClose={onClose}
      title="Заявка в техническую поддержку"
      footer={<>
        <button className="btn" onClick={onClose}>Отмена</button>
        <button className="btn btn--primary" disabled={!valid || createM.isPending} onClick={() => createM.mutate()}>
          {createM.isPending ? 'Отправляем…' : 'Отправить заявку'}
        </button>
      </>}
    >
      <div className="stack" style={{ gap: 12 }}>
        <div className="alert alert--info">
          Заявку увидят системные администраторы школы. Назначать исполнителя не нужно —
          её возьмёт в работу тот, кто освободится первым.
          {slaHours ? <> Норматив реакции для выбранного приоритета — <b>{slaHours} ч</b>.</> : null}
        </div>

        <label className="field">
          <span className="field__label">Что случилось</span>
          <input className="input" value={title} maxLength={300} placeholder="Например: зажевало бумагу в принтере"
                 onChange={(e) => setTitle(e.target.value)} />
        </label>

        <label className="field">
          <span className="field__label">Описание</span>
          <textarea className="input" rows={5} value={description}
                    placeholder="Когда началось, что уже пробовали, мешает ли проведению уроков"
                    onChange={(e) => setDescription(e.target.value)} />
          <span className="field__hint">Чем подробнее, тем выше шанс починить с первого визита</span>
        </label>

        <div className="row" style={{ gap: 12, flexWrap: 'wrap' }}>
          <label className="field" style={{ flex: 2, minWidth: 240 }}>
            <span className="field__label">Категория</span>
            <select className="input" value={category} onChange={(e) => setCategory(e.target.value as TicketCategory)}>
              {(directory?.categories ?? (Object.keys(TICKET_CATEGORY_LABELS) as TicketCategory[])
                .map((value) => ({ value, label: TICKET_CATEGORY_LABELS[value] })))
                .map((c) => (
                  <option key={c.value} value={c.value}>
                    {TICKET_CATEGORY_ICONS[c.value as TicketCategory]} {c.label}
                  </option>
                ))}
            </select>
          </label>
          <label className="field" style={{ flex: 1, minWidth: 170 }}>
            <span className="field__label">Срочность</span>
            <select className="input" value={priority} onChange={(e) => setPriority(e.target.value as TicketPriority)}>
              {(directory?.priorities ?? (Object.keys(TICKET_PRIORITY_LABELS) as TicketPriority[])
                .map((value) => ({ value, label: TICKET_PRIORITY_LABELS[value] })))
                .map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
            </select>
            <span className="field__hint">
              {priority === 'URGENT' && 'Срывается учебный процесс — реакция в течение 2 часов'}
              {priority === 'HIGH' && 'Мешает работе — реакция в течение 4 часов'}
              {priority === 'NORMAL' && 'Штатная неисправность — реакция в течение рабочего дня'}
              {priority === 'LOW' && 'Не мешает работе — реакция в течение суток'}
            </span>
          </label>
        </div>

        <div className="row" style={{ gap: 12, flexWrap: 'wrap' }}>
          <label className="field" style={{ flex: 1, minWidth: 200 }}>
            <span className="field__label">Где находится</span>
            <input className="input" value={location} maxLength={200} placeholder="кабинет 214, актовый зал"
                   onChange={(e) => setLocation(e.target.value)} />
          </label>
          <label className="field" style={{ flex: 1, minWidth: 180 }}>
            <span className="field__label">Телефон для связи</span>
            <input className="input" value={phone} maxLength={40} placeholder="2-14, +7…"
                   onChange={(e) => setPhone(e.target.value)} />
            <span className="field__hint">Администратору часто быстрее позвонить, чем переписываться</span>
          </label>
        </div>

        <label className="field">
          <span className="field__label">Фото или документ (необязательно)</span>
          <input ref={fileRef} type="file" className="input" multiple />
          <span className="field__hint">
            Фотография неисправности помогает понять, что везти с собой. К документу автоматически
            создаётся PDF-копия — его можно будет открыть прямо в браузере.
          </span>
        </label>
      </div>
    </Modal>
  )
}

// ------------------------------------------------------------------ карточка заявки
function TicketDetailModal({ id, onClose, onChanged }: { id: string; onClose: () => void; onChanged: () => void }) {
  const toast = useToast()
  const me = useAuthStore((st) => st.user)
  const detailQ = useQuery({ queryKey: qk(`/helpdesk/${id}`), queryFn: () => helpdeskApi.detail(id) })
  const t = detailQ.data

  const [action, setAction] = useState<null | 'take' | 'resolve' | 'reject' | 'reopen' | 'cancel' | 'confirm' | 'delete'>(null)
  const [message, setMessage] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  const isManager = can(me?.capabilities, CAP.helpdeskManage)
  const isAuthor = Boolean(t && me && t.author?.id === me.id)
  const isAssignee = Boolean(t && me && t.assignee?.id === me.id)

  const reload = () => {
    onChanged()
    void detailQ.refetch()
  }

  const messageM = useMutation({
    mutationFn: (body: string) => helpdeskApi.message(id, body),
    onSuccess: () => { setMessage(''); reload() },
    onError: (e: Error) => toast.show(e.message, 'error'),
  })
  const uploadM = useMutation({
    mutationFn: (f: File) => helpdeskApi.addFile(id, f),
    onSuccess: () => { reload(); toast.show('Файл прикреплён, PDF-копия готовится', 'success') },
    onError: (e: Error) => toast.show(e.message, 'error'),
  })

  // круг переназначения нужен только тому, кто видит очередь; хук вызываем до
  // ранних возвратов, иначе порядок хуков поедет на загрузке
  const staffQ = useQuery({
    queryKey: qk('/helpdesk/staff'),
    queryFn: () => helpdeskApi.staff(),
    enabled: isManager || me?.role === 'ADMIN',
  })

  if (detailQ.isLoading) return <Modal open onClose={onClose} title="Заявка"><Spinner /></Modal>
  if (detailQ.isError || !t) {
    return <Modal open onClose={onClose} title="Заявка"><ErrorState error={detailQ.error} /></Modal>
  }

  const staff = staffQ.data ?? []

  return (
    <Modal
      open
      wide
      onClose={onClose}
      title={<span>{TICKET_CATEGORY_ICONS[t.category]} Заявка №{t.number}/{t.year} — {t.title}</span>}
      footer={<>
        {isManager && me?.role === 'OWNER' && (
          <button className="btn btn--danger" onClick={() => setAction('delete')}>Удалить</button>
        )}
        <span className="grow" />
        <button className="btn" onClick={onClose}>Закрыть</button>
      </>}
    >
      <div className="stack" style={{ gap: 14 }}>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          <Badge tone={STATUS_TONE[t.status]}>{TICKET_STATUS_LABELS[t.status]}</Badge>
          <Badge tone={PRIORITY_TONE[t.priority]}>
            {TICKET_PRIORITY_LABELS[t.priority]}{t.sla_hours ? ` · реакция ${t.sla_hours} ч` : ''}
          </Badge>
          <span className="small muted">{TICKET_CATEGORY_LABELS[t.category]}</span>
          {t.sla_breached && <Badge tone="danger">норматив реакции истёк</Badge>}
        </div>

        <div className="ticket-facts">
          <div><span>Автор</span><b><Avatar name={t.author?.full_name} size="sm" /> {shortName(t.author?.full_name)}</b></div>
          <div><span>Исполнитель</span><b>{t.assignee ? shortName(t.assignee.full_name) : 'не назначен'}</b></div>
          <div><span>Где</span><b>{t.location || '—'}</b></div>
          <div><span>Телефон</span><b>{t.contact_phone || '—'}</b></div>
          <div><span>Оборудование</span><b>{t.equipment_name || '—'}</b></div>
          <div><span>Подана</span><b>{formatDateTime(t.created_at)}</b></div>
          <div><span>Взята в работу</span><b>{t.taken_at ? `${formatDateTime(t.taken_at)} (${formatDuration(t.reaction_minutes)})` : '—'}</b></div>
          <div><span>Крайний срок реакции</span><b className={t.sla_breached ? 'danger' : undefined}>{t.sla_deadline ? formatDateTime(t.sla_deadline) : '—'}</b></div>
        </div>

        <div className="ticket-description">{t.description}</div>

        {t.resolution && (
          <div className="alert alert--success">
            <b>Выполнено {formatDateTime(t.resolved_at)}:</b> {t.resolution}
            {t.work_minutes != null && <div className="small muted">В работе: {formatDuration(t.work_minutes)}</div>}
          </div>
        )}
        {t.rejected_reason && (
          <div className="alert alert--error">
            <b>Отклонена:</b> {t.rejected_reason}
          </div>
        )}
        {t.status === 'CLOSED' && (
          <div className="alert alert--info">
            Закрыта {formatDateTime(t.closed_at)}
            {t.auto_closed
              ? ' автоматически: автор не ответил в срок'
              : ` · подтвердил(а) ${shortName(t.closed_by?.full_name)}`}
          </div>
        )}

        {/* --- действия --- */}
        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          {isManager && t.status === 'NEW' && (
            <button className="btn btn--success" onClick={() => setAction('take')}>Взять в работу</button>
          )}
          {isManager && t.status === 'IN_PROGRESS' && !isAssignee && (
            <button className="btn" onClick={() => setAction('take')}>Перенять / переназначить</button>
          )}
          {isManager && (t.status === 'IN_PROGRESS' || (t.status === 'NEW' && isAssignee)) && (
            <button className="btn btn--primary" onClick={() => setAction('resolve')}>Выполнено</button>
          )}
          {isManager && ACTIVE.includes(t.status) && (
            <button className="btn btn--danger" onClick={() => setAction('reject')}>Отклонить</button>
          )}
          {isAuthor && t.status === 'RESOLVED' && (
            <>
              <button className="btn btn--success" onClick={() => setAction('confirm')}>Работает, закрыть</button>
              <button className="btn btn--danger" onClick={() => setAction('reopen')}>Не решено</button>
            </>
          )}
          {(isAuthor || isManager) && (t.status === 'NEW' || t.status === 'IN_PROGRESS') && (
            <button className="btn" onClick={() => setAction('cancel')}>Отозвать заявку</button>
          )}
        </div>

        {/* --- вложения --- */}
        <Section title={`Документы и фото (${t.files.length})`}>
          <DocList docs={t.files} empty={<span className="muted small">Вложений нет</span>}>
            {(f) => (
              <DocLink
                doc={f}
                meta={f.kind === 'STAFF' ? 'приложил администратор' : undefined}
                onOpen={() => helpdeskApi.openFile(id, f.id)}
                onDownload={() => helpdeskApi.downloadFile(id, f.id, f.original_name)}
                onRemove={ACTIVE.includes(t.status) && (f.uploaded_by?.id === me?.id || me?.role === 'OWNER')
                  ? () => void helpdeskApi.removeFile(id, f.id).then(reload).catch((e: Error) => toast.show(e.message, 'error'))
                  : undefined}
              />
            )}
          </DocList>
          {ACTIVE.includes(t.status) && (
            <input
              type="file"
              className="input"
              style={{ marginTop: 8 }}
              ref={fileRef}
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) uploadM.mutate(f)
                e.target.value = ''
              }}
            />
          )}
        </Section>

        {/* --- лента событий и переписка --- */}
        <Section title="Переписка и история">
          <div className="ticket-thread">
            {t.messages.map((m) => (
              <div key={m.id} className={m.kind === 'SYSTEM' ? 'thread-item thread-item--system' : 'thread-item'}>
                {m.kind === 'SYSTEM' ? (
                  <>
                    <span className="thread-item__icon">⚙</span>
                    <span className="thread-item__body">{m.body}</span>
                    <span className="thread-item__when">{formatDateTime(m.created_at)}</span>
                  </>
                ) : (
                  <>
                    <Avatar name={m.author?.full_name} size="sm" />
                    <div className="thread-item__content">
                      <div className="thread-item__head">
                        <b>{shortName(m.author?.full_name) ?? 'Система'}</b>
                        <span className="small muted">{formatDateTime(m.created_at)}</span>
                        {m.author?.id === me?.id && (
                          <button
                            className="link link--danger small"
                            title="Удалить свою реплику"
                            onClick={() => void helpdeskApi.removeMessage(id, m.id).then(reload).catch((e: Error) => toast.show(e.message, 'error'))}
                          >
                            удалить
                          </button>
                        )}
                      </div>
                      <div className="thread-item__text">{m.body}</div>
                    </div>
                  </>
                )}
              </div>
            ))}
          </div>
          {ACTIVE.includes(t.status) && (
            <div className="row" style={{ gap: 8, marginTop: 10, alignItems: 'flex-end' }}>
              <label className="field" style={{ flex: 1 }}>
                <span className="field__label">Сообщение</span>
                <textarea className="input" rows={2} value={message} maxLength={4000}
                          placeholder="Уточнить детали, сообщить о ходе работ…"
                          onChange={(e) => setMessage(e.target.value)} />
              </label>
              <button className="btn btn--primary" disabled={!message.trim() || messageM.isPending}
                      onClick={() => messageM.mutate(message.trim())}>
                {messageM.isPending ? '…' : 'Отправить'}
              </button>
            </div>
          )}
        </Section>
      </div>

      {action === 'take' && (
        <TakeModal
          ticket={t}
          staff={staff}
          onClose={() => setAction(null)}
          onSubmit={async (assigneeId, comment) => {
            await helpdeskApi.take(id, assigneeId, comment)
            reload()
            setAction(null)
            toast.show(assigneeId && assigneeId !== me?.id ? 'Заявка передана' : 'Заявка у вас в работе', 'success')
          }}
        />
      )}
      {action === 'resolve' && (
        <ReasonModal
          title="Заявка выполнена"
          label="Что сделано (увидит автор)"
          placeholder="Заменил картридж, прочистил ролики подачи, проверил печать"
          submitLabel="Отправить автору на подтверждение"
          min={3}
          onClose={() => setAction(null)}
          onSubmit={async (text) => { await helpdeskApi.resolve(id, text); reload(); setAction(null); toast.show('Заявка выполнена', 'success') }}
        />
      )}
      {action === 'reject' && (
        <ReasonModal
          title="Отклонить заявку"
          label="Причина (обязательно — автор должен понимать, куда идти дальше)"
          placeholder="Не наш профиль: нужна закупка картриджа — оформите служебную записку"
          submitLabel="Отклонить"
          danger
          min={3}
          onClose={() => setAction(null)}
          onSubmit={async (text) => { await helpdeskApi.reject(id, text); reload(); setAction(null); toast.show('Заявка отклонена', 'success') }}
        />
      )}
      {action === 'reopen' && (
        <ReasonModal
          title="Вернуть на доработку"
          label="Что осталось не сделано"
          placeholder="Принтер снова зажёвывает бумагу на втором листе"
          submitLabel="Вернуть в работу"
          danger
          min={3}
          onClose={() => setAction(null)}
          onSubmit={async (text) => { await helpdeskApi.reopen(id, text); reload(); setAction(null); toast.show('Заявка возвращена в работу', 'success') }}
        />
      )}
      {action === 'cancel' && (
        <ReasonModal
          title="Отозвать заявку"
          label="Комментарий (необязательно)"
          placeholder="Разобрался сам / подал ошибочно"
          submitLabel="Отозвать"
          min={0}
          optional
          onClose={() => setAction(null)}
          onSubmit={async (text) => { await helpdeskApi.cancel(id, text); reload(); setAction(null); toast.show('Заявка отозвана', 'success') }}
        />
      )}
      {action === 'confirm' && (
        <ConfirmModal
          onClose={() => setAction(null)}
          onConfirm={async (comment) => { await helpdeskApi.confirm(id, comment); reload(); setAction(null); toast.show('Заявка закрыта', 'success') }}
        />
      )}
      {action === 'delete' && (
        <Modal open onClose={() => setAction(null)} title="Удалить заявку?"
          footer={<>
            <button className="btn" onClick={() => setAction(null)}>Отмена</button>
            <button className="btn btn--danger" onClick={() => void helpdeskApi.remove(id)
              .then(() => { onChanged(); onClose(); toast.show('Заявка удалена', 'success') })
              .catch((e: Error) => toast.show(e.message, 'error'))}>
              Удалить безвозвратно
            </button>
          </>}>
          <p className="small muted">
            Заявка №{t.number}/{t.year} будет удалена вместе с перепиской и вложениями.
            Действие остаётся в журнале событий.
          </p>
        </Modal>
      )}
    </Modal>
  )
}

function TakeModal({ ticket, staff, onClose, onSubmit }: {
  ticket: Ticket
  staff: SysadminUser[]
  onClose: () => void
  onSubmit: (assigneeId: string | undefined, comment?: string) => Promise<void>
}) {
  const me = useAuthStore((st) => st.user)
  const toast = useToast()
  const [assigneeId, setAssigneeId] = useState<string>(me?.id ?? '')
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const others = staff.filter((u) => u.id !== me?.id)

  const submit = async () => {
    setBusy(true)
    try {
      await onSubmit(assigneeId === me?.id ? undefined : assigneeId, comment.trim() || undefined)
    } catch (e) {
      toast.show((e as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open onClose={onClose} title="Взять заявку в работу"
      footer={<>
        <button className="btn" onClick={onClose}>Отмена</button>
        <button className="btn btn--success" disabled={busy || !assigneeId} onClick={() => void submit()}>
          {busy ? '…' : assigneeId === me?.id ? 'Взять в работу' : 'Передать'}
        </button>
      </>}>
      <div className="stack" style={{ gap: 12 }}>
        <p className="small muted">
          Заявка «{ticket.title}»{ticket.location ? ` · ${ticket.location}` : ''}.
          {ticket.assignee && ticket.assignee.id !== me?.id
            ? ` Сейчас исполнитель — ${shortName(ticket.assignee.full_name)}.`
            : ' Исполнитель ещё не назначен.'}
        </p>
        <label className="field">
          <span className="field__label">Исполнитель</span>
          <select className="input" value={assigneeId} onChange={(e) => setAssigneeId(e.target.value)}>
            <option value={me?.id ?? ''}>Я ({shortName(me?.full_name)})</option>
            {others.map((u) => (
              <option key={u.id} value={u.id}>
                {u.full_name}{u.positions.length ? ` — ${u.positions.join(', ')}` : ''}
              </option>
            ))}
          </select>
          <span className="field__hint">
            Передать заявку можно только системному администратору — список назначает Владелец
          </span>
        </label>
        <label className="field">
          <span className="field__label">Комментарий (необязательно)</span>
          <textarea className="input" rows={3} value={comment} maxLength={4000}
                    onChange={(e) => setComment(e.target.value)} />
        </label>
      </div>
    </Modal>
  )
}

function ReasonModal({ title, label, placeholder, submitLabel, onSubmit, onClose, danger, min = 3, optional }: {
  title: string
  label: string
  placeholder?: string
  submitLabel: string
  onSubmit: (text: string) => Promise<void>
  onClose: () => void
  danger?: boolean
  min?: number
  optional?: boolean
}) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const toast = useToast()
  const valid = optional || text.trim().length >= min

  return (
    <Modal open onClose={onClose} title={title}
      footer={<>
        <button className="btn" onClick={onClose}>Отмена</button>
        <button
          className={danger ? 'btn btn--danger' : 'btn btn--primary'}
          disabled={!valid || busy}
          onClick={() => {
            setBusy(true)
            onSubmit(text.trim())
              .catch((e: Error) => toast.show(e.message, 'error'))
              .finally(() => setBusy(false))
          }}
        >
          {busy ? '…' : submitLabel}
        </button>
      </>}>
      <label className="field">
        <span className="field__label">{label}</span>
        <textarea className="input" rows={4} value={text} maxLength={4000} placeholder={placeholder}
                  onChange={(e) => setText(e.target.value)} />
        {!optional && <span className="field__hint">Не менее {min} {plural(min, 'символа', 'символов', 'символов')}</span>}
      </label>
    </Modal>
  )
}

function ConfirmModal({ onConfirm, onClose }: { onConfirm: (comment?: string) => Promise<void>; onClose: () => void }) {
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const toast = useToast()
  return (
    <Modal open onClose={onClose} title="Подтвердить выполнение"
      footer={<>
        <button className="btn" onClick={onClose}>Отмена</button>
        <button className="btn btn--success" disabled={busy}
                onClick={() => {
                  setBusy(true)
                  onConfirm(comment.trim() || undefined)
                    .catch((e: Error) => toast.show(e.message, 'error'))
                    .finally(() => setBusy(false))
                }}>
          {busy ? '…' : 'Да, всё работает'}
        </button>
      </>}>
      <p className="small muted">
        Заявка будет закрыта. Если проблема осталась — нажмите «Не решено», и заявка вернётся
        в работу к системному администратору.
      </p>
      <label className="field">
        <span className="field__label">Комментарий (необязательно)</span>
        <textarea className="input" rows={2} value={comment} onChange={(e) => setComment(e.target.value)} />
      </label>
    </Modal>
  )
}
