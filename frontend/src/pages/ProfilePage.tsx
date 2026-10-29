/** Профиль: данные учётной записи, фото (аватар), смена пароля и набор прав роли.
 *  Смена пароля — только для локальных учёток (доменные пароли живёт в AD).
 *  После смены пароля все сессии отзываются сервером — клиента разлогинивает. */
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { QRCodeSVG } from 'qrcode.react'

import { authApi, metaApi, systemApi, totpApi, usersApi } from '@/api/endpoints'
import type { TotpSetupResult } from '@/api/endpoints'
import { PepSignModal } from '@/components/PepSignModal'
import { ApiError, downloadAuthorized } from '@/api/client'
import { clearTokens } from '@/api/client'
import { qk } from '@/api/query'
import { Modal } from '@/components/Modal'
import { Badge, Section } from '@/components/ui'
import { UserAvatar, dropAvatarCache } from '@/components/UserAvatar'
import { useToast } from '@/components/Toast'
import { CAP, ROLE_LABEL } from '@/lib/permissions'
import { formatDateTime, formatSize } from '@/lib/format'
import { authStore, useAuthStore } from '@/store/auth'

const CAPTION: Record<string, string> = {
  [CAP.ordersRead]: 'Просмотр опубликованных приказов',
  [CAP.ordersManage]: 'Создание и редактирование приказов, загрузка документов',
  [CAP.ordersAck]: 'Подтверждение ознакомления с приказами',
  [CAP.ordersAckReport]: 'Лист ознакомления и выгрузка в CSV',
  [CAP.tasksReadOwn]: 'Просмотр только своих задач',
  [CAP.tasksReadAll]: 'Просмотр всех задач школы',
  [CAP.tasksCreate]: 'Создание задач',
  [CAP.tasksAssign]: 'Назначение исполнителей и дедлайна',
  [CAP.tasksDelete]: 'Удаление задач',
  [CAP.plansRead]: 'Просмотр планов работы на неделю',
  [CAP.plansManage]: 'Создание, публикация и архивирование планов работы',
  [CAP.usersRead]: 'Просмотр справочника сотрудников',
  [CAP.usersManage]: 'Создание и редактирование учётных записей',
  [CAP.usersManageAdmins]: 'Управление администраторами и владельцем',
  [CAP.auditRead]: 'Чтение журнала событий',
  [CAP.dashboardGlobal]: 'Сводка по всей школе',
}

