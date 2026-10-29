/** Форма задачи: создание (OWNER/ADMIN) и редактирование.
 *  v1.2: исполнителей может быть НЕСКОЛЬКО; для учителя — урезанный набор полей. */
import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { useQuery } from '@tanstack/react-query'

import { tasksApi, usersApi } from '@/api/endpoints'
import { qk } from '@/api/query'
import { Modal } from '@/components/Modal'
import { UserMultiSelect } from '@/components/UserMultiSelect'
import { useToast } from '@/components/Toast'
import { PRIORITY_LABEL, RECURRENCE_LABEL, WEEKDAY_SHORT, fromDateTimeLocal, shortName, toDateTimeLocal } from '@/lib/format'
import { CAP, can } from '@/lib/permissions'
import { useAuthStore } from '@/store/auth'
import type { Task, TaskPriority, TaskRecurrence } from '@/types'

interface Props {
  open: boolean
  task?: Task | null
  defaultAssigneeId?: string | null
  onClose: () => void
  onSaved: (task: Task) => void
}

interface FormState {
  title: string
  incoming_number: string
  /** ручной № регистрации для журнала входящих; пусто — авто-номер */
  reg_no: string
  description: string
  assignee_ids: string[]
  deadline: string
  priority: TaskPriority
  /** периодичность повторяющейся задачи */
  recurrence: TaskRecurrence
  recurrence_weekdays: number[]
  recurrence_day: string
}

const ACCEPT = '.pdf,.doc,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document' 

