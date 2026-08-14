import { RefObject, useEffect, useRef, useState } from 'react'

const FOCUSABLE = [
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'a[href]',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

/** 为自绘弹窗提供初始焦点、Tab 焦点锁、Esc 关闭和关闭后焦点恢复。 */
export function useDialogFocus(
  open: boolean,
  ref: RefObject<HTMLElement>,
  onClose: () => void,
) {
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  useEffect(() => {
    if (!open) return
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const frame = window.requestAnimationFrame(() => {
      const root = ref.current
      const initial = root?.querySelector<HTMLElement>('[data-autofocus]')
        ?? root?.querySelector<HTMLElement>(FOCUSABLE)
      initial?.focus()
    })
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        closeRef.current()
        return
      }
      if (event.key !== 'Tab') return
      const root = ref.current
      if (!root) return
      const items = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE))
        .filter(item => item.getClientRects().length > 0)
      if (!items.length) {
        event.preventDefault()
        root.focus()
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      window.cancelAnimationFrame(frame)
      document.removeEventListener('keydown', onKeyDown)
      previous?.focus()
    }
  }, [open, ref])
}

export type ViewportMode = 'wide' | 'compact' | 'narrow'

function currentViewportMode(): ViewportMode {
  if (window.innerWidth < 900) return 'narrow'
  if (window.innerWidth < 1100) return 'compact'
  return 'wide'
}

export function useViewportMode(): ViewportMode {
  const [mode, setMode] = useState<ViewportMode>(currentViewportMode)
  useEffect(() => {
    const update = () => setMode(currentViewportMode())
    window.addEventListener('resize', update)
    return () => window.removeEventListener('resize', update)
  }, [])
  return mode
}
