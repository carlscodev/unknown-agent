import { createI18n } from 'vue-i18n'
import en from './locales/en.json'
import es from './locales/es.json'
import pt from './locales/pt.json'

const messages = { en, es, pt }

function getBrowserLocale() {
  if (typeof navigator !== 'undefined') {
    const navLang = navigator.language || navigator.userLanguage
    if (navLang.startsWith('es')) return 'es'
    if (navLang.startsWith('pt')) return 'pt'
  }
  return 'en'
}

function getInitialLocale() {
  if (typeof localStorage !== 'undefined') {
    const saved = localStorage.getItem('unknown-agent-locale')
    if (saved && ['en', 'es', 'pt'].includes(saved)) {
      return saved
    }
  }
  return getBrowserLocale()
}

const i18n = createI18n({
  legacy: false,
  locale: getInitialLocale(),
  fallbackLocale: 'en',
  messages,
})

if (typeof localStorage !== 'undefined') {
  i18n.global.onLanguageChanged = (locale) => {
    localStorage.setItem('unknown-agent-locale', locale)
  }
}

export default i18n
