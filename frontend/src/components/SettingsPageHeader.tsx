import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'

interface SettingsPageHeaderProps {
  action?: ReactNode
  description: string
  eyebrow: string
  icon: LucideIcon
  title: string
}

export function SettingsPageHeader({
  action,
  description,
  eyebrow,
  icon: Icon,
  title,
}: SettingsPageHeaderProps) {
  return (
    <header className="settings-content-header">
      <span className="settings-content-icon" aria-hidden="true">
        <Icon size={22} />
      </span>
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action && <div className="settings-header-action">{action}</div>}
    </header>
  )
}
