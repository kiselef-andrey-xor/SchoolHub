/** Карточка задачи: статус-машина, исполнители и их отметки «выполнено», вложения, комментарии, история. */
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { blobWithMime, downloadAuthorized } from '@/api/client'
import { tasksApi } from '@/api/endpoints'
import { qk } from '@/api/query'
import { Modal } from '@/components/Modal'
import { TaskFormModal } from '@/components/TaskFormModal'
import { UserAvatar } from '@/components/UserAvatar'
import {
  Avatar,
  Badge,
  EmptyState,
  ErrorState,
  PriorityBadge,
  Progress,
  Spinner,
  TaskStatusBadge,
} from '@/components/ui'
import { useToast } from '@/components/Toast'
import { CAP, can } from '@/lib/permissions'
import { TASK_STATUS_LABEL, deadlineLabel, formatDateIso, formatDateTime, formatSize, plural, recurrenceLabel, relativeTime, shortName } from '@/lib/format'
import { useAuthStore } from '@/store/auth'
import type { TaskStatusStored } from '@/types'

const NEXT_STATUSES: Record<TaskStatusStored, TaskStatusStored[]> = {
  NEW: ['IN_PROGRESS', 'DONE'],
  IN_PROGRESS: ['DONE', 'NEW'],
  DONE: ['IN_PROGRESS'],
}

