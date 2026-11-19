/** 局域网访问解锁页（/unlock）：密码解锁 + Steam 找回双视图 */
const access = {
  'access.title': '局域网访问',
  'access.hint': '输入访问密码以继续使用',
  'access.passwordPlaceholder': '访问密码',
  'access.unlock': '解锁',
  'access.unlocking': '解锁中…',
  'access.unlocked': '已解锁，正在进入…',
  'access.failed': '解锁失败',
  'access.forgot': '忘记访问密码？',
  'access.forgotHint': '点「用 Steam 账号找回」登录绑定过的 Steam 账号即可直接进入；或在装有 Holdexar 的电脑上打开应用程序，在 设置 → 局域网访问 中设置新密码（无需旧密码）。',
  'access.recover': '用 Steam 账号找回',
  'access.recoverBack': '返回密码解锁',
  'access.recoverTitle': 'Steam 账号找回',
  'access.recoverHint': '用绑定到本应用的 Steam 账号登录，验证是号主后直接进入应用。',
  'access.recoverAccount': 'Steam 账号',
  'access.recoverPassword': 'Steam 密码',
  'access.recoverStart': '登录 Steam 验证',
  'access.recoverStartFailed': '找回登录启动失败',
  'access.recoverWorking': '正在验证…',
  'access.recoverCodePlaceholder': 'Steam Guard 验证码',
  'access.recoverCodeEmail': '验证码已发送到账号绑定邮箱',
  'access.recoverCodeTotp': '输入手机令牌上显示的验证码',
  'access.recoverSubmitCode': '提交验证码',
  'access.recoverCancel': '取消',
} as const

export default access
