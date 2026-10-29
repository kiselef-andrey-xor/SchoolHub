/** Журнал служебных записок (модуль 2 ТЗ).
 *  Цикл: черновик → на согласовании → утверждена (подпись кодом TOTP, всегда) → задача исполнителю → выполнена.
 *  Отклонение — возврат автору с обязательным комментарием. Печатная форма с визой, экспорт CSV. */
import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { notesApi, usersApi } from '@/api/endpoints'
import { qk } from '@/api/query'
import { Modal } from '@/components/Modal'
import { PepSignModal } from '@/components/PepSignModal'
import { DocLink, DocList } from '@/components/DocLink'
import { Badge, EmptyState, ErrorState, Spinner, type Tone } from '@/components/ui'
import { useToast } from '@/components/Toast'
import { formatDate, formatDateTime, shortName } from '@/lib/format'
import { CAP, can } from '@/lib/permissions'
import { useAuthStore } from '@/store/auth'
import { SIGN_METHOD_LABELS, NOTE_STATUS_LABELS, type NoteStatus, type ServiceNote } from '@/types'

const STATUS_TONE: Record<NoteStatus, Tone> = {
  DRAFT: 'muted',
  ON_APPROVAL: 'warn',
  APPROVED: 'ok',
  REJECTED: 'danger',
  COMPLETED: 'brand',
}

