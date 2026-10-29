/** Стопка аватаров исполнителей (до 4 + счётчик «+N»): фото, если загружено, иначе инициалы. */
import { UserAvatar } from '@/components/UserAvatar'
import type { User } from '@/types'

export type StackUser = Pick<User, 'id' | 'full_name' | 'avatar'>

export function AvatarStack({ users, size = 'sm' }: { users: StackUser[]; size?: 'sm' | 'md' }) {
  const list = users.slice(0, 4)
  const rest = users.length - list.length
  if (users.length === 0) return <span className="small muted">не назначен</span>
  return (
    <span className="avatar-stack">
      {list.map((u) => (
        <UserAvatar key={u.id} user={u} size={size} />
      ))}
      {rest > 0 && (
        <span className={`avatar avatar--${size}`} style={{ background: 'var(--muted-bg)', color: 'var(--ink-500)' }}>
          +{rest}
        </span>
      )}
    </span>
  )
}
