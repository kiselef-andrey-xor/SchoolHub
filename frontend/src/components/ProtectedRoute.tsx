/**
 * Защита маршрутов на клиенте.
 * Дублирует проверки backend'а для UX (не показываем недоступное),
 * но НЕ является границей безопасности: API всё равно вернёт 403.
 */
import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'

import { can, type Capability } from '@/lib/permissions'
import { useAuthStore } from '@/store/auth'
import { EmptyState } from '@/components/ui'

export function RequirePermission({ cap, children }: { cap: Capability | Capability[]; children: ReactNode }) {
  const user = useAuthStore((s) => s.user)
  const location = useLocation()

  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />

  const caps = Array.isArray(cap) ? cap : [cap]
  const allowed = caps.some((c) => can(user.capabilities, c))
  if (!allowed) {
    return (
      <div className="card">
        <EmptyState
          icon="🔒"
          title="Раздел недоступен для вашей роли"
          hint="Если вы считаете, что это ошибка — обратитесь к администратору школы."
        />
      </div>
    )
  }
  return <>{children}</>
}
