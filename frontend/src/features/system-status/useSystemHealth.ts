import { useQuery } from '@tanstack/react-query'

import { fetchHealth } from './api'

export const systemHealthQueryKey = ['system', 'health'] as const

export function useSystemHealth() {
  return useQuery({
    queryKey: systemHealthQueryKey,
    queryFn: fetchHealth,
    refetchInterval: 15_000,
  })
}