export function TaskDetailPage() {
  const { id = '' } = useParams()
  const user = useAuthStore((s) => s.user)!
  const toast = useToast()
  const qc = useQueryClient()
  const fileInput = useRef<HTMLInputElement>(null)
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const [editOpen, setEditOpen] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [preview, setPreview] = useState<{ id: string; name: string; mime: string } | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  // фактический MIME от сервера: DOCX может прийти PDF-ом (конвертация LibreOffice)
  const [previewMime, setPreviewMime] = useState<string | null>(null)

  const canDelete = can(user.capabilities, CAP.tasksDelete)
  const isStaff = can(user.capabilities, CAP.tasksReadAll)

  const { data: task, isLoading, error, refetch } = useQuery({
    queryKey: qk(`/tasks/${id}`),
    queryFn: () => tasksApi.detail(id),
  })

  // история повторов — только для повторяющихся задач (v1.10)
  const { data: occurrences } = useQuery({
    queryKey: qk(`/tasks/${id}/occurrences`),
    queryFn: () => tasksApi.occurrences(id),
    enabled: !!task && task.recurrence !== 'NONE',
  })

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['/tasks'] })
    void qc.invalidateQueries({ queryKey: qk(`/tasks/${id}`) })
    void qc.invalidateQueries({ queryKey: qk(`/tasks/${id}/occurrences`) })
    void qc.invalidateQueries({ queryKey: ['/dashboard'] })
    void qc.invalidateQueries({ queryKey: qk('/notifications/unread-count') })
  }

  // превью документа: тянем blob с авторизацией и показываем во всплывающем окне.
  // ВАЖНО: хук стоит ДО ранних return (isLoading / error / !task) — порядок хуков
  // между рендерами обязан быть постоянным, иначе React падает с ошибкой #310.
  useEffect(() => {
    if (!preview || !task) return
    let url: string | null = null
    let alive = true
    setPreviewMime(null)
    blobWithMime(`${tasksApi.fileDownloadPath(task.id, preview.id)}?inline=true`)
      .then((r) => {
        if (alive) {
          url = r.url
          setPreviewUrl(r.url)
          setPreviewMime(r.mime)
        }
      })
      .catch(() => {
        if (alive) setPreviewUrl(null)
      })
    return () => {
      alive = false
      if (url) URL.revokeObjectURL(url)
      setPreviewUrl(null)
      setPreviewMime(null)
    }
  }, [preview, task])

  if (isLoading) return <Spinner large label="Открываем задачу…" />
  if (error) return <ErrorState error={error} onRetry={() => void refetch()} />
  if (!task) return null

  const isAssignee = task.assignees.some((a) => a.user.id === user.id)
  const isCreator = task.creator?.id === user.id

  const marksTotal = task.assignees.length
  const marksPct = marksTotal ? (task.marks_done / marksTotal) * 100 : 0

  async function changeStatus(status: TaskStatusStored) {
    setBusy(true)
    try {
      await tasksApi.changeStatus(task!.id, status)
      toast.success(`Статус изменён на «${TASK_STATUS_LABEL[status]}»`)
      invalidate()
      void refetch()
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  async function toggleMark() {
    setBusy(true)
    try {
      const updated = task!.my_done
        ? await tasksApi.clearMyMark(task!.id)
        : await tasksApi.markMyDone(task!.id)
      if (!task!.my_done && updated.status === 'DONE') {
        toast.success('Все исполнители отметились — задача закрыта')
      } else {
        toast.success(task!.my_done ? 'Отметка снята' : `Отметка «выполнено» поставлена (${updated.marks_done} из ${marksTotal})`)
      }
      invalidate()
      void refetch()
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  async function onUpload(files: FileList | null) {
    if (!files || files.length === 0) return
    setBusy(true)
    try {
      for (const file of Array.from(files)) {
        await tasksApi.uploadFile(task!.id, file)
      }
      toast.success(files.length > 1 ? `Прикреплено файлов: ${files.length}` : `Файл «${files[0].name}» прикреплён`)
      invalidate()
      void refetch()
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
      if (fileInput.current) fileInput.current.value = ''
    }
  }

  async function removeFile(fileId: string, name: string) {
    if (!window.confirm(`Уверены, что хотите удалить файл «${name}» из задачи? Действие необратимо.`)) return
    try {
      await tasksApi.removeFile(task!.id, fileId)
      toast.success(`Файл «${name}» удалён`)
      invalidate()
      void refetch()
    } catch (err) {
      toast.error(err)
    }
  }

  async function submitComment(e: FormEvent) {
    e.preventDefault()
    if (!comment.trim()) return
    setBusy(true)
    try {
      await tasksApi.comment(task!.id, comment.trim())
      setComment('')
      invalidate()
      void refetch()
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  const canAddFiles = isStaff || isAssignee || isCreator

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div className="page-head__text">
          <div className="row" style={{ gap: 8, marginBottom: 6 }}>
            <Link className="tiny" to="/tasks">← Все задачи</Link>
            <TaskStatusBadge status={task.effective_status} />
            <PriorityBadge priority={task.priority} />
            {isAssignee && <Badge tone="brand">я исполнитель</Badge>}
            {marksTotal > 1 && (
              <Badge tone="muted">исполнителей: {marksTotal} · отметок {task.marks_done}/{marksTotal}</Badge>
            )}
          </div>
          <h1>{task.title}</h1>
          <div className="page-head__hint">
            создана {relativeTime(task.created_at)}
            {task.creator ? ` · постановщик ${shortName(task.creator.full_name)}` : ''}
            {task.completed_at ? ` · закрыта ${formatDateTime(task.completed_at)}` : ''}
          </div>
        </div>

        <div className="page-head__actions">
          {/* отметка исполнителя — свой чекбокс «выполнено» */}
          {task.can_mark && task.status !== 'DONE' && (
            task.my_done ? (
              <button className="btn" disabled={busy} onClick={() => void toggleMark()}>↩ Снять отметку</button>
            ) : (
              <button className="btn btn--success" disabled={busy} onClick={() => void toggleMark()}>
                ✓ Я выполнил(а) свою часть
              </button>
            )
          )}
          {/* исполнитель НЕ из администрации: собственный вход в работу;
              у администрации ниже есть своя кнопка статуса — дубль убираем */}
          {isAssignee && !isStaff && task.status === 'NEW' && (
            <button
              className="btn"
              title="Вы исполнитель: перевести задачу в работу"
              disabled={busy}
              onClick={() => void changeStatus('IN_PROGRESS')}
            >
              ▶ Взять в работу
            </button>
          )}
          {/* ручное управление статусом задачи — у администрации и Владельца */}
          {isStaff &&
            NEXT_STATUSES[task.status].map((next) => (
              <button
                key={next}
                className={next === 'DONE' ? 'btn btn--success' : 'btn'}
                title={
                  next === 'DONE'
                    ? 'Администрация: закрыть задачу по всем исполнителям'
                    : next === 'IN_PROGRESS'
                      ? 'Администрация: перевести задачу в работу (статус всей задачи)'
                      : 'Администрация: вернуть задачу в новые'
                }
                disabled={busy}
                onClick={() => void changeStatus(next)}
              >
                {next === 'DONE' ? '✓ Закрыть задачу' : next === 'IN_PROGRESS' ? '▶ В работу' : '↩ Вернуть в новые'}
              </button>
            ))}
          {task.can_edit && <button className="btn" onClick={() => setEditOpen(true)}>✎ Редактировать</button>}
          {canDelete && (
            <button className="btn btn--danger" onClick={() => setConfirmDelete(true)}>🗑 Удалить</button>
          )}
        </div>
      </div>

      {task.is_overdue && (
        <div className="alert alert--error">
          <strong>Задача просрочена.</strong> Срок исполнения истёк {deadlineLabel(task.deadline)}.
          Статус «Просрочена» вычисляется автоматически — обновите дедлайн или закройте задачу.
        </div>
      )}

      <div className="grid grid--2" style={{ alignItems: 'start' }}>
        <section className="card">
          <header className="card__head"><h3>Содержание</h3></header>
          <div className="card__body stack" style={{ gap: 14 }}>
            <p style={{ whiteSpace: 'pre-wrap' }}>{task.description || <span className="muted">Описание не заполнено</span>}</p>

            <div>
              <div className="field__label">{task.recurrence !== 'NONE' ? 'Повторение' : 'Срок'}</div>
              {task.recurrence !== 'NONE' ? (
                <>
                  <div className="strong">🔁 {recurrenceLabel(task)}</div>
                  <div className="tiny muted">
                    {task.done_this_period
                      ? 'В текущем периоде выполнено — к следующему периоду задача снова станет новой'
                      : task.last_done_at
                        ? `Последний раз выполнено ${formatDateTime(task.last_done_at)}`
                        : 'Задача повторяется: отмечайте выполнение в каждом периоде'}
                  </div>
                  {(task.missed_streak ?? 0) > 0 && (
                    <div className="tiny" style={{ color: 'var(--danger)' }}>
                      ⚠ Пропущено подряд: {task.missed_streak} {plural(task.missed_streak ?? 0, 'период', 'периода', 'периодов')}
                    </div>
                  )}
                </>
              ) : (
                <>
                  <div className="strong">{task.deadline ? formatDateTime(task.deadline) : 'без срока'}</div>
                  <div className="tiny muted">{deadlineLabel(task.deadline, task.status === 'DONE')}</div>
                </>
              )}
            </div>

            <div>
              <div className="field__label">
                Исполнители и отметки «выполнено» {marksTotal > 0 && <Badge tone="muted">{task.marks_done}/{marksTotal}</Badge>}
              </div>
              {marksTotal === 0 ? (
                <div className="small muted">Исполнители не назначены.</div>
              ) : (
                <div className="mark-list">
                  {task.assignees.map((a) => (
                    <div className={a.done_at ? 'mark-row mark-row--done' : 'mark-row'} key={a.id}>
                      <UserAvatar user={a.user} size="sm" />
                      <div className="grow" style={{ minWidth: 0 }}>
                        <div className="user-chip__name">{a.user.full_name}</div>
                        <div className="tiny muted">{(a.user.positions || []).map((p) => p.title).join(', ') || 'сотрудник'}</div>
                      </div>
                      {a.done_at ? (
                        <Badge tone="ok">✓ Выполнено · {formatDateTime(a.done_at)}</Badge>
                      ) : (
                        <span className="tiny muted">отметки нет</span>
                      )}
                    </div>
                  ))}
                  <div style={{ marginTop: 4 }}>
                    <Progress value={marksPct} tone={marksPct === 100 ? 'ok' : undefined} />
                  </div>
                </div>
              )}
            </div>

            <div>
              <div className="row row--between">
                <div className="field__label">
                  Вложения <Badge tone="muted">{task.files.length}</Badge>
                </div>
                {canAddFiles && (
                  <button className="btn btn--sm" disabled={busy} onClick={() => fileInput.current?.click()}>
                    📎 Прикрепить файл
                  </button>
                )}
                <input
                  ref={fileInput}
                  type="file"
                  multiple
                  style={{ display: 'none' }}
                  onChange={(e) => void onUpload(e.target.files)}
                />
              </div>
              <div className="stack" style={{ gap: 8, marginTop: 8 }}>
                {task.files.length === 0 ? (
                  <div className="small muted">Файлов нет.</div>
                ) : (
                  task.files.map((f) => {
                    const canRemove = isStaff || isCreator || f.uploaded_by?.id === user.id
                    return (
                      <div className="file-row" key={f.id}>
                        <span style={{ fontSize: 17 }}>📄</span>
                        <div className="grow" style={{ minWidth: 0 }}>
                          <a
                            className="small strong"
                            href="#download"
                            onClick={(e) => {
                              e.preventDefault()
                              setPreview({ id: f.id, name: f.original_name, mime: f.mime_type })
                            }}
                            style={{ overflowWrap: 'anywhere' }}
                          >
                            {f.original_name}
                          </a>
                          <div className="tiny muted">
                            {formatSize(f.size_bytes)}
                            {f.uploaded_by ? ` · добавил(а) ${shortName(f.uploaded_by.full_name)}` : ''} · {relativeTime(f.created_at)}
                          </div>
                        </div>
                        {canRemove && (
                          <button className="btn btn--sm" disabled={busy} onClick={() => void removeFile(f.id, f.original_name)}>✕</button>
                        )}
                      </div>
                    )
                  })
                )}
              </div>
            </div>
          </div>
        </section>

        <div className="stack" style={{ gap: 16 }}>
          <section className="card">
            <header className="card__head">
              <h3>Комментарии</h3>
              <span className="badge badge--muted">{task.comments.length}</span>
            </header>
            <div className="card__body">
              {task.comments.length === 0 ? (
                <div className="small muted">Пока нет комментариев — уточните детали или сообщите о прогрессе.</div>
              ) : (
                task.comments.map((c) => (
                  <div className="comment" key={c.id}>
                    {c.author ? <UserAvatar user={c.author} size="sm" /> : <Avatar name="Система" size="sm" />}
                    <div className="comment__body">
                      <div className="comment__meta">
                        <strong>{c.author?.full_name ?? 'Система'}</strong> · {relativeTime(c.created_at)}
                      </div>
                      <div className="small" style={{ whiteSpace: 'pre-wrap' }}>{c.body}</div>
                    </div>
                  </div>
                ))
              )}

              <form onSubmit={submitComment} className="stack" style={{ gap: 8, marginTop: 12 }}>
                <textarea
                  className="textarea"
                  value={comment}
                  onChange={(e) => setComment(e.target.value)}
                  placeholder="Написать комментарий…"
                  style={{ minHeight: 64 }}
                />
                <div className="row">
                  <span className="grow" />
                  <button className="btn btn--primary btn--sm" type="submit" disabled={busy || !comment.trim()}>
                    Отправить
                  </button>
                </div>
              </form>
            </div>
          </section>

          <section className="card">
            <header className="card__head"><h3>История статусов</h3></header>
            <div className="card__body">
              {task.history.length === 0 ? (
                <EmptyState icon="🕓" title="История пуста" />
              ) : (
                <div className="timeline">
                  {task.history.map((h) => (
                    <div className="timeline__item" key={h.id}>
                      <span className="timeline__dot" />
                      <div>
                        <div className="small">
                          {h.to_status === 'EVENT' ? (
                            <strong>{h.note}</strong>
                          ) : (
                            <>
                              {h.from_status ? (
                                <>
                                  {TASK_STATUS_LABEL[h.from_status as keyof typeof TASK_STATUS_LABEL] ?? h.from_status} →{' '}
                                </>
                              ) : null}
                              <strong>{TASK_STATUS_LABEL[h.to_status as keyof typeof TASK_STATUS_LABEL] ?? h.to_status}</strong>
                            </>
                          )}
                        </div>
                        <div className="tiny muted">
                          {relativeTime(h.created_at)}
                          {h.actor ? ` · ${shortName(h.actor.full_name)}` : ''}
                          {h.note && h.to_status !== 'EVENT' ? ` · ${h.note}` : ''}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </section>

          {task.recurrence !== 'NONE' && (
            <section className="card">
              <header className="card__head">
                <h3>История повторений</h3>
                {(task.missed_streak ?? 0) > 0 && (
                  <Badge tone="danger">пропущено {task.missed_streak} подряд</Badge>
                )}
              </header>
              <div className="card__body">
                {!occurrences || occurrences.length === 0 ? (
                  <EmptyState
                    icon="🔁"
                    title="Закрытых периодов пока нет"
                    hint="Каждый истёкший период повторения отмечается здесь «выполнено» или «пропущено»."
                  />
                ) : (
                  <div className="timeline">
                    {occurrences.map((o) => (
                      <div className="timeline__item" key={o.period_start}>
                        <span className="timeline__dot" />
                        <div>
                          <div className="small">
                            <strong>{formatDateIso(o.period_start)}</strong>{' '}
                            {o.status === 'DONE' ? <Badge tone="ok">выполнено</Badge> : <Badge tone="danger">пропущено</Badge>}
                          </div>
                          <div className="tiny muted">
                            {o.status === 'DONE'
                              ? [
                                  o.completed_at ? formatDateTime(o.completed_at) : null,
                                  o.completed_by.length ? o.completed_by.join(', ') : null,
                                ].filter(Boolean).join(' · ')
                              : 'период истёк, задача не была выполнена'}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </section>
          )}
        </div>
      </div>

      <TaskFormModal open={editOpen} task={task} onClose={() => setEditOpen(false)} onSaved={() => { invalidate(); void refetch() }} />

      <Modal
        open={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        title="Удалить задачу?"
        footer={
          <>
            <button className="btn" onClick={() => setConfirmDelete(false)}>Отмена</button>
            <button
              className="btn btn--danger"
              onClick={async () => {
                setConfirmDelete(false)
                try {
                  await tasksApi.remove(task.id)
                  toast.success('Задача удалена')
                  invalidate()
                  window.history.back()
                } catch (err) {
                  toast.error(err)
                }
              }}
            >
              Удалить
            </button>
          </>
        }
      >
        <p>Задача «{task.title}», её комментарии и вложения будут удалены безвозвратно. Действие попадёт в журнал событий.</p>
      </Modal>
      <Modal
        open={preview !== null}
        onClose={() => setPreview(null)}
        width="78vw"
        title={preview?.name ?? 'Документ'}
        footer={
          <>
            {preview && (
              <button
                className="btn"
                onClick={() => {
                  void downloadAuthorized(tasksApi.fileDownloadPath(task!.id, preview.id), preview.name)
                }}
              >
                ⬇ Скачать
              </button>
            )}
            <button className="btn btn--primary" onClick={() => setPreview(null)}>Закрыть</button>
          </>
        }
      >
        {preview && (
          <div style={{ minHeight: 320 }}>
            {previewUrl === null ? (
              <Spinner label="Открываем документ…" />
            ) : (() => {
              // сервер мог сконвертировать DOCX в PDF — смотрим на фактический MIME ответа
              const mime = previewMime ?? preview.mime
              return mime === 'application/pdf' || mime.startsWith('image/') ? (
                <iframe
                  title={preview.name}
                  src={previewUrl}
                  style={{ width: '100%', height: '74vh', border: '1px solid var(--border)', borderRadius: 8, background: '#fff' }}
                />
              ) : (
                <div className="alert alert--info small">
                  Предпросмотр для этого формата недоступен в браузере — нажмите «⬇ Скачать»
                  (DOCX можно открыть в Word; после пересборки образа с LibreOffice Word-файлы
                  просматриваются прямо здесь).
                </div>
              )
            })()}
          </div>
        )}
      </Modal>
    </div>
  )
}