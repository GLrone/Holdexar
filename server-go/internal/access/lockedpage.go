package access

import (
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"io"
	"net/http"
	"path"
	"strings"
)

func randomLockNonce() string {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		panic("锁定页 nonce 生成失败：" + err.Error())
	}
	return base64.RawURLEncoding.EncodeToString(buf)
}

// serveLockedPage 仅对无扩展名 GET/HEAD 返回内联解锁页，资源路径与写操作一律 404。
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
	nonce := randomLockNonce()
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Security-Policy", fmt.Sprintf(
		"default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; "+
			"script-src 'nonce-%s'; connect-src 'self'; "+
			"object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
		nonce,
	))
	w.WriteHeader(http.StatusOK)
	if r.Method == http.MethodHead {
		return
	}
	_, _ = io.WriteString(w, strings.ReplaceAll(lockedPageHTML, "__NONCE__", nonce))
}

// lockedPageHTML 内联解锁页（密码解锁与 /api/v1/session/recovery/* 账号找回视图）。
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
.brand{display:flex;align-items:center;gap:10px;margin:0 0 14px}
.brand__logo{width:36px;height:36px;border-radius:8px;flex-shrink:0}
.brand__name{font-size:18px;font-weight:700;letter-spacing:.02em;color:#e8eef5}
#logoLight{display:none}
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
input{background:#f4f7fa;border-color:#c6d4e0;color:#1b2838}.brand__name{color:#1b2838}#logoLight{display:block}#logoDark{display:none}}
</style>
</head>
<body>
<!-- holdexar-unlock-page -->
<div class="card">
<div class="brand"><img class="brand__logo" id="logoDark" alt="Holdexar" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAOY0lEQVR4nN1bCXhU1b3/nXPvJJlMZpLJhAQXRBZ5r9SyCH7aWoGHgIDwpG5gsbW0AgVcoFb7aa0PnxVFvip9iNU+4RNNKYii4oKyFlBEpM+HEqwVGqBsCclsYbLN3Hve9//fueOQZZJJAi/x/32TzJ1zzj3nvy/nHIGWQQNgNPq1b99MRKMCnQFcLhP799c3sW4FwEw1VKRok/EX0Afuwj7fFVKNglJDAVykADcUT/L/D4IZFATwdyHEdglzffDkocMpGZgY2jQkBrkLe90mBe6BEEMhJKAsmjBVOhEwIsJCRykzAojVMMTj4VMHDzRkZqNxDUAHEHMX9OondPxBCDmSkFaKMFdGfJbOIfqNgdaoAKELKaFMMwzgwXBZ6dKkNZ9BBNEU8p7CXqOFEKsgRb4yjRiY9UzFrgI2syxCGMaycPmhGV+3fU0E0VDscwt7jVIC70CIDCiTkCeidFUgQsSE1B2mYaypKi+dHGekaRNBxDuyjni69e2tpPlXIZALRRTsJEau3aCiFhFiS6vKD91pSzoaibUwlkshci3Of1OQJxAOZRpRqWlzPEU9p8SRZ/wkMJyoYXqKek4WmhymlNHVxb450JRSJpRcknNev4K4GkgJbOMvCuIBMvUJX/LNA3YLQpMF0ojeZ9mA4ZKRdRddfIUUchdTqGtZ+3SBGEx8Dso6o08odCTAyAqFsRCSLGbKsLGzgmi90ArANKUmvSpTH4ckbg+FUqJLir8QMAwDJkeorRoQjwPUGJsAAgIXNXCLXQZMw4DLlQ1nVlZriSChiArq29ZD374ZCiKns8X2rQFd03A6FMYT//kbjB45HJFwFTStZRPGmYwQ+ejfP0NySqs6v+GTUjLCyXpvQsHhzMJ148Zg0MBLoWIxaForPTjhHImQGzy7QAvVkhaeLtBYQr7q9Gn4/X721PY7g8dP4MZJE9GtwIdJE8fDne9FKBRKaz7Z5pW1cvGmaSIYDPEzhxlpIk5jw+Ew/m3YVbhz1nTouo76+nr+bdItN+GZp56AYZq4pE9vFC9/Dud3787Eau1cEmcBiEO00FAozEZq/kP3Y8emt+Bxu9lip3JbhDS1B4JBRCIRXD9hLNa/sRob334NS556Ar58L4oKfPh4+wa8vnoFP2tSMqH//bprcejLT/Hm6peQmZGRkJZUoHc08oR4bW0tQoEgxoweiccf/Q0uGzQAsVgMzmwn/IFASuQj1dXIcDhw/YRxuHvWdIwccXWivaa2Fg6Hgwm74MmnWfxvveUGRp4Qrampxe/+61ls3rqdpYJ+a0kS9I5CnCajj7+iEhf2uACLFz2G6dN+xG3E9erqGopEUyJfXV2NAd/uj6WLF+KKy4fw74QcfYiwxGkpBQKhMF5f9w5eW/0ayspPYe6dM7nPzLvuxcvLlkP35CHb6Tx3KiDIIpsmi+xPp93G4knIE/UJeVufU42PRmPI93qx7tViRp4khrhI42yjxhGMIvsgkZ/vhbvAh4cffQKhcBjvvLcRL79YjIIePZHjcrV67To6CKLRKJ5/5ilM+9Gt/NwQ8VRVRCIAGbYLL7wE55/XnYlJ7qyR+ia9IhaNsZ6TxH28+694f+NWzu1M0+Dx54wAGrmjYAg3TJrAyBPnpNSadEUp1VFYXoI+zUuL1c7dhUB1TQ3M2gh+u/AptvyZrmwYRnrpjJ5W76bWTQs3DPS44Pw45QXraRNrb6ooe0a7zlxvORonAtXW1GDIkMEYMewqLF7yHGrr6uB256RNAIkOAltf2wqk100SLglUUuE3IysLJSVfYMjggdj9wUYU+PJRXx9NJzNk6DACyBYmJhsQF95G8QL9rw342cVx32Z0xTRMViNqJxULnY7gF796GN/6l3648+d3IBIOpx11SnQUtIbw6kzEyVCSEaurq8PUabfj2cVPNom87U2ys50sZUwAMnixKIZeNpDbcnJy2pTM6uggoJw6JcSLbYQ4GUrK4gq7F7Hh/PHUyRh62aAmh9nehIACnKNHj3H6G4lUIy8vDw/cN5fb39u4BdLhSCvc7kACtK6USK6yqrISF1x8EX52+1TcPXsGevfqyW0cucUNHD9zyGzFAKcjETz5uyX4/dLnIen5dIQDnVeKl2HwwO/gz6+sxeYt2+DxWKH2OSaASPqbopcU8Hq9+Nkjt2HmHT9G96Ii/t3ODUikbVFPDn4o4vuPRxfi8337kefNQzAQxLf+tR+W/3EJrrx8CN56dwOmz56LzKzMtLnfbgKcYXGbkQC7T7YzG1vfewP53rxGHE6OGG3EN23ZhkVPP4NNW7fD6cxCtiubM8C7Zk/Hwt8+DKfTieUvrcQ9v3yQ5ybVSicAajcBNE5calgfGdGW+lP46s07g8OEeMwwuNBBz2Qb1m/YzH59+4cfsbiTbw8FQxg8aAAem/8gxl07ivV/2sy78eJLK+H2uNuMfJsJwC4oFMLAAZfixMky9sut8b+2+0rmOCFfV1ePP61ag2f/uByf7t0H3aGzVac5clzZeOyRX2PeXbNYEnbt3oM7Zs9Dyb4SeH2+RLLUVtDTHqDr8B8/gXn3zcWQywZhxpx51v7K12g2O9Y0rQMbtqhTJvfyyldQvGoN9u7dh6xsJ7yk56EQE4gSqnvnzkG/vn14PCVMs+fej5LPS1BQVMj5Q3tBTxv5snLMvmc25t31cwy+ckSiTtecBDCH4n7bLlj+8+gxPPfCi1hRvArHjh2H0+WCr8DHHA8Gg7hu7Gg88tCvMGjgd7g/qQYlR/Su2to6ON05iEWj7UY+LQIQ8gF/ADdPvhFLn16I0RNugr8ygG6F3RAOWSWvZE7TJhMRhfTdlo8t23ageOUa1vOTJ07Ck5cLn8+HQDDElaNxY0axkaMK7xkeQtPiNlaxrUlVVzgrBJDxYkXPnj2w4oWl2LD5L9i0YRPyCwvZiBHQQm0fbMX0lmQc/Ecptm7/EK+uXccEIDHOzfXA6/MiEAhxdHfTDyZi9oyfYvjV3+MxdlaYqAPE3Rv9s2v/HVXG11vbMVofxeJFC3gDYu2bb0Nw+ZmWQemgJeq0YCLCocNH8MFHu/HGunew+S87WKwdGRnweDyoq69nq57vy8ftUydj1oxpieoPgW0cm1Ipzifa4OvbRQBNkwiHq3DNyOFcoKQF0LNgfba4nuVy4d33NyFcVYW9n5XgYOkhVFb6oekaF0IpAKK2Sr8fvS/uiXvvnoWpU27iSq4Nb7+7Abs+2YNHH36g+cWQZKBjN7BkSx0oWDHqoxh/7TXW8SMhMH7sKJiR6oSYulxOHD12HMtW/Amfl+xno0ViLoVEoNLPOjtpwnisKV7O5bL5v76fkafxRLjxP5iCidffglfXvpXSnVoH1M6xBIAmlBIFBXSmQCEaNXDrzTcwp59e8gcYZI1pTaT3UqKmugb10Si6+Xy4+ntXYuL4MRgx7Pvod4nlygjI/a157U2sWfsmdu7ew+Mz3TnIy8tNuRSTzjcwF3AOCSBoR9lAWVl5ovjpcGRg0YL5uOXG67Hjw13st8lG6A4HuhcV4pK+vTHg0v78PbF408Se//lfrFrzOtuQw4ePQHM44M7JYSkiFYkZsbiUNV6GbRu4BI5zSADTNFnHKe6mwMQqO1mFx8uHDOZPs2OVYvdGXoSM3w9/MhMHS76Aq8DHRpDcZeMoLm5Yk+anTNGh69i5YyeOHj3OEWHrt8PbaQNM02TL/7cvv8KEG3+Iz/btZ07Y1ZtkIN0nt0gE4uJm3IczAWrr+F3uwkIeG4ulrt7aeQKNJeRffX0dfjJ9DhvWjrQCems6EQc87hzs3PUJrh41AVd99wrO40l8iRuUoo69dhTGXDOCkUquDRIi9Bulq2RQrapxY+JxgJPU384TDhz8B+Y/9iRWrl7Lc2VkZLQr9m8TAWwi5OS4+P/7G7fAjMWsJEjXoapPIdOZdQYBOOGhHZ14MPP5p3tRWVkJh6P5Ka2dH4t4VAShrHDx0udRWeGHN9/b7sSn3bmAScgJsIuzDJWVh/tPAe74bgzrbFKm5/cH8GLxKs7to7EYi3/DYMbaC7D2F06dqsAHH32Mhx5ZgP37vkBOrgf5Pi+rzNkAPd0BtPaGZSfW/bhOk4gSHD9xkuMCSngOfnUQrlxPk8hb71TIyHBwaj30+6NwsuwU/5bfrYDnOlvIE+h82aC2pt1ydTpymsX3y68O4PkXVnCdjhIepys7gUhLYSypQEWln/MDm7BnDeiAicNBJ8PoTsDFn0ipDVVWmpVWqdza2IyiqLAbp6/bduyEv6ICLrcbmZmZrUK84fs6Ot5vAKYQQiplfhYuOzTIOicIUULnB1u6XtIU0GJJtMtOVeCNde8yMfILChLl73SROcvI8xTxBO4IfY9zW2yIp3VtCrKYCLrOoSypQVsQP3fA9Tv6sof+WBKQFVtvGmYgXttq08rtOl/nRdwGQWeGSdfWJ06Lh44coXMr/y14g4/vCXxTwRBCCJP1v5QkIHFaXCjNsUgZZgXojkkbbEHXAKt8LaAet3AcntiP5usydJlASP3PyjSp4tg4Xu3SoGJCaLoyze3h8tIR8ejbtF0eib0eLju8yjSMpUJqDrpmgm8OGBBSN5UKCujTkhtEg+8s/u7CXqulpt2saP/Zuj3S5Q5Rfw0qRshDqXqhcF2ovHRT8r1Imdwzrvuiqrx0ijJiyyxJYPdIIVlnN+8NwbRujGm6UvBDYUJD5NEMZxMXDD1FveYAWCCk9FhBIhGiS1yc1NjWcVRpblExzKqqKP178m0xG0QzL7KRND3devSBcDwAqaYIIV1nVmc7DyQ26dmTE/PVHlPh91XlpcWp7hCLFt6bGOQ9r/dFhsJ4pdQwAP0A5HWyy9NVAjgCIfYoU2yqKj/4UUNmNj0ULYK1AdCQev37Z9B5+45Yf7vB4VA4cKCuiZaUN8epx/8BwGzlmV1D2bUAAAAASUVORK5CYII="><img class="brand__logo" id="logoLight" alt="Holdexar" src="data:image/png;base64,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"><span class="brand__name">Holdexar</span></div>
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
<script nonce="__NONCE__">
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
