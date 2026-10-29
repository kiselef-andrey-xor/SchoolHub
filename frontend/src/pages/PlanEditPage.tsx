/** Редактор плана работы: неделя, дежурство, блоки «месяц/неделя», мероприятия по дням.
 *  Ответственные выбираются из справочника сотрудников (при публикации им придёт уведомление). */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { plansApi, usersApi, type PlanContentPayload } from '@/api/endpoints'
import { qk } from '@/api/query'
import { RespMultiSelect } from '@/components/RespMultiSelect'
import { positionsApi } from '@/api/endpoints'
import { Badge, ErrorState, Section, Spinner } from '@/components/ui'
import { useToast } from '@/components/Toast'
import { PLAN_STATUS_LABEL, formatDateIso } from '@/lib/format'
import type { PlanStatus, RespRef } from '@/types'

/** RespRef из ответа сервера → сырая ссылка для сохранения («<uuid>» | «pos:<uuid>» | «admin»). */
function refOf(r: RespRef): string {
  if (r.kind === 'admin') return 'admin'
  if (r.kind === 'pos') return `pos:${r.id ?? ''}`
  return r.id ?? ''
}

interface DayDraft {
  date: string
  events: { title: string; time: string; place: string; resp: string[] }[]
}

interface Draft {
  weekStart: string
  dutyKlass: string
  dutyTeacherId: string
  notes: string[]
  monthItems: { text: string; resp: string[] }[]
  weekItems: { text: string; place: string; resp: string[] }[]
  days: DayDraft[]
}

const WEEKDAY = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота']

