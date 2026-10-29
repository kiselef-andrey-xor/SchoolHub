/** Справка и инструкции (модуль 4 ТЗ).
 *  Системные карточки (пароль, роли, подпись, локдаун) + загружаемые инструкции администрации.
 *  Категории, поиск; у инструкции флаг «обязательно к прочтению» — лист ознакомления по механизму приказов.
 *  Ознакомление: клик или ПЭП (код TOTP), если включён системный флаг help_require_code.
 *
 *  v1.12: у инструкции НЕСКОЛЬКО вложений с подписями («Приложение 1», «Форма заявления»),
 *  каждое открывается кликом в новой вкладке — сервер отдаёт автоматически созданную
 *  PDF-копию, поэтому Word- и Excel-файлы читаются без установленного Office.
 *
 *  Видимость статьи (visibility) задаёт круг читателей: ALL — всем сотрудникам,
 *  ADMIN — только администрации, OWNER — только Владельцу. Недоступная статья
 *  не приходит ни в списке, ни в категориях, ни в глобальном поиске, а запрос
 *  по id получает 404. Лист ознакомления обязательной инструкции ограничен
 *  тем же кругом, поэтому «долг» по невидимой инструкции не появляется. */
import { useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { helpApi, type HelpArticlePayload } from '@/api/endpoints'
import { DocLink, DocList } from '@/components/DocLink'
import { Modal } from '@/components/Modal'
import { PepSignModal } from '@/components/PepSignModal'
import { Badge, EmptyState, ErrorState, Spinner } from '@/components/ui'
import { useToast } from '@/components/Toast'
import { formatDateTime } from '@/lib/format'
import { CAP, can } from '@/lib/permissions'
import { useAuthStore } from '@/store/auth'
import {
  SIGN_METHOD_LABELS,
  VISIBILITY_BADGE,
  VISIBILITY_LABELS,
  type HelpArticle,
  type HelpVisibility,
} from '@/types'

/** Сколько документов приложено к инструкции (включая перенесённые из старой колонки). */
function fileCount(a: HelpArticle): number {
  return a.files?.length ?? (a.file_name ? 1 : 0)
}

const KIND_LABEL: Record<string, string> = { SYSTEM: 'системная', UPLOAD: 'инструкция' }

export function HelpPage() {
  const toast = useToast()
  const qc = useQueryClient()
  const me = useAuthStore((st) => st.user)
  const isOwner = me?.role === 'OWNER'
  const isStaff = Boolean(me && can(me.capabilities, CAP.helpManage))
  // системные карточки правит только Владелец — у администрации к ним доступ на чтение
  const canEditArticle = (a: HelpArticle | null) => Boolean(a) && (isOwner || (isStaff && a!.kind === 'UPLOAD'))

  const [q, setQ] = useState('')
  const [category, setCategory] = useState('')
  const [visibility, setVisibility] = useState<'' | HelpVisibility>('')
  /** какие значения видимости может задать текущий пользователь (OWNER — все три) */
  const allowedVisibility: HelpVisibility[] = isOwner ? ['ALL', 'ADMIN', 'OWNER'] : ['ALL', 'ADMIN']
  const [openId, setOpenId] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [editArt, setEditArt] = useState<HelpArticle | null>(null)
  const [signArt, setSignArt] = useState<HelpArticle | null>(null)
  /** подпись к файлу, который сейчас прикладываем («Приложение 1», «Бланк заявления») */
  const [caption, setCaption] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  const listQ = useQuery({
    queryKey: ['help', 'list', q, category, visibility],
    queryFn: () => helpApi.list({
      q: q || undefined,
      category: category || undefined,
      visibility: visibility || undefined,
    }),
  })
  const catsQ = useQuery({ queryKey: ['help', 'categories'], queryFn: helpApi.categories })
  const detailQ = useQuery({
    queryKey: ['help', 'detail', openId],
    queryFn: () => helpApi.detail(openId!),
    enabled: Boolean(openId),
  })

  const reload = () => {
    void qc.invalidateQueries({ queryKey: ['help'] })
    void qc.invalidateQueries({ queryKey: ['helpPending'] })
  }

  const createM = useMutation({
    mutationFn: (v: HelpFormValues) => helpApi.create(v),
    onSuccess: (art) => {
      toast.success('Инструкция создана')
      setCreateOpen(false)
      reload()
      // сразу даём прикрепить файл
      if (fileRef.current && art.kind === 'UPLOAD') setOpenId(art.id)
    },
    onError: (e: unknown) => toast.error(e),
  })

  const updateM = useMutation({
    mutationFn: (v: { id: string; patch: Partial<HelpArticlePayload> }) => helpApi.update(v.id, v.patch),
    onSuccess: () => {
      toast.success('Статья обновлена')
      setEditArt(null)
      reload()
    },
    onError: (e: unknown) => toast.error(e),
  })

  const removeM = useMutation({
    mutationFn: (id: string) => helpApi.remove(id),
    onSuccess: () => {
      toast.success('Инструкция удалена')
      setOpenId(null)
      reload()
    },
    onError: (e: unknown) => toast.error(e),
  })

  const uploadM = useMutation({
    mutationFn: async (files: File[]) => {
      // грузим последовательно: порядок вложений в списке имеет смысл
      for (const f of files) await helpApi.uploadFiles(openId!, f, caption)
      return files.length
    },
    onSuccess: (n) => {
      toast.success(n > 1 ? `Прикреплено файлов: ${n}` : 'Файл прикреплён')
      setCaption('')
      if (fileRef.current) fileRef.current.value = ''
      reload()
    },
    onError: (e: unknown) => {
      toast.error(e)
      if (fileRef.current) fileRef.current.value = ''
    },
  })

  const removeFileM = useMutation({
    mutationFn: (v: { id: string; fileId: string; name: string }) => helpApi.removeFile(v.id, v.fileId),
    onSuccess: () => {
      toast.success('Документ откреплён')
      reload()
    },
    onError: (e: unknown) => toast.error(e),
  })

  const ackM = useMutation({
    mutationFn: (v: { id: string; sign?: { code?: string; word?: string } }) => helpApi.ack(v.id, v.sign),
    onSuccess: () => {
      toast.success('Ознакомление подтверждено')
      reload()
    },
    onError: (e: unknown) => {
      toast.error(e)
      // 409 «нужен код / нет привязки» — открываем окно подписи
      if (e && typeof e === 'object' && 'status' in e && (e as { status: number }).status === 409) {
        setSignArt(openArticle ?? null)
      }
    },
  })

  const openArticle = useMemo(
    () => detailQ.data ?? (listQ.data ?? []).find((a) => a.id === openId) ?? null,
    [detailQ.data, listQ.data, openId],
  )

  return (
    <div className="page">
      <div className="page__head">
        <div>
          <h1 className="page__title">Справка и инструкции</h1>
          <p className="page__hint">
            Системные карточки и инструкции администрации. Поиск попадает в глобальный поиск (Ctrl+K).
          </p>
        </div>
        {isStaff && (
          <button className="btn btn--primary" onClick={() => setCreateOpen(true)}>
            ＋ Новая инструкция
          </button>
        )}
      </div>

      <div className="toolbar">
        <input
          className="input"
          style={{ maxWidth: 320 }}
          placeholder="Поиск по названию и тексту…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <select className="input" style={{ maxWidth: 220 }} value={category} onChange={(e) => setCategory(e.target.value)}>
          <option value="">Все категории</option>
          {(catsQ.data ?? []).map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
        {isStaff && (
          <select
            className="input"
            style={{ maxWidth: 240 }}
            value={visibility}
            onChange={(e) => setVisibility(e.target.value as '' | HelpVisibility)}
          >
            <option value="">Любая видимость</option>
            {allowedVisibility.map((v) => (
              <option key={v} value={v}>{VISIBILITY_LABELS[v]}</option>
            ))}
          </select>
        )}
      </div>

      {listQ.isLoading ? <Spinner /> : listQ.isError ? (
        <ErrorState error={listQ.error} onRetry={() => void listQ.refetch()} />
      ) : (listQ.data ?? []).length === 0 ? (
        <EmptyState icon="❔" title="Ничего не найдено" hint="Измените запрос или категорию." />
      ) : (
        <div className="card-grid">
          {(listQ.data ?? []).map((a) => (
            <button key={a.id} className="card card--clickable" onClick={() => setOpenId(a.id)}>
              <div className="card__title">{a.title}</div>
              <div className="card__meta">
                <Badge tone={a.kind === 'SYSTEM' ? 'brand' : 'neutral'}>{KIND_LABEL[a.kind]}</Badge>
                <span>{a.category}</span>
                {fileCount(a) > 0 && (
                  <Badge tone="neutral">📎 {fileCount(a)} {fileCount(a) === 1 ? 'файл' : 'файла(ов)'}</Badge>
                )}
                <span className="muted">обновлено {formatDateTime(a.updated_at)}</span>
                {a.visibility !== 'ALL' && <Badge tone="brand">🔒 {VISIBILITY_BADGE[a.visibility]}</Badge>}
                {a.mandatory_read && <Badge tone="warn">Обязательно к прочтению</Badge>}
                {a.mandatory_read && (
                  a.my_ack_at
                    ? <Badge tone="ok" dot>Ознакомлен · {formatDateTime(a.my_ack_at)}</Badge>
                    : <Badge tone="danger" dot>Не прочитано</Badge>
                )}
              </div>
            </button>
          ))}
        </div>
      )}

      {/* ---------------- просмотр статьи ---------------- */}
      {openArticle && (
        <Modal open onClose={() => setOpenId(null)} title={openArticle.title} width="720px">
          <div className="stack">
            <div className="row gap" style={{ flexWrap: 'wrap' }}>
              <Badge tone={openArticle.kind === 'SYSTEM' ? 'brand' : 'neutral'}>{KIND_LABEL[openArticle.kind]}</Badge>
              <span className="muted">{openArticle.category}</span>
              {openArticle.visibility !== 'ALL' && (
                <Badge tone="brand">🔒 {VISIBILITY_LABELS[openArticle.visibility]}</Badge>
              )}
              <span className="muted">обновлено {formatDateTime(openArticle.updated_at)}</span>
              {openArticle.created_by && <span className="muted">автор: {openArticle.created_by.full_name}</span>}
            </div>

            {openArticle.body && <article className="markdown">{openArticle.body}</article>}

            {/* ---------------- документы инструкции ---------------- */}
            <div>
              <div className="row row--between" style={{ marginBottom: 6 }}>
                <h3 className="section__title">
                  Документы{openArticle.files && openArticle.files.length > 0
                    ? ` (${openArticle.files.length})`
                    : ''}
                </h3>
                {canEditArticle(openArticle) && (
                  <span className="tiny muted">каждый документ открывается в новой вкладке</span>
                )}
              </div>

              {openArticle.files && openArticle.files.length > 0 ? (
                <DocList docs={openArticle.files} empty={null}>
                  {(f) => (
                    <DocLink
                      doc={f}
                      meta={f.uploaded_by ? `добавил(а) ${f.uploaded_by.full_name}` : undefined}
                      onOpen={() => helpApi.openArticleFile(openArticle.id, f.id)}
                      onDownload={() => helpApi.downloadArticleFile(openArticle.id, f.id, f.original_name)}
                      onRemove={canEditArticle(openArticle)
                        ? () => removeFileM.mutate({ id: openArticle.id, fileId: f.id, name: f.original_name })
                        : undefined}
                      removeLabel="Открепить документ"
                    />
                  )}
                </DocList>
              ) : openArticle.file_name ? (
                /* наследство до v1.12: файл есть в старой колонке, но не перенесён в список */
                <div className="row gap" style={{ flexWrap: 'wrap' }}>
                  <button
                    className="btn btn--sm"
                    onClick={() => void helpApi.downloadFile(openArticle.id, openArticle.file_name!)
                      .catch((e: unknown) => toast.error(e))}
                  >
                    ⬇ {openArticle.file_name}
                  </button>
                </div>
              ) : (
                <p className="muted small">Документов нет — инструкция описана текстом выше.</p>
              )}

              {canEditArticle(openArticle) && (
                <div className="file-attach">
                  <input
                    className="input input--sm"
                    placeholder="Подпись к документу (необязательно): Приложение 1, Бланк заявления"
                    value={caption}
                    maxLength={200}
                    onChange={(e) => setCaption(e.target.value)}
                  />
                  <input
                    ref={fileRef}
                    type="file"
                    className="input"
                    multiple
                    disabled={uploadM.isPending}
                    onChange={(e) => {
                      const files = Array.from(e.target.files ?? [])
                      if (files.length) uploadM.mutate(files)
                    }}
                  />
                  {uploadM.isPending && <Spinner label="Загружаем…" />}
                  <span className="tiny muted">
                    К Word-, Excel- и PowerPoint-файлам сервер автоматически создаёт PDF-копию —
                    документ откроется в браузере без установленного Office. Оригинал остаётся доступен для скачивания.
                  </span>
                </div>
              )}
            </div>

            {openArticle.mandatory_read && !openArticle.my_ack_at && (
              <div className="alert alert--warn">
                Эта инструкция обязательна к прочтению. Отметьтесь, пожалуйста, в листе ознакомления.
              </div>
            )}
            {openArticle.mandatory_read && (
              <div className="row gap">
                <button
                  className="btn btn--success"
                  disabled={Boolean(openArticle.my_ack_at) || ackM.isPending}
                  onClick={() => ackM.mutate({ id: openArticle.id })}
                >
                  {openArticle.my_ack_at ? 'Вы уже ознакомились' : '✓ Ознакомлен'}
                </button>
              </div>
            )}

            {/* лист ознакомления — администрации */}
            {openArticle.acks && (
              <div>
                <h3 className="section__title">Лист ознакомления</h3>
                {openArticle.acks.length === 0 ? (
                  <p className="muted">Отметок пока нет.</p>
                ) : (
                  <table className="table">
                    <thead>
                      <tr><th>Сотрудник</th><th>Когда</th><th>Способ подписи</th></tr>
                    </thead>
                    <tbody>
                      {openArticle.acks.map((r) => (
                        <tr key={r.id}>
                          <td>{r.user.full_name}</td>
                          <td>{r.acked_at ? formatDateTime(r.acked_at) : '—'}</td>
                          <td>{SIGN_METHOD_LABELS[r.sign_method ?? ''] ?? '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}

            {/* действия редактора */}
            {canEditArticle(openArticle) && (
              <div className="row gap" style={{ marginTop: 8 }}>
                <button className="btn btn--sm" onClick={() => setEditArt(openArticle)}>Редактировать</button>
                {openArticle.kind === 'UPLOAD' && (
                  <button
                    className="btn btn--sm btn--danger"
                    disabled={removeM.isPending}
                    onClick={() => {
                      if (confirm(`Удалить инструкцию «${openArticle.title}»?`)) removeM.mutate(openArticle.id)
                    }}
                  >
                    Удалить
                  </button>
                )}
                {openArticle.kind === 'SYSTEM' && !isOwner && (
                  <span className="muted">Системные карточки правит только Владелец.</span>
                )}
              </div>
            )}
          </div>
        </Modal>
      )}

      {/* ---------------- создание ---------------- */}
      {createOpen && (
        <NoteFormModal
          title="Новая инструкция"
          categories={catsQ.data ?? []}
          busy={createM.isPending}
          initial={{ title: '', category: 'Инструкции', body: '', mandatory_read: false, visibility: 'ALL' }}
          allowedVisibility={allowedVisibility}
          onClose={() => setCreateOpen(false)}
          onSubmit={(v) => createM.mutate(v)}
        />
      )}

      {/* ---------------- редактирование ---------------- */}
      {editArt && (
        <NoteFormModal
          title="Редактирование статьи"
          categories={[...(catsQ.data ?? []), editArt.category]}
          busy={updateM.isPending}
          initial={{
            title: editArt.title,
            category: editArt.category,
            body: editArt.body ?? '',
            mandatory_read: editArt.mandatory_read,
            visibility: editArt.visibility,
          }}
          allowedVisibility={allowedVisibility}
          onClose={() => setEditArt(null)}
          onSubmit={(v) => updateM.mutate({ id: editArt.id, patch: v })}
        />
      )}

      {/* ---------------- ПЭП для ознакомления ---------------- */}
      <PepSignModal
        open={Boolean(signArt)}
        title="Подпись ознакомления (ПЭП)"
        description={`Инструкция «${signArt?.title ?? ''}» требует усиленную подпись кодом TOTP.`}
        danger={false}
        submitLabel="Подписать «Ознакомлен»"
        onClose={() => setSignArt(null)}
        onConfirm={async ({ code, word }) => {
          await ackM.mutateAsync({ id: signArt!.id, sign: { code, word } })
        }}
      />

    </div>
  )
}

interface HelpFormValues {
  title: string
  category: string
  body: string
  mandatory_read: boolean
  visibility: HelpVisibility
}

function NoteFormModal(props: {
  title: string
  categories: string[]
  busy: boolean
  initial: HelpFormValues
  /** какие значения видимости разрешено выбрать (зависит от роли автора) */
  allowedVisibility: HelpVisibility[]
  onClose: () => void
  onSubmit: (v: HelpFormValues) => void
}) {
  const [v, setV] = useState(props.initial)
  return (
    <Modal
      open
      onClose={props.onClose}
      title={props.title}
      footer={
        <>
          <button className="btn" onClick={props.onClose} disabled={props.busy}>Отмена</button>
          <button
            className="btn btn--primary"
            disabled={props.busy || !v.title.trim()}
            onClick={() => props.onSubmit(v)}
          >
            Сохранить
          </button>
        </>
      }
    >
      <div className="form-grid">
        <label>Название
          <input className="input" value={v.title} onChange={(e) => setV({ ...v, title: e.target.value })} maxLength={300} />
        </label>
        <label>Категория
          <input
            className="input"
            list="help-cats"
            value={v.category}
            onChange={(e) => setV({ ...v, category: e.target.value })}
            maxLength={120}
          />
          <datalist id="help-cats">
            {Array.from(new Set(props.categories)).map((c) => <option key={c} value={c} />)}
          </datalist>
        </label>
        <label>Текст (поддерживается простой markdown)
          <textarea className="input" rows={8} value={v.body} onChange={(e) => setV({ ...v, body: e.target.value })} />
        </label>
        <label>Кто видит эту статью
          <select
            className="input"
            value={v.visibility}
            onChange={(e) => setV({ ...v, visibility: e.target.value as HelpVisibility })}
          >
            {props.allowedVisibility.map((opt) => (
              <option key={opt} value={opt}>{VISIBILITY_LABELS[opt]}</option>
            ))}
          </select>
          <span className="tiny muted">
            {v.visibility === 'ALL'
              ? 'Статья видна всем сотрудникам и попадает в глобальный поиск.'
              : v.visibility === 'ADMIN'
                ? 'Служебная статья: учителя её не видят и не получают по листу ознакомления.'
                : 'Статья только для Владельца: администрация не увидит её и не сможет править.'}
          </span>
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={v.mandatory_read}
            onChange={(e) => setV({ ...v, mandatory_read: e.target.checked })}
          />
          Обязательно к прочтению
          {v.visibility === 'ALL'
            ? ' (создаёт лист ознакомления для всех сотрудников)'
            : ` (лист ознакомления — ${VISIBILITY_LABELS[v.visibility]})`}
        </label>
      </div>
    </Modal>
  )
}
