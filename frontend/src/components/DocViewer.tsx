/** Просмотр документа приказа: PDF — инлайн в iframe, DOCX — скачивание (или PDF-конвертация, если на сервере есть LibreOffice). */
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { blobUrl, downloadAuthorized } from '@/api/client'
import { ordersApi } from '@/api/endpoints'
import { qk } from '@/api/query'
import { Spinner } from '@/components/ui'
import { formatSize } from '@/lib/format'

export function DocViewer({ orderId, fileName }: { orderId: string; fileName?: string }) {
  const { data: meta, isLoading } = useQuery({
    queryKey: qk(`/orders/${orderId}/preview`),
    queryFn: () => ordersApi.previewMeta(orderId),
  })

  const [url, setUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let objectUrl: string | null = null
    let alive = true
    setUrl(null)
    setFailed(false)

    if (!meta) return
    // Загружаем blob с заголовком Authorization (в <iframe src> заголовок не передать)
    void (async () => {
      try {
        objectUrl = await blobUrl(ordersApi.filePath(orderId, meta.file_id, true))
        if (alive) setUrl(objectUrl)
      } catch {
        if (alive) setFailed(true)
      }
    })()

    return () => {
      alive = false
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [meta, orderId])

  if (isLoading) return <Spinner label="Готовим документ к просмотру…" />

  if (!meta) return null

  if (meta.preview === 'download' || failed) {
    return (
      <div className="empty">
        <div className="empty__icon">📄</div>
        <div className="empty__title">Инлайн-просмотр недоступен для этого формата</div>
        <div className="small">
          Документ {meta.original_name} можно скачать и открыть в Word / LibreOffice.
          {meta.converter ? null : ' Чтобы включить превью DOCX прямо в браузере, установите на сервере LibreOffice (см. README).'}
        </div>
        <div style={{ marginTop: 12 }}>
          <button
            className="btn btn--primary"
            onClick={() => void downloadAuthorized(ordersApi.filePath(orderId, meta.file_id, false), meta.original_name)}
          >
            ⬇ Скачать документ
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="row small muted">
        <span>📄 {fileName ?? meta.original_name}</span>
        <span className="grow" />
        <button
          className="btn btn--sm"
          onClick={() => void downloadAuthorized(ordersApi.filePath(orderId, meta.file_id, false), meta.original_name)}
        >
          ⬇ Скачать
        </button>
      </div>
      {url ? (
        <iframe
          className="doc-frame"
          src={url}
          title={`Документ приказа: ${meta.original_name}`}
          // документ отдаётся с X-Content-Type-Options: nosniff и Content-Disposition
        />
      ) : (
        <Spinner label="Загружаем документ…" />
      )}
    </div>
  )
}

export async function downloadOrderFile(orderId: string, fileId?: string, name?: string) {
  await downloadAuthorized(ordersApi.filePath(orderId, fileId, false), name ?? 'document')
}

export { formatSize }
