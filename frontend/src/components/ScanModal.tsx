/** Сканирование штрихкодов инвентаря.
 *
 * Порядок движков декодирования:
 *   1. нативный BarcodeDetector (Chrome/Android, ChromeOS) — без зависимостей;
 *   2. вендорная библиотека /vendor/html5-qrcode.min.js (ZXing-порт) — грузится лениво;
 *   3. ручное поле: аппаратные USB/Bluetooth-сканеры работают как клавиатура —
 *      код прилетает набором текста + Enter, камера вообще не нужна.
 */
import { useEffect, useRef, useState } from 'react'

import { Modal } from '@/components/Modal'
import { useToast } from '@/components/Toast'
import { Spinner } from '@/components/ui'

interface ScanModalProps {
  open: boolean
  onClose: () => void
  /** вернуть true, если код найден в базе (модалка закроется) */
  onCode: (code: string) => Promise<boolean>
}

type Engine = 'starting' | 'native' | 'library' | 'manual' | 'error'

declare global {
  interface Window {
    BarcodeDetector?: new (opts?: { formats?: string[] }) => {
      detect: (src: CanvasImageSource) => Promise<{ rawValue: string }[]>
    }
    HTML5Qrcode?: new (id: string, verbose?: boolean) => {
      start: (
        camera: string | { facingMode: string },
        config: { fps: number; qrbox?: number | { width: number; height: number } },
        onOk: (text: string) => void,
        onErr: () => void,
      ) => Promise<void>
      stop: () => Promise<void>
      clear: () => void
      scanFile: (file: File, showImage?: boolean) => Promise<string>
    }
  }
}

function loadScannerLib(): Promise<void> {
  return new Promise((resolve, reject) => {
    if (window.HTML5Qrcode) return resolve()
    const script = document.createElement('script')
    script.src = `${import.meta.env.BASE_URL}vendor/html5-qrcode.min.js`
    script.onload = () => resolve()
    script.onerror = () => reject(new Error('не удалось загрузить модуль сканирования'))
    document.head.appendChild(script)
  })
}

