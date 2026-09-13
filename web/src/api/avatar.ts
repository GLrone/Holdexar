/**
 * Steam 头像 URL 统一出口 —— 全项目头像一律经此归一后展示。
 *
 * Valve 多次轮换头像 CDN 域名：akamaized.net（已死，DNS 不解析）→
 * queniuqe.com / eccdnx.com（完美世界国服）→ fastly.steamstatic.com（现行，
 * miniprofile 实测返回）。同一 hash 路径跨域等价——统一归一到现行规范域，
 * 存量旧域 URL 就地自愈；数据层（store）与展示层（头像组件）双端接入，
 * 同一账号在任何页面都是同一个 URL（错乱/裂图在结构上消除）。
 */
const CANONICAL_HOST = 'https://avatars.fastly.steamstatic.com/'
const HOST_SUFFIXES = ['akamaized.net', 'steamstatic.com', 'queniuqe.com', 'eccdnx.com']

export function normalizeAvatarUrl(url: string | null | undefined): string {
  const u = (url || '').trim()
  if (!u) return ''
  const m = /^(https?:\/\/avatars\.[^/?#]+\/)(.*)$/.exec(u)
  if (!m) return u
  const host = m[1].slice(0, -1)
  if (HOST_SUFFIXES.some((s) => host.endsWith(s)) && !host.endsWith('fastly.steamstatic.com')) {
    return CANONICAL_HOST + m[2]
  }
  return u
}
