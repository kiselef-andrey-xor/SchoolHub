/**
 * RBAC на клиенте.
 *
 * ВАЖНО: это НЕ граница безопасности. Реальные проверки выполняются на backend
 * (зависимости require_role + проверки в сервисах). Словарь возможностей
 * приходит с сервера в GET /auth/me → capabilities, поэтому UI и API не разъезжаются.
 */
import type { Role } from '@/types'

export const CAP = {
  ordersRead: 'orders:read',
  ordersManage: 'orders:manage',
  ordersAck: 'orders:ack',
  ordersAckReport: 'orders:ack-report',
  tasksReadOwn: 'tasks:read-own',
  tasksReadAll: 'tasks:read-all',
  tasksCreate: 'tasks:create',
  tasksAssign: 'tasks:assign',
  tasksDelete: 'tasks:delete',
  plansRead: 'plans:read',
  plansManage: 'plans:manage',
  usersRead: 'users:read',
  usersManage: 'users:manage',
  usersManageAdmins: 'users:manage-admins',
  incomingRead: 'incoming:read',
  movementRead: 'movement:read',
  equipmentRead: 'equipment:read',
  certificationsRead: 'certifications:read',
  certificationsManage: 'certifications:manage',
  auditRead: 'audit:read',
  dashboardGlobal: 'dashboard:global',
  notesRead: 'notes:read',
  notesManage: 'notes:manage',
  substitutionsRead: 'substitutions:read',
  substitutionsManage: 'substitutions:manage',
  helpManage: 'help:manage',
  /** Helpdesk (v1.12): read — у всех ролей (заявку подаёт любой сотрудник),
   *  manage — у Владельца и у сотрудников с грантом «системный администратор». */
  helpdeskRead: 'helpdesk:read',
  helpdeskManage: 'helpdesk:manage',
} as const

export type Capability = (typeof CAP)[keyof typeof CAP]

export const ROLE_LABEL: Record<Role, string> = {
  OWNER: 'Владелец',
  ADMIN: 'Администрация',
  TEACHER: 'Учитель',
}

export const ROLE_SHORT: Record<Role, string> = {
  OWNER: 'Владелец',
  ADMIN: 'Админ',
  TEACHER: 'Учитель',
}

/** Иерархия ролей — та же, что в backend (app/enums.py::ROLE_LEVEL). */
export const ROLE_LEVEL: Record<Role, number> = { TEACHER: 1, ADMIN: 2, OWNER: 3 }

export function can(capabilities: string[] | undefined, cap: Capability): boolean {
  return Boolean(capabilities?.includes(cap))
}

export function canAny(capabilities: string[] | undefined, caps: Capability[]): boolean {
  return caps.some((c) => can(capabilities, c))
}

export function isStaff(role?: Role | null): boolean {
  return role === 'OWNER' || role === 'ADMIN'
}

/** Разрешено ли актору управлять целевой ролью (анти-эскалация, как на сервере). */
export function canManageRole(actor: Role, target: Role): boolean {
  if (actor === 'OWNER') return true
  return ROLE_LEVEL[target] < ROLE_LEVEL[actor]
}
