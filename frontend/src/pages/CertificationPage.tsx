/** Аттестация и КПК: сроки квалификационных категорий и курсов повышения квалификации.
 *  Администрация ведёт раздел (добавление, правки, массовый импорт/экспорт CSV),
 *  сотрудник видит только свои записи и сроки. */
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { certificationsApi, usersApi } from '@/api/endpoints'
import { downloadAuthorized } from '@/api/client'
import { qk } from '@/api/query'
import { Modal } from '@/components/Modal'
import { UserAvatar } from '@/components/UserAvatar'
import { Badge, EmptyState, ErrorState, Pagination, Spinner, type Tone } from '@/components/ui'
import { useToast } from '@/components/Toast'
import { CERT_KIND_LABEL, CERT_STATE_LABEL, certDaysLabel, formatDateIso, shortName } from '@/lib/format'
import { CAP, can } from '@/lib/permissions'
import { useAuthStore } from '@/store/auth'
import type { CertKind, CertState, Certification } from '@/types'

const PER_PAGE = 25

/** подсказки единых написаний категорий */
const CATEGORY_PRESETS = ['Высшая', 'Первая', 'Соответствие занимаемой должности', 'Без категории']

const STATE_TONE: Record<CertState, Tone> = {
  valid: 'ok',
  expiring: 'warn',
  expired: 'danger',
  indefinite: 'muted',
}

interface FormState {
  id?: string
  user_id: string
  kind: CertKind
  category: string
  title: string
  hours: string
  provider: string
  document_no: string
  awarded_at: string
  expires_at: string
  note: string
}

const emptyForm = (userId = ''): FormState => ({
  user_id: userId,
  kind: 'CATEGORY',
  category: 'Высшая',
  title: '',
  hours: '',
  provider: '',
  document_no: '',
  awarded_at: '',
  expires_at: '',
  note: '',
})

function formFromRow(row: Certification): FormState {
  return {
    id: row.id,
    user_id: row.user.id,
    kind: row.kind,
    category: row.category ?? '',
    title: row.title ?? '',
    hours: row.hours != null ? String(row.hours) : '',
    provider: row.provider ?? '',
    document_no: row.document_no ?? '',
    awarded_at: row.awarded_at ?? '',
    expires_at: row.expires_at ?? '',
    note: row.note ?? '',
  }
}

