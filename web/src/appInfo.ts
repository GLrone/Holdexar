/** 应用元信息：唯一的品牌常量来源（改名只改这里）。 */
export const APP_NAME = 'Holdexar'
export const APP_SLUG = APP_NAME.toLowerCase()
/**
 * 品牌副标题的**词典 key**，不是文案本身。
 *
 * 此前这里另存了一份中文串，与词典的 `app.subtitle` 是两份独立副本——双语之后
 * 它们必然分叉（改了词典、侧边栏跟随语言，这里却永远是中文）。品牌名 `APP_NAME`
 * 是不翻译的专名，留在常量里没问题；副标题是文案，只能有一个来源。
 */
export const APP_SUBTITLE_KEY = 'app.subtitle' as const

/** 主题（当前固定深色；支持浅色后由主题状态决定） */
export const THEME = 'dark' as 'dark' | 'light'

/** 主题对应 logo（素材：用户提供的双版本 ico） */
export const APP_LOGO = THEME === 'dark' ? '/assets/logo_dark.ico' : '/assets/logo_light.ico'