export function ProfilePage() {
  const user = useAuthStore((s) => s.user)!
  const toast = useToast()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const avatarRef = useRef<HTMLInputElement>(null)
  const [pwd, setPwd] = useState({ current: '', next: '', confirm: '' })
  const [pwdBusy, setPwdBusy] = useState(false)
  const [avatarBusy, setAvatarBusy] = useState(false)
  const [backupBusy, setBackupBusy] = useState(false)
  const [restoreOpen, setRestoreOpen] = useState(false)
  const [restoreFile, setRestoreFile] = useState<File | null>(null)
  const [restoreWord, setRestoreWord] = useState('')
  const [restoreBusy, setRestoreBusy] = useState(false)
  const [schoolName, setSchoolName] = useState<string | null>(null)
  const [schoolBusy, setSchoolBusy] = useState(false)
  const [filesZipBusy, setFilesZipBusy] = useState(false)
  const [lockdownBusy, setLockdownBusy] = useState(false)
  const [filesRestoreOpen, setFilesRestoreOpen] = useState(false)
  const [filesRestoreFile, setFilesRestoreFile] = useState<File | null>(null)
  const [filesRestoreBusy, setFilesRestoreBusy] = useState(false)
  const [filesReport, setFilesReport] = useState<Awaited<ReturnType<typeof systemApi.filesRestore>> | null>(null)

  const { data: schoolMeta } = useQuery({ queryKey: qk('/meta/school'), queryFn: metaApi.school })
  useEffect(() => {
    if (schoolMeta && schoolName === null) setSchoolName(schoolMeta.school_name ?? '')
  }, [schoolMeta, schoolName])

  async function saveSchool() {
    setSchoolBusy(true)
    try {
      await metaApi.updateSchool((schoolName ?? '').trim())
      void qc.invalidateQueries({ queryKey: ['/meta/school'] })
      toast.success('Название школы сохранено')
    } catch (err) {
      toast.error(err)
    } finally {
      setSchoolBusy(false)
    }
  }

  const { data: matrix } = useQuery({
    queryKey: qk('/meta/permissions'),
    queryFn: metaApi.permissions,
  })

  // «Файлы школы»: сводка по файловому хранилищу (только Владелец)
  const isOwner = user.role === 'OWNER'

  // ── ПЭП: привязка аутентификатора (TOTP), контрольное слово, подпись кодом+словом ──
  const [pepBusy, setPepBusy] = useState(false)
  const [pepSetup, setPepSetup] = useState<TotpSetupResult | null>(null)
  const [pepCode, setPepCode] = useState('')
  const [pepWord, setPepWord] = useState('')
  const [pepWordSaved, setPepWordSaved] = useState(!!user.recovery_word_set)
  /** отвязка ДЕЙСТВУЮЩЕГО аутентификатора: сервер требует текущий код (иначе 409) */
  const [removeOpen, setRemoveOpen] = useState(false)
  const [removeCode, setRemoveCode] = useState('')
  const [removeError, setRemoveError] = useState<string | null>(null)
  /** резервный сброс аутентификатора Владельца без телефона (пароль + контрольное слово) */
  const [bgOpen, setBgOpen] = useState(false)
  const [bgPassword, setBgPassword] = useState('')
  const [bgWord, setBgWord] = useState('')
  const [bgAgree, setBgAgree] = useState(false)
  const [bgBusy, setBgBusy] = useState(false)
  /** окно подписи для чувствительных операций (восстановление БД/файлов, локдаун) */
  const [signAction, setSignAction] = useState<
    'dbRestore' | 'filesRestore' | 'lockdownOn' | 'lockdownOff' | 'dbBackup' | 'filesZip' | null
  >(null)

  async function pepDoSetup() {
    setPepBusy(true)
    try {
      const res = await totpApi.setup()
      setPepSetup(res)
      setPepCode('')
    } catch (err) {
      toast.error(err)
    } finally {
      setPepBusy(false)
    }
  }

  async function pepDoConfirm() {
    if (!/^\d{6}$/.test(pepCode)) return
    setPepBusy(true)
    try {
      await totpApi.confirm(pepCode)
      setPepSetup(null)
      setPepCode('')
      void qc.invalidateQueries({ queryKey: ['/users/me'] })
      void qc.invalidateQueries({ queryKey: ['/auth/me'] })
      authStore.set({ user: { ...user, totp_enabled: true } })
      toast.success('Аутентификатор привязан. Электронная подпись активна.')
    } catch (err) {
      toast.error(err)
    } finally {
      setPepBusy(false)
    }
  }

  async function pepDoRemove() {
    setPepBusy(true)
    setRemoveError(null)
    try {
      // код обязателен: без него сервер отвечает 409 TOTP_CODE_REQUIRED —
      // иначе украденный access-токен снял бы чужой аутентификатор и обнулил ПЭП
      await totpApi.remove(removeCode.trim())
      setPepSetup(null)
      setRemoveOpen(false)
      setRemoveCode('')
      void qc.invalidateQueries({ queryKey: ['/users/me'] })
      void qc.invalidateQueries({ queryKey: ['/auth/me'] })
      authStore.set({ user: { ...user, totp_enabled: false, recovery_word_set: false } })
      setPepWordSaved(false)
      toast.success('Аутентификатор отвязан, секрет уничтожен')
    } catch (err) {
      // ошибку показываем в окне, а не тостом: пользователь сразу видит, что ввести
      const message = err instanceof Error ? err.message : String(err)
      setRemoveError(message)
      setRemoveCode('')
      if (err instanceof ApiError && err.code === 'TOTP_CODE_REQUIRED') {
        setRemoveError('Введите текущий код из приложения-аутентификатора. '
          + 'Если телефон утерян — попросите Владельца сбросить привязку в вашей карточке сотрудника.')
      }
    } finally {
      setPepBusy(false)
    }
  }

  /** Отмена НЕПОДТВЕРЖДЁННОЙ привязки: секрет ещё не активен, код не требуется.
   *  Без этого запроса «висящий» секрет остался бы в базе до следующей привязки. */
  async function pepCancelSetup() {
    setPepBusy(true)
    try {
      await totpApi.remove()
    } catch {
      // не критично: секрет не подтверждён и подписью не является
    } finally {
      setPepSetup(null)
      setPepCode('')
      setPepBusy(false)
    }
  }

  async function pepDoSetWord() {
    if (!pepWord.trim()) return
    setPepBusy(true)
    try {
      await totpApi.setWord(pepWord.trim(), pepCode)
      setPepWord('')
      setPepCode('')
      setPepWordSaved(true)
      void qc.invalidateQueries({ queryKey: ['/users/me'] })
      toast.success('Контрольное слово сохранено. Подпись «код + слово» доступна.')
    } catch (err) {
      toast.error(err)
    } finally {
      setPepBusy(false)
    }
  }

  /** выполняет действие с подписью «код + слово»; вызывается из PepSignModal */
  async function pepRunSignedAction(sign: { code: string; word: string }) {
    const action = signAction
    if (!action) return
    try {
      if (action === 'dbRestore') {
        await doRestoreWithSign(sign)
      } else if (action === 'filesRestore') {
        await doFilesRestoreWithSign(sign)
      } else if (action === 'dbBackup') {
        await doBackup(sign)
      } else if (action === 'filesZip') {
        await doFilesZip(sign)
      } else {
        await toggleLockdownWithSign(sign, action === 'lockdownOn')
      }
      setSignAction(null)
    } catch (err) {
      toast.error(err)
      throw err // окно должно остаться открытым
    }
  }

  const { data: lockdown, refetch: refetchLockdown } = useQuery({
    queryKey: qk('/system/lockdown'),
    queryFn: systemApi.lockdownStatus,
    enabled: isOwner,
  })

  async function toggleLockdownWithSign(sign?: { code: string; word: string }, forceEnabled?: boolean) {
    setLockdownBusy(true)
    try {
      const res = await systemApi.lockdownSet(forceEnabled ?? !lockdown?.enabled, sign)
      void refetchLockdown()
      void qc.invalidateQueries({ queryKey: ['/system/lockdown'] })
      toast.success(res.enabled ? 'Локдаун включён: данные скрыты от всех, кроме вас' : 'Локдаун выключен')
    } catch (err) {
      toast.error(err)
    } finally {
      setLockdownBusy(false)
    }
  }
  const { data: filesSummary } = useQuery({
    queryKey: qk('/system/files-summary'),
    queryFn: systemApi.filesSummary,
    enabled: isOwner,
  })

  async function doFilesZip(sign: { code: string; word: string }) {
    setFilesZipBusy(true)
    try {
      // архив содержит ВСЕ документы школы: сначала подпись, потом одноразовый билет
      const { ticket } = await systemApi.exportTicket('files_zip', sign)
      const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-')
      await downloadAuthorized(systemApi.filesZipPath, `schoolhub-files-${stamp}.zip`, { ticket })
      toast.success('Архив файлов школы выгружен')
    } finally {
      setFilesZipBusy(false)
    }
  }

  async function doFilesRestoreWithSign(sign: { code: string; word: string }) {
    if (!filesRestoreFile) return
    setFilesRestoreBusy(true)
    try {
      const report = await systemApi.filesRestore(filesRestoreFile, sign)
      setFilesRestoreOpen(false)
      setFilesRestoreFile(null)
      setFilesReport(report)
      void qc.invalidateQueries({ queryKey: ['/system/files-summary'] })
    } finally {
      setFilesRestoreBusy(false)
    }
  }

  const allCaps = matrix?.capabilities.map((c) => c.capability) ?? Object.keys(CAPTION)
  const isLdap = user.auth_source === 'ldap'

  async function changePassword(e: FormEvent) {
    e.preventDefault()
    if (pwd.next !== pwd.confirm) {
      toast.error('Новый пароль и повтор не совпадают')
      return
    }
    setPwdBusy(true)
    try {
      await authApi.changePassword(pwd.current, pwd.next)
      toast.success('Пароль изменён. Войдите заново с новым паролем.')
      clearTokens()
      authStore.set({ status: 'guest', user: null })
      navigate('/login')
    } catch (err) {
      toast.error(err)
    } finally {
      setPwdBusy(false)
    }
  }

  /**
   * Резервный (аварийный) сброс аутентификатора Владельца.
   *
   * Штатный путь «Владелец подписывает сброс кодом» здесь не работает: подписывать
   * нечем — устройство с приложением-аутентификатором утрачено. Поэтому подпись
   * заменяется парой факторов, которые Владелец знает: локальный пароль + контрольное
   * слово, заданное ЗАРАНЕЕ. Сервер отзывает ВСЕ сессии (включая текущую) и снимает
   * cookie — после успеха интерфейс обязан вернуть пользователя на экран входа.
   */
  async function doBreakGlass() {
    setBgBusy(true)
    try {
      await totpApi.breakGlass(bgPassword, bgWord)
      toast.success('Привязка сброшена. Войдите заново и привяжите новое устройство.')
      clearTokens()
      authStore.set({ status: 'guest', user: null })
      navigate('/login')
    } catch (err) {
      toast.error(err)
    } finally {
      setBgBusy(false)
    }
  }

  async function doBackup(sign: { code: string; word: string }) {
    setBackupBusy(true)
    try {
      // дамп раскрывает все персональные данные: скачивание только по подписи ПЭП.
      // Билет одноразовый и живёт 2 минуты, поэтому повторное нажатие кнопки
      // запрашивает подпись заново — это ожидаемое поведение.
      const { ticket } = await systemApi.exportTicket('db_backup', sign)
      const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-')
      await downloadAuthorized(systemApi.backupPath, `schoolhub-backup-${stamp}.sql`, { ticket })
      toast.success('Резервная копия выгружена')
    } finally {
      setBackupBusy(false)
    }
  }

  async function doRestoreWithSign(sign: { code: string; word: string }) {
    if (!restoreFile) return
    setRestoreBusy(true)
    try {
      const res = await systemApi.restore(restoreFile, sign)
      toast.success(res.detail || 'База данных восстановлена')
      // сессии хранятся в БД и заменены вместе с ней — выходим и входим заново
      clearTokens()
      authStore.set({ status: 'guest', user: null })
      navigate('/login')
    } finally {
      setRestoreBusy(false)
    }
  }

  async function uploadAvatar(file: File) {
    setAvatarBusy(true)
    try {
      const updated = await usersApi.uploadMyAvatar(file)
      dropAvatarCache(user.id)
      authStore.set({ user: { ...user, avatar: updated.avatar } })
      void qc.invalidateQueries({ queryKey: ['/users'] })
      toast.success('Фото загружено')
    } catch (err) {
      toast.error(err)
    } finally {
      setAvatarBusy(false)
    }
  }

  async function removeAvatar() {
    setAvatarBusy(true)
    try {
      await usersApi.deleteMyAvatar()
      dropAvatarCache(user.id)
      authStore.set({ user: { ...user, avatar: null } })
      void qc.invalidateQueries({ queryKey: ['/users'] })
      toast.success('Фото удалено')
    } catch (err) {
      toast.error(err)
    } finally {
      setAvatarBusy(false)
    }
  }

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div className="page-head__text">
          <h1>Мой профиль</h1>
          <div className="page-head__hint">Данные учётной записи и права, назначенные вашей ролью.</div>
        </div>
      </div>

      <div className="grid grid--2" style={{ alignItems: 'start' }}>
        <Section title="Учётная запись">
          <div className="row" style={{ gap: 14 }}>
            <UserAvatar user={user} size="lg" />
            <div>
              <div style={{ fontSize: 17, fontWeight: 650 }}>{user.full_name}</div>
              <div className="small muted">{(user.positions || []).map((p) => p.title).join(', ') || 'должность не указана'}</div>
              <div style={{ marginTop: 6 }}>
                <Badge tone="brand">{ROLE_LABEL[user.role]}</Badge>
              </div>
            </div>
          </div>
          <dl className="stack" style={{ gap: 8, margin: '16px 0 0' }}>
            <Info label="E-mail" value={user.email ?? '—'} />
            {user.login && <Info label="Логин для входа" value={user.login} />}
            <Info label="Телефон" value={user.phone ?? '—'} />
            <Info label="Роль в системе" value={ROLE_LABEL[user.role]} />
            <Info
              label="Источник учётки"
              value={isLdap ? `Active Directory${user.domain_account ? ` (${user.domain_account})` : ''}` : 'локальная (SchoolHub)'}
            />
            <Info label="Учётная запись" value={user.is_active ? 'активна' : 'заблокирована' } />
          </dl>
        </Section>

        {isLdap ? (
          <Section title="Пароль домена">
            <div className="stack" style={{ gap: 10 }}>
              <div className="alert alert--info small">
                Вы входите под доменной учётной записью{' '}
                <strong className="mono">{user.domain_account ?? user.email}</strong>. Пароль управляется
                в Active Directory: смените его комбинацией <strong>Ctrl+Alt+Del → «Сменить пароль»</strong>{' '}
                на рабочем компьютере или обратитесь к администратору домена.
              </div>
              {user.ad_groups && user.ad_groups.length > 0 && (
                <div>
                  <div className="field__label" style={{ marginBottom: 6 }}>Группы Active Directory</div>
                  <div className="row" style={{ gap: 6 }}>
                    {user.ad_groups.slice(0, 12).map((g) => (
                      <Badge key={g} tone="muted">{g}</Badge>
                    ))}
                    {user.ad_groups.length > 12 && <span className="tiny muted">+{user.ad_groups.length - 12}</span>}
                  </div>
                </div>
              )}
              {user.last_ad_sync_at && (
                <div className="tiny muted">Данные синхронизированы с AD: {formatDateTime(user.last_ad_sync_at)}</div>
              )}
            </div>
          </Section>
        ) : (
          <Section title="Смена пароля">
            <form className="stack" style={{ gap: 10 }} onSubmit={(e) => void changePassword(e)}>
              <label className="field">
                <span className="field__label">Текущий пароль</span>
                <input className="input" type="password" autoComplete="current-password" value={pwd.current} onChange={(e) => setPwd({ ...pwd, current: e.target.value })} required />
              </label>
              <label className="field">
                <span className="field__label">Новый пароль</span>
                <input className="input" type="password" autoComplete="new-password" minLength={8} value={pwd.next} onChange={(e) => setPwd({ ...pwd, next: e.target.value })} required />
                <span className="field__hint">Минимум 8 символов</span>
              </label>
              <label className="field">
                <span className="field__label">Повторите новый пароль</span>
                <input className="input" type="password" autoComplete="new-password" minLength={8} value={pwd.confirm} onChange={(e) => setPwd({ ...pwd, confirm: e.target.value })} required />
              </label>
              <div>
                <button className="btn btn--primary" type="submit" disabled={pwdBusy}>
                  {pwdBusy ? 'Меняем…' : 'Сменить пароль'}
                </button>
              </div>
              <div className="tiny muted">
                После смены пароля все сессии (включая эту) завершатся — потребуется вход заново.
              </div>
            </form>
          </Section>
        )}

        <Section title="Фото (аватар)">
          <div className="row" style={{ gap: 14, alignItems: 'center' }}>
            <UserAvatar user={user} size="lg" />
            <div className="stack" style={{ gap: 8 }}>
              <div className="row" style={{ gap: 8 }}>
                <button className="btn btn--sm" disabled={avatarBusy} onClick={() => avatarRef.current?.click()}>
                  {avatarBusy ? 'Загружаем…' : 'Загрузить фото'}
                </button>
                {user.avatar && (
                  <button className="btn btn--sm btn--danger" disabled={avatarBusy} onClick={() => void removeAvatar()}>Удалить</button>
                )}
              </div>
              <input
                ref={avatarRef}
                type="file"
                accept="image/jpeg,image/png,image/webp"
                style={{ display: 'none' }}
                onChange={(e) => { const f = e.target.files?.[0]; if (f) void uploadAvatar(f); e.target.value = '' }}
              />
              <span className="tiny muted">JPEG, PNG или WebP до 5 МБ. Фото видно коллегам: в списке пользователей, в шапке и в профиле.</span>
            </div>
          </div>
        </Section>
      </div>

      {/* ── ПЭП: электронная подпись через приложение-аутентификатор (TOTP) ── */}
      <Section title="Электронная подпись (ПЭП)">
        <div className="stack" style={{ gap: 12 }}>
          <div className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            {user.totp_enabled ? (
              <>
                <Badge tone="ok">✓ Аутентификатор привязан</Badge>
                <span className="small muted">Подпись кодом доступна. </span>
                {pepWordSaved || user.recovery_word_set ? (
                  <Badge tone="ok">Контрольное слово задано</Badge>
                ) : (
                  <Badge tone="warn">Слово не задано — уровень «код + слово» недоступен</Badge>
                )}
                <span className="grow" />
                <button
                  className="btn btn--sm btn--danger"
                  disabled={pepBusy}
                  onClick={() => { setRemoveCode(''); setRemoveError(null); setRemoveOpen(true) }}
                >
                  Отвязать
                </button>
              </>
            ) : pepSetup ? (
              <Badge tone="warn">Ожидает подтверждения первым кодом</Badge>
            ) : (
              <Badge tone="muted">Не привязан</Badge>
            )}
          </div>

          <div className="small muted">
            Одноразовые шестизначные коды (TOTP) поддерживает любое приложение-аутентификатор:
            Google Authenticator, Aegis, FreeOTP, Яндекс Ключ. Уровень подписи каждого приказа
            («без подписи», «ознакомлен кликом», «код», «код + слово») задаёт администрация;
            приказы о движении обучающихся подписываются только кодом. Если аутентификатор
            не привязан, подписание такого документа блокируется — пароль вместо кода не принимается.
          </div>

          {!user.totp_enabled && !pepSetup && (
            <button className="btn btn--primary" disabled={pepBusy} onClick={() => void pepDoSetup()}>
              {pepBusy ? 'Готовим секрет…' : '🔑 Привязать аутентификатор'}
            </button>
          )}

          {pepSetup && (
            <div className="card" style={{ padding: 14, background: 'var(--surface-2)' }}>
              <div className="stack" style={{ gap: 10 }}>
                <strong className="small">Шаг 1. Добавьте ключ в приложение</strong>
                <div className="small muted">
                  Секрет выдаётся один раз и больше не показывается. Отсканируйте QR или введите ключ вручную:
                </div>
                <div className="row" style={{ gap: 14, flexWrap: 'wrap', alignItems: 'center' }}>
                  <div style={{ padding: 10, background: '#fff', borderRadius: 8, border: '1px solid var(--border)' }} title="QR-код для приложения-аутентификатора">
                    <QRCodeSVG value={pepSetup.otpauth_uri} size={168} level="M" />
                  </div>
                  <div className="stack" style={{ gap: 8, flex: 1, minWidth: 220 }}>
                    <div className="mono small" style={{ wordBreak: 'break-all', userSelect: 'all', padding: 8, border: '1px solid var(--border)', borderRadius: 6 }}>
                      {pepSetup.secret}
                    </div>
                    <div className="tiny muted mono" style={{ wordBreak: 'break-all' }}>{pepSetup.otpauth_uri}</div>
                  </div>
                </div>
                <strong className="small">Шаг 2. Подтвердите первым кодом</strong>
                <form
                  className="row"
                  style={{ gap: 8, alignItems: 'flex-end' }}
                  onSubmit={(e) => { e.preventDefault(); void pepDoConfirm() }}
                >
                  <label className="field">
                    <span className="field__label">Код из приложения</span>
                    <input
                      className="input"
                      inputMode="numeric"
                      maxLength={6}
                      placeholder="000000"
                      value={pepCode}
                      onChange={(e) => setPepCode(e.target.value.replace(/\D/g, ''))}
                      autoComplete="one-time-code"
                    />
                  </label>
                  <button className="btn btn--success" type="submit" disabled={pepBusy || !/^\d{6}$/.test(pepCode)}>
                    {pepBusy ? 'Проверяем…' : 'Подтвердить привязку'}
                  </button>
                  <button className="btn" type="button" disabled={pepBusy} onClick={() => void pepCancelSetup()}>
                    Отменить
                  </button>
                </form>
              </div>
            </div>
          )}

          {user.totp_enabled && (
            <div className="card" style={{ padding: 14, background: 'var(--surface-2)' }}>
              <div className="stack" style={{ gap: 10 }}>
                <strong className="small">Контрольное слово (подпись «код + слово»)</strong>
                <div className="tiny muted">
                  Требуется для самых чувствительных операций: восстановление из копии, очистка
                  журнала событий, локдаун, удаление учётной записи. Слово хранится в секрете —
                  при смене достаточно нового кода из приложения.
                </div>
                <form
                  className="row"
                  style={{ gap: 8, alignItems: 'flex-end', flexWrap: 'wrap' }}
                  onSubmit={(e) => { e.preventDefault(); void pepDoSetWord() }}
                >
                  <label className="field field--grow">
                    <span className="field__label">{pepWordSaved ? 'Новое контрольное слово' : 'Контрольное слово'}</span>
                    <input
                      className="input"
                      type="password"
                      autoComplete="new-password"
                      value={pepWord}
                      onChange={(e) => setPepWord(e.target.value)}
                      minLength={4}
                    />
                  </label>
                  <label className="field">
                    <span className="field__label">Текущий код</span>
                    <input
                      className="input"
                      inputMode="numeric"
                      maxLength={6}
                      placeholder="000000"
                      value={pepCode}
                      onChange={(e) => setPepCode(e.target.value.replace(/\D/g, ''))}
                      autoComplete="one-time-code"
                    />
                  </label>
                  <button
                    className="btn btn--primary"
                    type="submit"
                    disabled={pepBusy || !pepWord.trim() || !/^\d{6}$/.test(pepCode)}
                  >
                    {pepBusy ? 'Сохраняем…' : pepWordSaved ? 'Сменить слово' : 'Задать слово'}
                  </button>
                </form>
              </div>
            </div>
          )}
        </div>
      </Section>

      {/* ── Резервный доступ: что делать, если телефон с аутентификатором утрачен ── */}
      {isOwner && (
        <Section title="Резервный доступ (потеря устройства)">
          <div className="stack" style={{ gap: 10 }}>
            <div className="small muted">
              Все чувствительные операции Владельца подписываются кодом из приложения-аутентификатора:
              восстановление из копии, выход из локдауна, сброс привязки сотрудника. Если устройство
              утрачено, подписывать нечем — и без резервной процедуры доступ возвращался только правкой
              базы данных на NAS вручную.
            </div>
            <div className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              {user.recovery_word_set || pepWordSaved ? (
                <Badge tone="ok">Контрольное слово задано — резервный сброс возможен</Badge>
              ) : (
                <Badge tone="warn">Контрольное слово не задано</Badge>
              )}
              <span className="grow" />
              <button
                className="btn btn--sm btn--danger"
                disabled={!(user.recovery_word_set || pepWordSaved)}
                title={user.recovery_word_set || pepWordSaved
                  ? 'Сбросить привязку аутентификатора по паролю и контрольному слову'
                  : 'Сначала задайте контрольное слово в блоке выше'}
                onClick={() => { setBgPassword(''); setBgWord(''); setBgAgree(false); setBgOpen(true) }}
              >
                🚨 Сбросить аутентификатор без телефона
              </button>
            </div>
            <div className="tiny muted">
              Заданное контрольное слово хранится зашифрованным и не показывается. Резервный сброс
              подтверждается локальным паролем и этим словом, закрывает все сессии Владельца
              на всех устройствах и уведомляет администрацию школы.
            </div>
          </div>
        </Section>
      )}

      <Section title="Ваши права (capabilities)">
        <p className="small muted">
          Набор прав приходит с сервера вместе с профилем и управляет интерфейсом. Фактические проверки
          выполняются на API — даже при подмене клиента сервер вернёт 403.
        </p>
        <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 8 }}>
          {allCaps.map((cap) => {
            const granted = user.capabilities.includes(cap)
            return (
              <div key={cap} className="row" style={{ gap: 8, padding: '7px 10px', borderRadius: 8, background: granted ? 'var(--ok-bg)' : 'var(--muted-bg)' }}>
                <span>{granted ? '✅' : '⛔'}</span>
                <div className="grow">
                  <div className="small strong">{CAPTION[cap] ?? cap}</div>
                  <div className="tiny muted mono">{cap}</div>
                </div>
              </div>
            )
          })}
        </div>
      </Section>

      {user.role === 'OWNER' && (
        <Section
          title="Данные школы"
          actions={
            <span className="row" style={{ gap: 8, alignItems: 'center' }}>
              <span className="tiny muted">Версия SchoolHub</span>
              <Badge tone="muted">{schoolMeta?.version ?? '—'}</Badge>
            </span>
          }
        >
          <div className="stack" style={{ gap: 10 }}>
            <div className="small muted">
              Название школы используется в печатных формах (план работы, приказы по движению).
            </div>
            <div className="row small" style={{ gap: 8, alignItems: 'baseline', flexWrap: 'wrap' }}>
              <span className="muted">Установленная версия:</span>
              <span className="strong mono">{schoolMeta?.version ?? '—'}</span>
              <span className="tiny muted">
                сверяйте перед обновлением и сообщайте при обращении в поддержку
              </span>
            </div>
            <form
              className="row"
              style={{ gap: 8, alignItems: 'flex-end' }}
              onSubmit={(e) => {
                e.preventDefault()
                void saveSchool()
              }}
            >
              <label className="field field--grow">
                <span className="field__label">Полное название школы</span>
                <input className="input" value={schoolName ?? ''} onChange={(e) => setSchoolName(e.target.value)} placeholder="ОГАОУ «Лицей ядерных технологий» при НИЯУ МИФИ" />
              </label>
              <button className="btn btn--primary" type="submit" disabled={schoolBusy}>
                {schoolBusy ? 'Сохраняем…' : 'Сохранить'}
              </button>
            </form>
          </div>
        </Section>
      )}

      {user.role === 'OWNER' && (
        <Section title="Резервное копирование">
          <div className="stack" style={{ gap: 10 }}>
            <div className="small muted">
              Кнопка выгружает полный дамп PostgreSQL (plain SQL): пользователи, приказы,
              задачи, планы и журнал. Ежедневные копии с ротацией делайте скриптом
              <span className="mono"> backup-db.sh</span> через планировщик DSM; восстановление —
              <span className="mono"> restore-db.sh</span> (оба в deploy/synology).
            </div>
            <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
              <button
                className="btn"
                disabled={backupBusy}
                onClick={() => setSignAction('dbBackup')}
              >
                {backupBusy ? 'Готовим дамп…' : '⬇ Скачать резервную копию БД'}
              </button>
              <button
                className="btn btn--danger"
                onClick={() => { setRestoreFile(null); setRestoreWord(''); setRestoreOpen(true) }}
              >
                ⬆ Восстановление из копии
              </button>
            </div>
            <div className="small muted">
              «Восстановление из копии» полностью заменяет базу данных содержимым .sql-файла
              (копия, скачанная кнопкой выше или созданная backup-db.sh).
            </div>

            <div className="card" style={{ marginTop: 14, padding: 14, background: 'var(--surface-2)' }}>
              <div className="row" style={{ gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
                <strong>Локдаун (обезличивание)</strong>
                <span className="grow" />
                <button
                  className={lockdown?.enabled ? 'btn btn--sm btn--danger' : 'btn btn--sm'}
                  disabled={lockdownBusy}
                  onClick={() => setSignAction(lockdown?.enabled ? 'lockdownOff' : 'lockdownOn')}
                >
                  {lockdown?.enabled ? '🔓 Выключить локдаун' : '🔒 Включить локдаун'}
                </button>
              </div>
              <div className="tiny muted">
                Экстренный режим для демонстрации экрана и проверок: ФИО, телефоны, e-mail,
                логины, даты рождения и фото заменяются метками «Сотрудник N», «Ученик N»,
                «Представитель N» для всех ролей, кроме Владельца. Приказы, задачи и журналы
                продолжают работать; редактирование персональных данных на это время отключено.
              </div>
            </div>

            <div className="card" style={{ marginTop: 14, padding: 14, background: 'var(--surface-2)' }}>
              <div className="row" style={{ gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
                <strong>Файлы школы</strong>
                <span className="tiny muted">документы приказов, вложения задач, фото — то, чего нет в дампе БД</span>
                <span className="grow" />
                <button
                  className="btn btn--sm"
                  title="Обновить сводку"
                  onClick={() => void qc.invalidateQueries({ queryKey: ['/system/files-summary'] })}
                >
                  🔄
                </button>
              </div>
              {filesSummary ? (
                <div className="stack" style={{ gap: 6 }}>
                  <div className="row small" style={{ gap: 14, flexWrap: 'wrap' }}>
                    <span>🗄 Файлов на диске: <strong>{filesSummary.files_on_disk}</strong> ({formatSize(filesSummary.bytes_on_disk)})</span>
                    <span>🔗 Ссылок из базы: <strong>{filesSummary.db_references}</strong></span>
                    <span style={{ color: filesSummary.broken_links.length ? 'var(--danger)' : undefined }}>
                      ⚠ Битых ссылок: <strong>{filesSummary.broken_links.length}</strong>
                    </span>
                    <span style={{ color: filesSummary.orphan_files_total ? 'var(--warn)' : undefined }}>
                      📄 Без записи в базе: <strong>{filesSummary.orphan_files_total}</strong>
                    </span>
                  </div>
                  {filesSummary.broken_links.length > 0 && (
                    <div className="alert alert--warn small" style={{ margin: 0 }}>
                      Записи в базе есть, а файлов на диске нет — вложения не откроются, пока файлы
                      не вернутся из архива: {filesSummary.broken_links.slice(0, 5).map((b) => b.storage_path).join(', ')}
                      {filesSummary.broken_links.length > 5 ? '…' : ''}
                    </div>
                  )}
                  <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginTop: 4 }}>
                    <button className="btn" disabled={filesZipBusy} onClick={() => setSignAction('filesZip')}>
                      {filesZipBusy ? 'Собираем архив…' : '⬇ Скачать все файлы (ZIP)'}
                    </button>
                    <button
                      className="btn"
                      onClick={() => { setFilesRestoreFile(null); setFilesRestoreOpen(true) }}
                    >
                      ⬆ Загрузить архив с файлами
                    </button>
                  </div>
                  <div className="tiny muted">
                    В архиве файлы лежат под исходными именами (папка files/) плюс служебный
                    manifest.json с путями хранилища и sha256. При загрузке архива отсутствующие
                    файлы возвращаются на свои пути, одинаковые не трогаются, отличающиеся
                    заменяются версией из архива.
                  </div>
                </div>
              ) : (
                <div className="tiny muted">Сводка загружается…</div>
              )}
            </div>
          </div>
        </Section>
      )}

      <Modal
        open={restoreOpen}
        onClose={() => { if (!restoreBusy) setRestoreOpen(false) }}
        title="Восстановление базы из копии"
        footer={
          <>
            <button className="btn" disabled={restoreBusy} onClick={() => setRestoreOpen(false)}>
              Отмена
            </button>
            <button
              className="btn btn--danger"
              disabled={restoreBusy || !restoreFile || restoreWord.trim().toLowerCase() !== 'восстановить'}
              onClick={() => { setRestoreOpen(false); setSignAction('dbRestore') }}
            >
              {restoreBusy ? 'Восстанавливаем…' : 'Заменить базу и выйти'}
            </button>
          </>
        }
      >
        <div className="stack" style={{ gap: 12 }}>
          <div className="alert alert--error small">
            <strong>Текущие данные будут удалены безвозвратно.</strong> База полностью заменяется
            содержимым файла. Операция выполняется одной транзакцией: если что-то пойдёт не так,
            старые данные сохранятся.
          </div>
          <ul className="small muted" style={{ paddingLeft: 18, margin: 0 }}>
            <li>Используйте .sql-копию, созданную кнопкой «Скачать резервную копию БД» или backup-db.sh.</li>
            <li>После восстановления ВСЕ пользователи выйдут из системы и войдут заново
              (по учетным данным из копии).</li>
            <li>Во время восстановления сервис будет недоступен (обычно меньше минуты).</li>
          </ul>
          <label className="field">
            <span className="field__label">Файл копии (.sql)</span>
            <input
              className="input"
              type="file"
              accept=".sql,application/sql,text/plain"
              disabled={restoreBusy}
              onChange={(e) => setRestoreFile(e.target.files?.[0] ?? null)}
            />
          </label>
          <label className="field">
            <span className="field__label">
              Для подтверждения введите слово <span className="mono strong">восстановить</span>
            </span>
            <input
              className="input"
              value={restoreWord}
              disabled={restoreBusy}
              autoComplete="off"
              onChange={(e) => setRestoreWord(e.target.value)}
              placeholder="восстановить"
            />
          </label>
        </div>
      </Modal>

      <Modal
        open={filesRestoreOpen}
        onClose={() => { if (!filesRestoreBusy) setFilesRestoreOpen(false) }}
        title="Восстановление файлов школы из архива"
        footer={
          <>
            <button className="btn" disabled={filesRestoreBusy} onClick={() => setFilesRestoreOpen(false)}>
              Отмена
            </button>
            <button
              className="btn btn--primary"
              disabled={filesRestoreBusy || !filesRestoreFile}
              onClick={() => { setFilesRestoreOpen(false); setSignAction('filesRestore') }}
            >
              {filesRestoreBusy ? 'Восстанавливаем…' : '⬆ Загрузить и применить'}
            </button>
          </>
        }
      >
        <div className="stack" style={{ gap: 12 }}>
          <div className="small muted">
            Подходит архив, созданный кнопкой «Скачать все файлы (ZIP)»: внутри папка files/
            и manifest.json. Архив без manifest.json будет отклонён. Отсутствие файлов
            вернёт их на прежние пути (ссылки из базы «оживут»), одинаковые файлы не
            трогаются, отличающиеся заменяются версией из архива.
          </div>
          <label className="field">
            <span className="field__label">Архив (.zip)</span>
            <input
              className="input"
              type="file"
              accept=".zip,application/zip"
              disabled={filesRestoreBusy}
              onChange={(e) => setFilesRestoreFile(e.target.files?.[0] ?? null)}
            />
          </label>
        </div>
      </Modal>

      <Modal
        open={filesReport !== null}
        onClose={() => setFilesReport(null)}
        title="Отчёт: восстановление файлов из архива"
        footer={<button className="btn btn--primary" onClick={() => setFilesReport(null)}>Готово</button>}
      >
        {filesReport && (
          <div className="stack" style={{ gap: 10 }}>
            <div className="row small" style={{ gap: 14, flexWrap: 'wrap' }}>
              <span style={{ color: 'var(--ok)' }}>✅ восстановлено: <strong>{filesReport.restored.length}</strong></span>
              <span style={{ color: 'var(--warn)' }}>🔁 заменено: <strong>{filesReport.replaced.length}</strong></span>
              <span>✔ уже на месте: <strong>{filesReport.already.length}</strong></span>
              <span>📄 сирот: <strong>{filesReport.orphans.length}</strong></span>
              <span style={{ color: filesReport.errors.length ? 'var(--danger)' : undefined }}>
                ⚠ ошибок: <strong>{filesReport.errors.length}</strong>
              </span>
            </div>
            {filesReport.restored.length > 0 && (
              <div className="tiny muted">Вернулись на свои пути: {filesReport.restored.join(', ')}</div>
            )}
            {filesReport.replaced.length > 0 && (
              <div className="tiny muted">Заменены версией из архива: {filesReport.replaced.join(', ')}</div>
            )}
            {filesReport.orphans.length > 0 && (
              <div className="tiny muted">
                «Сироты» в архиве (без записи в manifest.json) — не распакованы: {filesReport.orphans.join(', ')}
              </div>
            )}
            {filesReport.errors.length > 0 && (
              <ul className="small" style={{ margin: 0, paddingLeft: 18, color: 'var(--danger)' }}>
                {filesReport.errors.map((e, i) => (
                  <li key={i}>{e.item}: {e.reason}</li>
                ))}
              </ul>
            )}
          </div>
        )}
      </Modal>

      <Section title="Сессия">
        <div className="row small muted">
          <span>Выход завершает сессию на этом устройстве: токены удаляются, событие пишется в журнал.</span>
          <span className="grow" />
          <button className="btn btn--sm" onClick={() => void authStore.logout()}>🚪 Выйти</button>
        </div>
      </Section>

      {/* окно подписи «код + слово» для чувствительных операций */}
      <PepSignModal
        open={signAction !== null}
        title={
          signAction === 'dbRestore' ? 'Восстановление базы из копии — подпись' :
          signAction === 'filesRestore' ? 'Восстановление файлов из архива — подпись' :
          signAction === 'dbBackup' ? 'Выгрузка резервной копии БД — подпись' :
          signAction === 'filesZip' ? 'Выгрузка архива файлов школы — подпись' :
          'Локдаун — подпись'
        }
        description={
          signAction === 'dbRestore' ? 'База будет полностью заменена содержимым копии. Операция необратима.' :
          signAction === 'filesRestore' ? 'Файлы школы будут восстановлены из загруженного ZIP-архива.' :
          signAction === 'dbBackup'
            ? 'Дамп содержит все персональные данные школы: сотрудников, обучающихся, контакты. '
              + 'Подпись выдаёт одноразовый билет на одно скачивание (действует 2 минуты).' :
          signAction === 'filesZip'
            ? 'В архив попадут все документы школы: приказы, вложения задач, сертификаты, фото. '
              + 'Подпись выдаёт одноразовый билет на одно скачивание (действует 2 минуты).' :
          signAction === 'lockdownOn' ? 'Персональные данные будут скрыты от всех, кроме Владельца.' :
          'Локдаун будет выключен, персональные данные снова станут видны ролям по правам.'
        }
        danger={signAction !== 'lockdownOff' && signAction !== 'dbBackup' && signAction !== 'filesZip'}
        submitLabel={
          signAction === 'dbBackup' || signAction === 'filesZip' ? 'Подписать и скачать' : undefined
        }
        onClose={() => setSignAction(null)}
        onConfirm={(sign) => pepRunSignedAction(sign)}
      />

      {/* отвязка аутентификатора: подтверждается текущим кодом из приложения */}
      <Modal
        open={removeOpen}
        onClose={() => setRemoveOpen(false)}
        title="Отвязать аутентификатор"
        width="440px"
        footer={
          <>
            <button className="btn" disabled={pepBusy} onClick={() => setRemoveOpen(false)}>Отмена</button>
            <button
              className="btn btn--danger"
              disabled={pepBusy || !/^\d{6}$/.test(removeCode)}
              onClick={() => void pepDoRemove()}
            >
              {pepBusy ? 'Отвязываем…' : 'Отвязать'}
            </button>
          </>
        }
      >
        <div className="stack" style={{ gap: 12 }}>
          <div className="alert alert--error small">
            Секрет будет уничтожен, контрольное слово сброшено, а электронная подпись кодом
            станет недоступна. Уже подписанные документы сохранятся.
          </div>
          <label className="field">
            <span className="field__label">Текущий код из приложения</span>
            <input
              className="input"
              inputMode="numeric"
              maxLength={6}
              placeholder="000000"
              value={removeCode}
              onChange={(e) => { setRemoveCode(e.target.value.replace(/\D/g, '')); setRemoveError(null) }}
              autoComplete="one-time-code"
              autoFocus
            />
            <span className="field__hint">
              Отвязка действующей привязки подтверждается кодом: так систему не обнулит
              чужой доступ к вашему браузеру.
            </span>
          </label>
          {removeError && <div className="alert alert--error small">{removeError}</div>}
          <div className="tiny muted">
            Телефон утерян или приложение удалено? Попросите Владельца нажать
            «Сбросить аутентификатор» в вашей карточке сотрудника.
          </div>
        </div>
      </Modal>

      {/* резервный сброс Владельца: подпись заменяется паролем + контрольным словом */}
      <Modal
        open={bgOpen}
        onClose={() => setBgOpen(false)}
        title="Резервный сброс аутентификатора"
        width="500px"
        footer={
          <>
            <button className="btn" disabled={bgBusy} onClick={() => setBgOpen(false)}>Отмена</button>
            <button
              className="btn btn--danger"
              disabled={bgBusy || !bgPassword || !bgWord.trim() || !bgAgree}
              onClick={() => void doBreakGlass()}
            >
              {bgBusy ? 'Сбрасываем…' : 'Сбросить привязку и выйти'}
            </button>
          </>
        }
      >
        <div className="stack" style={{ gap: 12 }}>
          <div className="alert alert--error small">
            Аварийная процедура. Привязка аутентификатора будет уничтожена, контрольное слово
            сброшено, а <b>все сессии Владельца на всех устройствах закрыты</b> — вас вернёт
            на экран входа. Администрация школы получит уведомление, действие останется в журнале.
          </div>
          <div className="tiny muted">
            Применяйте только если устройство с приложением-аутентификатором утрачено или
            недоступно. В штатном случае привязку снимает кнопка «Отвязать» выше — она
            подтверждается текущим кодом.
          </div>
          <label className="field">
            <span className="field__label">Пароль учётной записи Владельца</span>
            <input
              className="input"
              type="password"
              autoComplete="current-password"
              value={bgPassword}
              onChange={(e) => setBgPassword(e.target.value)}
              autoFocus
            />
            <span className="field__hint">Проверяется по локальной базе, а не по домену</span>
          </label>
          <label className="field">
            <span className="field__label">Контрольное слово</span>
            <input
              className="input"
              type="password"
              autoComplete="off"
              value={bgWord}
              onChange={(e) => setBgWord(e.target.value)}
            />
            <span className="field__hint">
              Слово, заданное заранее в блоке «Электронная подпись (ПЭП)». Сервер не показывает его.
            </span>
          </label>
          <label className="check">
            <input type="checkbox" checked={bgAgree} onChange={(e) => setBgAgree(e.target.checked)} />
            Я понимаю, что после сброса придётся войти заново и привязать новое устройство
          </label>
          <div className="tiny muted">
            Подбор пароля и слова ограничен: после нескольких неудач процедура блокируется
            на время, а каждая неудачная попытка записывается в журнал событий.
          </div>
        </div>
      </Modal>
    </div>
  )
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div className="row" style={{ gap: 10 }}>
      <dt className="small muted" style={{ flex: '0 0 150px' }}>{label}</dt>
      <dd className="small strong" style={{ margin: 0, flex: 1 }}>{value}</dd>
    </div>
  )
}
