import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom'

import { queryClientConfig } from '@/api/query'
import { setupApi } from '@/api/endpoints'
import { AppLayout } from '@/components/AppLayout'
import { RequirePermission } from '@/components/ProtectedRoute'
import { ToastProvider } from '@/components/Toast'
import { CAP } from '@/lib/permissions'
import { authStore, useAuthStore } from '@/store/auth'
import { AuditPage } from '@/pages/AuditPage'
import { CertificationPage } from '@/pages/CertificationPage'
import { IncomingPage } from '@/pages/IncomingPage'
import { MovementPage } from '@/pages/MovementPage'
import { EquipmentPage } from '@/pages/EquipmentPage'
import { DashboardPage } from '@/pages/DashboardPage'
import { LoginPage } from '@/pages/LoginPage'
import { NotificationsPage } from '@/pages/NotificationsPage'
import { SetupPage } from '@/pages/SetupPage'
import { OrderDetailPage } from '@/pages/OrderDetailPage'
import { OrdersPage } from '@/pages/OrdersPage'
import { PlanDetailPage } from '@/pages/PlanDetailPage'
import { VshkPage } from '@/pages/VshkPage'
import { PlanEditPage } from '@/pages/PlanEditPage'
import { PlansPage } from '@/pages/PlansPage'
import { ProfilePage } from '@/pages/ProfilePage'
import { HelpdeskPage } from '@/pages/HelpdeskPage'
import { NotesPage } from '@/pages/NotesPage'
import { SubstitutionsPage } from '@/pages/SubstitutionsPage'
import { HelpPage } from '@/pages/HelpPage'
import { TaskDetailPage } from '@/pages/TaskDetailPage'
import { TasksPage } from '@/pages/TasksPage'
import { UsersPage } from '@/pages/UsersPage'

const queryClient = new QueryClient(queryClientConfig)

function BootstrappedRoutes() {
  const status = useAuthStore((s) => s.status)
  const location = useLocation()

  useEffect(() => {
    // восстанавливаем сессию после перезагрузки страницы
    if (status === 'loading') void authStore.loadProfile()
  }, [status])

  if (status === 'loading') {
    return (
      <div className="login-page">
        <div className="loading-box" style={{ color: '#fff' }}>
          <div className="spinner spinner--lg" />
          <span>Проверяем сессию…</span>
        </div>
      </div>
    )
  }

  if (status === 'guest') {
    return <GuestRoutes location={location} />
  }

  return (
    <Routes>
      <Route path="/login" element={<Navigate to="/" replace />} />
      <Route element={<AppLayout />}>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/orders" element={<OrdersPage />} />
        <Route path="/orders/:id" element={<OrderDetailPage />} />
        <Route path="/tasks" element={<TasksPage />} />
        <Route path="/tasks/:id" element={<TaskDetailPage />} />
        <Route path="/plans" element={<RequirePermission cap={CAP.plansRead}><PlansPage /></RequirePermission>} />
        <Route path="/plans/:id" element={<RequirePermission cap={CAP.plansRead}><PlanDetailPage /></RequirePermission>} />
        <Route path="/vshk" element={<VshkPage />} />
        <Route
          path="/plans/:id/edit"
          element={<RequirePermission cap={CAP.plansManage}><PlanEditPage /></RequirePermission>}
        />
        <Route path="/notifications" element={<NotificationsPage />} />
        <Route path="/notes" element={<RequirePermission cap={CAP.notesRead}><NotesPage /></RequirePermission>} />
        <Route path="/helpdesk" element={<RequirePermission cap={CAP.helpdeskRead}><HelpdeskPage /></RequirePermission>} />
        <Route path="/substitutions" element={<RequirePermission cap={CAP.substitutionsRead}><SubstitutionsPage /></RequirePermission>} />
        <Route path="/help" element={<HelpPage />} />
        <Route path="/profile" element={<ProfilePage />} />
        <Route path="/users" element={<RequirePermission cap={CAP.usersRead}><UsersPage /></RequirePermission>} />
        <Route path="/audit" element={<RequirePermission cap={CAP.auditRead}><AuditPage /></RequirePermission>} />
        <Route path="/incoming" element={<RequirePermission cap={CAP.incomingRead}><IncomingPage /></RequirePermission>} />
        <Route path="/movement" element={<RequirePermission cap={CAP.movementRead}><MovementPage /></RequirePermission>} />
        <Route path="/equipment" element={<RequirePermission cap={CAP.equipmentRead}><EquipmentPage /></RequirePermission>} />
        <Route path="/certifications" element={<RequirePermission cap={CAP.certificationsRead}><CertificationPage /></RequirePermission>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}

/** Не аутентифицирован: либо первоначальная настройка, либо экран входа. */
function GuestRoutes({ location }: { location: { pathname: string } }) {
  const { data: setup, refetch } = useQuery({
    queryKey: ['/setup/status'],
    queryFn: setupApi.status,
    // пока система не настроена — периодически переспрашиваем (настройка может быть
    // завершена из терминала, тогда страница сама пустит пользователя на экран входа)
    refetchInterval: 20_000,
    retry: false,
  })

  // статус ещё грузится (или API недоступно) — показываем экран входа
  if (!setup) {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Navigate to="/login" replace state={{ from: location.pathname }} />} />
      </Routes>
    )
  }

  if (setup.setup_required) {
    return (
      <Routes>
        <Route
          path="*"
          element={
            <SetupPage
              onDone={() => {
                void refetch()
                void authStore.loadProfile()
              }}
            />
          }
        />
      </Routes>
    )
  }

  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="*" element={<Navigate to="/login" replace state={{ from: location.pathname }} />} />
    </Routes>
  )
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <BrowserRouter>
          <BootstrappedRoutes />
        </BrowserRouter>
      </ToastProvider>
    </QueryClientProvider>
  )
}
