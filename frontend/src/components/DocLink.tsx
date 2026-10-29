/**
 * Ссылка на документ с учётом автоматической PDF-копии (v1.12).
 *
 * Один компонент на все разделы (служебки, Helpdesk, справки, задачи): поведение
 * «документ открывается кликом в новой вкладке» должно быть одинаковым везде,
 * а дублирование логики по четырём страницам неизбежно разошлось бы.
 *
 * Что показываем по состоянию копии (`pdf_status`):
 *   READY   — «Открыть» (новая вкладка, PDF) + «Скачать» (оригинал);
 *   PENDING — «Открыть» тоже рисуем: сервер доделает копию по запросу, а рядом
 *              подсказка «PDF-копия готовится»;
 *   NA      — только «Скачать» (архив) или «Открыть», если это уже PDF/картинка;
 *   FAILED  — «Скачать» + понятная причина (нет конвертера, повреждённый файл).
 *
 * Оригинал доступен ВСЕГДА: PDF-копия — удобство просмотра, а не замена файла.
 */
import { useState } from 'react'

import { useToast } from '@/components/Toast'
import type { PdfStatus } from '@/types'

export interface DocLike {
  id: string
  original_name: string
  mime_type: string
  size_bytes: number
  pdf_status: PdfStatus
  pdf_note?: string | null
  pdf_ready: boolean
  /** необязательная подпись: «Приложение 1» у документов инструкции */
  caption?: string | null
}

export function formatFileSize(bytes?: number | null): string {
  if (!bytes || bytes <= 0) return '—'
  if (bytes < 1024) return `${bytes} Б`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} КБ`
  return `${(bytes / 1024 / 1024).toFixed(1)} МБ`
}

/** Иконка по MIME: журнал вложений читается глазами. */
export function docIcon(mime: string, name: string): string {
  const m = (mime || '').toLowerCase()
  const n = (name || '').toLowerCase()
  if (m === 'application/pdf' || n.endsWith('.pdf')) return '📕'
  if (m.startsWith('image/')) return '🖼'
  if (m.includes('word') || n.endsWith('.docx') || n.endsWith('.doc')) return '📘'
  if (m.includes('sheet') || m.includes('excel') || n.endsWith('.xlsx') || n.endsWith('.xls') || n.endsWith('.csv')) return '📗'
  if (m.includes('presentation') || n.endsWith('.pptx') || n.endsWith('.ppt')) return '📙'
  if (m.includes('zip') || m.includes('rar') || m.includes('7z')) return '🗜'
  if (m.startsWith('text/')) return '📄'
  return '📎'
}

const PDF_HINT: Record<PdfStatus, { text: string; tone: 'muted' | 'warn' | 'danger' } | null> = {
  READY: null,
  PENDING: { text: 'PDF-копия готовится', tone: 'muted' },
  NA: null,
  FAILED: { text: 'PDF-копию сделать не удалось', tone: 'warn' },
}

export function DocLink({
  doc,
  onOpen,
  onDownload,
  onRemove,
  removeLabel = 'Удалить',
  compact = false,
  meta,
}: {
  doc: DocLike
  /** открыть в новой вкладке (сервер отдаёт PDF-копию); undefined — кнопка не рисуется.
   *  Возврат значения допускается (unknown): вызывающий код может вернуть флаг
   *  «окно заблокировано», а компоненту он не нужен. */
  onOpen?: () => unknown
  onDownload?: () => unknown
  onRemove?: () => void
  removeLabel?: string
  /** только имя и размер, без строки состояния — для плотных списков */
  compact?: boolean
  /** дополнительная подпись справа (кто приложил, роль вложения) */
  meta?: React.ReactNode
}) {
  const toast = useToast()
  const [busy, setBusy] = useState<'open' | 'download' | null>(null)

  const hint = PDF_HINT[doc.pdf_status]
  const hintText = hint?.text ?? (doc.pdf_status === 'NA' ? doc.pdf_note ?? null : null)
  // PDF и картинки браузер показывает сам — «Открыть» имеет смысл даже без копии
  const openable = Boolean(onOpen) && (doc.pdf_ready || doc.pdf_status === 'PENDING'
    || doc.mime_type === 'application/pdf' || doc.mime_type.startsWith('image/'))

  const run = async (kind: 'open' | 'download', fn?: () => unknown) => {
    if (!fn) return
    setBusy(kind)
    try {
      await fn()
    } catch (e) {
      toast.show((e as Error).message, 'error')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className={`doc-link${compact ? ' doc-link--compact' : ''}`}>
      <span className="doc-link__icon" aria-hidden>{docIcon(doc.mime_type, doc.original_name)}</span>
      <span className="doc-link__main">
        <span className="doc-link__name" title={doc.original_name}>
          {doc.caption ? <span className="doc-link__caption">{doc.caption} · </span> : null}
          {doc.original_name}
        </span>
        <span className="doc-link__meta">
          {formatFileSize(doc.size_bytes)}
          {meta ? <> · {meta}</> : null}
          {!compact && hintText ? (
            <span className={`doc-link__hint doc-link__hint--${hint?.tone ?? 'muted'}`}> · {hintText}</span>
          ) : null}
        </span>
      </span>
      <span className="doc-link__actions">
        {openable && (
          <button
            type="button"
            className="btn btn--sm"
            disabled={busy !== null}
            title="Открыть документ в новой вкладке (PDF)"
            onClick={() => void run('open', onOpen)}
          >
            {busy === 'open' ? 'Открываем…' : 'Открыть'}
          </button>
        )}
        {onDownload && (
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            disabled={busy !== null}
            title="Скачать оригинал файла"
            onClick={() => void run('download', onDownload)}
          >
            {busy === 'download' ? '…' : 'Скачать'}
          </button>
        )}
        {onRemove && (
          <button type="button" className="btn btn--sm btn--ghost link--danger" onClick={onRemove} title={removeLabel}>
            ✕
          </button>
        )}
      </span>
    </div>
  )
}

/**
 * Список вложений. Дженерик по типу документа: у разных разделов свои поля
 * (у заявки Helpdesk — `kind` и `uploaded_by`, у приложения справки — `caption`),
 * и колбэку они нужны, а терять типизацию ради общей обёртки не хочется.
 */
export function DocList<T extends DocLike>({ docs, empty, children }: {
  docs: T[]
  empty?: React.ReactNode
  children: (doc: T) => React.ReactNode
}) {
  if (!docs.length) return <>{empty ?? <span className="muted small">Файлов нет</span>}</>
  return <div className="doc-list">{docs.map((d) => <div key={d.id}>{children(d)}</div>)}</div>
}
