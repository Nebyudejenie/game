// i18n loader. Amharic default, English fallback -- mirrors
// services/bot/i18n.py's rules so the product reads consistently whether a
// player is in the bot or the Mini App. Every user-facing string in this
// app goes through t(), same discipline as the bot side.

const SUPPORTED = ["am", "en"];
const DEFAULT_LANGUAGE = "am";
const FALLBACK_LANGUAGE = "en";

const catalogs = {};
let currentLanguage = DEFAULT_LANGUAGE;

export async function loadCatalog(language) {
  if (catalogs[language]) return catalogs[language];
  const response = await fetch(`locales/${language}.json`);
  const data = await response.json();
  catalogs[language] = data;
  return data;
}

export async function initI18n(preferredLanguage) {
  const language = SUPPORTED.includes(preferredLanguage) ? preferredLanguage : DEFAULT_LANGUAGE;
  await Promise.all([loadCatalog(DEFAULT_LANGUAGE), loadCatalog(FALLBACK_LANGUAGE)]);
  if (language !== DEFAULT_LANGUAGE && language !== FALLBACK_LANGUAGE) {
    await loadCatalog(language);
  }
  currentLanguage = language;
}

export function setLanguage(language) {
  if (SUPPORTED.includes(language)) currentLanguage = language;
}

export function t(key, params) {
  const template =
    (catalogs[currentLanguage] && catalogs[currentLanguage][key]) ||
    (catalogs[FALLBACK_LANGUAGE] && catalogs[FALLBACK_LANGUAGE][key]) ||
    (catalogs[DEFAULT_LANGUAGE] && catalogs[DEFAULT_LANGUAGE][key]);
  if (template === undefined) {
    console.error(`missing i18n key: ${key}`);
    return key;
  }
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (_, name) => (name in params ? params[name] : `{${name}}`));
}
