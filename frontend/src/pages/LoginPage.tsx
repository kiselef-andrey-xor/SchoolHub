/** Экран входа. Демо-учётки подсказываются чипами (подставляются в форму). */
import { useEffect, useState, type FormEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { authApi } from '@/api/endpoints'
import { Badge } from '@/components/ui'
import { useToast } from '@/components/Toast'
import { firstName } from '@/lib/format'
import { idleLimitLabel, takeIdleReason } from '@/lib/idle'
import { ROLE_LABEL } from '@/lib/permissions'
import { authStore } from '@/store/auth'
import type { Role } from '@/types'

const DEMO_ACCOUNTS: { email: string; role: Role; hint: string }[] = [
  { email: 'owner@schoolhub.ru', role: 'OWNER', hint: 'полный доступ' },
  { email: 'admin@schoolhub.ru', role: 'ADMIN', hint: 'приказы и все задачи' },
  { email: 'ivanov@schoolhub.ru', role: 'TEACHER', hint: 'только свои задачи' },
]
const DEMO_PASSWORD = 'Demo1234!'
// демо-учётки существуют только если сервер поднят с AUTO_SEED=true
const DEMO_ENABLED = (import.meta.env.VITE_SHOW_DEMO_ACCOUNTS ?? 'false') === 'true'

export function LoginPage() {
  // публичный запрос: показываем пользователю правильный формат логина
  const { data: options } = useQuery({
    queryKey: ['/auth/login-options'],
    queryFn: authApi.loginOptions,
    staleTime: 5 * 60_000,
  })
  const ldapEnabled = options?.ldap_enabled ?? false

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [remember, setRemember] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** «Сеанс завершён из-за неактивности» — ставит таймер простоя (lib/idle.ts) */
  const [idleNotice, setIdleNotice] = useState<string | null>(null)

  const navigate = useNavigate()
  const location = useLocation()
  const toast = useToast()
  const qc = useQueryClient()
  const from = (location.state as { from?: string } | null)?.from ?? '/'

  useEffect(() => {
    if (takeIdleReason()) setIdleNotice(`Сеанс завершён: ${idleLimitLabel()} без активности. Войдите снова.`)
  }, [])

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const me = await authStore.login(email, password, remember)
      qc.clear()
      toast.success(`Добро пожаловать, ${firstName(me.full_name)}!`)
      navigate(from, { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось войти')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-card__logo">
          <div className="sidebar__logo" style={{ width: 40, height: 40, borderRadius: 11 }}>SH</div>
          <div>
            <div className="login-card__title">SchoolHub</div>
            <div className="tiny muted">единая цифровая среда школы</div>
          </div>
        </div>
        <p className="login-card__subtitle">
          Приказы с электронным ознакомлением и постановка задач сотрудникам.
        </p>

        {ldapEnabled ? (
          <div className="alert alert--info small" style={{ marginBottom: 16 }}>
            <strong>Вход через домен</strong>
            <div>
              Используйте свою доменную учётную запись: <span className="mono">ivanov</span>,{' '}
              <span className="mono">ivanov@school.local</span> или{' '}
              <span className="mono">SCHOOL\ivanov</span>. Пароль проверяет контроллер домена и
              нигде не сохраняется.
            </div>
          </div>
        ) : null}

        <form onSubmit={onSubmit} className="stack" style={{ gap: 14 }}>
          {idleNotice && !error && <div className="alert alert--warn">{idleNotice}</div>}
          {error && <div className="alert alert--error">{error}</div>}

          <label className="field">
            <span className="field__label">{ldapEnabled ? 'Доменная учётная запись или e-mail' : 'E-mail или логин'}</span>
            <input
              className="input"
              type="text"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder={ldapEnabled ? (options?.login_hint ?? 'ivanov@school.local') : 'ivanov@schoolhub.ru или ivanov.ivan'}
            />
          </label>

          <label className="field">
            <span className="field__label">Пароль</span>
            <input
              className="input"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
            />
          </label>

          <label className="row small muted" style={{ gap: 8 }}>
            <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
            Запомнить меня на этом устройстве
          </label>

          <button className="btn btn--primary" type="submit" disabled={busy} style={{ padding: '10px 14px' }}>
            {busy ? 'Проверяем…' : 'Войти'}
          </button>
        </form>

        {DEMO_ENABLED && (
        <div className="demo-accounts">
          <div className="demo-accounts__title">Демо-доступ (пароль {DEMO_PASSWORD})</div>
          {DEMO_ACCOUNTS.map((acc) => (
            <button
              key={acc.email}
              type="button"
              className="demo-account"
              onClick={() => {
                setEmail(acc.email)
                setPassword(DEMO_PASSWORD)
                setError(null)
              }}
            >
              <Badge tone={acc.role === 'OWNER' ? 'brand' : acc.role === 'ADMIN' ? 'info' : 'neutral'}>
                {ROLE_LABEL[acc.role]}
              </Badge>
              <span className="small grow">{acc.email}</span>
              <span className="tiny muted">{acc.hint}</span>
            </button>
          ))}
        </div>
        )}
      </div>
    </div>
  )
}
