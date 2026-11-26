import { useQuery } from '@tanstack/react-query'

import { apiClient } from '../../lib/api/client'

/** Whether the operator turned on "Continue with Google" (ADR 0040). */
export function useGoogleSignIn(): boolean {
  const { data } = useQuery({
    queryKey: ['auth', 'google'],
    queryFn: async () => (await apiClient.get<{ enabled: boolean }>('/auth/google', { skipAuth: true })).data,
    staleTime: 5 * 60_000,
    retry: false,
  })
  return data?.enabled === true
}
