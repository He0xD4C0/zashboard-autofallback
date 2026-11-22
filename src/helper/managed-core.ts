import { getUrlFromBackend } from '@/helper/utils'
import type { Backend } from '@/types'

export const isManagedPanel = import.meta.env.VITE_MANAGED_PANEL === "1"

export const managedCoreUrl = new URL('./autofallback/core', window.location.href)
const controllerUrl = new URL('./mihomo-api/', window.location.href)

export const isManagedBackend = (backend?: Backend) => {
  if (!isManagedPanel || backend?.type !== 'clash') return false
  try {
    const url = new URL(getUrlFromBackend(backend))
    return url.origin === controllerUrl.origin &&
      url.pathname.replace(/\/?$/, '/') === controllerUrl.pathname
  } catch {
    return false
  }
}
