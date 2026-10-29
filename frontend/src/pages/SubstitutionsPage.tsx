/** Журнал замещений (модуль 3 ТЗ).
 *  Администрация создаёт замещение: отсутствующий → замещающий, причина, период.
 *  Флаги «передать задачи / планы / ВШК» описывают, ЧТО будет передано: сами объекты
 *  переходят замещающему ТОЛЬКО после его подтверждения (v1.11), а при отмене,
 *  завершении и удалении строки возвращаются отсутствующему.
 *  Период можно продлить (больничный продлили). Строки удаляет Владелец и назначенный
 *  им кадровик (грант `substitutions_access` — должность значения не имеет).
 *  Замещающий подтверждает простым кликом. Экспорт CSV для отчётности. */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { substitutionsApi, usersApi } from '@/api/endpoints'
import { qk } from '@/api/query'
import { Modal } from '@/components/Modal'
import { Badge, EmptyState, ErrorState, Spinner, type Tone } from '@/components/ui'
import { useToast } from '@/components/Toast'
import { formatDate, formatDateTime, shortName } from '@/lib/format'
import { CAP, can } from '@/lib/permissions'
import { useAuthStore } from '@/store/auth'
import { SUBSTITUTION_STATUS_LABELS, type Substitution, type SubstitutionStatus } from '@/types'

const STATUS_TONE: Record<SubstitutionStatus, Tone> = {
  ACTIVE: 'warn',
  CONFIRMED: 'ok',
  ENDED: 'muted',
  CANCELLED: 'danger',
}

/** Что именно передаётся по замещению (для значков в таблице). */
function transferObjects(s: Substitution): string[] {
  return [
    s.transfer_tasks ? 'задачи' : '',
    s.transfer_plans ? 'планы' : '',
    s.transfer_vshk ? 'ВШК' : '',
  ].filter(Boolean)
}

/** Состояние передачи: ждём подтверждения / передано / возвращено. */
function TransferState({ sub }: { sub: Substitution }) {
  const objects = transferObjects(sub)
  if (objects.length === 0) return <div className="tiny muted">без передачи объектов</div>
  if (sub.transferred_at) {
    return <div className="tiny muted">передано {formatDateTime(sub.transferred_at)}</div>
  }
  if ((sub.status === 'ACTIVE')) {
    return <div className="tiny muted">передастся после подтверждения</div>
  }
  if (sub.note?.includes('возвращено')) {
    return <div className="tiny muted">возвращено отсутствующему</div>
  }
  return <div className="tiny muted">не передавалось</div>
}

