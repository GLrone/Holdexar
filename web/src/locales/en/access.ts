/** LAN access unlock page (/unlock): password unlock + Steam recovery dual view */
const access = {
  'access.title': 'LAN Access',
  'access.hint': 'Enter the access password to continue',
  'access.passwordPlaceholder': 'Access password',
  'access.unlock': 'Unlock',
  'access.unlocking': 'Unlocking…',
  'access.unlocked': 'Unlocked, entering…',
  'access.failed': 'Unlock failed',
  'access.forgot': 'Forgot the access password?',
  'access.forgotHint': 'Use "Recover with Steam account" to sign in with a bound Steam account and enter directly; or open the Holdexar desktop app on the computer running it and set a new password under Settings → LAN access (no old password needed).',
  'access.recover': 'Recover with Steam account',
  'access.recoverBack': 'Back to password unlock',
  'access.recoverTitle': 'Steam Account Recovery',
  'access.recoverHint': 'Sign in with a Steam account bound to this app; once verified as the owner you enter directly.',
  'access.recoverAccount': 'Steam account',
  'access.recoverPassword': 'Steam password',
  'access.recoverStart': 'Sign in with Steam',
  'access.recoverStartFailed': 'Failed to start recovery sign-in',
  'access.recoverWorking': 'Verifying…',
  'access.recoverCodePlaceholder': 'Steam Guard code',
  'access.recoverCodeEmail': 'A code has been sent to the account email',
  'access.recoverCodeTotp': 'Enter the code from your Steam mobile authenticator',
  'access.recoverSubmitCode': 'Submit code',
  'access.recoverCancel': 'Cancel',
} as const

export default access
