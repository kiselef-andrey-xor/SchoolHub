/** Пользователи (OWNER/ADMIN): создание, смена роли, блокировка, сброс пароля. */
import { useMemo, useState, type FormEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'

import { authApi, positionsApi, usersApi } from '@/api/endpoints'
import { PositionsModal } from '@/components/PositionsModal'
import { qk } from '@/api/query'
import { ADImportModal } from '@/components/ADImportModal'
import { BulkImportModal } from '@/components/BulkImportModal'
import { Modal } from '@/components/Modal'
import { PepSignModal } from '@/components/PepSignModal'
import { UserAvatar } from '@/components/UserAvatar'
import {
  Badge,
  EmptyState,
  ErrorState,
  Pagination,
  RoleBadge,
  Spinner,
} from '@/components/ui'
import { useToast } from '@/components/Toast'
import { CAP, can, canManageRole } from '@/lib/permissions'
import { ROLE_LABEL_FULL, formatDate, formatDateTime } from '@/lib/format'
import { useAuthStore } from '@/store/auth'
import type { Role, User } from '@/types'

const PER_PAGE = 20
const ROLES: Role[] = ['OWNER', 'ADMIN', 'TEACHER']

interface FormState {
  id?: string
  email: string
  login: string
  full_name: string
  position_ids: string[]
  phone: string
  birth_date: string
  role: Role
  is_active: boolean
  password: string
  must_change: boolean
  incoming_access: boolean
  movement_access: boolean
  equipment_access: boolean
  notes_access: boolean
  /** «кадровик»: журнал замещений целиком + удаление строк замещений */
  subs_access: boolean
  /** «системный администратор»: видит очередь заявок техподдержки и закрывает их (v1.12) */
  helpdesk_access: boolean
}

const emptyForm = (actorRole: Role): FormState => ({
  email: '',
  login: '',
  full_name: '',
  position_ids: [],
  phone: '',
  birth_date: '',
  role: actorRole === 'OWNER' ? 'ADMIN' : 'TEACHER',
  is_active: true,
  password: '',
  must_change: false,
  incoming_access: false,
  movement_access: false,
  equipment_access: false,
  notes_access: false,
  subs_access: false,
  helpdesk_access: false,
})

export function UsersPage() {
  const me = useAuthStore((s) => s.user)!
  const toast = useToast()
  const qc = useQueryClient()
  const canManage = can(me.capabilities, CAP.usersManage)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [bulkEditOpen, setBulkEditOpen] = useState(false)
  const [bulkEditBusy, setBulkEditBusy] = useState(false)
  const [bulkEditForm, setBulkEditForm] = useState<{
    add: string[]
    remove: string[]
    incoming: '' | 'true' | 'false'
    movement: '' | 'true' | 'false'
    equipment: '' | 'true' | 'false'
    active: '' | 'true' | 'false'
  }>({ add: [], remove: [], incoming: '', movement: '', equipment: '', active: '' })

  const [searchParams] = useSearchParams()
  const [showDeleted, setShowDeleted] = useState(false)
  // ?search=… — переход из глобального поиска (Ctrl+K)
  const [search, setSearch] = useState(() => searchParams.get('search') ?? '')
  const [debounced, setDebounced] = useState(() => searchParams.get('search') ?? '')
  const [role, setRole] = useState<Role | ''>('')
  const [page, setPage] = useState(1)
  const [form, setForm] = useState<FormState | null>(null)
  const [adOpen, setAdOpen] = useState(false)
  const [bulkOpen, setBulkOpen] = useState(false)
  const [posOpen, setPosOpen] = useState(false)
  const positionsQuery = useQuery({ queryKey: ['/meta/positions'], queryFn: positionsApi.list })
  const [busy, setBusy] = useState(false)
  /** действие Владельца, требующее ПЭП «код + слово»: удаление учётки / сброс аутентификатора */
  const [signFor, setSignFor] = useState<{ kind: 'delete' | 'totpReset'; user: User } | null>(null)

  const params = useMemo(
    () => ({
      search: debounced || undefined,
      role: role || undefined,
      include_deleted: me.role === 'OWNER' && showDeleted ? true : undefined,
      page,
      per_page: PER_PAGE,
    }),
    [debounced, role, page, showDeleted, me.role],
  )

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: qk('/users', params as never),
    queryFn: () => usersApi.list(params),
  })

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['/users'] })
    void qc.invalidateQueries({ queryKey: ['/dashboard'] })
  }

  function toggleSelect(id: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  async function submitBulk() {
    const payload: Record<string, unknown> = { user_ids: Array.from(selected) }
    if (bulkEditForm.add.length) payload.position_ids_add = bulkEditForm.add
    if (bulkEditForm.remove.length) payload.position_ids_remove = bulkEditForm.remove
    if (bulkEditForm.incoming !== '') payload.incoming_journal_access = bulkEditForm.incoming === 'true'
    if (bulkEditForm.movement !== '') payload.movement_access = bulkEditForm.movement === 'true'
    if (bulkEditForm.equipment !== '') payload.equipment_access = bulkEditForm.equipment === 'true'
    if (bulkEditForm.active !== '') payload.is_active = bulkEditForm.active === 'true'
    setBulkEditBusy(true)
    try {
      const res = await usersApi.bulkUpdate(payload as never)
      const skipped = res.skipped.length
      toast.success(
        `Обновлено сотрудников: ${res.updated}` + (skipped ? ` · пропущено: ${skipped} (${res.skipped[0].reason})` : ''),
      )
      setBulkEditOpen(false)
      setSelected(new Set())
      setBulkEditForm({ add: [], remove: [], incoming: '', movement: '', equipment: '', active: '' })
      invalidate()
      void refetch()
    } catch (err) {
      toast.error(err)
    } finally {
      setBulkEditBusy(false)
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!form) return
    setBusy(true)
    try {
      if (form.id) {
        const payload: Record<string, unknown> = {
          full_name: form.full_name.trim(),
          position_ids: form.position_ids,
          phone: form.phone.trim() || null,
          birth_date: form.birth_date || null,
          role: form.role,
          is_active: form.is_active,
        }
        if (form.email.trim()) payload.email = form.email.trim().toLowerCase()
        if (me.role === 'OWNER' && form.login.trim() && form.login.trim() !== editing?.login) {
          payload.login = form.login.trim().toLowerCase()
        }
        if (form.password.trim()) payload.password = form.password.trim()
        if (me.role === 'OWNER' && editing?.auth_source !== 'ldap') {
          payload.password_must_change = form.must_change
        }
        if (me.role === 'OWNER') {
          payload.incoming_journal_access = form.incoming_access
          payload.movement_access = form.movement_access
          payload.equipment_access = form.equipment_access
          payload.notes_full_access = form.notes_access
          payload.substitutions_access = form.subs_access
          payload.helpdesk_access = form.helpdesk_access
        }
        await usersApi.update(form.id, payload as never)
        toast.success('Данные сотрудника обновлены')
      } else {
        await usersApi.create({
          email: form.email.trim() ? form.email.trim().toLowerCase() : undefined,
          login: me.role === 'OWNER' && form.login.trim() ? form.login.trim().toLowerCase() : undefined,
          password: form.password,
          full_name: form.full_name.trim(),
          role: form.role,
          position_ids: form.position_ids,
          phone: form.phone.trim() || undefined,
          birth_date: form.birth_date || undefined,
        })
        toast.success('Учётная запись создана')
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

  /**
   * Удаление учётки и сброс чужого аутентификатора — операции максимального уровня:
   * сервер требует ПЭП «код + слово» и без подписи отвечает 409, поэтому кнопка
   * лишь открывает окно подписи, а запрос выполняется здесь.
   */
  async function runSigned(sign: { code: string; word: string }) {
    if (!signFor) return
    const { kind, user } = signFor
    try {
      if (kind === 'delete') {
        await usersApi.remove(user.id, sign)
        toast.success('Учётная запись удалена (история сохранена)')
      } else {
        await usersApi.resetTotp(user.id, sign)
        toast.success(`Аутентификатор сброшен: ${user.full_name} может привязать приложение заново`)
      }
      invalidate()
      void refetch()
    } catch (err) {
      toast.error(err)
      throw err            // окно подписи останется открытым — можно повторить
    }
  }

  async function restoreUser(u: User) {
    try {
      await usersApi.restore(u.id)
      toast.success('Учётка восстановлена: задайте логин/e-mail и включите её в редактировании')
      invalidate()
      void refetch()
    } catch (err) {
      toast.error(err)
    }
  }

  const editing = form?.id ? data?.items.find((u) => u.id === form.id) ?? null : null
  const selfEdit = form?.id === me.id

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div className="page-head__text">
          <h1>Пользователи</h1>
          <div className="page-head__hint">
            {me.role === 'OWNER'
              ? 'Полное управление доступом: роли, блокировки, сброс паролей.'
              : 'Сотрудники школы. Назначайте задачи и управляйте учётными записями учителей.'}
          </div>
        </div>
        <div className="page-head__actions">
          {canManage && (
            <button className="btn" onClick={() => setAdOpen(true)} title="Найти сотрудников в каталоге домена и завести им учётки">
              🌐 Импорт из AD
            </button>
          )}
          {canManage && (
            <button className="btn" onClick={() => setBulkOpen(true)} title="Завести учётки таблицей: ФИО, ПАРОЛЬ[, e-mail[, роль]]">
              ⬆ Массовая загрузка
            </button>
          )}
          {canManage && (
            <button className="btn" onClick={() => setPosOpen(true)} title="Справочник должностей: назначение, синонимы для импорта">
              ⚙ Должности
            </button>
          )}
          {canManage && (
            <button className="btn btn--primary" onClick={() => setForm(emptyForm(me.role))}>
              + Локальная учётка
            </button>
          )}
        </div>
      </div>

      <section className="card">
        <div className="card__body">
          <div className="filters">
            <label className="field field--grow">
              <span className="field__label">Поиск</span>
              <input
                className="input"
                value={search}
                onChange={(e) => { setSearch(e.target.value); setPage(1) }}
                onBlur={() => setDebounced(search)}
                onKeyDown={(e) => { if (e.key === 'Enter') { setDebounced(search); setPage(1) } }}
                placeholder="ФИО, e-mail или должность…"
              />
            </label>
            {me.role === 'OWNER' && (
              <label className="row small" style={{ gap: 8, alignSelf: 'flex-end', paddingBottom: 8 }}>
                <input type="checkbox" checked={showDeleted} onChange={(e) => { setShowDeleted(e.target.checked); setPage(1) }} />
                показывать удалённые
              </label>
            )}
            <label className="field">
              <span className="field__label">Роль</span>
              <select className="select" value={role} onChange={(e) => { setRole(e.target.value as Role | ''); setPage(1) }}>
                <option value="">Все роли</option>
                {ROLES.filter((r) => me.role === 'OWNER' || r !== 'OWNER').map((r) => (
                  <option key={r} value={r}>{ROLE_LABEL_FULL[r]}</option>
                ))}
              </select>
            </label>
          </div>
        </div>
      </section>

      <section className="card">
        {isLoading ? (
          <Spinner label="Загружаем список сотрудников…" />
        ) : error ? (
          <ErrorState error={error} onRetry={() => void refetch()} />
        ) : !data || data.items.length === 0 ? (
          <EmptyState icon="👥" title="Никого не найдено" hint="Измените условия поиска" />
        ) : (
          <>
            {canManage && selected.size > 0 && (
              <div className="row" style={{ gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
                <Badge tone="brand">отмечено: {selected.size}</Badge>
                <button
                  className="btn btn--sm btn--primary"
                  onClick={() => {
                    setBulkEditForm({ add: [], remove: [], incoming: '', movement: '', equipment: '', active: '' })
                    setBulkEditOpen(true)
                  }}
                >
                  ✎ Изменить отмеченных
                </button>
                <button className="btn btn--sm" onClick={() => setSelected(new Set())}>✕ Снять выделение</button>
                <span className="tiny muted">массово: должности и гранты модулей</span>
              </div>
            )}
            <table className="table">
              <thead>
                <tr>
                  {canManage && (
                    <th style={{ width: 36 }}>
                      <input
                        type="checkbox"
                        title="Отметить всех на странице"
                        checked={data.items.length > 0 && data.items.every((u) => selected.has(u.id))}
                        onChange={(e) =>
                          setSelected(e.target.checked ? new Set(data.items.map((u) => u.id)) : new Set())
                        }
                      />
                    </th>
                  )}
                  <th>Сотрудник</th>
                  <th style={{ width: 220 }}>Должность</th>
                  <th style={{ width: 150 }}>Роль</th>
                  <th style={{ width: 130 }}>Статус</th>
                  <th style={{ width: 160 }}>Последний вход</th>
                  <th style={{ width: 130 }} />
                </tr>
              </thead>
              <tbody>
                {data.items.map((u) => (
                  <tr key={u.id}>
                    {canManage && (
                      <td>
                        <input
                          type="checkbox"
                          checked={selected.has(u.id)}
                          onChange={() => toggleSelect(u.id)}
                          title="Отметить для массового изменения"
                        />
                      </td>
                    )}
                    <td>
                      <div className="user-chip">
                        <UserAvatar user={u} />
                        <div>
                          <div className="user-chip__name">
                            {u.full_name}
                            {u.id === me.id && <Badge tone="brand">это вы</Badge>}
                            {u.is_deleted && <Badge tone="muted">удалён</Badge>}
                            {u.password_must_change && !u.is_deleted && <Badge tone="warn">смена пароля</Badge>}
                          </div>
                          <div className="tiny muted">
                            {u.email ?? <span title="Вход по логину: e-mail не задан">логин: <span className="mono">{u.login}</span></span>}
                          </div>
                        </div>
                      </div>
                    </td>
                    <td className="small">{(u.positions || []).map((p) => p.title).join(', ') || '—'}</td>
                    <td><RoleBadge role={u.role} /></td>
                    <td>
                      {u.is_active ? <Badge tone="ok" dot>Активен</Badge> : <Badge tone="muted" dot>Заблокирован</Badge>}
                    </td>
                    <td className="small nowrap">
                      {u.last_login_at ? formatDateTime(u.last_login_at) : 'не входил'}
                      <div className="tiny muted">создан {formatDate(u.created_at)}</div>
                    </td>
                    <td>
                      <div className="row" style={{ gap: 6, justifyContent: 'flex-end' }}>
                        {canManage && (
                          <button
                            className="btn btn--sm"
                            disabled={!canManageRole(me.role, u.role) && u.id !== me.id}
                            onClick={() =>
                              setForm({
                                id: u.id,
                                email: u.email ?? '',
                                login: u.login ?? '', 
                                full_name: u.full_name,
                                position_ids: (u.positions || []).map((pp) => pp.id),
                                phone: u.phone ?? '',
                                birth_date: (u.birth_date ?? '').slice(0, 10),
                                role: u.role,
                                is_active: u.is_active,
                                password: '',
                                must_change: u.password_must_change ?? false,
                                incoming_access: u.incoming_journal_access ?? false,
                                movement_access: u.movement_access ?? false,
                                equipment_access: u.equipment_access ?? false,
                                notes_access: u.notes_full_access ?? false,
                                subs_access: u.substitutions_access ?? false,
                                helpdesk_access: u.helpdesk_access ?? false,
                              })
                            }
                          >
                            ✎ Изменить
                          </button>
                        )}
                        {me.role === 'OWNER' && u.id !== me.id && u.is_deleted && (
                          <button className="btn btn--sm" onClick={() => void restoreUser(u)}>♻ Восстановить</button>
                        )}
                        {me.role === 'OWNER' && u.id !== me.id && !u.is_deleted && u.totp_enabled && (
                          <button
                            className="btn btn--sm"
                            title="Снять привязку приложения-аутентификатора: сотрудник потерял телефон или удалил приложение"
                            onClick={() => setSignFor({ kind: 'totpReset', user: u })}
                          >
                            🔑 Сбросить аутентификатор
                          </button>
                        )}
                        {me.role === 'OWNER' && u.id !== me.id && !u.is_deleted && (
                          <button
                            className="btn btn--sm btn--danger"
                            onClick={() => setSignFor({ kind: 'delete', user: u })}
                          >
                            🗑 Удалить
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Pagination page={data.page} pages={data.pages} total={data.total} perPage={data.per_page} onPage={setPage} />
          </>
        )}
      </section>

      {me.role === 'OWNER' && <ADDiagnostics />}

      <BulkImportModal
        open={bulkOpen}
        actorRole={me.role}
        onClose={() => setBulkOpen(false)}
        onDone={() => { invalidate(); void refetch() }}
      />

      <ADImportModal
        open={adOpen}
        actorRole={me.role}
        onClose={() => setAdOpen(false)}
        onDone={() => { invalidate(); void refetch() }}
      />

      {posOpen && canManage && <PositionsModal onClose={() => setPosOpen(false)} />}

      <Modal
        open={form !== null}
        onClose={() => setForm(null)}
        title={form?.id ? 'Редактирование сотрудника' : 'Новая локальная учётка'}
        footer={
          <>
            <button className="btn" onClick={() => setForm(null)} disabled={busy}>Отмена</button>
            <button className="btn btn--primary" form="user-form" type="submit" disabled={busy}>
              {busy ? 'Сохраняем…' : 'Сохранить'}
            </button>
          </>
        }
      >
        {form && (
          <form id="user-form" onSubmit={submit} className="stack" style={{ gap: 16 }}>
            <section>
              <div className="form-section__title">Учётная запись</div>
              <div className="form-grid">
                <label className="field" style={{ gridColumn: '1 / -1' }}>
                  <span className="field__label">ФИО *</span>
                  <input
                    className="input"
                    required
                    value={form.full_name}
                    onChange={(e) => setForm({ ...form, full_name: e.target.value })}
                    placeholder="Фамилия Имя Отчество"
                  />
                </label>
                <label className="field">
                  <span className="field__label">E-mail (необязательно)</span>
                  <input className="input" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
                  <span className="field__hint">
                    {form.id
                      ? 'Пусто — вход по логину; очистка поля при редактировании e-mail не меняет'
                      : 'Без e-mail вход по логину (транслит ФИО) — он появится в списке'}
                  </span>
                </label>
                <label className="field">
                  <span className="field__label">Логин</span>
                  <input
                    className="input mono"
                    value={form.login}
                    disabled={me.role !== 'OWNER'}
                    onChange={(e) => setForm({ ...form, login: e.target.value })}
                    placeholder={form.id ? '' : 'пусто — сгенерируется из ФИО'}
                  />
                  <span className="field__hint">
                    {me.role === 'OWNER' ? 'Задаётся и меняется только Владельцем' : 'Назначает и меняет только Владелец'}
                  </span>
                </label>
                <label className="field">
                  <span className="field__label">Роль *</span>
                  <select
                    className="select"
                    value={form.role}
                    disabled={selfEdit}
                    onChange={(e) => setForm({ ...form, role: e.target.value as Role })}
                  >
                    {ROLES.filter((r) => canManageRole(me.role, r) || r === form.role).map((r) => (
                      <option key={r} value={r}>{ROLE_LABEL_FULL[r]}</option>
                    ))}
                  </select>
                  <span className="field__hint">
                    {me.role === 'OWNER' ? 'Владелец может назначать любую роль' : 'Администрация назначает только роль «Учитель»'}
                  </span>
                </label>
                {editing?.auth_source === 'ldap' ? (
                  <div className="field">
                    <span className="field__label">Пароль</span>
                    <div className="alert alert--info small" style={{ margin: 0 }}>
                      Учётная запись домена: пароль меняется в Active Directory
                      {editing.domain_account ? ` (${editing.domain_account})` : ''}.
                    </div>
                  </div>
                ) : (
                  <label className="field">
                    <span className="field__label">{form.id ? 'Новый пароль (необязательно)' : 'Пароль *'}</span>
                    <input
                      className="input"
                      type="password"
                      minLength={8}
                      required={!form.id}
                      value={form.password}
                      onChange={(e) => setForm({ ...form, password: e.target.value })}
                      placeholder={form.id ? 'оставьте пустым, чтобы не менять' : 'минимум 8 символов'}
                    />
                  </label>
                )}
              </div>
            </section>

            <section>
              <div className="form-section__title">Кадровые данные</div>
              <div className="form-grid">
                <label className="field">
                  <span className="field__label">Телефон</span>
                  <input className="input" value={form.phone} onChange={(e) => setForm({ ...form, phone: e.target.value })} placeholder="+7 …" />
                </label>
                <label className="field">
                  <span className="field__label">Дата рождения</span>
                  <input
                    className="input"
                    type="date"
                    value={form.birth_date}
                    onChange={(e) => setForm({ ...form, birth_date: e.target.value })}
                  />
                  <span className="field__hint">Необязательно: для виджета «Дни рождения»</span>
                </label>
                <div className="field" style={{ gridColumn: '1 / -1' }}>
                  <span className="field__label">Должности (можно несколько)</span>
                  <div className="pos-pick">
                    {(positionsQuery.data ?? []).map((p) => (
                      <label className="pos-pick__item" key={p.id}>
                        <input
                          type="checkbox"
                          checked={form.position_ids.includes(p.id)}
                          onChange={(e) =>
                            setForm({
                              ...form,
                              position_ids: e.target.checked
                                ? [...form.position_ids, p.id]
                                : form.position_ids.filter((x) => x !== p.id),
                            })
                          }
                        />
                        <span>{p.title}</span>
                      </label>
                    ))}
                    {!(positionsQuery.data ?? []).length && <span className="tiny muted">справочник пуст</span>}
                  </div>
                  <span className="field__hint">Справочник должностей — кнопка «⚙ Должности»</span>
                </div>
              </div>
            </section>

            {form.id && (
              <section>
                <div className="form-section__title">Статус и доступы</div>
                <div className="stack" style={{ gap: 8 }}>
                  {form.id && !selfEdit && (
                    <label className="row small" style={{ gap: 8 }}>
                      <input type="checkbox" checked={form.is_active} onChange={(e) => setForm({ ...form, is_active: e.target.checked })} />
                      <span><strong>Учётная запись активна</strong> <span className="tiny muted">— снимите, чтобы заблокировать вход</span></span>
                    </label>
                  )}
                  {form.id && me.role === 'OWNER' && editing?.auth_source !== 'ldap' && (
                    <label className="row small" style={{ gap: 8 }}>
                      <input type="checkbox" checked={form.must_change} onChange={(e) => setForm({ ...form, must_change: e.target.checked })} />
                      <span><strong>Требовать смену пароля</strong> <span className="tiny muted">— при следующем входе (флаг снимется после смены)</span></span>
                    </label>
                  )}
                  {me.role === 'OWNER' && (
                    <div className="stack" style={{ gap: 6, marginTop: 2 }}>
                      {(
                        [
                          ['incoming_access', 'Журнал входящих писем', 'по умолчанию журнал видит только Владелец'],
                          ['movement_access', 'Раздел «Движение обучающихся»', 'по умолчанию — только Владелец'],
                          ['equipment_access', 'Учёт оборудования и инвентаря', 'по умолчанию — только Владелец'],
                          ['notes_access', 'Все служебные записки', 'по умолчанию каждый видит только свои записки'],
                          ['subs_access', 'Кадровик: замещения', 'журнал замещений целиком и удаление строк; должность значения не имеет'],
                          ['helpdesk_access', 'Системный администратор (техподдержка)', 'видит всю очередь заявок, берёт в работу и закрывает; сотрудники подают заявки и без гранта'],
                        ] as const
                      ).map(([key, title, hint]) => (
                        <label key={key} className="row small" style={{ gap: 8 }}>
                          <input
                            type="checkbox"
                            checked={form[key]}
                            onChange={(e) => setForm({ ...form, [key]: e.target.checked })}
                          />
                          <span><strong>{title}</strong> <span className="tiny muted">— {hint}</span></span>
                        </label>
                      ))}
                    </div>
                  )}
                </div>
              </section>
            )}

            {editing && (
              <div className="tiny muted">
                Логин для входа: <span className="mono">{editing.login}</span> · текущая роль:{' '}
                {ROLE_LABEL_FULL[editing.role]} · последний вход:{' '}
                {editing.last_login_at ? formatDateTime(editing.last_login_at) : 'не входил'}
              </div>
            )}
          </form>
        )}
      </Modal>

      <Modal
        open={bulkEditOpen}
        onClose={() => { if (!bulkEditBusy) setBulkEditOpen(false) }}
        title={`Массовое изменение: ${selected.size} сотр.`}
        footer={
          <>
            <button className="btn" disabled={bulkEditBusy} onClick={() => setBulkEditOpen(false)}>Отмена</button>
            <button className="btn btn--primary" disabled={bulkEditBusy} onClick={() => void submitBulk()}>
              {bulkEditBusy ? 'Применяем…' : 'Применить к отмеченным'}
            </button>
          </>
        }
      >
        <div className="stack" style={{ gap: 12 }}>
          <div className="field">
            <span className="field__label">Добавить должности (например, «Руководитель ШМО»)</span>
            <div className="stack" style={{ gap: 4, maxHeight: 140, overflowY: 'auto' }}>
              {(positionsQuery.data ?? []).map((p) => (
                <label key={p.id} className="row small" style={{ gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={bulkEditForm.add.includes(p.id)}
                    onChange={(e) =>
                      setBulkEditForm({
                        ...bulkEditForm,
                        add: e.target.checked ? [...bulkEditForm.add, p.id] : bulkEditForm.add.filter((x) => x !== p.id),
                        remove: bulkEditForm.remove.filter((x) => x !== p.id),
                      })
                    }
                  />
                  {p.title}
                </label>
              ))}
            </div>
          </div>
          <div className="field">
            <span className="field__label">Убрать должности</span>
            <div className="stack" style={{ gap: 4, maxHeight: 120, overflowY: 'auto' }}>
              {(positionsQuery.data ?? []).map((p) => (
                <label key={p.id} className="row small" style={{ gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={bulkEditForm.remove.includes(p.id)}
                    onChange={(e) =>
                      setBulkEditForm({
                        ...bulkEditForm,
                        remove: e.target.checked ? [...bulkEditForm.remove, p.id] : bulkEditForm.remove.filter((x) => x !== p.id),
                        add: bulkEditForm.add.filter((x) => x !== p.id),
                      })
                    }
                  />
                  {p.title}
                </label>
              ))}
            </div>
          </div>
          {me.role === 'OWNER' && (
            <div className="form-grid">
              {([
                ['incoming', 'Доступ к журналу входящих'],
                ['movement', 'Доступ к «Движение»'],
                ['equipment', 'Доступ к «Инвентарь»'],
                ['active', 'Активен (не заблокирован)'],
              ] as const).map(([key, label]) => (
                <label className="field" key={key}>
                  <span className="field__label">{label}</span>
                  <select
                    className="select"
                    value={bulkEditForm[key]}
                    onChange={(e) => setBulkEditForm({ ...bulkEditForm, [key]: e.target.value as never })}
                  >
                    <option value="">— не менять —</option>
                    <option value="true">Да</option>
                    <option value="false">Нет</option>
                  </select>
                </label>
              ))}
            </div>
          )}
          <div className="tiny muted">
            Изменения применяются ко всем отмеченным; учётки, которые вам недоступны по уровню
            роли, будут пропущены с указанием причины. Событие попадает в журнал одной записью.
          </div>
        </div>
      </Modal>

      {/* ПЭП «код + слово» для операций Владельца: удаление учётки, сброс аутентификатора */}
      <PepSignModal
        open={signFor !== null}
        title={signFor?.kind === 'delete' ? 'Удаление учётной записи — подпись' : 'Сброс аутентификатора — подпись'}
        description={
          signFor?.kind === 'delete'
            ? `Учётная запись «${signFor?.user.full_name ?? ''}» будет скрыта из списков, вход закроется, `
              + 'e-mail и логин освободятся. История (приказы, задачи, ознакомления) сохранится; '
              + 'действие обратимо — Владелец может восстановить учётку.'
            : `Привязка приложения-аутентификатора «${signFor?.user.full_name ?? ''}» будет снята: `
              + 'секрет и контрольное слово уничтожаются, электронная подпись кодом станет недоступна, '
              + 'пока сотрудник не привяжет приложение заново. Сотрудник получит уведомление, '
              + 'действие попадёт в журнал событий.'
        }
        submitLabel={signFor?.kind === 'delete' ? 'Подписать и удалить' : 'Подписать и сбросить'}
        onClose={() => setSignFor(null)}
        onConfirm={runSigned}
      />
    </div>
  )
}


/** Панель диагностики подключения к Active Directory (только Владелец). */
function ADDiagnostics() {
  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: qk('/auth/ad/diagnostics'),
    queryFn: authApi.adDiagnostics,
  })
  const [open, setOpen] = useState(false)

  if (isLoading) return null
  const diag = data as
    | { enabled?: boolean; hint?: string; config?: Record<string, unknown>; problems?: string[]; ping?: Record<string, unknown> }
    | undefined

  if (!diag) return null
  if (diag.enabled === false) {
    return (
      <section className="card">
        <div className="card__body">
          <div className="alert alert--warn small">
            <strong>Active Directory не включена.</strong> {diag.hint ?? 'Установите LDAP_ENABLED=true в .env'} —
            сейчас доступен только вход по локальным учётным записям SchoolHub.
          </div>
        </div>
      </section>
    )
  }

  const problems = diag.problems ?? []
  const ping = (diag.ping ?? {}) as { ok?: boolean; error?: string; server?: string; skipped?: boolean }

  return (
    <section className="card">
      <header className="card__head">
        <h3>Диагностика Active Directory</h3>
        <span className="grow" />
        {ping.ok ? (
          <Badge tone="ok" dot>DC отвечает</Badge>
        ) : ping.skipped ? (
          <Badge tone="warn" dot>не проверялось</Badge>
        ) : (
          <Badge tone="danger" dot>нет связи с DC</Badge>
        )}
        <button className="btn btn--sm" onClick={() => void refetch()} disabled={isFetching}>
          {isFetching ? 'Проверяем…' : '↻ Проверить'}
        </button>
        <button className="btn btn--sm btn--ghost" onClick={() => setOpen((v) => !v)}>
          {open ? 'Скрыть детали' : 'Детали'}
        </button>
      </header>
      <div className="card__body stack" style={{ gap: 10 }}>
        {problems.length > 0 && (
          <div className="alert alert--error small">
            <strong>Требуется настройка:</strong>
            <ul style={{ margin: '6px 0 0 18px', padding: 0 }}>
              {problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          </div>
        )}
        {ping.ok === false && ping.error && <div className="alert alert--error small">Ответ каталога: {ping.error}</div>}
        {problems.length === 0 && ping.ok && (
          <div className="alert alert--info small">
            Конфигурация корректна, контроллер домена доступен. Сотрудники могут входить под доменными учётками.
          </div>
        )}

        {open && (
          <pre className="mono" style={{ background: 'var(--surface-2)', padding: 12, borderRadius: 8, overflow: 'auto', maxHeight: 320 }}>
            {JSON.stringify({ config: diag.config, ping: diag.ping }, null, 2)}
          </pre>
        )}
      </div>
    </section>
  )
}