export function SubstitutionsPage() {
  const toast = useToast()
  const qc = useQueryClient()
  const me = useAuthStore((st) => st.user)
  /** оформляет, продлевает, завершает и отменяет — администрация и Владелец */
  const isStaff = Boolean(me && can(me.capabilities, CAP.substitutionsManage))
  /** кадровик (грант Владельца): видит журнал целиком и удаляет строки */
  const isHr = Boolean(me?.substitutions_access)
  const canDelete = me?.role === 'OWNER' || isHr
  const canExport = isStaff || isHr

  const [status, setStatus] = useState<SubstitutionStatus | ''>('')
  const [ongoing, setOngoing] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [extendSub, setExtendSub] = useState<Substitution | null>(null)
  /** подтверждение закрывающего действия: отмена / завершение / удаление строки */
  const [ask, setAsk] = useState<{ kind: 'cancel' | 'end' | 'delete'; sub: Substitution } | null>(null)

  const listQ = useQuery({
    queryKey: qk('/substitutions', { status, ongoing }),
    queryFn: () => substitutionsApi.list({ status: status || undefined, ongoing: ongoing || undefined }),
  })

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['/substitutions'] })
    void qc.invalidateQueries({ queryKey: ['/tasks'] })
    void qc.invalidateQueries({ queryKey: ['/plans'] })
  }

  const confirmM = useMutation({
    mutationFn: (id: string) => substitutionsApi.confirm(id),
    onSuccess: () => { invalidate(); toast.show('Замещение подтверждено, объекты переданы', 'success') },
    onError: (e: Error) => toast.show(e.message, 'error'),
  })

  /** одно действие на три кнопки: завершить / отменить / удалить строку */
  const closeM = useMutation({
    mutationFn: async (v: { kind: 'cancel' | 'end' | 'delete'; sub: Substitution }) => {
      if (v.kind === 'end') return substitutionsApi.end(v.sub.id)
      if (v.kind === 'cancel') return substitutionsApi.cancel(v.sub.id)
      await substitutionsApi.remove(v.sub.id)
      return null
    },
    onSuccess: (_r, v) => {
      invalidate()
      setAsk(null)
      const msg = v.kind === 'delete'
        ? 'Строка замещения удалена'
        : v.kind === 'end' ? 'Замещение завершено' : 'Замещение отменено'
      toast.show(transferObjects(v.sub).length ? `${msg}; объекты возвращены отсутствующему` : msg, 'success')
    },
    onError: (e: Error) => toast.show(e.message, 'error'),
  })

  const rows = listQ.data ?? []

  return (
    <div className="page">
      <div className="page__head">
        <h1>Замещения</h1>
        <div className="row" style={{ gap: 8 }}>
          {canExport && (
            <button className="btn" onClick={() => void substitutionsApi.exportCsv().catch((e: Error) => toast.show(e.message, 'error'))}>
              Экспорт CSV
            </button>
          )}
          {isStaff && <button className="btn btn--primary" onClick={() => setCreateOpen(true)}>+ Новое замещение</button>}
        </div>
      </div>

      <div className="alert alert--info small">
        Задачи, планы и поручения ВШК переходят замещающему <strong>только после его подтверждения</strong>.
        При отмене, завершении и удалении строки они возвращаются отсутствующему сотруднику.
      </div>

      <div className="filters row" style={{ gap: 8, flexWrap: 'wrap' }}>
        <select className="input input--sm" value={status} onChange={(e) => setStatus(e.target.value as SubstitutionStatus | '')}>
          <option value="">Все статусы</option>
          {(Object.keys(SUBSTITUTION_STATUS_LABELS) as SubstitutionStatus[]).map((s) => (
            <option key={s} value={s}>{SUBSTITUTION_STATUS_LABELS[s]}</option>
          ))}
        </select>
        <label className="row" style={{ gap: 4 }}>
          <input type="checkbox" checked={ongoing} onChange={(e) => setOngoing(e.target.checked)} />
          <span>только текущие</span>
        </label>
        {!isStaff && !isHr && (
          <span className="tiny muted">Показаны только замещения, в которых вы участвуете.</span>
        )}
      </div>

      {listQ.isLoading ? <Spinner /> : listQ.isError ? <ErrorState error={listQ.error} /> : rows.length === 0 ? (
        <EmptyState icon="🔁" title="Замещений нет" hint="Администрация может оформить замещение на время отсутствия сотрудника." />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Отсутствующий</th><th>Замещающий</th><th>Период</th><th>Причина</th>
                <th>Передача</th><th>Статус</th><th>Действия</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => {
                const live = s.status === 'ACTIVE' || s.status === 'CONFIRMED'
                const canConfirm = s.status === 'ACTIVE' && me?.id === s.substitute_user.id
                return (
                  <tr key={s.id}>
                    <td>{shortName(s.absent_user.full_name)}</td>
                    <td>{shortName(s.substitute_user.full_name)}</td>
                    <td style={{ whiteSpace: 'nowrap' }}>
                      {formatDate(s.start_date)} — {s.end_date ? formatDate(s.end_date) : 'бессрочно'}
                    </td>
                    <td title={s.reason}>{s.reason.length > 40 ? s.reason.slice(0, 40) + '…' : s.reason}</td>
                    <td>
                      <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
                        {transferObjects(s).map((o) => <Badge key={o} tone="neutral">{o}</Badge>)}
                      </div>
                      <TransferState sub={s} />
                    </td>
                    <td>
                      <Badge tone={STATUS_TONE[s.status]}>{SUBSTITUTION_STATUS_LABELS[s.status]}</Badge>
                      {s.confirmed_at && <div className="tiny muted">подтв. {formatDateTime(s.confirmed_at)}</div>}
                    </td>
                    {/* ВАЖНО: flex-класс .row нельзя вешать на сам <td> — ячейка выпадает
                        из табличной раскладки и кнопки «уезжают» (v1.11) */}
                    <td>
                      <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
                        {canConfirm && (
                          <button className="btn btn--sm btn--success" disabled={confirmM.isPending}
                            onClick={() => confirmM.mutate(s.id)}>
                            Подтвердить
                          </button>
                        )}
                        {isStaff && live && (
                          <>
                            <button className="btn btn--sm" onClick={() => setExtendSub(s)}>Продлить</button>
                            <button className="btn btn--sm" onClick={() => setAsk({ kind: 'end', sub: s })}>Завершить</button>
                            <button className="btn btn--sm btn--danger" onClick={() => setAsk({ kind: 'cancel', sub: s })}>Отменить</button>
                          </>
                        )}
                        {canDelete && (
                          <button className="btn btn--sm btn--ghost" title="Удалить строку замещения (Владелец или кадровик)"
                            onClick={() => setAsk({ kind: 'delete', sub: s })}>
                            Удалить
                          </button>
                        )}
                        {!canConfirm && !(isStaff && live) && !canDelete && <span className="tiny muted">—</span>}
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {createOpen && <SubCreateModal onClose={() => setCreateOpen(false)} onDone={invalidate} />}
      {extendSub && (
        <ExtendModal
          sub={extendSub}
          onClose={() => setExtendSub(null)}
          onDone={() => { setExtendSub(null); invalidate() }}
        />
      )}

      {ask && (
        <Modal open onClose={() => setAsk(null)} width="560px"
          title={ask.kind === 'delete'
            ? 'Удалить строку замещения?'
            : ask.kind === 'end' ? 'Завершить замещение?' : 'Отменить замещение?'}
          footer={<>
            <button className="btn" onClick={() => setAsk(null)}>Не сейчас</button>
            <button className={ask.kind === 'cancel' ? 'btn btn--danger' : 'btn btn--primary'}
              disabled={closeM.isPending} onClick={() => closeM.mutate(ask)}>
              {closeM.isPending ? 'Выполняем…'
                : ask.kind === 'delete' ? 'Удалить строку'
                  : ask.kind === 'end' ? 'Завершить' : 'Отменить замещение'}
            </button>
          </>}>
          <div className="stack">
            <p className="small">
              {shortName(ask.sub.absent_user.full_name)} → {shortName(ask.sub.substitute_user.full_name)},
              {' '}{formatDate(ask.sub.start_date)} — {ask.sub.end_date ? formatDate(ask.sub.end_date) : 'бессрочно'}.
            </p>
            {transferObjects(ask.sub).length > 0 && ask.sub.status !== 'CANCELLED' && (
              <div className="alert alert--warn small">
                Переданные объекты ({transferObjects(ask.sub).join(', ')}) вернутся
                к {shortName(ask.sub.absent_user.full_name)}. Закрытые задачи не трогаем.
              </div>
            )}
            {ask.kind === 'delete' && (
              <p className="small muted">
                Строка будет удалена из журнала безвозвратно; действие и возврат объектов
                останутся в журнале событий и в уведомлениях участников.
              </p>
            )}
            {ask.kind === 'cancel' && (
              <p className="small muted">
                Отмена означает, что замещение не состоялось. Если сотрудник просто вышел
                на работу — используйте «Завершить».
              </p>
            )}
          </div>
        </Modal>
      )}
    </div>
  )
}

/** Продление периода: больничный продлили — сдвигаем окончание, объекты не трогаем. */
function ExtendModal({ sub, onClose, onDone }: { sub: Substitution; onClose: () => void; onDone: () => void }) {
  const toast = useToast()
  const [end, setEnd] = useState(sub.end_date ? sub.end_date.slice(0, 10) : '')
  const [comment, setComment] = useState('')

  const extendM = useMutation({
    mutationFn: () => substitutionsApi.extend(sub.id, new Date(end).toISOString(), comment.trim() || undefined),
    onSuccess: () => { onDone(); toast.show('Период замещения продлён', 'success') },
    onError: (e: Error) => toast.show(e.message, 'error'),
  })

  return (
    <Modal open onClose={onClose} title="Продлить замещение" width="520px"
      footer={<>
        <button className="btn" onClick={onClose}>Отмена</button>
        <button className="btn btn--primary" disabled={!end || extendM.isPending} onClick={() => extendM.mutate()}>
          {extendM.isPending ? 'Продлеваем…' : 'Продлить'}
        </button>
      </>}>
      <div className="stack" style={{ gap: 12 }}>
        <p className="small muted">
          {shortName(sub.absent_user.full_name)} → {shortName(sub.substitute_user.full_name)} ·
          с {formatDate(sub.start_date)}
          {sub.end_date ? ` по ${formatDate(sub.end_date)}` : ' (бессрочно)'}
        </p>
        <label className="field">
          <span className="field__label">Новая дата окончания</span>
          <input className="input" type="date" value={end} onChange={(e) => setEnd(e.target.value)}
            min={sub.end_date ? sub.end_date.slice(0, 10) : undefined} />
        </label>
        <label className="field">
          <span className="field__label">Комментарий (необязательно)</span>
          <input className="input" value={comment} onChange={(e) => setComment(e.target.value)} maxLength={500}
            placeholder="Например: больничный продлён до 12.11" />
        </label>
        <div className="alert alert--info small">
          Переданные объекты остаются у замещающего; участники получат уведомление о новом периоде.
        </div>
      </div>
    </Modal>
  )
}

function SubCreateModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const toast = useToast()
  const [absentId, setAbsentId] = useState('')
  const [substId, setSubstId] = useState('')
  const [reason, setReason] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [tTasks, setTTasks] = useState(false)
  const [tPlans, setTPlans] = useState(false)
  const [tVshk, setTVshk] = useState(false)

  const usersQ = useQuery({ queryKey: qk('/users/options/all'), queryFn: () => usersApi.options() })

  const createM = useMutation({
    mutationFn: () => substitutionsApi.create({
      absent_user_id: absentId,
      substitute_user_id: substId,
      reason: reason.trim(),
      start_date: new Date(start).toISOString(),
      end_date: end ? new Date(end).toISOString() : null,
      transfer_tasks: tTasks,
      transfer_plans: tPlans,
      transfer_vshk: tVshk,
    }),
    onSuccess: () => { onDone(); onClose(); toast.show('Замещение создано, участникам отправлены уведомления', 'success') },
    onError: (e: Error) => toast.show(e.message, 'error'),
  })

  const ok = absentId && substId && absentId !== substId && reason.trim().length >= 3 && start

  return (
    <Modal open onClose={onClose} title="Новое замещение"
      footer={<>
        <button className="btn" onClick={onClose}>Отмена</button>
        <button className="btn btn--primary" disabled={!ok || createM.isPending} onClick={() => createM.mutate()}>
          {createM.isPending ? 'Создаём…' : 'Создать'}
        </button>
      </>}>
      <div className="stack" style={{ gap: 12 }}>
        <div className="row" style={{ gap: 12, flexWrap: 'wrap' }}>
          <label className="field" style={{ flex: 1, minWidth: 220 }}>
            <span className="field__label">Отсутствующий сотрудник</span>
            <select className="input" value={absentId} onChange={(e) => setAbsentId(e.target.value)}>
              <option value="">— выбрать —</option>
              {(usersQ.data ?? []).map((u) => <option key={u.id} value={u.id}>{u.full_name}</option>)}
            </select>
          </label>
          <label className="field" style={{ flex: 1, minWidth: 220 }}>
            <span className="field__label">Замещающий</span>
            <select className="input" value={substId} onChange={(e) => setSubstId(e.target.value)}>
              <option value="">— выбрать —</option>
              {(usersQ.data ?? []).filter((u) => u.id !== absentId).map((u) => <option key={u.id} value={u.id}>{u.full_name}</option>)}
            </select>
          </label>
        </div>
        <div className="row" style={{ gap: 12, flexWrap: 'wrap' }}>
          <label className="field"><span className="field__label">Начало</span>
            <input className="input" type="date" value={start} onChange={(e) => setStart(e.target.value)} /></label>
          <label className="field"><span className="field__label">Окончание (необязательно)</span>
            <input className="input" type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></label>
        </div>
        <label className="field">
          <span className="field__label">Причина отсутствия</span>
          <textarea className="input" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>
        <fieldset>
          <legend>Передать замещающему</legend>
          <label className="row" style={{ gap: 6 }}><input type="checkbox" checked={tTasks} onChange={(e) => setTTasks(e.target.checked)} /> активные задачи (с записью в историю)</label>
          <label className="row" style={{ gap: 6 }}><input type="checkbox" checked={tPlans} onChange={(e) => setTPlans(e.target.checked)} /> планы работы</label>
          <label className="row" style={{ gap: 6 }}><input type="checkbox" checked={tVshk} onChange={(e) => setTVshk(e.target.checked)} /> поручения ВШК</label>
        </fieldset>
        <div className="alert alert--info">
          Замещающий подтвердит замещение простым кликом — и только тогда отмеченные объекты
          перейдут к нему (с записью в историю задач и уведомлениями). Если замещение отменят
          или завершат, объекты вернутся отсутствующему.
        </div>
      </div>
    </Modal>
  )
}
