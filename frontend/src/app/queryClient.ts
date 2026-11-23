import { QueryClient } from '@tanstack/react-query'

import { ApiError } from '../lib/api/errors'

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        refetchOnWindowFocus: false,
        // Don't retry client errors (4xx); they won't succeed on retry.
        retry: (failureCount, error) =>
          !(error instanceof ApiError && error.status !== null && error.status < 500) && failureCount < 2,
      },
    },
  })
}
