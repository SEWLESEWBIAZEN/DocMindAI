import { QueryClientProvider } from '@tanstack/react-query'
import { useState, type ReactNode } from 'react'
import { RouterProvider } from 'react-router'

import { ConfirmProvider, ToastProvider } from '../components/ui'
import { AuthProvider } from '../features/auth/AuthProvider'
import { createQueryClient } from './queryClient'
import { createRouter } from './routes'

export function AppProviders({ children }: { children: ReactNode }) {
  const [queryClient] = useState(createQueryClient)
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <ToastProvider>
          <ConfirmProvider>{children}</ConfirmProvider>
        </ToastProvider>
      </AuthProvider>
    </QueryClientProvider>
  )
}

export function App() {
  const [router] = useState(createRouter)
  return (
    <AppProviders>
      <RouterProvider router={router} />
    </AppProviders>
  )
}