function weekDates(mondayIso: string): string[] {
  const [y, m, d] = mondayIso.split('-').map(Number)
  const base = new Date(y, (m || 1) - 1, d || 1)
  return Array.from({ length: 6 }, (_, i) => {
    const x = new Date(base)
    x.setDate(base.getDate() + i)
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${x.getFullYear()}-${pad(x.getMonth() + 1)}-${pad(x.getDate())}`
  })
}

export function PlanEditPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const toast = useToast()
  const qc = useQueryClient()
  const [draft, setDraft] = useState<Draft | null>(null)
  const [busy, setBusy] = useState(false)
  const importRef = useRef<HTMLInputElement>(null)

  const { data: plan, isLoading, error, refetch } = useQuery({
    queryKey: qk(`/plans/${id}`),
    queryFn: () => plansApi.detail(id),
  })

  const { data: positions = [] } = useQuery({ queryKey: ['/meta/positions'], queryFn: positionsApi.list })
  const { data: people = [] } = useQuery({
    queryKey: qk('/users/options'),
    queryFn: () => usersApi.options(),
  })

  // инициализация черновика из загруженного плана (один раз)
  useEffect(() => {
    if (!plan || draft) return
    setDraft({
      weekStart: plan.week_start,
      dutyKlass: plan.content.duty.klass ?? '',
      dutyTeacherId: plan.content.duty.teacher?.id ?? '',
      notes: [...plan.content.notes],
      monthItems: plan.content.month_items.map((i) => ({ text: i.text, resp: i.resp.map(refOf) })),
      weekItems: plan.content.week_items.map((i) => ({ text: i.text, place: i.place ?? '', resp: i.resp.map(refOf) })),
      days: weekDates(plan.week_start).map((date) => {
        const src = plan.content.days.find((d) => d.date === date)
        return {
          date,
          events: (src?.events ?? []).map((e) => ({
            title: e.title,
            time: e.time ?? '',
            place: e.place ?? '',
            resp: e.resp.map(refOf),
          })),
        }
      }),
    })
  }, [plan, draft])

  const eventsTotal = useMemo(
    () => (draft ? draft.days.reduce((n, d) => n + d.events.filter((e) => e.title.trim()).length, 0) : 0),
    [draft],
  )

  if (isLoading) return <Spinner large label="Открываем редактор…" />
  if (error) return <ErrorState error={error} onRetry={() => void refetch()} />
  if (!plan || !draft) return null
  const planId = plan.id
  const planStatus = plan.status

  const set = (patch: Partial<Draft>) => setDraft((prev) => (prev ? { ...prev, ...patch } : prev))

  function changeWeek(value: string) {
    if (!draft) return
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) {
      set({ weekStart: value })
      return
    }
    // пересчитываем даты пн–сб; мероприятия сохраняются только для совпавших дат
    const dates = weekDates(value)
    set({
      weekStart: value,
      days: dates.map((date) => {
        const old = draft.days.find((d) => d.date === date)
        return old ?? { date, events: [] }
      }),
    })
  }

  async function importFromDocx(f: File) {
    if (!draft) return
    setBusy(true)
    try {
      const res = await plansApi.parseDocument(f)
      const weekStart = res.week_start ?? draft.weekStart
      const eventsTotal = res.content.days.reduce((n, d) => n + d.events.length, 0)
      setDraft({
        weekStart,
        dutyKlass: res.content.duty.klass ?? '',
        dutyTeacherId: res.content.duty.teacher_id ?? '',
        notes: [...res.content.notes],
        monthItems: res.content.month_items.map((i) => ({
          text: i.place ? `${i.text} Место: ${i.place}` : i.text,
          resp: i.resp,
        })),
        weekItems: res.content.week_items.map((i) => ({ text: i.text, place: i.place ?? '', resp: i.resp })),
        days: weekDates(weekStart).map((dt) => {
          const src = res.content.days.find((d) => d.date === dt)
          return {
            date: dt,
            events: (src?.events ?? []).map((e) => ({
              title: e.title,
              time: e.time ?? '',
              place: e.place ?? '',
              resp: e.resp,
            })),
          }
        }),
      })
      toast.success(`Документ разобран: мероприятий в таблице — ${eventsTotal}. Проверьте ответственных и сохраните план.`)
      if (res.warnings.length > 0) {
        toast.error(new Error(`Не распознано: ${res.warnings.slice(0, 3).join('; ')}`))
      }
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  async function save() {
    if (!draft) return
    setBusy(true)
    try {
      const content: PlanContentPayload = {
        duty: { klass: draft.dutyKlass.trim() || null, teacher_id: draft.dutyTeacherId || null },
        notes: draft.notes.filter((n) => n.trim()),
        month_items: draft.monthItems
          .filter((i) => i.text.trim())
          .map((i) => ({ text: i.text.trim(), place: null, resp: i.resp })),
        week_items: draft.weekItems
          .filter((i) => i.text.trim())
          .map((i) => ({ text: i.text.trim(), place: i.place.trim() || null, resp: i.resp })),
        days: draft.days.map((d) => ({
          date: d.date,
          events: d.events
            .filter((e) => e.title.trim())
            .map((e) => ({
              title: e.title.trim(),
              time: e.time.trim() || null,
              place: e.place.trim() || null,
              resp: e.resp,
            })),
        })),
      }
      await plansApi.update(planId, { week_start: draft.weekStart, content })
      toast.success('План сохранён')
      void qc.invalidateQueries({ queryKey: ['/plans'] })
      void qc.invalidateQueries({ queryKey: qk(`/plans/${planId}`) })
      navigate(`/plans/${planId}`)
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="page-head no-print">
        <div className="page-head__text">
          <div className="row" style={{ gap: 8, marginBottom: 6 }}>
            <button className="btn btn--sm btn--ghost" onClick={() => navigate(`/plans/${planId}`)}>
              ← Отменить редактирование
            </button>
            <Badge tone={planStatus === 'PUBLISHED' ? 'ok' : 'warn'}>
              {PLAN_STATUS_LABEL[planStatus as PlanStatus]}
            </Badge>
            <Badge tone="muted">мероприятий: {eventsTotal}</Badge>
          </div>
          <h1>Редактирование плана</h1>
          <div className="page-head__hint">
            Заполните блоки как в Word-шаблоне: дежурство, «в течение месяца», «в течение недели» и мероприятия
            по дням. Ответственные выбираются из списка сотрудников — они получат уведомление при публикации.
          </div>
        </div>
      </div>

      <Section title="Неделя и дежурство">
        <div className="form-grid">
          <label className="field">
            <span className="field__label">Понедельник недели *</span>
            <input className="input" type="date" value={draft.weekStart} onChange={(e) => changeWeek(e.target.value)} />
            <span className="field__hint">Даты (пн–сб) пересчитаются автоматически</span>
          </label>
          <label className="field">
            <span className="field__label">Дежурный класс</span>
            <input
              className="input"
              value={draft.dutyKlass}
              onChange={(e) => set({ dutyKlass: e.target.value })}
              placeholder="Например: 9 «Б»"
            />
          </label>
          <label className="field">
            <span className="field__label">Классный руководитель дежурного класса</span>
            <select className="select" value={draft.dutyTeacherId} onChange={(e) => set({ dutyTeacherId: e.target.value })}>
              <option value="">— не выбран —</option>
              {people.map((u) => (
                <option key={u.id} value={u.id}>{u.full_name}</option>
              ))}
            </select>
          </label>
        </div>
        <div style={{ marginTop: 12 }}>
          <div className="row row--between">
            <span className="field__label">Дополнительные строки шапки (дежурство в столовой и т. п.)</span>
            <button className="btn btn--sm" onClick={() => set({ notes: [...draft.notes, ''] })}>＋ Строка</button>
          </div>
          {draft.notes.map((n, i) => (
            <div className="plan-note-line" key={i}>
              <input
                className="input"
                value={n}
                onChange={(e) => {
                  const notes = [...draft.notes]
                  notes[i] = e.target.value
                  set({ notes })
                }}
                placeholder="Например: В столовой дежурят 7-ые классы."
              />
              <button
                className="btn btn--sm btn--danger"
                onClick={() => set({ notes: draft.notes.filter((_, j) => j !== i) })}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      </Section>

      <Section
        title="В течение месяца"
        actions={
          <button className="btn btn--sm" onClick={() => set({ monthItems: [...draft.monthItems, { text: '', resp: [] }] })}>
            ＋ Пункт
          </button>
        }
      >
        {draft.monthItems.length === 0 && <div className="tiny muted">Пунктов нет.</div>}
        {draft.monthItems.map((item, i) => (
          <div className="item-line" key={i}>
            <div>
              <textarea
                className="textarea"
                style={{ minHeight: 52 }}
                value={item.text}
                onChange={(e) => {
                  const monthItems = [...draft.monthItems]
                  monthItems[i] = { ...item, text: e.target.value }
                  set({ monthItems })
                }}
                placeholder="Что делаем в течение месяца…"
              />
              <div style={{ marginTop: 7 }}>
                <RespMultiSelect
                  users={people}
                  positions={positions}
                  value={item.resp}
                  onChange={(ids) => {
                    const monthItems = [...draft.monthItems]
                    monthItems[i] = { ...item, resp: ids }
                    set({ monthItems })
                  }}
                  placeholder="Ответственные не выбраны"
                />
              </div>
            </div>
            <button
              className="btn btn--sm btn--danger"
              onClick={() => set({ monthItems: draft.monthItems.filter((_, j) => j !== i) })}
            >
              🗑
            </button>
          </div>
        ))}
      </Section>

      <Section
        title="В течение недели"
        actions={
          <button
            className="btn btn--sm"
            onClick={() => set({ weekItems: [...draft.weekItems, { text: '', place: '', resp: [] }] })}
          >
            ＋ Пункт
          </button>
        }
      >
        {draft.weekItems.length === 0 && <div className="tiny muted">Пунктов нет.</div>}
        {draft.weekItems.map((item, i) => (
          <div className="item-line" key={i}>
            <div>
              <textarea
                className="textarea"
                style={{ minHeight: 52 }}
                value={item.text}
                onChange={(e) => {
                  const weekItems = [...draft.weekItems]
                  weekItems[i] = { ...item, text: e.target.value }
                  set({ weekItems })
                }}
                placeholder="Что делаем в течение недели…"
              />
              <input
                className="input"
                style={{ marginTop: 6, maxWidth: 280 }}
                value={item.place}
                onChange={(e) => {
                  const weekItems = [...draft.weekItems]
                  weekItems[i] = { ...item, place: e.target.value }
                  set({ weekItems })
                }}
                placeholder="Место проведения (необязательно)"
              />
              <div style={{ marginTop: 7 }}>
                <RespMultiSelect
                  users={people}
                  positions={positions}
                  value={item.resp}
                  onChange={(ids) => {
                    const weekItems = [...draft.weekItems]
                    weekItems[i] = { ...item, resp: ids }
                    set({ weekItems })
                  }}
                  placeholder="Ответственные не выбраны"
                />
              </div>
            </div>
            <button
              className="btn btn--sm btn--danger"
              onClick={() => set({ weekItems: draft.weekItems.filter((_, j) => j !== i) })}
            >
              🗑
            </button>
          </div>
        ))}
      </Section>

      <Section title="Мероприятия по дням (пн–сб)">
        <div className="stack" style={{ gap: 10 }}>
          {draft.days.map((day, di) => (
            <div className="day-card" key={day.date}>
              <div className="day-card__head">
                <strong>{formatDateIso(day.date)}</strong>
                <Badge tone="muted">{WEEKDAY[di] ?? ''}</Badge>
                <span className="grow" />
                <button
                  className="btn btn--sm"
                  onClick={() => {
                    const days = [...draft.days]
                    days[di] = { ...day, events: [...day.events, { title: '', time: '', place: '', resp: [] }] }
                    set({ days })
                  }}
                >
                  ＋ Мероприятие
                </button>
              </div>
              {day.events.length === 0 && <div className="tiny muted">Нет мероприятий — можно добавить.</div>}
              {day.events.map((ev, ei) => (
                <div className="evt" key={ei}>
                  <input
                    className="input"
                    value={ev.title}
                    placeholder="Мероприятие…"
                    onChange={(e) => {
                      const days = [...draft.days]
                      const events = [...day.events]
                      events[ei] = { ...ev, title: e.target.value }
                      days[di] = { ...day, events }
                      set({ days })
                    }}
                  />
                  <div className="evt__grid">
                    <input
                      className="input"
                      value={ev.time}
                      placeholder="Время (напр. 08.00)"
                      onChange={(e) => {
                        const days = [...draft.days]
                        const events = [...day.events]
                        events[ei] = { ...ev, time: e.target.value }
                        days[di] = { ...day, events }
                        set({ days })
                      }}
                    />
                    <input
                      className="input"
                      value={ev.place}
                      placeholder="Место"
                      onChange={(e) => {
                        const days = [...draft.days]
                        const events = [...day.events]
                        events[ei] = { ...ev, place: e.target.value }
                        days[di] = { ...day, events }
                        set({ days })
                      }}
                    />
                    <RespMultiSelect
                      users={people}
                      positions={positions}
                      value={ev.resp}
                      onChange={(ids) => {
                        const days = [...draft.days]
                        const events = [...day.events]
                        events[ei] = { ...ev, resp: ids }
                        days[di] = { ...day, events }
                        set({ days })
                      }}
                      placeholder="Ответственные"
                    />
                    <button
                      className="btn btn--sm btn--danger"
                      onClick={() => {
                        const days = [...draft.days]
                        days[di] = { ...day, events: day.events.filter((_, j) => j !== ei) }
                        set({ days })
                      }}
                    >
                      🗑
                    </button>
                  </div>
                </div>
              ))}
            </div>
          ))}
        </div>
      </Section>

      <div className="row no-print">
        <button className="btn" onClick={() => navigate(`/plans/${planId}`)} disabled={busy}>Отмена</button>
        <span className="grow" />
        <input
          ref={importRef}
          type="file"
          accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
          style={{ display: 'none' }}
          onChange={(e) => {
            const f = e.target.files?.[0]
            if (f) void importFromDocx(f)
            e.target.value = ''
          }}
        />
        <button
          className="btn"
          disabled={busy}
          onClick={() => {
            if (draft && (draft.days.some((d) => d.events.length > 0) || draft.weekItems.length > 0)) {
              if (!window.confirm('Импорт из документа ЗАМЕНИТ текущее содержимое черновика. Продолжить?')) return
            }
            importRef.current?.click()
          }}
          title="Разобрать DOCX в школьном Word-макете и заполнить редактор"
        >
          📄 Импорт из Word
        </button>
        <button className="btn btn--primary" onClick={() => void save()} disabled={busy}>
          {busy ? 'Сохраняем…' : '💾 Сохранить план'}
        </button>
      </div>
    </div>
  )
}