export function ScanModal({ open, onClose, onCode }: ScanModalProps) {
  const toast = useToast()
  const [engine, setEngine] = useState<Engine>('starting')
  const [engineError, setEngineError] = useState('')
  const [manual, setManual] = useState('')
  const [busy, setBusy] = useState(false)
  const [lastMiss, setLastMiss] = useState('')
  const [fileBusy, setFileBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const videoRef = useRef<HTMLVideoElement>(null)
  const boxRef = useRef<HTMLDivElement>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const libRef = useRef<InstanceType<NonNullable<typeof window.HTML5Qrcode>> | null>(null)
  const aliveRef = useRef(false)
  const handleRef = useRef(onCode)
  handleRef.current = onCode

  async function submit(code: string): Promise<boolean> {
    const clean = code.trim()
    if (!clean) return false
    setBusy(true)
    try {
      const found = await handleRef.current(clean)
      if (!found) setLastMiss(clean)
      return found
    } finally {
      setBusy(false)
    }
  }

  /** Декодирование из фотографии этикетки (работает без камеры и без HTTPS). */
  async function onLabelPhoto(file: File) {
    setFileBusy(true)
    try {
      await loadScannerLib()
      const inst = new window.HTML5Qrcode!('scan-file-mount')
      const code = await inst.scanFile(file, false)
      const ok = await submit(code)
      if (ok) setManual('')
    } catch {
      setLastMiss('фото: ' + file.name)
      toast.error('Не удалось распознать штрихкод на фото. Сфотографируйте крупнее, без бликов.')
    } finally {
      setFileBusy(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  // остановка камеры/библиотеки при закрытии
  function stopAll() {
    aliveRef.current = false
    if (libRef.current) {
      void libRef.current.stop().catch(() => undefined).then(() => libRef.current?.clear())
      libRef.current = null
    }
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
  }

  useEffect(() => {
    if (!open) {
      stopAll()
      setEngine('starting')
      setManual('')
      setLastMiss('')
      return
    }
    aliveRef.current = true
    let cancelled = false

    void (async () => {
      // камера браузером отдаётся только в secure-context (HTTPS или localhost)
      if (!window.isSecureContext) {
        setEngine('manual')
        setEngineError(
          'Камера браузера работает только по HTTPS (текущее подключение не шифруется). ' +
            'Аппаратный сканер, фото этикетки и ручной ввод доступны и без HTTPS.',
        )
        return
      }
      // 1) нативный детектор
      if ('BarcodeDetector' in window && navigator.mediaDevices) {
        try {
          const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } })
          if (cancelled) return stream.getTracks().forEach((t) => t.stop())
          streamRef.current = stream
          const video = videoRef.current
          if (video) {
            video.srcObject = stream
            await video.play()
            const Detector = window.BarcodeDetector!
            const detector = new Detector({ formats: ['ean_13', 'code_128', 'ean_8', 'code_39'] })
            setEngine('native')
            const tick = async () => {
              if (!aliveRef.current) return
              try {
                const codes = await detector.detect(video)
                if (codes.length && aliveRef.current) {
                  const ok = await submit(codes[0].rawValue)
                  if (ok) return
                }
              } catch {
                /* кадр не декодировался — пробуем следующий */
              }
              if (aliveRef.current) window.setTimeout(tick, 350)
            }
            void tick()
            return
          }
        } catch {
          /* нет камеры/прав — пробуем библиотеку */
        }
      }
      // 2) вендорная библиотека
      try {
        await loadScannerLib()
        if (cancelled || !window.HTML5Qrcode || !boxRef.current) throw new Error('no lib')
        setEngine('library')
        const inst = new window.HTML5Qrcode('scan-mount')
        libRef.current = inst
        await inst.start(
          { facingMode: 'environment' },
          { fps: 8 },
          (text) => { void submit(text) },
          () => undefined,
        )
        return
      } catch (e) {
        if (!cancelled) {
          setEngine('manual')
          setEngineError(e instanceof Error && e.message.includes('getUserMedia') ? 'Камера недоступна или запрещена.' : 'Камера/декодер недоступны в этом браузере.')
        }
      }
    })()

    return () => {
      cancelled = true
      stopAll()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  return (
    <Modal open={open} onClose={() => { if (!busy) onClose() }} title="Сканирование штрихкода" width="520px">
      <div className="stack" style={{ gap: 12 }}>
        <div
          ref={boxRef}
          style={{
            position: 'relative',
            background: '#000',
            borderRadius: 8,
            minHeight: 220,
            display: engine === 'manual' ? 'none' : 'block',
            overflow: 'hidden',
          }}
        >
          <video ref={videoRef} muted playsInline style={{ width: '100%', display: 'block' }} />
          <div id="scan-mount" style={{ width: '100%' }} />
          {engine === 'starting' && (
            <div style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center' }}>
              <Spinner label="Включаем камеру…" />
            </div>
          )}
        </div>

        {engine === 'manual' && (
          <div className="alert alert--info small" style={{ margin: 0 }}>
            {engineError || 'Камера недоступна.'} Введите код вручную или используйте аппаратный
            сканер (он работает как клавиатура: наведите на этикетку и нажмите курок).
          </div>
        )}

        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            style={{ display: 'none' }}
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) void onLabelPhoto(f)
            }}
          />
          <button
            type="button"
            className="btn"
            disabled={fileBusy}
            onClick={() => fileRef.current?.click()}
            title="Распознать EAN-13/Code-128 с фотографии этикетки"
          >
            {fileBusy ? 'Распознаём фото…' : '📃 Распознать с фото'}
          </button>
          <div id="scan-file-mount" style={{ display: 'none' }} />
        </div>

        <form
          className="row"
          style={{ gap: 8 }}
          onSubmit={(e) => {
            e.preventDefault()
            void submit(manual).then((ok) => { if (ok) setManual('') })
          }}
        >
          <input
            className="input grow"
            placeholder="Код с этикетки (например, 2000000044323) + Enter"
            value={manual}
            autoFocus={engine === 'manual'}
            onChange={(e) => { setManual(e.target.value); setLastMiss('') }}
          />
          <button className="btn btn--primary" type="submit" disabled={busy || !manual.trim()}>
            Найти
          </button>
        </form>

        {lastMiss && (
          <div className="alert alert--warn small" style={{ margin: 0 }}>
            Код <span className="mono">{lastMiss}</span> в инвентаре не найден. Добавьте штрихкод
            в карточку позиции или проверьте этикетку.
          </div>
        )}

        <div className="tiny muted">
          Поддерживаются EAN-13 и Code-128. Этикетку держите целиком в кадре, без бликов.
          Аппаратный сканер работает и в поле поиска списка инвентаря.
        </div>
      </div>
    </Modal>
  )
}
