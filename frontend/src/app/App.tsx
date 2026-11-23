import { QueryClientProvider } from '@tanstack/react-query'
import { useState } from 'react'

import { createQueryClient } from './queryClient'
import { resolvePage } from './routes'

export function App() {
  const [queryClient] = useState(createQueryClient)
  return <QueryClientProvider client={queryClient}>{resolvePage(window.location.pathname)}</QueryClientProvider>
}
