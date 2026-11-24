/** The landing page shares the application's icon set (at its original 20px default). */
import { Icon as AppIcon, type IconName } from '../../../components/ui/Icon'

export type { IconName }

export function Icon({ name, size = 20, className }: { name: IconName; size?: number; className?: string }) {
  return <AppIcon name={name} size={size} className={className} />
}