export function CertificationPage() {
  const me = useAuthStore((s) => s.user)!
  const canManage = can(me.capabilities, CAP.certificationsManage)
  const toast = useToast()
  const qc = useQueryClient()

  const [search, setSearch] = useState('')
  const [debounced, setDebounced] = useState('')
  const [kind, setKind] = useState<CertKind | ''>('')
  const [state, setState] = useState<CertState | ''>('')
  const [userFilter, setUserFilter] = useState('')
  const [page, setPage] = useState(1)
  const [form, setForm] = useState<FormState | null>(null)
  const [busy, setBusy] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [importResult, setImportResult] = useState<{
    created: number
    updated: number
    total: number
    failed: { row: number; reason: string }[]
  } | null>(null)
  const importRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const id = window.setTimeout(() => { setDebounced(search); setPage(1) }, 350)
    return () => window.clearTimeout(id)
  }, [search])

  const params = useMemo(
    () => ({
      search: debounced || undefined,
      kind: kind || undefined,
      state: state || undefined,
      user_id: canManage && userFilter ? userFilter : undefined,
      sort: 'expires' as const,
      order: 'asc' as const,
      page,
      per_page: PER_PAGE,
    }),
    [debounced, kind, state, userFilter, canManage, page],
  )

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: qk('/certifications', params as never),
    queryFn: () => certificationsApi.list(params),
  })

  const { data: people = [] } = useQuery({
    queryKey: qk('/users/options'),
    queryFn: () => usersApi.options(),
    enabled: canManage,
  })

  const invalidate = () => void qc.invalidateQueries({ queryKey: ['/certifications'] })

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!form) return
    if (!form.user_id) {
      toast.error(new Error('Выберите сотрудника'))
      return
    }
    setBusy(true)
    try {
      const payload = {
        user_id: form.user_id,
        kind: form.kind,
        category: form.kind === 'CATEGORY' ? form.category.trim() || null : null,
        title: form.kind === 'CPK' ? form.title.trim() || null : null,
        hours: form.hours ? Number(form.hours) : null,
        provider: form.provider.trim() || null,
        document_no: form.document_no.trim() || null,
        awarded_at: form.awarded_at || null,
        expires_at: form.expires_at || null,
        note: form.note.trim() || null,
      }
      if (form.id) {
        await certificationsApi.update(form.id, payload)
        toast.success('Запись обновлена')
      } else {
        await certificationsApi.create(payload)
        toast.success('Запись добавлена')
      }
      setForm(null)
      invalidate()
      void refetch()
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  async function removeRow(row: Certification) {
    const what = row.kind === 'CPK' ? `курс «${row.title}»` : `категорию «${row.category}»`
    if (!window.confirm(`Удалить ${what} — ${row.user.full_name}? Действие необратимо.`)) return
    try {
      await certificationsApi.remove(row.id)
      toast.success('Запись удалена')
      invalidate()
      void refetch()
    } catch (err) {
      toast.error(err)
    }
  }

  async function onImportFile(f: File) {
    setBusy(true)
    try {
      const res = await certificationsApi.importCsv(f)
      setImportResult(res)
      invalidate()
      void refetch()
      if (res.created || res.updated) toast.success(`Импорт: создано ${res.created}, обновлено ${res.updated}`)
      if (res.failed.length) toast.error(`Строк с ошибками: ${res.failed.length}`)
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  async function onAttachFile(row: Certification, f: File) {
    try {
      await certificationsApi.uploadFile(row.id, f)
      toast.success('Скан приложен')
      invalidate()
      void refetch()
    } catch (err) {
      toast.error(err)
    }
  }

  const counts = data?.state_counts

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div className="page-head__text">
          <h1>Аттестация и КПК</h1>
          <div className="page-head__hint">
            {canManage
              ? 'Сроки квалификационных категорий и курсов повышения квалификации. Система сама напомнит сотрудникам и администрации о приближающейся переаттестации.'
              : 'Ваши квалификационные категории и курсы повышения квалификации со сроками действия.'}
          </div>
        </div>
        <div className="page-head__actions">
          {canManage && (
            <>
              <button
                className="btn"
                onClick={() => void downloadAuthorized(certificationsApi.exportCsv(), 'attestacia-i-kpk.csv')}
                title="Выгрузить все записи в CSV (открывается в Excel)"
              >
                ⬇ Экспорт CSV
              </button>
              <button className="btn" onClick={() => { setImportResult(null); setImportOpen(true) }}>
                ⬆ Массовый импорт
              </button>
            </>
          )}
          {canManage && (
            <button className="btn btn--primary" onClick={() => setForm(emptyForm())}>
              + Добавить запись
            </button>
          )}
        </div>
      </div>

      {counts && (counts.expiring > 0 || counts.expired > 0) && (
        <div className="row" style={{ gap: 10 }}>
          {counts.expired > 0 && (
            <div className="alert alert--error small" style={{ flex: '1 1 260px' }}>
              <strong>Срок истёк у {counts.expired}</strong>{' '}
              {counts.expired === 1 ? 'записи' : 'записей'} — нужна переаттестация
            </div>
          )}
          {counts.expiring > 0 && (
            <div className="alert alert--warn small" style={{ flex: '1 1 260px' }}>
              <strong>Скоро истекает: {counts.expiring}</strong> — запланируйте курсы заранее
            </div>
          )}
        </div>
      )}

      <section className="card">
        <div className="card__body">
          <div className="filters">
            <label className="field field--grow">
              <span className="field__label">Поиск</span>
              <input
                className="input"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={canManage ? 'ФИО, курс, категория, документ…' : 'Курс, категория, документ…'}
              />
            </label>
            <label className="field">
              <span className="field__label">Тип</span>
              <select className="select" value={kind} onChange={(e) => { setKind(e.target.value as CertKind | ''); setPage(1) }}>
                <option value="">Все записи</option>
                <option value="CATEGORY">Категории</option>
                <option value="CPK">Курсы (КПК)</option>
              </select>
            </label>
            {canManage && (
              <label className="field">
                <span className="field__label">Сотрудник</span>
                <select className="select" value={userFilter} onChange={(e) => { setUserFilter(e.target.value); setPage(1) }}>
                  <option value="">Все сотрудники</option>
                  {people.map((p) => (
                    <option key={p.id} value={p.id}>{p.full_name}</option>
                  ))}
                </select>
              </label>
            )}
          </div>
          <div className="row" style={{ marginTop: 10, gap: 6 }}>
            {(['', 'valid', 'expiring', 'expired', 'indefinite'] as const).map((s) => (
              <button
                key={s || 'all'}
                className={state === s ? 'chip chip--active' : 'chip'}
                onClick={() => { setState(s); setPage(1) }}
              >
                {s === '' ? 'Все' : CERT_STATE_LABEL[s as CertState]}
                {counts && s ? <span className="chip__count"> · {counts[s as CertState] ?? 0}</span> : null}
                {counts && s === '' ? <span className="chip__count"> · {counts.all}</span> : null}
              </button>
            ))}
          </div>
        </div>
      </section>

      {isLoading ? (
        <Spinner large label="Загружаем записи…" />
      ) : error ? (
        <ErrorState error={error} onRetry={() => void refetch()} />
      ) : !data || data.items.length === 0 ? (
        <div className="card">
          <EmptyState
            icon="🏅"
            title="Записей не найдено"
            hint={canManage
              ? 'Добавьте первую запись вручную или загрузите весь список из Excel (CSV)'
              : 'Здесь появятся ваши категории и курсы, когда администрация внесёт их в систему'}
            action={canManage ? <button className="btn btn--primary btn--sm" onClick={() => setForm(emptyForm())}>+ Добавить запись</button> : undefined}
          />
        </div>
      ) : (
        <section className="card">
          <table className="table">
            <thead>
              <tr>
                <th>Сотрудник</th>
                <th style={{ width: 110 }}>Тип</th>
                <th>Категория / курс</th>
                <th style={{ width: 90 }}>Часы</th>
                <th style={{ width: 130 }}>Документ</th>
                <th style={{ width: 120 }}>Дата</th>
                <th style={{ width: 190 }}>Действует до</th>
                <th style={{ width: canManage ? 150 : 60 }}>Файл</th>
                {canManage && <th style={{ width: 90 }}></th>}
              </tr>
            </thead>
            <tbody>
              {data.items.map((row) => (
                <tr key={row.id}>
                  <td>
                    {canManage ? (
                      <div className="row" style={{ gap: 8, flexWrap: 'nowrap' }}>
                        <UserAvatar user={row.user} size="sm" />
                        <span className="small strong nowrap">{shortName(row.user.full_name)}</span>
                      </div>
                    ) : (
                      <span className="small strong">{shortName(row.user.full_name)}</span>
                    )}
                  </td>
                  <td>
                    <Badge tone={row.kind === 'CPK' ? 'info' : 'brand'}>{CERT_KIND_LABEL[row.kind]}</Badge>
                  </td>
                  <td>
                    <div className="strong">{row.kind === 'CPK' ? row.title : row.category}</div>
                    {row.kind === 'CPK' && row.provider && <div className="tiny muted">{row.provider}</div>}
                    {row.note && <div className="tiny muted">💬 {row.note}</div>}
                  </td>
                  <td className="small">{row.hours ?? '—'}</td>
                  <td className="small mono">{row.document_no ?? '—'}</td>
                  <td className="small nowrap">{formatDateIso(row.awarded_at)}</td>
                  <td>
                    <div className="small nowrap">{formatDateIso(row.expires_at)}</div>
                    <Badge tone={STATE_TONE[row.state]}>
                      {row.state === 'indefinite' ? CERT_STATE_LABEL.indefinite : certDaysLabel(row.days_left)}
                    </Badge>
                  </td>
                  <td>
                    {row.has_file ? (
                      <button
                        className="btn btn--sm btn--ghost"
                        title={row.file_name ?? 'Скан документа'}
                        onClick={() => void downloadAuthorized(certificationsApi.fileUrl(row.id), row.file_name ?? 'document')}
                      >
                        📎 <span className="small">{row.file_name ? shortFileName(row.file_name) : 'скан'}</span>
                      </button>
                    ) : (
                      <span className="tiny muted">нет</span>
                    )}
                  </td>
                  {canManage && (
                    <td>
                      <div className="row" style={{ gap: 4, flexWrap: 'nowrap' }}>
                        <button className="btn btn--sm btn--icon" title="Редактировать" onClick={() => setForm(formFromRow(row))}>✎</button>
                        <label className="btn btn--sm btn--icon" title="Приложить скан (удостоверение, приказ)">
                          📎
                          <input
                            type="file"
                            style={{ display: 'none' }}
                            onChange={(e) => {
                              const f = e.target.files?.[0]
                              e.target.value = ''
                              if (f) void onAttachFile(row, f)
                            }}
                          />
                        </label>
                        <button className="btn btn--sm btn--icon btn--danger" title="Удалить" onClick={() => void removeRow(row)}>🗑</button>
                      </div>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination page={data.page} pages={data.pages} total={data.total} perPage={data.per_page} onPage={setPage} />
        </section>
      )}

      {/* ------------------------------------------------ форма записи */}
      {form && (
        <Modal
          open
          onClose={() => setForm(null)}
          wide
          title={form.id ? 'Редактирование записи' : 'Новая запись об аттестации / КПК'}
          footer={
            <>
              <button className="btn" onClick={() => setForm(null)} disabled={busy}>Отмена</button>
              <button className="btn btn--primary" form="cert-form" type="submit" disabled={busy}>
                {busy ? 'Сохраняем…' : 'Сохранить'}
              </button>
            </>
          }
        >
          <form id="cert-form" onSubmit={(e) => void submit(e)} className="stack" style={{ gap: 14 }}>
            <div className="form-grid">
              <label className="field">
                <span className="field__label">Сотрудник *</span>
                <select
                  className="select"
                  value={form.user_id}
                  onChange={(e) => setForm({ ...form, user_id: e.target.value })}
                  required
                >
                  <option value="">Выберите сотрудника…</option>
                  {people.map((p) => (
                    <option key={p.id} value={p.id}>{p.full_name}</option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field__label">Тип записи *</span>
                <select
                  className="select"
                  value={form.kind}
                  onChange={(e) => setForm({ ...form, kind: e.target.value as CertKind })}
                >
                  <option value="CATEGORY">Квалификационная категория</option>
                  <option value="CPK">Курс повышения квалификации (КПК)</option>
                </select>
              </label>
            </div>

            {form.kind === 'CATEGORY' ? (
              <label className="field">
                <span className="field__label">Категория *</span>
                <input
                  className="input"
                  list="cert-category-presets"
                  value={form.category}
                  onChange={(e) => setForm({ ...form, category: e.target.value })}
                  placeholder="Например: Высшая"
                  required
                />
                <datalist id="cert-category-presets">
                  {CATEGORY_PRESETS.map((c) => <option key={c} value={c} />)}
                </datalist>
                <span className="field__hint">Выберите из списка или напишите своё название</span>
              </label>
            ) : (
              <>
                <label className="field">
                  <span className="field__label">Название курса *</span>
                  <input
                    className="input"
                    value={form.title}
                    onChange={(e) => setForm({ ...form, title: e.target.value })}
                    placeholder="Например: Цифровая грамотность педагога"
                    required
                  />
                </label>
                <div className="form-grid">
                  <label className="field">
                    <span className="field__label">Часы</span>
                    <input
                      className="input"
                      inputMode="numeric"
                      value={form.hours}
                      onChange={(e) => setForm({ ...form, hours: e.target.value.replace(/[^\d]/g, '').slice(0, 5) })}
                      placeholder="72"
                    />
                  </label>
                  <label className="field">
                    <span className="field__label">Учебное заведение</span>
                    <input
                      className="input"
                      value={form.provider}
                      onChange={(e) => setForm({ ...form, provider: e.target.value })}
                      placeholder="ИРО, университет…"
                    />
                  </label>
                </div>
              </>
            )}

            <div className="form-grid">
              <label className="field">
                <span className="field__label">Номер документа</span>
                <input
                  className="input"
                  value={form.document_no}
                  onChange={(e) => setForm({ ...form, document_no: e.target.value })}
                  placeholder="приказ № 123-к / уд. № 456"
                />
              </label>
              <label className="field">
                <span className="field__label">Дата {form.kind === 'CPK' ? 'окончания' : 'присвоения'}</span>
                <input
                  className="input"
                  type="date"
                  value={form.awarded_at}
                  onChange={(e) => setForm({ ...form, awarded_at: e.target.value })}
                />
              </label>
              <label className="field">
                <span className="field__label">Действует до</span>
                <input
                  className="input"
                  type="date"
                  value={form.expires_at}
                  onChange={(e) => setForm({ ...form, expires_at: e.target.value })}
                />
                <span className="field__hint">
                  Пусто — бессрочно
                  {form.kind === 'CATEGORY' && (
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost"
                      style={{ marginLeft: 6 }}
                      onClick={() => setForm({ ...form, expires_at: plusYears(form.awarded_at, 5) })}
                      disabled={!form.awarded_at}
                      title="Дата присвоения + 5 лет"
                    >
                      +5 лет
                    </button>
                  )}
                </span>
              </label>
            </div>

            <label className="field">
              <span className="field__label">Примечание</span>
              <textarea
                className="textarea"
                value={form.note}
                onChange={(e) => setForm({ ...form, note: e.target.value })}
                placeholder="Основание, особые отметки…"
              />
            </label>

            {!form.id && (
              <div className="alert alert--info small">
                Скан удостоверения или приказа можно приложить после сохранения — кнопкой 📎 в строке записи.
              </div>
            )}
          </form>
        </Modal>
      )}

      {/* ------------------------------------------------ импорт из CSV */}
      <Modal
        open={importOpen}
        onClose={() => setImportOpen(false)}
        title="Массовый импорт из CSV (Excel)"
        footer={<button className="btn btn--primary" onClick={() => setImportOpen(false)}>Закрыть</button>}
      >
        <div className="stack" style={{ gap: 12 }}>
          <div className="alert alert--info small">
            1. Скачайте шаблон и заполните его в Excel или LibreOffice (колонки: ФИО, Тип
            («Категория» или «КПК»), Категория, Название курса, Часы, Учебное заведение, Номер
            документа, Дата, Действует до, Примечание).<br />
            2. Сохраните как CSV и загрузите сюда. Сотрудники определяются по ФИО — они должны
            быть заведены в системе. Повторная загрузка того же файла обновит записи, дубли
            создаваться не будут.
          </div>
          <div className="row">
            <button
              className="btn"
              onClick={() => void downloadAuthorized(certificationsApi.templateCsv(), 'shablon-attestacia-kpk.csv')}
            >
              ⬇ Скачать шаблон
            </button>
            <button className="btn btn--primary" disabled={busy} onClick={() => importRef.current?.click()}>
              {busy ? 'Загружаем…' : '⬆ Выбрать файл CSV'}
            </button>
            <input
              ref={importRef}
              type="file"
              accept=".csv,text/csv"
              style={{ display: 'none' }}
              onChange={(e) => {
                const f = e.target.files?.[0]
                e.target.value = ''
                if (f) void onImportFile(f)
              }}
            />
          </div>
          {importResult && (
            <div className="stack" style={{ gap: 6 }}>
              <div className="row">
                <Badge tone="ok">создано: {importResult.created}</Badge>
                <Badge tone="info">обновлено: {importResult.updated}</Badge>
                {importResult.failed.length > 0 && <Badge tone="danger">с ошибками: {importResult.failed.length}</Badge>}
                <Badge tone="muted">всего строк: {importResult.total}</Badge>
              </div>
              {importResult.failed.length > 0 && (
                <div className="doc-frame" style={{ height: 160, padding: 8 }}>
                  {importResult.failed.map((f) => (
                    <div key={f.row} className="small" style={{ color: 'var(--danger)' }}>
                      строка {f.row}: {f.reason}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </Modal>
    </div>
  )
}

/** «2023-03-15» + N лет → «2028-03-15» (29.02 переносится на 28.02). */
function plusYears(iso: string, years: number): string {
  if (!iso) return ''
  const [y, m, d] = iso.split('-').map(Number)
  let day = d
  if (m === 2 && d === 29) day = 28
  return `${y + years}-${String(m).padStart(2, '0')}-${String(day).padStart(2, '0')}`
}

/** Короткое имя файла для кнопки: «удостоверение.pdf» → «удостовер…pdf». */
function shortFileName(name: string, limit = 14): string {
  if (name.length <= limit) return name
  const dot = name.lastIndexOf('.')
  const ext = dot > 0 ? name.slice(dot) : ''
  return `${name.slice(0, limit - ext.length - 1)}…${ext}`
}