export function NotesPage() {
  const toast = useToast()
  const qc = useQueryClient()
  const me = useAuthStore((st) => st.user)
  const isStaff = Boolean(me && can(me.capabilities, CAP.notesManage))

  const [status, setStatus] = useState<NoteStatus | ''>('')
  const [q, setQ] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [detailId, setDetailId] = useState<string | null>(null)
  const [signNote, setSignNote] = useState<ServiceNote | null>(null)
  const [rejectNote, setRejectNote] = useState<ServiceNote | null>(null)
  const [deleteNote, setDeleteNote] = useState<ServiceNote | null>(null)

  const listQ = useQuery({
    queryKey: qk('/notes', { status, q }),
    queryFn: () => notesApi.list({ status, q: q || undefined }),
  })

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['/notes'] })
    void qc.invalidateQueries({ queryKey: ['/tasks'] })
  }

  const submitM = useMutation({
    mutationFn: (id: string) => notesApi.submit(id),
    onSuccess: () => { invalidate(); toast.show('Записка отправлена на согласование', 'success') },
    onError: (e: Error) => toast.show(e.message, 'error'),
  })
  const approveM = useMutation({
    mutationFn: ({ id, code, word }: { id: string; code: string; word?: string }) =>
      notesApi.approve(id, { code, word }),
    onSuccess: (n) => {
      invalidate(); setSignNote(null)
      toast.show(n.task_id ? 'Утверждена, задача исполнителю создана' : 'Утверждена', 'success')
    },
    onError: (e: Error) => toast.show(e.message, 'error'),
  })
  const rejectM = useMutation({
    mutationFn: ({ id, comment }: { id: string; comment: string }) => notesApi.reject(id, comment),
    onSuccess: () => { invalidate(); setRejectNote(null); toast.show('Отклонена, возвращено автору', 'success') },
    onError: (e: Error) => toast.show(e.message, 'error'),
  })
  const delM = useMutation({
    mutationFn: (id: string) => notesApi.remove(id),
    onSuccess: () => { invalidate(); setDeleteNote(null); toast.show('Записка удалена', 'success') },
    onError: (e: Error) => toast.show(e.message, 'error'),
  })

  /** Печать через авторизованный запрос: прямая ссылка в новой вкладке отдаёт 401.
   *  Форма открывается БЕЗ автопечати — её сначала читают, печать запускают кнопкой. */
  const openPrint = async (id: string, autoPrint = false) => {
    try {
      const blob = await notesApi.printHtml(id, autoPrint)
      const url = URL.createObjectURL(new Blob([blob], { type: 'text/html; charset=utf-8' }))
      const w = window.open(url, '_blank')
      if (!w) toast.show('Браузер заблокировал окно печати — разрешите всплывающие окна', 'error')
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
    } catch (e) {
      toast.show((e as Error).message, 'error')
    }
  }

  const canDelete = (n: ServiceNote) =>
    me?.role === 'OWNER' ||
    Boolean(me?.notes_full_access) ||
    (n.author?.id === me?.id && (n.status === 'DRAFT' || n.status === 'REJECTED'))

  const rows = listQ.data ?? []

  return (
    <div className="page">
      <div className="page__head">
        <h1>Служебные записки</h1>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn" onClick={() => void notesApi.exportCsv().catch((e: Error) => toast.show(e.message, 'error'))}>
            Экспорт CSV
          </button>
          <button className="btn btn--primary" onClick={() => setCreateOpen(true)}>+ Новая записка</button>
        </div>
      </div>

      <div className="filters row" style={{ gap: 8, flexWrap: 'wrap' }}>
        <select className="input input--sm" value={status} onChange={(e) => setStatus(e.target.value as NoteStatus | '')}>
          <option value="">Все статусы</option>
          {(Object.keys(NOTE_STATUS_LABELS) as NoteStatus[]).map((s) => (
            <option key={s} value={s}>{NOTE_STATUS_LABELS[s]}</option>
          ))}
        </select>
        <input
          className="input input--sm"
          placeholder="Поиск по теме…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>

      {listQ.isLoading ? <Spinner /> : listQ.isError ? <ErrorState error={listQ.error} /> : rows.length === 0 ? (
        <EmptyState icon="📝" title="Записок нет" hint="Создайте первую служебную записку — она пройдёт согласование и станет задачей исполнителю." />
      ) : (
        <table className="table">
          <thead>
            <tr><th>№</th><th>Тема</th><th>Автор</th><th>Исполнитель</th><th>Согласующий</th><th>Срок</th><th>Статус</th><th>Подпись</th><th /></tr>
          </thead>
          <tbody>
            {rows.map((n) => (
              <tr key={n.id}>
                <td>{n.number ? `${n.number}/${n.year}` : '—'}</td>
                <td><button className="link" onClick={() => setDetailId(n.id)}>{n.subject}</button></td>
                <td>{shortName(n.author?.full_name)}</td>
                <td>{shortName(n.executor?.full_name)}</td>
                <td title={n.approver ? 'Назначенный согласующий' : 'Утвердить может любой из администрации'}>
                  {n.approver ? shortName(n.approver.full_name) : 'администрация'}
                </td>
                <td>{n.deadline ? formatDate(n.deadline) : '—'}</td>
                <td><Badge tone={STATUS_TONE[n.status]}>{NOTE_STATUS_LABELS[n.status]}</Badge></td>
                <td>{n.approved_sign_method ? SIGN_METHOD_LABELS[n.approved_sign_method] ?? n.approved_sign_method : '—'}</td>
                <td className="row" style={{ gap: 6 }}>
                  {n.status === 'DRAFT' && n.author?.id === me?.id && (
                    <button className="btn btn--sm" onClick={() => submitM.mutate(n.id)}>На согласование</button>
                  )}
                  {n.status === 'ON_APPROVAL' && isStaff && (
                    <>
                      <button className="btn btn--sm btn--success" onClick={() => setSignNote(n)}>Утвердить</button>
                      <button className="btn btn--sm btn--danger" onClick={() => setRejectNote(n)}>Отклонить</button>
                    </>
                  )}
                  {n.task_id && <Link className="btn btn--sm" to={`/tasks/${n.task_id}`}>Задача</Link>}
                  <button className="btn btn--sm" onClick={() => void openPrint(n.id)}>Печать</button>
                  {canDelete(n) && (
                    <button className="btn btn--sm btn--danger" onClick={() => setDeleteNote(n)}>Удалить</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {createOpen && <NoteCreateModal onClose={() => setCreateOpen(false)} onDone={invalidate} />}
      {detailId && <NoteDetailModal id={detailId} onClose={() => setDetailId(null)} onChanged={invalidate} />}

      <PepSignModal
        open={signNote !== null}
        title="Утверждение служебной записки"
        description={`Подтвердите действие одноразовым кодом из аутентификатора${signNote ? `: «${signNote.subject}»` : ''}. Утверждение всегда требует подпись кодом.`}
        danger={false}
        submitLabel="Подписать и утвердить"
        onClose={() => setSignNote(null)}
        onConfirm={async ({ code, word }) => {
          if (!signNote) return
          await approveM.mutateAsync({ id: signNote.id, code, word })
        }}
      />

      {rejectNote && <RejectModal note={rejectNote} onClose={() => setRejectNote(null)} onSubmit={(c) => rejectM.mutate({ id: rejectNote.id, comment: c })} busy={rejectM.isPending} />}

      {deleteNote && (
        <Modal open onClose={() => setDeleteNote(null)} title={`Удалить записку «${deleteNote.subject}»?`}
          footer={<>
            <button className="btn" onClick={() => setDeleteNote(null)}>Отмена</button>
            <button className="btn btn--danger" disabled={delM.isPending} onClick={() => delM.mutate(deleteNote.id)}>
              {delM.isPending ? 'Удаляем…' : 'Удалить'}
            </button>
          </>}>
          <p className="small muted">
            Записка будет удалена безвозвратно вместе с вложениями.
            Задача, созданная из утверждённой записки, останется у исполнителя.
          </p>
        </Modal>
      )}
    </div>
  )
}

function RejectModal({ note, onClose, onSubmit, busy }: { note: ServiceNote; onClose: () => void; onSubmit: (c: string) => void; busy: boolean }) {
  const [comment, setComment] = useState('')
  return (
    <Modal open onClose={onClose} title={`Отклонить записку «${note.subject}»`}
      footer={<>
        <button className="btn" onClick={onClose}>Отмена</button>
        <button className="btn btn--danger" disabled={comment.trim().length < 3 || busy} onClick={() => onSubmit(comment.trim())}>Вернуть автору</button>
      </>}>
      <label className="field">
        <span className="field__label">Комментарий автору (обязательно)</span>
        <textarea className="input" rows={4} value={comment} onChange={(e) => setComment(e.target.value)} />
      </label>
    </Modal>
  )
}

function NoteCreateModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const toast = useToast()
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [executorId, setExecutorId] = useState('')
  const [approverId, setApproverId] = useState('')
  const [deadline, setDeadline] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  const executorsQ = useQuery({ queryKey: qk('/users/options'), queryFn: () => usersApi.options() })
  const approvers = (executorsQ.data ?? []).filter((u) => u.role === 'ADMIN' || u.role === 'OWNER')

  const createM = useMutation({
    mutationFn: async () => {
      const note = await notesApi.create({
        subject: subject.trim(),
        body: body.trim(),
        executor_id: executorId || null,
        approver_id: approverId || null,
        deadline: deadline ? new Date(deadline).toISOString() : null,
      })
      const f = fileRef.current?.files?.[0]
      if (f) await notesApi.addFile(note.id, f)
      return note
    },
    onSuccess: () => { onDone(); onClose(); toast.show('Черновик создан', 'success') },
    onError: (e: Error) => toast.show(e.message, 'error'),
  })

  return (
    <Modal open wide onClose={onClose} title="Новая служебная записка"
      footer={<>
        <button className="btn" onClick={onClose}>Отмена</button>
        <button className="btn btn--primary" disabled={createM.isPending || subject.trim().length < 3 || body.trim().length < 10}
          onClick={() => createM.mutate()}>{createM.isPending ? 'Сохраняем…' : 'Создать черновик'}</button>
      </>}>
      <div className="stack" style={{ gap: 12 }}>
        <label className="field">
          <span className="field__label">Тема</span>
          <input className="input" value={subject} onChange={(e) => setSubject(e.target.value)} maxLength={300} />
        </label>
        <label className="field">
          <span className="field__label">Текст записки</span>
          <textarea className="input" rows={8} value={body} onChange={(e) => setBody(e.target.value)} />
          <span className="field__hint">Не менее 10 символов</span>
        </label>
        <div className="row" style={{ gap: 12, flexWrap: 'wrap' }}>
          <label className="field" style={{ flex: 1, minWidth: 220 }}>
            <span className="field__label">Исполнитель</span>
            <select className="input" value={executorId} onChange={(e) => setExecutorId(e.target.value)}>
              <option value="">— выбрать —</option>
              {(executorsQ.data ?? []).map((u) => <option key={u.id} value={u.id}>{u.full_name}</option>)}
            </select>
          </label>
          <label className="field" style={{ flex: 1, minWidth: 220 }}>
            <span className="field__label">Кто утверждает</span>
            <select className="input" value={approverId} onChange={(e) => setApproverId(e.target.value)}>
              <option value="">— администрация (входящий пул) —</option>
              {approvers.map((u) => <option key={u.id} value={u.id}>{u.full_name}</option>)}
            </select>
            <span className="field__hint">
              Если выбран конкретный человек — утвердить сможет только он (и Владелец)
            </span>
          </label>
          <label className="field">
            <span className="field__label">Срок выполнения</span>
            <input className="input" type="date" value={deadline} onChange={(e) => setDeadline(e.target.value)} />
          </label>
        </div>
        <label className="field">
          <span className="field__label">Вложение (необязательно)</span>
          <input ref={fileRef} type="file" className="input" />
        </label>
      </div>
    </Modal>
  )
}

function NoteDetailModal({ id, onClose, onChanged }: { id: string; onClose: () => void; onChanged: () => void }) {
  const toast = useToast()
  const me = useAuthStore((st) => st.user)
  const detailQ = useQuery({ queryKey: qk(`/notes/${id}`), queryFn: () => notesApi.detail(id) })
  const d = detailQ.data

  const delM = useMutation({
    mutationFn: () => notesApi.remove(id),
    onSuccess: () => { onChanged(); onClose(); toast.show('Записка удалена', 'success') },
    onError: (e: Error) => toast.show(e.message, 'error'),
  })
  const canDel = Boolean(d) && (
    me?.role === 'OWNER' ||
    Boolean(me?.notes_full_access) ||
    (d!.author?.id === me?.id && (d!.status === 'DRAFT' || d!.status === 'REJECTED'))
  )
  /** вложения правит только автор и только в черновике/отклонённой — как на сервере */
  const canEditFiles = Boolean(d) && d!.author?.id === me?.id
    && (d!.status === 'DRAFT' || d!.status === 'REJECTED')

  const delFileM = useMutation({
    mutationFn: (fid: string) => notesApi.removeFile(id, fid),
    onSuccess: () => { onChanged(); void detailQ.refetch() },
    onError: (e: Error) => toast.show(e.message, 'error'),
  })
  const uploadM = useMutation({
    mutationFn: (f: File) => notesApi.addFile(id, f),
    onSuccess: () => { onChanged(); void detailQ.refetch() },
    onError: (e: Error) => toast.show(e.message, 'error'),
  })

  return (
    <Modal open wide onClose={onClose} title={d ? `Записка ${d.number ? `№ ${d.number}/${d.year}` : ''} — ${d.subject}` : 'Записка'}
      footer={<>
        {canDel && (
          <button className="btn btn--danger" disabled={delM.isPending} onClick={() => delM.mutate()}>
            {delM.isPending ? 'Удаляем…' : 'Удалить'}
          </button>
        )}
        <span className="grow" />
        <button className="btn" onClick={onClose}>Закрыть</button>
      </>}>
      {detailQ.isLoading ? <Spinner /> : detailQ.isError ? <ErrorState error={detailQ.error} /> : d && (
        <div className="stack" style={{ gap: 10 }}>
          <div className="row" style={{ gap: 12, flexWrap: 'wrap' }}>
            <Badge tone={STATUS_TONE[d.status]}>{NOTE_STATUS_LABELS[d.status]}</Badge>
            <span>Автор: {shortName(d.author?.full_name)}</span>
            <span>Исполнитель: {shortName(d.executor?.full_name)}</span>
            <span>Согласующий: {d.approver ? shortName(d.approver.full_name) : 'администрация'}</span>
            <span>Срок: {d.deadline ? formatDate(d.deadline) : '—'}</span>
          </div>
          {d.status === 'REJECTED' && d.reject_comment && (
            <div className="alert alert--error">Возврат автору: {d.reject_comment}</div>
          )}
          {d.approved_at && (
            <div className="alert alert--info">
              Утверждена {formatDateTime(d.approved_at)} · {shortName(d.approver?.full_name)} ·{' '}
              подпись: {SIGN_METHOD_LABELS[d.approved_sign_method ?? ''] ?? d.approved_sign_method ?? '—'}
            </div>
          )}
          <div style={{ whiteSpace: 'pre-wrap' }}>{d.body}</div>
          <div>
            <b>Документы</b>
            <span className="field__hint" style={{ marginLeft: 8 }}>
              клик по «Открыть» — документ в новой вкладке (PDF-копия готовится автоматически)
            </span>
            <DocList docs={d.files} empty={<span className="muted small">Вложений нет</span>}>
              {(f) => (
                <DocLink
                  doc={f}
                  onOpen={() => notesApi.openFile(d.id, f.id)}
                  onDownload={() => notesApi.downloadFile(d.id, f.id, f.original_name)}
                  onRemove={canEditFiles ? () => delFileM.mutate(f.id) : undefined}
                />
              )}
            </DocList>
          </div>
          {canEditFiles && (
            <label className="field">
              <span className="field__label">Добавить документ</span>
              <input
                type="file"
                className="input"
                onChange={(e) => {
                  const f = e.target.files?.[0]
                  if (f) uploadM.mutate(f)
                  e.target.value = ''   // повторная загрузка того же файла тоже должна сработать
                }}
              />
              <span className="field__hint">
                Офисные документы, изображения и архивы до 25 МБ. Копия в PDF создаётся автоматически,
                оригинал сохраняется.
              </span>
            </label>
          )}
          {d.task_id && <Link className="btn btn--sm" to={`/tasks/${d.task_id}`}>Перейти к задаче</Link>}
        </div>
      )}
    </Modal>
  )
}
