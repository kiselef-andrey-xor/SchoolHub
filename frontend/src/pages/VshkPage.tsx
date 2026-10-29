/** Модуль ВШК — годовой план внутришкольного контроля.
 *  Вид «документ»: месяцы учебного года → направления контроля → таблица мероприятий.
 *  Администрация: импорт DOCX, ручное ведение, статусы, результаты (текст + файл), публикация, печать.
 *  Остальные: опубликованный план; свои мероприятия подсвечены и попадают на дашборд. */
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'

import { positionsApi, usersApi, vshkApi } from '@/api/endpoints'
import { qk } from '@/api/query'
import { Modal } from '@/components/Modal'
import { RespMultiSelect } from '@/components/RespMultiSelect'
import { useToast } from '@/components/Toast'
import { Badge, ErrorState, Section, Spinner } from '@/components/ui'
import { useAuthStore } from '@/store/auth'
import type { Me, VshkItem, VshkPlan } from '@/types'

const MONTH_ORDER = [8, 9, 10, 11, 12, 1, 2, 3, 4, 5, 6]
const MONTHS_RU: Record<number, string> = {
  1: 'ЯНВАРЬ', 2: 'ФЕВРАЛЬ', 3: 'МАРТ', 4: 'АПРЕЛЬ', 5: 'МАЙ', 6: 'ИЮНЬ',
  8: 'АВГУСТ', 9: 'СЕНТЯБРЬ', 10: 'ОКТЯБРЬ', 11: 'НОЯБРЬ', 12: 'ДЕКАБРЬ',
}
const STATUS_LABEL: Record<string, string> = {
  PLANNED: 'запланировано',
  IN_WORK: 'проводится',
  DONE: 'выполнено',
}

function currentYearStart(): number {
  const d = new Date()
  return d.getMonth() + 1 >= 8 ? d.getFullYear() : d.getFullYear() - 1
}

function myKeys(me: Me): Set<string> {
  const keys = new Set<string>([me.id])
  ;(me.positions || []).forEach((p) => keys.add(`pos:${p.id}`))
  if (me.role === 'OWNER' || me.role === 'ADMIN') keys.add('admin')
  return keys
}

