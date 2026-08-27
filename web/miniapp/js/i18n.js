// i18n loader. Amharic default, English fallback -- mirrors
// services/bot/i18n.py's rules so the product reads consistently whether a
// player is in the bot or the Mini App. Every user-facing string in this
// app goes through t(), same discipline as the bot side.
//
// om/ti mirror services/bot/locales/om.json and ti.json: deliberate empty
// stubs (spec 7.5 lists all four as supported), so every key falls through
// t()'s own fallback chain to English, then Amharic, until real
// translations exist -- not fabricated here.

const SUPPORTED = ["am", "en", "om", "ti"];
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

// Spec 7.5: "The Mini App reads language_code as a hint but the DB value
// wins." initI18n()/setLanguage() above apply the Telegram client hint
// immediately at boot, before auth; this is called once the `authed`
// frame's real users.language arrives, so the DB value can override that
// hint the same session, not just on the next cold start.
export async function applyServerLanguage(language) {
  if (!SUPPORTED.includes(language) || language === currentLanguage) return;
  await loadCatalog(language);
  currentLanguage = language;
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
