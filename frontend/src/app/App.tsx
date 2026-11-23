import { QueryClientProvider } from '@tanstack/react-query'
import { useState } from 'react'

import { SystemStatusPage } from '../features/system-status/SystemStatusPage'
import { createQueryClient } from './queryClient'

export function App() {
  const [queryClient] = useState(createQueryClient)
  return (
    <QueryClientProvider client={queryClient}>
      <SystemStatusPage />
    </QueryClientProvider>
  )
}
