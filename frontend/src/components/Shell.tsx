import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  Box,
  ChevronDown,
  Download,
  Library,
  LogOut,
  Menu,
  Moon,
  Search,
  Server,
  Settings,
  Sun,
  X,
} from 'lucide-react'
import { NavLink, useNavigate } from 'react-router-dom'
import type { User } from '../types'

interface ShellProps {
  children: ReactNode
  activeDownloads: number
  user: User
  onLogout: () => void
}

const links = [
  { to: '/models', label: 'Models', icon: Box },
  { to: '/library', label: 'Library', icon: Library },
  { to: '/downloads', label: 'Downloads', icon: Download },
  { to: '/runtimes', label: 'Runtimes', icon: Server, adminOnly: true },
]

export default function Shell({ children, activeDownloads, user, onLogout }: ShellProps) {
  const navigate = useNavigate()
  const searchInput = useRef<HTMLInputElement>(null)
  const accountMenu = useRef<HTMLDivElement>(null)
  const accountButton = useRef<HTMLButtonElement>(null)
  const [query, setQuery] = useState('')
  const [mobileOpen, setMobileOpen] = useState(false)
  const [accountOpen, setAccountOpen] = useState(false)
  const [theme, setTheme] = useState<'light' | 'dark'>(() =>
    localStorage.getItem('hugginghack-theme') === 'dark' ? 'dark' : 'light',
  )
  const userInitials = user.display_name
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0])
    .join('')
    .toUpperCase() || user.username.slice(0, 2).toUpperCase()

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('hugginghack-theme', theme)
  }, [theme])

  useEffect(() => {
    function focusSearch(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null
      const isTyping = target?.matches('input, textarea, select, [contenteditable="true"]')
      if (event.key === '/' && !isTyping) {
        event.preventDefault()
        searchInput.current?.focus()
      }
    }
    window.addEventListener('keydown', focusSearch)
    return () => window.removeEventListener('keydown', focusSearch)
  }, [])

  useEffect(() => {
    if (!accountOpen) return

    function closeAccountMenu(event: MouseEvent) {
      if (!accountMenu.current?.contains(event.target as Node)) setAccountOpen(false)
    }

    function closeAccountMenuWithKeyboard(event: KeyboardEvent) {
      if (event.key !== 'Escape') return
      setAccountOpen(false)
      accountButton.current?.focus()
    }

    document.addEventListener('mousedown', closeAccountMenu)
    document.addEventListener('keydown', closeAccountMenuWithKeyboard)
    return () => {
      document.removeEventListener('mousedown', closeAccountMenu)
      document.removeEventListener('keydown', closeAccountMenuWithKeyboard)
    }
  }, [accountOpen])

  function search(event: FormEvent) {
    event.preventDefault()
    navigate(`/models?search=${encodeURIComponent(query.trim())}`)
    setMobileOpen(false)
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="topbar-inner">
          <NavLink to="/models" className="brand" aria-label="HuggingHack home">
            <img src="/hugginghack-mark.svg" alt="" className="brand-mark" />
            <span className="brand-name">HuggingHack</span>
            <span className="brand-local">local</span>
          </NavLink>

          <form className="global-search" onSubmit={search} role="search">
            <Search size={17} aria-hidden="true" />
            <input
              ref={searchInput}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search models"
              aria-label="Search models on Hugging Face"
            />
            <kbd>/</kbd>
          </form>

          <nav className="primary-nav" aria-label="Primary navigation">
            {links.filter((link) => !link.adminOnly || user.role === 'admin').map(({ to, label, icon: Icon }) => (
              <NavLink key={to} to={to} className={({ isActive }) => (isActive ? 'active' : '')}>
                <Icon size={16} aria-hidden="true" />
                <span>{label}</span>
                {to === '/downloads' && activeDownloads > 0 && (
                  <span className="nav-count">{activeDownloads}</span>
                )}
              </NavLink>
            ))}
          </nav>

          <div className="account-menu" ref={accountMenu}>
            <button
              ref={accountButton}
              type="button"
              className="account-chip"
              onClick={() => setAccountOpen((open) => !open)}
              aria-expanded={accountOpen}
              aria-controls="account-dropdown"
              aria-label={`Open account menu for ${user.display_name}`}
              title={`${user.display_name} · ${user.role}`}
            >
              <span className="account-avatar" aria-hidden="true">{userInitials}</span>
              <ChevronDown size={15} aria-hidden="true" />
            </button>
            {accountOpen && (
              <div id="account-dropdown" className="account-dropdown">
                <div className="account-dropdown-identity">
                  <span className="account-avatar" aria-hidden="true">{userInitials}</span>
                  <span>
                    <strong>{user.display_name}</strong>
                    <small>@{user.username} · {user.role}</small>
                  </span>
                </div>
                <NavLink to="/settings" onClick={() => setAccountOpen(false)}>
                  <Settings size={16} aria-hidden="true" />
                  Settings
                </NavLink>
                <button
                  type="button"
                  onClick={() => {
                    setTheme(theme === 'light' ? 'dark' : 'light')
                    setAccountOpen(false)
                  }}
                >
                  {theme === 'light' ? <Moon size={16} aria-hidden="true" /> : <Sun size={16} aria-hidden="true" />}
                  {theme === 'light' ? 'Dark theme' : 'Light theme'}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setAccountOpen(false)
                    onLogout()
                  }}
                >
                  <LogOut size={16} aria-hidden="true" />
                  Log out
                </button>
              </div>
            )}
          </div>
          <button
            type="button"
            className="icon-button mobile-menu-button"
            onClick={() => setMobileOpen(!mobileOpen)}
            aria-label="Toggle navigation"
            aria-expanded={mobileOpen}
          >
            {mobileOpen ? <X size={20} /> : <Menu size={20} />}
          </button>
        </div>
        {mobileOpen && (
          <div className="mobile-panel">
            <form className="mobile-search" onSubmit={search}>
              <Search size={17} />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search models"
              />
            </form>
            {links.filter((link) => !link.adminOnly || user.role === 'admin').map(({ to, label, icon: Icon }) => (
              <NavLink key={to} to={to} onClick={() => setMobileOpen(false)}>
                <Icon size={18} />
                {label}
                {to === '/downloads' && activeDownloads > 0 && (
                  <span className="nav-count">{activeDownloads}</span>
                )}
              </NavLink>
            ))}
            <NavLink to="/settings" onClick={() => setMobileOpen(false)}>
              <Settings size={18} />
              Settings
            </NavLink>
            <div className="mobile-user-summary">
              <span className="account-avatar" aria-hidden="true">{userInitials}</span>
              <span><strong>{user.display_name}</strong><small>@{user.username} · {user.role}</small></span>
            </div>
            <button type="button" className="mobile-account" onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}>
              {theme === 'light' ? <Moon size={18} /> : <Sun size={18} />}
              {theme === 'light' ? 'Dark theme' : 'Light theme'}
            </button>
            <button type="button" className="mobile-account" onClick={onLogout}>
              <LogOut size={18} />
              Log out
            </button>
          </div>
        )}
      </header>
      <main>{children}</main>
    </div>
  )
}
