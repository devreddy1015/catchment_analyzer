/**
 * Light and dark, remembered.
 *
 * The choice is written to `data-theme` on the document element, which every
 * colour in `styles.css` keys off, and kept in localStorage so a reload does not
 * flip the page back. "system" follows the operating system and keeps following
 * it — the whole point of that setting is that it is not a snapshot.
 */

export type Theme = 'light' | 'dark' | 'system';

const KEY = 'catchment-theme';

const prefersDark = () =>
  typeof window !== 'undefined' && window.matchMedia('(prefers-color-scheme: dark)').matches;

/** What the user chose last time, or "system" if they never did. */
export function storedTheme(): Theme {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === 'light' || saved === 'dark' || saved === 'system') return saved;
  } catch {
    // Private browsing, or storage turned off. The default is a fine answer.
  }
  return 'system';
}

/** The theme actually painted, with "system" resolved to what the OS is asking for. */
export function resolve(theme: Theme): 'light' | 'dark' {
  return theme === 'system' ? (prefersDark() ? 'dark' : 'light') : theme;
}

export function applyTheme(theme: Theme): void {
  document.documentElement.setAttribute('data-theme', resolve(theme));
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    // Not being able to remember it is not a reason to fail to apply it.
  }
}

/** Call `onChange` when the OS theme changes; only matters while set to "system". */
export function watchSystem(onChange: () => void): () => void {
  const query = window.matchMedia('(prefers-color-scheme: dark)');
  query.addEventListener('change', onChange);
  return () => query.removeEventListener('change', onChange);
}
