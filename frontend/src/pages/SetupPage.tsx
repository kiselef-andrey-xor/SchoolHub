/** Первоначальная настройка: создание первого Владельца через браузер (без терминала). */
import { useState, type FormEvent } from 'react'

import { http } from '@/api/client'
import type { SetupStatus } from '@/types'

interface Props {
  onDone: (email: string) => void
}

export function SetupPage({ onDone }: Props) {
  const [token, setToken] = useState('')
  const [fullName, setFullName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [repeat, setRepeat] = useState('')
  const [school, setSchool] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showHelp, setShowHelp] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError(null)

    if (password !== repeat) {
      setError('Пароли не совпадают')
      return
    }
    if (password.length < 8) {
      setError('Пароль должен быть не короче 8 символов')
      return
    }

    setBusy(true)
    try {
      await http.post('/setup/first-owner', {
        setup_token: token.trim().toUpperCase(),
        email: email.trim(),
        password,
        full_name: fullName.trim(),
        school_name: school.trim() || null,
      })
      onDone(email.trim())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось создать учётную запись')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-card" style={{ maxWidth: 520 }}>
        <div className="login-card__logo">
          <div className="sidebar__logo" style={{ width: 40, height: 40, borderRadius: 11 }}>SH</div>
          <div>
            <div className="login-card__title">Первоначальная настройка</div>
            <div className="tiny muted">SchoolHub · шаг 1 из 1</div>
          </div>
        </div>

        <p className="login-card__subtitle">
          Создайте учётную запись <strong>Владельца</strong> — администратора системы. Он сможет
          добавлять сотрудников, публиковать приказы и назначать задачи.
        </p>

        <form onSubmit={submit} className="stack" style={{ gap: 13 }}>
          {error && <div className="alert alert--error">{error}</div>}

          <label className="field">
            <span className="field__label">Код настройки *</span>
            <input
              className="input mono"
              required
              value={token}
              onChange={(e) => setToken(e.target.value.toUpperCase())}
              placeholder="Например: VW88DT6V"
              style={{ letterSpacing: '0.12em', textTransform: 'uppercase' }}
              autoComplete="off"
            />
            <span className="field__hint">
              Одноразовый код из журнала контейнера backend.{' '}
              <button type="button" className="btn btn--ghost btn--sm" style={{ padding: '0 4px' }} onClick={() => setShowHelp((v) => !v)}>
                {showHelp ? 'Скрыть подсказку' : 'Где взять код?'}
              </button>
            </span>
          </label>

          {showHelp && (
            <div className="alert alert--info small">
              <strong>Synology (Container Manager):</strong>
              <ol style={{ margin: '6px 0 0 18px', padding: 0 }}>
                <li>Откройте проект <span className="mono">schoolhub</span> → контейнер <span className="mono">schoolhub-backend</span>.</li>
                <li>Вкладка <strong>Журнал</strong> (Log) — найдите строку «Введите код настройки: XXXXXXXX».</li>
              </ol>
              <strong style={{ display: 'inline-block', marginTop: 8 }}>Или по SSH:</strong>
              <div className="mono" style={{ marginTop: 4 }}>
                docker exec -it schoolhub-backend python -m scripts.first_owner --show-token
              </div>
              <div style={{ marginTop: 8 }}>
                Код можно выпустить заново:{' '}
                <span className="mono">… first_owner --reset-token</span>
              </div>
            </div>
          )}

          <div className="form-grid">
            <label className="field">
              <span className="field__label">ФИО *</span>
              <input
                className="input"
                required
                minLength={2}
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                placeholder="Соколова Ирина Аркадьевна"
              />
            </label>
            <label className="field">
              <span className="field__label">E-mail (логин) *</span>
              <input
                className="input"
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="admin@school.local"
                autoComplete="username"
              />
            </label>
          </div>

          <label className="field">
            <span className="field__label">Название школы</span>
            <input className="input" value={school} onChange={(e) => setSchool(e.target.value)} placeholder="Например: МАОУ «Лицей № 1»" />
          </label>

          <div className="form-grid">
            <label className="field">
              <span className="field__label">Пароль *</span>
              <input
                className="input"
                type="password"
                required
                minLength={8}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="new-password"
              />
              <span className="field__hint">Не короче 8 символов</span>
            </label>
            <label className="field">
              <span className="field__label">Повторите пароль *</span>
              <input
                className="input"
                type="password"
                required
                minLength={8}
                value={repeat}
                onChange={(e) => setRepeat(e.target.value)}
                autoComplete="new-password"
              />
            </label>
          </div>

          <button className="btn btn--primary" type="submit" disabled={busy} style={{ padding: '10px 14px' }}>
            {busy ? 'Создаём учётную запись…' : 'Создать Владельца и войти'}
          </button>
        </form>

        <div className="demo-accounts">
          <div className="demo-accounts__title">Уже настроено?</div>
          <div className="small muted">
            Если Владелец уже создан, перезагрузите страницу — откроется экран входа.
            Забыли пароль? Сбросьте его на сервере командой{' '}
            <span className="mono">docker exec -it schoolhub-backend python -m scripts.first_owner --force --email … --password …</span>
          </div>
        </div>
      </div>
    </div>
  )
}

export async function fetchSetupStatus(): Promise<SetupStatus> {
  return http.public.get<SetupStatus>('/setup/status')
}
