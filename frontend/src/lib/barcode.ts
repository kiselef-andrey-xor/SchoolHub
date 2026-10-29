/** Штрихкоды для инвентаря: генерация EAN-13 и Code-128 в SVG (без зависимостей).
 *  EAN-13 — для этикеток как в школьной учётной системе («2 000000 044323»),
 *  Code-128 — для произвольных кодов (инвентарные номера, серийники).
 *  Таблицы паттернов — по ISO/IEC 15417 (Code 128) и спецификации EAN-13. */

// --------------------------------------------------------------------- EAN-13
const EAN_L = ['0001101', '0011001', '0010011', '0111101', '0100011', '0110001', '0101111', '0111011', '0110111', '0001011']
const EAN_R = EAN_L.map((c) => c.split('').map((b) => (b === '0' ? '1' : '0')).join(''))
const EAN_G = EAN_R.map((c) => c.split('').reverse().join(''))
const EAN_PARITY = ['LLLLLL', 'LLGLGG', 'LLGGLG', 'LLGGGL', 'LGLLGG', 'LGGLLG', 'LGGGLL', 'LGLGLG', 'LGLGGL', 'LGGLGL']

export function ean13CheckDigit(digits12: string): number {
  let sum = 0
  for (let i = 0; i < 12; i++) sum += Number(digits12[i]) * (i % 2 === 0 ? 1 : 3)
  return (10 - (sum % 10)) % 10
}

export function isEan13Valid(code: string): boolean {
  if (!/^\d{13}$/.test(code)) return false
  return ean13CheckDigit(code.slice(0, 12)) === Number(code[12])
}

/** Служебный EAN-13 для позиции без штрихкода: префикс 200 + 9 цифр из id + контроль. */
export function internalEan13(seed: string): string {
  let h = 0
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0
  const body = ('200' + String(h).padStart(9, '0')).slice(0, 12)
  return body + String(ean13CheckDigit(body))
}

function ean13Bits(code: string): string {
  const d = code.slice(0, 13).split('').map(Number)
  const parity = EAN_PARITY[d[0]]
  let bits = '101'
  for (let i = 1; i <= 6; i++) bits += parity[i - 1] === 'L' ? EAN_L[d[i]] : EAN_G[d[i]]
  bits += '01010'
  for (let i = 7; i <= 12; i++) bits += EAN_R[d[i]]
  return bits + '101'
}

// ------------------------------------------------------------------ Code-128
/** Паттерны значений 0..106 (11 модулей) + stop-паттерн из 13 модулей. */
const CODE128: string[] = [
  '11011001100', '11001101100', '11001100110', '10010011000', '10010001100', '10001001100', '10011001000', '10011000100', '10001100100', '11001001000',
  '11001000100', '11000100100', '10110011100', '10011011100', '10011001110', '10111001100', '10011101100', '10011100110', '11001110010', '11001011100',
  '11001001110', '11011100100', '11001110100', '11101101110', '11101001100', '11100101100', '11100100110', '11101100100', '11100110100', '11100110010',
  '11011011000', '11011000110', '11000110110', '10100011000', '10001011000', '10001000110', '10110001000', '10001101000', '10001100010', '11010001000',
  '11000101000', '11000100010', '10110111000', '10110001110', '10001101110', '10111011000', '10111000110', '10001110110', '11101110110', '11010001110',
  '11000101110', '11011101000', '11011100010', '11011101110', '11101011000', '11101000110', '11100010110', '11101101000', '11101100010', '11100011010',
  '11101111010', '11001000010', '11110001010', '10100110000', '10100001100', '10010110000', '10010000110', '10000101100', '10000100110', '10110010000',
  '10110000100', '10011010000', '10011000010', '10000110100', '10000110010', '11000010010', '11001010000', '11110111010', '11000010100', '10001111010',
  '10100111100', '10010111100', '10010011110', '10111100100', '10011110100', '10011110010', '11110100100', '11110010100', '11110010010', '11011011110',
  '11011110110', '11110110110', '10101111000', '10100011110', '10001011110', '10111101000', '10111100010', '11110101000', '11110100010', '10111011110',
  '10111101110', '11101011110', '11110101110', '11010000100', '11010010000', '11010011100', '11000111010',
]
const C128_START_B = 104
const C128_STOP = '1100011101011'

function code128Bits(text: string): string | null {
  const values: number[] = [C128_START_B]
  for (const ch of text) {
    const code = ch.charCodeAt(0)
    if (code < 32 || code > 126) return null
    values.push(code - 32)
  }
  let sum = values[0]
  for (let i = 1; i < values.length; i++) sum += values[i] * i
  values.push(sum % 103)
  let bits = ''
  for (const v of values) bits += CODE128[v]
  return bits + C128_STOP
}

// ---------------------------------------------------------------------- SVG
export type BarcodeKind = 'EAN-13' | 'CODE-128'

export interface BarcodeSvg {
  kind: BarcodeKind
  svg: string
  code: string
}

/** Экранирование для вставки в SVG/XML.
 *  Подпись под штрихкодом и aria-label — это данные из карточки позиции
 *  (их вводит человек или загружает из CSV), а готовый SVG этикетка вставляет
 *  через dangerouslySetInnerHTML. Без экранирования значение вида
 *  `"><script>…` превратило бы этикетку в хранимый XSS. */
function xmlEscape(value: string): string {
  return value.replace(/[&<>"']/g, (ch) =>
    ch === '&' ? '&amp;' : ch === '<' ? '&lt;' : ch === '>' ? '&gt;' : ch === '"' ? '&quot;' : '&#39;',
  )
}

function bitsToSvg(bits: string, code: string, kind: BarcodeKind, height = 90): string {
  const module = 2
  const quiet = kind === 'EAN-13' ? 9 : 10
  const width = (bits.length + quiet * 2) * module
  let rects = ''
  for (let i = 0; i < bits.length; i++) {
    if (bits[i] === '1') rects += `<rect x="${(i + quiet) * module}" y="0" width="${module}" height="${height}" fill="#000"/>`
  }
  const raw = kind === 'EAN-13' ? `${code[0]} ${code.slice(1, 7)} ${code.slice(7)}` : code
  const label = xmlEscape(raw)
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height + 18}" width="${width}" height="${height + 18}" role="img" aria-label="${label}">` +
    `<rect width="${width}" height="${height + 18}" fill="#fff"/>${rects}` +
    `<text x="${width / 2}" y="${height + 14}" text-anchor="middle" font-family="monospace" font-size="14" fill="#000">${label}</text></svg>`
  )
}

/** SVG штрихкода для кода: 12/13 цифр -> EAN-13 (12 цифр дополняются контролем),
 *  остальное -> Code-128 (B). null — если код не кодируем. */
export function barcodeSvg(rawCode: string): BarcodeSvg | null {
  const code = rawCode.trim()
  if (!code) return null
  if (/^\d{12}$/.test(code)) {
    const full = code + String(ean13CheckDigit(code))
    return { kind: 'EAN-13', code: full, svg: bitsToSvg(ean13Bits(full), full, 'EAN-13') }
  }
  if (isEan13Valid(code)) return { kind: 'EAN-13', code, svg: bitsToSvg(ean13Bits(code), code, 'EAN-13') }
  if (/^\d{13}$/.test(code)) return null // похож на EAN-13, но контрольная цифра не бьётся
  const bits = code128Bits(code)
  if (!bits) return null
  return { kind: 'CODE-128', code, svg: bitsToSvg(bits, code, 'CODE-128') }
}
