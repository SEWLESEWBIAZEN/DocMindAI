import { Spinner } from '../components/ui'

export function FullPageLoading({ label = 'Loading DocuMind' }: { label?: string }) {
  return (
    <div className="full-page-loading">
      <Spinner size={24} label={label} />
    </div>
  )
}
