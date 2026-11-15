package access

import (
	"io"
	"net/http"
	"path"
	"strings"
)

// serveLockedPage 锁定态分流：页面导航（无扩展名路径）返回独立解锁页
// （零外部资源、全内联——主应用资产在锁定态一个字节都不出）；资源形态
// 路径与写操作一律 404，路由注入与资源探测拿到的只有「不存在」。
func serveLockedPage(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		writeJSON(w, http.StatusNotFound, map[string]any{"detail": "Not Found"})
		return
	}
	base := path.Base(r.URL.Path)
	if strings.Contains(base, ".") {
		writeJSON(w, http.StatusNotFound, map[string]any{"detail": "Not Found"})
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	if r.Method == http.MethodHead {
		return
	}
	_, _ = io.WriteString(w, lockedPageHTML)
}

// lockedPageHTML 独立解锁页：内联样式与脚本（不引用任何主应用资源）。
// 双视图：访问密码解锁 + Steam 账号找回（POST /session/recovery/* 五个
// 公开端点——找回的信任锚是 Steam 官方认证身份 ∈ 绑定账号集合）。文案
// 按浏览器语言双语切换。
const lockedPageHTML = `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Holdexar</title>
<style>
html,body{height:100%;margin:0}
body{display:flex;align-items:center;justify-content:center;background:#131c27;color:#c7d5e0;
font:15px/1.8 'Segoe UI',system-ui,sans-serif}
.card{width:min(360px,88vw);padding:32px 30px;border-radius:14px;background:#1b2838;
box-shadow:0 18px 50px rgba(0,0,0,.45)}
h1{margin:0 0 6px;font-size:20px;color:#e8eef5;letter-spacing:.5px}
p{margin:0 0 18px;font-size:13px;color:#8fa3b8}
input{width:100%;box-sizing:border-box;padding:11px 13px;border-radius:9px;border:1px solid #2e4257;
background:#0f1721;color:#dfe8f0;font-size:15px;outline:none}
input:focus{border-color:#66c0f4}
input[type=text]{margin-top:10px}
button{width:100%;margin-top:14px;padding:11px;border:0;border-radius:9px;cursor:pointer;
background:#66c0f4;color:#0f1721;font-size:15px;font-weight:600}
button:disabled{opacity:.55;cursor:wait}
.err{min-height:20px;margin-top:12px;font-size:13px;color:#e07b7b;white-space:pre-wrap}
.state{min-height:20px;margin-top:12px;font-size:13px;color:#8fa3b8;white-space:pre-wrap}
.alt{margin:14px 0 0;text-align:center}
a{color:#66c0f4;font-size:13px;text-decoration:none}
a:hover{text-decoration:underline}
@media (prefers-color-scheme:light){body{background:#e8edf2;color:#2a3b4d}
.card{background:#fff;box-shadow:0 14px 40px rgba(30,60,90,.18)}h1{color:#1b2838}p{color:#6c8098}
input{background:#f4f7fa;border-color:#c6d4e0;color:#1b2838}}
</style>
</head>
<body>
<!-- holdexar-unlock-page -->
<div class="card">
<h1>Holdexar</h1>
<div id="unlockView">
<p id="hint"></p>
<form id="f" autocomplete="off">
<input id="pw" type="password" autocomplete="current-password">
<button id="go" type="submit"></button>
</form>
<div class="err" id="err"></div>
<p class="alt"><a href="#" id="toRecovery"></a></p>
</div>
<div id="recView" data-view="holdexar-recovery-view" style="display:none">
<p id="recHint"></p>
<form id="rf" autocomplete="off">
<input id="acc" type="text" autocomplete="username">
<input id="rpw" type="password" autocomplete="current-password">
<button id="rstart" type="submit"></button>
</form>
<form id="cf" autocomplete="off" style="display:none">
<input id="code" type="text" inputmode="numeric" autocomplete="one-time-code">
<button id="rcode" type="submit"></button>
</form>
<p class="state" id="recState"></p>
<div class="err" id="recErr"></div>
<p class="alt"><a href="#" id="back"></a></p>
</div>
</div>
<script>
(function () {
  var zh = navigator.language && navigator.language.toLowerCase().indexOf('zh') === 0;
  var t = zh
    ? { hint: '本应用处于锁定状态，请输入访问密码解锁。', pw: '访问密码', go: '解锁',
        wrong: '访问密码不正确', net: '网络错误，请稍后再试', off: '解锁入口未开启',
        lockPrefix: '尝试过于频繁，请 ', lockSuffix: ' 秒后再试', bad: '请求无效',
        forgot: '忘记密码？', recHint: '用 Steam 账号重新登录以找回访问权限（须为本应用已绑定的账号）。',
        acc: 'Steam 账号', rpw: 'Steam 密码', rstart: '登录并找回', code: '验证码',
        rcode: '提交验证码', back: '← 返回解锁', confirm: '请在 Steam 手机端确认登录…' }
    : { hint: 'This app is locked. Enter the access password to continue.', pw: 'Access password', go: 'Unlock',
        wrong: 'Incorrect access password', net: 'Network error, try again later', off: 'Unlock entry is disabled',
        lockPrefix: 'Too many attempts, retry in ', lockSuffix: 's', bad: 'Invalid request',
        forgot: 'Forgot password?', recHint: 'Sign in with your Steam account to recover access (bound accounts only).',
        acc: 'Steam account', rpw: 'Steam password', rstart: 'Sign in & recover', code: 'Verification code',
        rcode: 'Submit code', back: '← Back to unlock', confirm: 'Confirm the login in the Steam mobile app…' };
  var err = document.getElementById('err');
  var recErr = document.getElementById('recErr');
  var recState = document.getElementById('recState');
  var unlockView = document.getElementById('unlockView');
  var recView = document.getElementById('recView');
  var accountForm = document.getElementById('rf');
  var codeForm = document.getElementById('cf');
  var pollTimer = null;

  document.getElementById('hint').textContent = t.hint;
  document.getElementById('pw').placeholder = t.pw;
  document.getElementById('go').textContent = t.go;
  document.getElementById('toRecovery').textContent = t.forgot;
  document.getElementById('recHint').textContent = t.recHint;
  document.getElementById('acc').placeholder = t.acc;
  document.getElementById('rpw').placeholder = t.rpw;
  document.getElementById('rstart').textContent = t.rstart;
  document.getElementById('code').placeholder = t.code;
  document.getElementById('rcode').textContent = t.rcode;
  document.getElementById('back').textContent = t.back;

  function show(which) {
    unlockView.style.display = which === 'rec' ? 'none' : '';
    recView.style.display = which === 'rec' ? '' : 'none';
  }
  function showAccountForm() {
    accountForm.style.display = '';
    codeForm.style.display = 'none';
  }
  function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (res) {
      return res.json().then(function (b) { return { status: res.status, body: b }; },
        function () { return { status: res.status, body: {} }; });
    }, function () { throw new Error('net'); });
  }
  function limitText(detail) {
    return t.lockPrefix + String(detail).replace(/[^0-9]/g, '') + t.lockSuffix;
  }

  document.getElementById('f').addEventListener('submit', function (ev) {
    ev.preventDefault();
    document.getElementById('go').disabled = true;
    err.textContent = '';
    post('/api/v1/session/unlock', { password: document.getElementById('pw').value })
      .then(function (r) {
        if (r.status === 200) { location.replace('/'); return; }
        var d = (r.body && r.body.detail) || '';
        if (r.status === 401) err.textContent = t.wrong;
        else if (r.status === 404) err.textContent = t.off;
        else if (r.status === 429) err.textContent = limitText(d);
        else if (r.status === 422) err.textContent = t.bad;
        else err.textContent = d || t.net;
      })
      .catch(function () { err.textContent = t.net; })
      .then(function () { document.getElementById('go').disabled = false; });
  });

  document.getElementById('toRecovery').addEventListener('click', function (ev) {
    ev.preventDefault();
    err.textContent = '';
    recErr.textContent = '';
    recState.textContent = '';
    showAccountForm();
    show('rec');
    startPolling();
  });
  document.getElementById('back').addEventListener('click', function (ev) {
    ev.preventDefault();
    post('/api/v1/session/recovery/cancel').catch(function () {});
    stopPolling();
    show('unlock');
  });

  document.getElementById('rf').addEventListener('submit', function (ev) {
    ev.preventDefault();
    recErr.textContent = '';
    document.getElementById('rstart').disabled = true;
    post('/api/v1/session/recovery/login/start', {
      account: document.getElementById('acc').value,
      password: document.getElementById('rpw').value
    }).then(function (r) {
      if (r.body && r.body.ok) {
        accountForm.style.display = 'none';
        pollOnce();
      } else {
        recErr.textContent = (r.body && r.body.error) || t.net;
        showAccountForm();
      }
    }).catch(function () {
      recErr.textContent = t.net;
      showAccountForm();
    }).then(function () { document.getElementById('rstart').disabled = false; });
  });

  document.getElementById('cf').addEventListener('submit', function (ev) {
    ev.preventDefault();
    recErr.textContent = '';
    document.getElementById('rcode').disabled = true;
    post('/api/v1/session/recovery/login/code', { code: document.getElementById('code').value })
      .then(function (r) {
        if (!(r.body && r.body.ok)) {
          recErr.textContent = (r.body && r.body.error) || t.net;
        }
      }).catch(function () { recErr.textContent = t.net; })
      .then(function () { document.getElementById('rcode').disabled = false; });
  });

  function renderStatus(st) {
    recState.textContent = st.message || '';
    if (st.state === 'awaiting_code' && st.code_available) {
      accountForm.style.display = 'none';
      codeForm.style.display = '';
    } else if (st.state === 'awaiting_confirmation') {
      accountForm.style.display = 'none';
      codeForm.style.display = 'none';
    } else if (st.state === 'done') {
      stopPolling();
      post('/api/v1/session/recovery/complete').then(function (r) {
        if (r.status === 200 && r.body && r.body.ok) { location.replace('/'); return; }
        recErr.textContent = (r.body && r.body.error) || t.net;
        showAccountForm();
      }).catch(function () { recErr.textContent = t.net; });
    } else if (st.state === 'failed' || st.state === 'idle') {
      stopPolling();
      if (st.error) recErr.textContent = st.error;
      showAccountForm();
    }
  }

  function pollOnce() {
    fetch('/api/v1/session/recovery/login/status').then(function (res) {
      return res.json();
    }).then(function (st) {
      if (st && st.state) renderStatus(st);
    }, function () {});
  }
  function startPolling() {
    stopPolling();
    pollTimer = setInterval(pollOnce, 1500);
    pollOnce();
  }

  document.getElementById('pw').focus();
})();
</script>
</body>
</html>
`