export function TaskFormModal({ open, task, defaultAssigneeId, onClose, onSaved }: Props) {
  const user = useAuthStore((s) => s.user)!
  const toast = useToast()
  const canAssign = can(user.capabilities, CAP.tasksAssign)
  const [busy, setBusy] = useState(false)
  const [files, setFiles] = useState<File[]>([])
  const [form, setForm] = useState<FormState>(() => empty(defaultAssigneeId))

  // справочник сотрудников — доступен только staff (учителю API его не отдаёт)
  const { data: people = [] } = useQuery({
    queryKey: qk('/users/options'),
    queryFn: () => usersApi.options(),
    enabled: open && canAssign,
  })

  useEffect(() => {
    if (!open) return
    setFiles([])
    setForm(
      task
        ? {
            title: task.title,
            incoming_number: task.incoming_number ?? '',
            // «12/2026» → «12»: номер регистрации подставляем из журнала входящих
            reg_no: task.incoming_reg ? String(task.incoming_reg.split('/')[0] ?? '') : '',
            description: task.description ?? '',
            assignee_ids: task.assignees.map((a) => a.user.id),
            deadline: task.deadline ? toDateTimeLocal(new Date(task.deadline.endsWith('Z') ? task.deadline : `${task.deadline}Z`)) : '',
            priority: task.priority,
            recurrence: task.recurrence ?? 'NONE',
            recurrence_weekdays: task.recurrence_weekdays ?? [],
            recurrence_day: task.recurrence_day ? String(task.recurrence_day) : '',
          }
        : empty(defaultAssigneeId),
    )
  }, [open, task, defaultAssigneeId])

  const options = useMemo(() => people, [people])

  function regNoOrNull(): number | null {
    const raw = form.reg_no.trim()
    return raw ? Number(raw) : null
  }

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (canAssign && form.assignee_ids.length === 0) {
      toast.error(new Error('Назначьте хотя бы одного исполнителя'))
      return
    }
    const regRaw = form.reg_no.trim()
    if (regRaw && (!/^\d+$/.test(regRaw) || Number(regRaw) < 1 || Number(regRaw) > 9999999)) {
      toast.error(new Error('Номер регистрации — целое число от 1 до 9999999'))
      return
    }
    const recurring = form.recurrence !== 'NONE'
    if (recurring && form.recurrence === 'WEEKLY' && form.recurrence_weekdays.length === 0) {
      toast.error(new Error('Выберите дни недели для еженедельной задачи'))
      return
    }
    if (recurring && form.recurrence === 'MONTHLY') {
      const day = Number(form.recurrence_day)
      if (!Number.isInteger(day) || day < 1 || day > 31) {
        toast.error(new Error('Для ежемесячной задачи укажите число месяца от 1 до 31'))
        return
      }
    }
    const recurrenceFields = {
      recurrence: form.recurrence,
      recurrence_weekdays: form.recurrence === 'WEEKLY' ? [...form.recurrence_weekdays].sort((a, b) => a - b) : null,
      recurrence_day: form.recurrence === 'MONTHLY' ? Number(form.recurrence_day) : null,
    }
    setBusy(true)
    try {
      const saved = canAssign
        ? task
          ? await tasksApi.update(task.id, {
              title: form.title.trim(),
              incoming_number: form.incoming_number.trim() || null,
              reg_no: regNoOrNull(),
              description: form.description.trim() || null,
              assignee_ids: form.assignee_ids,
              // у повторяющихся задач дедлайна нет — срок задаёт период повторения
              deadline: recurring ? null : fromDateTimeLocal(form.deadline),
              priority: form.priority,
              ...recurrenceFields,
            })
          : await tasksApi.create({
              title: form.title.trim(),
              incoming_number: form.incoming_number.trim() || null,
              reg_no: regNoOrNull(),
              description: form.description.trim() || null,
              assignee_ids: form.assignee_ids,
              deadline: recurring ? null : fromDateTimeLocal(form.deadline),
              priority: form.priority,
              ...recurrenceFields,
            })
        : // режим исполнителя: только описание
          await tasksApi.update(task!.id, { description: form.description.trim() || null })
      // документы, приложенные сразу при создании/редактировании
      let uploaded = 0
      let failed = 0
      for (const f of files) {
        try {
          await tasksApi.uploadFile(saved.id, f)
          uploaded += 1
        } catch {
          failed += 1
        }
      }
      toast.success(
        task ? 'Задача обновлена' : `Задача создана${form.assignee_ids.length > 1 ? `, ${form.assignee_ids.length} исполнителя уведомлены` : ' и назначена исполнителю'}`,
      )
      if (uploaded) toast.success(`Документов приложено: ${uploaded}`)
      if (failed) toast.error(`Не удалось загрузить документов: ${failed}`)
      onSaved(saved)
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
      wide
      title={task ? 'Редактирование задачи' : 'Новая задача'}
      footer={
        <>
          <button className="btn" onClick={onClose} disabled={busy}>Отмена</button>
          <button className="btn btn--primary" form="task-form" type="submit" disabled={busy}>
            {busy ? 'Сохраняем…' : task ? 'Сохранить' : 'Создать и назначить'}
          </button>
        </>
      }
    >
      <form id="task-form" onSubmit={submit} className="stack" style={{ gap: 14 }}>
        <label className="field">
          <span className="field__label">Что нужно сделать *</span>
          <input
            className="input"
            required
            minLength={3}
            value={form.title}
            disabled={!canAssign}
            onChange={(e) => setForm({ ...form, title: e.target.value })}
            placeholder="Например: сдать отчёт по успеваемости 9-х классов"
          />
        </label>

        {canAssign && (
          <div className="form-grid">
            <label className="field">
              <span className="field__label">Входящий № письма (необязательно)</span>
              <input
                className="input"
                value={form.incoming_number}
                onChange={(e) => setForm({ ...form, incoming_number: e.target.value })}
                placeholder="Например: 01-15/234"
              />
            </label>
            <label className="field">
              <span className="field__label">№ регистрации (необязательно)</span>
              <input
                className="input"
                inputMode="numeric"
                disabled={!form.incoming_number.trim()}
                title={form.incoming_number.trim() ? undefined : 'Сначала укажите входящий № письма'}
                value={form.reg_no}
                onChange={(e) => setForm({ ...form, reg_no: e.target.value.replace(/[^\d]/g, '') })}
                placeholder="авто"
              />
            </label>
          </div>
        )}
        {canAssign && (
          <span className="field__hint" style={{ marginTop: -8 }}>
            Если указан входящий № — запись появится в журнале входящих писем (тема и ответственный
            из задачи). Номер регистрации возьмите из бумажного журнала (поле активно при указанном
            входящем №); оставьте пустым — присвоится автоматически по порядку за год. Уберёте
            входящий № — запись журнала удалится.
          </span>
        )}

        <label className="field">
          <span className="field__label">Описание</span>
          <textarea
            className="textarea"
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
            placeholder="Контекст, формат результата, где отметить выполнение"
          />
        </label>

        {canAssign ? (
          <>
            <div className="field">
              <span className="field__label">Исполнители (можно несколько) *</span>
              <UserMultiSelect
                options={options}
                value={form.assignee_ids}
                onChange={(ids) => setForm({ ...form, assignee_ids: ids })}
                placeholder="Исполнители не выбраны"
              />
              <span className="field__hint">
                Каждый получит уведомление; у всех своя отметка «выполнено» — задача закроется, когда отметятся все
              </span>
            </div>

            <div className="form-grid">
              <label className="field">
                <span className="field__label">Повторять</span>
                <select
                  className="select"
                  value={form.recurrence}
                  onChange={(e) => setForm({ ...form, recurrence: e.target.value as TaskRecurrence })}
                >
                  {(Object.entries(RECURRENCE_LABEL) as [TaskRecurrence, string][]).map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </select>
                <span className="field__hint">
                  {form.recurrence === 'NONE'
                    ? 'Обычная задача с одним сроком'
                    : 'Задача будет автоматически возвращаться в работу каждый период'}
                </span>
              </label>

              <label className="field">
                <span className="field__label">Дедлайн</span>
                <input
                  className="input"
                  type="datetime-local"
                  disabled={form.recurrence !== 'NONE'}
                  title={form.recurrence !== 'NONE' ? 'У повторяющихся задач срок задаёт период повторения' : undefined}
                  value={form.deadline}
                  onChange={(e) => setForm({ ...form, deadline: e.target.value })}
                />
                <span className="field__hint">
                  {form.recurrence !== 'NONE'
                    ? 'Не нужен: срок повторяющейся задачи — её период'
                    : 'После дедлайна задача автоматически станет «Просрочена»'}
                </span>
              </label>

              <label className="field">
                <span className="field__label">Приоритет</span>
                <select
                  className="select"
                  value={form.priority}
                  onChange={(e) => setForm({ ...form, priority: e.target.value as TaskPriority })}
                >
                  {(Object.entries(PRIORITY_LABEL) as [TaskPriority, string][]).map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </select>
              </label>
            </div>

            {form.recurrence === 'WEEKLY' && (
              <div className="field">
                <span className="field__label">Дни недели *</span>
                <div className="row" style={{ gap: 6 }}>
                  {[1, 2, 3, 4, 5, 6, 7].map((d) => {
                    const active = form.recurrence_weekdays.includes(d)
                    return (
                      <button
                        key={d}
                        type="button"
                        className={active ? 'chip chip--active' : 'chip'}
                        onClick={() =>
                          setForm({
                            ...form,
                            recurrence_weekdays: active
                              ? form.recurrence_weekdays.filter((x) => x !== d)
                              : [...form.recurrence_weekdays, d],
                          })
                        }
                      >
                        {WEEKDAY_SHORT[d]}
                      </button>
                    )
                  })}
                  <button
                    type="button"
                    className="btn btn--sm btn--ghost"
                    onClick={() => setForm({ ...form, recurrence_weekdays: [1, 2, 3, 4, 5] })}
                    title="Понедельник — пятница"
                  >
                    По будням
                  </button>
                </div>
              </div>
            )}

            {form.recurrence === 'MONTHLY' && (
              <label className="field" style={{ maxWidth: 220 }}>
                <span className="field__label">Число месяца *</span>
                <input
                  className="input"
                  inputMode="numeric"
                  min={1}
                  max={31}
                  value={form.recurrence_day}
                  onChange={(e) => setForm({ ...form, recurrence_day: e.target.value.replace(/[^\d]/g, '').slice(0, 2) })}
                  placeholder="Например: 5"
                />
                <span className="field__hint">29–31 в короткие месяцы переносится на последний день</span>
              </label>
            )}
          </>
        ) : (
          <div className="alert alert--info small">
            Исполнители, дедлайн и приоритет назначаются администрацией. Вы можете уточнить описание,
            менять статус своей задачи, ставить отметку «выполнено» и прикреплять файлы.
            {task && task.assignees.length > 0 && (
              <div>Исполнители: {task.assignees.map((a) => shortName(a.user.full_name)).join(', ')}</div>
            )}
          </div>
        )}
        {canAssign && (
          <div className="field">
            <span className="field__label">Документы (можно приложить сразу)</span>
            <input
              type="file"
              multiple
              accept={ACCEPT}
              onChange={(e) => {
                // FileList читаем СРАЗУ: после сброса value он пуст, а апдейтер состояния отложен
                const picked = Array.from(e.target.files ?? [])
                e.target.value = ''
                if (picked.length) setFiles((prev) => [...prev, ...picked])
              }}
            />
            {files.length > 0 && (
              <div className="stack" style={{ gap: 4, marginTop: 6 }}>
                {files.map((f, i) => (
                  <div className="row small" key={`${f.name}-${i}`} style={{ gap: 8 }}>
                    <span>📎 {f.name}</span>
                    <span className="grow" />
                    <button type="button" className="btn btn--sm btn--ghost" onClick={() => setFiles((prev) => prev.filter((_, j) => j !== i))}>✕</button>
                  </div>
                ))}
              </div>
            )}
            <span className="field__hint">PDF или DOCX; загрузятся сразу после сохранения задачи</span>
          </div>
        )}

      </form>
    </Modal>
  )
}

function empty(defaultAssigneeId?: string | null): FormState {
  const deadline = new Date(Date.now() + 3 * 86_400_000)
  return {
    title: '',
    incoming_number: '',
    reg_no: '',
    description: '',
    assignee_ids: defaultAssigneeId ? [defaultAssigneeId] : [],
    deadline: toDateTimeLocal(deadline),
    priority: 'NORMAL',
    recurrence: 'NONE',
    recurrence_weekdays: [],
    recurrence_day: '',
  }
}