export function VshkPage() {
  const me = useAuthStore((s) => s.user)!
  const toast = useToast()
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()

  const { data: plans = [], isLoading: listLoading, error: listError } = useQuery({
    queryKey: ['/vshk'],
    queryFn: vshkApi.list,
  })

  const [planId, setPlanId] = useState<string | null>(null)
  useEffect(() => {
    if (planId === null && plans.length) {
      setPlanId(plans[0].id)
    }
  }, [plans, planId])

  const { data: plan, isLoading, error } = useQuery({
    queryKey: ['/vshk', planId],
    queryFn: () => vshkApi.detail(planId!),
    enabled: !!planId,
  })

  const { data: positions = [] } = useQuery({ queryKey: ['/meta/positions'], queryFn: positionsApi.list })
  const { data: people = [] } = useQuery({ queryKey: qk('/users/options'), queryFn: () => usersApi.options() })

  const [editing, setEditing] = useState<VshkItem | 'new' | null>(null)
  const [resultFor, setResultFor] = useState<VshkItem | null>(null)
  const [printing, setPrinting] = useState(false)
  const importRef = useRef<HTMLInputElement>(null)
  const [importYear, setImportYear] = useState(currentYearStart())

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['/vshk'] })
    void qc.invalidateQueries({ queryKey: ['/dashboard'] })
  }

  const importMut = useMutation({
    mutationFn: (file: File) => vshkApi.importDocx(file, importYear),
    onSuccess: (res) => {
      setPlanId(res.plan_id)
      invalidate()
      toast.success(`Импортировано мероприятий: ${res.items_created}${res.warnings.length ? `; замечаний: ${res.warnings.length}` : ''}`)
      res.warnings.slice(0, 5).forEach((w) => toast.error(`Не распознано: ${w}`))
    },
    onError: (e) => toast.error(e),
  })

  const publishMut = useMutation({
    mutationFn: () => vshkApi.publish(plan!.id),
    onSuccess: () => {
      invalidate()
      toast.success('План ВШК опубликован — ответственные получили уведомление')
    },
    onError: (e) => toast.error(e),
  })

  const createMut = useMutation({
    mutationFn: () => vshkApi.create(importYear),
    onSuccess: (p) => {
      setPlanId(p.id)
      invalidate()
      toast.success(`Создан черновик плана ВШК на ${p.year_label} учебный год`)
    },
    onError: (e) => toast.error(e),
  })

  const statusMut = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) => vshkApi.updateItem(id, { status }),
    onSuccess: invalidate,
    onError: (e) => toast.error(e),
  })

  const removeMut = useMutation({
    mutationFn: (id: string) => vshkApi.removeItem(id),
    onSuccess: () => {
      invalidate()
      toast.success('Мероприятие удалено')
    },
    onError: (e) => toast.error(e),
  })

  useEffect(() => {
    if (!printing) return
    document.body.classList.add('print-sheet-active')
    const t = window.setTimeout(() => window.print(), 250)
    const after = () => setPrinting(false)
    window.addEventListener('afterprint', after)
    return () => {
      window.clearTimeout(t)
      window.removeEventListener('afterprint', after)
      document.body.classList.remove('print-sheet-active')
    }
  }, [printing])

  const keys = useMemo(() => myKeys(me), [me])

  const grouped = useMemo(() => {
    const out: { month: number; sections: { name: string; items: VshkItem[] }[] }[] = []
    if (!plan) return out
    for (const month of MONTH_ORDER) {
      const monthItems = plan.items.filter((i) => i.month === month)
      if (!monthItems.length) continue
      const sections: { name: string; items: VshkItem[] }[] = []
      for (const item of monthItems) {
        let sec = sections.find((s) => s.name === item.section)
        if (!sec) {
          sec = { name: item.section, items: [] }
          sections.push(sec)
        }
        sec.items.push(item)
      }
      out.push({ month, sections })
    }
    return out
  }, [plan])

  // ?item=<uuid> — переход из глобального поиска: подсветка
  const [highlight] = useState<string | null>(() => params.get('item'))
  useEffect(() => {
    if (!highlight) return
    const t = window.setTimeout(() => {
      document.getElementById(`vshk-item-${highlight}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }, 350)
    return () => window.clearTimeout(t)
  }, [highlight, plan])
  useEffect(() => {
    if (params.get('item')) {
      const next = new URLSearchParams(params)
      next.delete('item')
      setParams(next, { replace: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (listLoading) return <Spinner />
  if (listError) return <ErrorState error={listError} />

  // Право управления зависит от роли, а не от наличия плана: иначе на «пустой»
  // школе страницы лишалась кнопок «Импорт DOCX» / «Новый план» и выглядела сломанной.
  const canManage = me.role === 'OWNER' || me.role === 'ADMIN' || !!plan?.can_manage

  return (
    <Section
      title="Внутришкольный контроль (ВШК)"
      actions={
        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          {plans.length > 0 && (
            <select
              className="select"
              value={planId ?? ''}
              onChange={(e) => setPlanId(e.target.value || null)}
              title="Учебный год"
            >
              {plans.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.year_label} учебный год · {p.status === 'DRAFT' ? 'черновик' : 'опубликован'} ({p.items_total})
                </option>
              ))}
            </select>
          )}
          {canManage && (
            <>
              <input
                ref={importRef}
                type="file"
                accept=".docx"
                style={{ display: 'none' }}
                onChange={(e) => {
                  const f = e.target.files?.[0]
                  if (f) importMut.mutate(f)
                  e.target.value = ''
                }}
              />
              <button className="btn" onClick={() => importRef.current?.click()} title="Импорт таблицы «Цель | Вид | Ответственный | Срок и форма»">
                 Импорт DOCX
              </button>
              <button className="btn" onClick={() => createMut.mutate()} disabled={createMut.isPending}
                title="Создать пустой черновик плана на выбранный учебный год">
                + Новый план
              </button>
              {plan?.status === 'DRAFT' && (
                <button className="btn btn--primary" onClick={() => publishMut.mutate()} disabled={publishMut.isPending}>
                  Опубликовать
                </button>
              )}
              {plan && <button className="btn" onClick={() => setPrinting(true)}>🖨 Печать</button>}
              {plan && <button className="btn" onClick={() => setEditing('new')}>+ Мероприятие</button>}
            </>
          )}
        </div>
      }
    >
      <div className="row" style={{ gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
        {canManage && (
        <label className="row tiny muted" style={{ gap: 6 }}>
          год для импорта/создания:
          <input
            className="input"
            type="number"
            style={{ width: 90 }}
            value={importYear}
            min={2000}
            max={2100}
            onChange={(e) => setImportYear(Number(e.target.value) || currentYearStart())}
          />
        </label>
        )}
        {plan?.status === 'DRAFT' && <Badge tone="warn">черновик — виден только администрации</Badge>}
        {plan?.status === 'PUBLISHED' && plan.published_at && (
          <Badge tone="ok">опубликован {new Date(plan.published_at).toLocaleString('ru-RU')}</Badge>
        )}
      </div>

      {isLoading && <Spinner />}
      {error && <ErrorState error={error} />}
      {!plans.length && (
        <div className="empty">
          {canManage ? (
            <>
              <div style={{ marginBottom: 10 }}>
                Плана ВШК ещё нет. Импортируйте DOCX годового плана (таблица «Цель | Вид | Ответственный | Срок и форма»)
                или создайте пустой черновик и наполните вручную.
              </div>
              <div className="row" style={{ gap: 8, justifyContent: 'center', flexWrap: 'wrap' }}>
                <button className="btn btn--primary" onClick={() => importRef.current?.click()}>
                  ⬆ Импорт DOCX плана
                </button>
                <button className="btn" onClick={() => createMut.mutate()} disabled={createMut.isPending}>
                  + Новый план на {importYear}-{importYear + 1} учебный год
                </button>
              </div>
            </>
          ) : (
            'План внутришкольного контроля ещё не опубликован администрацией школы.'
          )}
        </div>
      )}
      {plan && !plan.items.length && (
        <div className="empty">
          Мероприятий пока нет. Импортируйте DOCX плана ВШК или добавьте вручную{canManage ? '' : ' — администрация'}.
        </div>
      )}

      {grouped.map(({ month, sections }) => (
        <div key={month}>
          <div className="vshk-month">{MONTHS_RU[month]}</div>
          {sections.map((sec) => (
            <div key={sec.name || '(без направления)'}>
              {sec.name && <div className="vshk-section">{sec.name}</div>}
              <table className="vshk-table">
                <thead>
                  <tr>
                    <th style={{ width: '34%' }}>Цель контроля</th>
                    <th style={{ width: '11%' }}>Вид</th>
                    <th style={{ width: '19%' }}>Ответственный</th>
                    <th style={{ width: '14%' }}>Срок и форма итогов</th>
                    <th style={{ width: '11%' }}>Статус</th>
                    <th>Результат</th>
                    {canManage && <th style={{ width: 90 }} />}
                  </tr>
                </thead>
                <tbody>
                  {sec.items.map((item) => {
                    const mine = item.resp.some((r) => keys.has(r))
                    return (
                      <tr
                        key={item.id}
                        id={`vshk-item-${item.id}`}
                        className={`${mine ? 'vshk-item--mine' : ''} ${highlight === item.id ? 'search-hit' : ''}`}
                      >
                        <td>{item.title}</td>
                        <td className="small">{item.control_type}</td>
                        <td className="small">{item.resp_titles.join(', ') || '—'}</td>
                        <td className="small">{item.summary}</td>
                        <td>
                          {canManage ? (
                            <select
                              className="select"
                              style={{ minWidth: 120 }}
                              value={item.status}
                              onChange={(e) => statusMut.mutate({ id: item.id, status: e.target.value })}
                            >
                              <option value="PLANNED">запланировано</option>
                              <option value="IN_WORK">проводится</option>
                              <option value="DONE">выполнено</option>
                            </select>
                          ) : (
                            <span className={`vshk-status vshk-status--${item.status}`}>{STATUS_LABEL[item.status]}</span>
                          )}
                        </td>
                        <td className="small">
                          {item.result_text && <div>{item.result_text}</div>}
                          {item.result_file && (
                            <a href={vshkApi.fileUrl(item.id)} target="_blank" rel="noreferrer">
                              📎 {item.result_file_name ?? 'файл'}
                            </a>
                          )}
                          {!item.result_text && !item.result_file && <span className="muted">—</span>}
                        </td>
                        {canManage && (
                          <td className="row" style={{ gap: 4, justifyContent: 'flex-end' }}>
                            <button className="btn btn--sm" title="Итог контроля (текст и файл)" onClick={() => setResultFor(item)}>
                              📎
                            </button>
                            <button className="btn btn--sm" title="Редактировать" onClick={() => setEditing(item)}>✎</button>
                            <button
                              className="btn btn--sm btn--ghost"
                              title="Удалить"
                              onClick={() => {
                                if (window.confirm(`Удалить мероприятие «${item.title.slice(0, 60)}…»?`)) removeMut.mutate(item.id)
                              }}
                            >
                              🗑
                            </button>
                          </td>
                        )}
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      ))}

      {editing && plan && (
        <VshkItemModal
          plan={plan}
          item={editing === 'new' ? null : editing}
          users={people}
          positions={positions}
          onClose={() => setEditing(null)}
          onDone={() => {
            setEditing(null)
            invalidate()
          }}
        />
      )}

      {resultFor && (
        <ResultModal
          item={resultFor}
          onClose={() => setResultFor(null)}
          onDone={() => {
            setResultFor(null)
            invalidate()
          }}
        />
      )}

      {printing && plan && (
        <PrintSheet plan={plan} />
      )}
    </Section>
  )
}

/* ------------------------------------------------------------ форма мероприятия */
interface ItemForm {
  month: number
  section: string
  title: string
  control_type: string
  resp: string[]
  summary: string
  status: string
}

function VshkItemModal({
  plan,
  item,
  users,
  positions,
  onClose,
  onDone,
}: {
  plan: VshkPlan
  item: VshkItem | null
  users: { id: string; full_name: string; positions?: { id: string; title: string }[] }[]
  positions: { id: string; title: string }[]
  onClose: () => void
  onDone: () => void
}) {
  const toast = useToast()
  const [form, setForm] = useState<ItemForm>(
    item
      ? {
          month: item.month,
          section: item.section,
          title: item.title,
          control_type: item.control_type,
          resp: item.resp,
          summary: item.summary,
          status: item.status,
        }
      : {
          month: new Date().getMonth() + 1,
          section: '',
          title: '',
          control_type: 'Тематический',
          resp: [],
          summary: '',
          status: 'PLANNED',
        },
  )
  const [busy, setBusy] = useState(false)

  const sections = useMemo(() => Array.from(new Set(plan.items.map((i) => i.section).filter(Boolean))), [plan])

  const save = useMutation({
    mutationFn: () =>
      item
        ? vshkApi.updateItem(item.id, { ...form })
        : vshkApi.addItem(plan.id, { ...form }),
    onSuccess: () => {
      toast.success(item ? 'Мероприятие обновлено' : 'Мероприятие добавлено')
      onDone()
    },
    onError: (e) => toast.error(e),
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    if (!form.title.trim()) {
      toast.error('Укажите цель контроля')
      return
    }
    setBusy(true)
    save.mutate()
  }

  return (
    <Modal open title={item ? 'Мероприятие ВШК' : 'Новое мероприятие ВШК'} onClose={onClose} wide>
      <form id="vshk-item-form" onSubmit={submit} className="stack" style={{ gap: 10 }}>
        <div className="grid grid--2">
          <label className="field">
            <span className="field__label">Месяц *</span>
            <select className="select" value={form.month} onChange={(e) => setForm({ ...form, month: Number(e.target.value) })}>
              {MONTH_ORDER.map((m) => (
                <option key={m} value={m}>{MONTHS_RU[m]}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field__label">Вид контроля</span>
            <input className="input" value={form.control_type} onChange={(e) => setForm({ ...form, control_type: e.target.value })} placeholder="Тематический, фронтальный…" />
          </label>
        </div>
        <label className="field">
          <span className="field__label">Направление контроля</span>
          <input
            className="input"
            list="vshk-sections"
            value={form.section}
            onChange={(e) => setForm({ ...form, section: e.target.value })}
            placeholder="Контроль образовательных результатов учащихся"
          />
          <datalist id="vshk-sections">
            {sections.map((s) => (
              <option key={s} value={s} />
            ))}
          </datalist>
        </label>
        <label className="field">
          <span className="field__label">Цель контроля *</span>
          <textarea className="input" rows={2} value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
        </label>
        <div className="field">
          <span className="field__label">Ответственные</span>
          <RespMultiSelect
            users={users as never}
            positions={positions as never}
            value={form.resp}
            onChange={(resp) => setForm({ ...form, resp })}
          />
        </div>
        <div className="grid grid--2">
          <label className="field">
            <span className="field__label">Срок и форма подведения итогов</span>
            <input className="input" value={form.summary} onChange={(e) => setForm({ ...form, summary: e.target.value })} placeholder="Совещание при директоре, справка" />
          </label>
          <label className="field">
            <span className="field__label">Статус</span>
            <select className="select" value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}>
              <option value="PLANNED">запланировано</option>
              <option value="IN_WORK">проводится</option>
              <option value="DONE">выполнено</option>
            </select>
          </label>
        </div>
      </form>
      <div className="row" style={{ gap: 8, justifyContent: 'flex-end', marginTop: 12 }}>
        <button className="btn" onClick={onClose}>Отмена</button>
        <button className="btn btn--primary" form="vshk-item-form" type="submit" disabled={busy || save.isPending}>
          Сохранить
        </button>
      </div>
    </Modal>
  )
}

/* ---------------------------------------------------------------- форма результата */
function ResultModal({ item, onClose, onDone }: { item: VshkItem; onClose: () => void; onDone: () => void }) {
  const toast = useToast()
  const [text, setText] = useState(item.result_text ?? '')
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)

  const save = useMutation({
    mutationFn: () => vshkApi.setResult(item.id, text.trim() || null, file ?? undefined),
    onSuccess: () => {
      toast.success('Результат контроля сохранён')
      onDone()
    },
    onError: (e) => toast.error(e),
  })

  const clear = useMutation({
    mutationFn: () => vshkApi.clearResult(item.id),
    onSuccess: () => {
      toast.success('Результат снят')
      onDone()
    },
    onError: (e) => toast.error(e),
  })

  return (
    <Modal open title="Итог контроля" onClose={onClose} wide>
      <div className="stack" style={{ gap: 10 }}>
        <div className="tiny muted">{item.title}</div>
        <label className="field">
          <span className="field__label">Выводы / текст справки</span>
          <textarea className="input" rows={4} value={text} onChange={(e) => setText(e.target.value)} />
        </label>
        <label className="field">
          <span className="field__label">Файл справки (PDF, DOCX, изображение)</span>
          <input className="input" type="file" accept=".pdf,.docx,.png,.jpg,.jpeg" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          {item.result_file_name && (
            <span className="field__hint">
              сейчас приложен: <a href={vshkApi.fileUrl(item.id)} target="_blank" rel="noreferrer">{item.result_file_name}</a>
            </span>
          )}
        </label>
        <div className="row" style={{ gap: 8, justifyContent: 'flex-end' }}>
          {(item.result_text || item.result_file) && (
            <button className="btn" onClick={() => clear.mutate()} disabled={clear.isPending} style={{ marginRight: 'auto' }}>
              Снять результат
            </button>
          )}
          <button className="btn" onClick={onClose}>Отмена</button>
          <button className="btn btn--primary" onClick={() => { setBusy(true); save.mutate() }} disabled={busy || save.isPending}>
            Сохранить
          </button>
        </div>
      </div>
    </Modal>
  )
}

/* ------------------------------------------------------------------- печатная форма */
function PrintSheet({ plan }: { plan: VshkPlan }) {
  const me = useAuthStore((s) => s.user)!
  return createPortal(
    <div className="print-sheet">
      <h2 style={{ margin: '0 0 4px', fontSize: 17, textAlign: 'center' }}>
        ПЛАН ВНУТРИШКОЛЬНОГО КОНТРОЛЯ НА {plan.year_label} УЧЕБНЫЙ ГОД
      </h2>
      <div style={{ textAlign: 'center', marginBottom: 10 }} className="tiny">
        печать от {new Date().toLocaleDateString('ru-RU')} · {me.full_name}
      </div>
      {MONTH_ORDER.map((month) => {
        const items = plan.items.filter((i) => i.month === month)
        if (!items.length) return null
        return (
          <div key={month}>
            <div style={{ margin: '10px 0 4px', fontWeight: 800, textTransform: 'uppercase' }}>{MONTHS_RU[month]}</div>
            <table className="print-sheet__list" style={{ width: '100%' }}>
              <thead>
                <tr>
                  <th>Цель контроля</th>
                  <th style={{ width: '13%' }}>Вид</th>
                  <th style={{ width: '20%' }}>Ответственный</th>
                  <th style={{ width: '17%' }}>Срок и форма итогов</th>
                  <th style={{ width: '12%' }}>Статус / итог</th>
                </tr>
              </thead>
              <tbody>
                {items.map((i) => (
                  <tr key={i.id}>
                    <td>
                      {i.section && <div className="tiny">{i.section}</div>}
                      {i.title}
                    </td>
                    <td>{i.control_type}</td>
                    <td>{i.resp_titles.join(', ')}</td>
                    <td>{i.summary}</td>
                    <td>
                      {STATUS_LABEL[i.status]}
                      {i.result_text ? `; ${i.result_text}` : ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      })}
    </div>,
    document.body,
  )
}
