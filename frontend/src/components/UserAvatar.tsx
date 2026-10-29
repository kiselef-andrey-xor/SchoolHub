/** Аватар пользователя: тянет фото с сервера (blob с авторизацией) и кэширует object URL.
 *  Если фото нет или оно не загрузилось — рисуем инициалы (как раньше). */
import { useEffect, useState } from 'react'

import { usersApi } from '@/api/endpoints'
import { Avatar } from '@/components/ui'
import type { User } from '@/types'

const cache = new Map<string, string>()   // user.id -> objectURL
const pending = new Map<string, Promise<string | null>>()

function loadAvatar(id: string): Promise<string | null> {
  const hit = cache.get(id)
  if (hit) return Promise.resolve(hit)
  const inflight = pending.get(id)
  if (inflight) return inflight
  const promise = usersApi
    .avatarBlob(id)
    .then((blob) => {
      const url = URL.createObjectURL(blob)
      cache.set(id, url)
      return url
    })
    .catch(() => null)
    .finally(() => pending.delete(id))
  pending.set(id, promise)
  return promise
}

/** Сбросить кэш (после загрузки/удаления нового фото). */
export function dropAvatarCache(id: string) {
  const url = cache.get(id)
  if (url) URL.revokeObjectURL(url)
  cache.delete(id)
}

export function UserAvatar({ user, size = 'md' }: { user: Pick<User, 'id' | 'full_name' | 'avatar'>; size?: 'sm' | 'md' | 'lg' }) {
  const [url, setUrl] = useState<string | null>(() => (user.avatar ? (cache.get(user.id) ?? null) : null))

  useEffect(() => {
    let alive = true
    if (!user.avatar) {
      setUrl(null)
      return
    }
    const cached = cache.get(user.id)
    if (cached) {
      setUrl(cached)
      return
    }
    void loadAvatar(user.id).then((u) => {
      if (alive) setUrl(u)
    })
    return () => {
      alive = false
    }
  }, [user.id, user.avatar])

  if (!url) return <Avatar name={user.full_name} size={size} />
  const px = size === 'sm' ? 52 : size === 'lg' ? 88 : 64
  const cls = size === 'sm' ? 'avatar avatar--sm' : size === 'lg' ? 'avatar avatar--lg' : 'avatar'
  return (
    <img
      src={url}
      alt={user.full_name}
      title={user.full_name}
      className={cls}
      style={{ width: px, height: px, objectFit: 'cover', borderRadius: '50%' }}
    />
  )
}
