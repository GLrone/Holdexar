// ==UserScript==
// @name         SteamHL助手
// @namespace    http://tampermonkey.net/
// @version      1.2.0
// @description  配合本地 HTML 报告使用：实现跨域查价 + 本地收藏数据同步 + Steam API 代理 + Package 凭证持久化 + 家庭组愿望单同步 + Epic地区价格代理（TOP100热榜已迁移至后端）
// @author       GLrone
// @match        file:///*.html
// @match        *steamhl.com*
// @match        https://glrone.github.io/SteamHLWeb*
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_deleteValue
// @grant        GM_registerMenuCommand
// @grant        GM_notification
// @connect      store.steampowered.com
// @connect      api.steampowered.com
// @connect      steampy.com
// @connect      steamcici.com
// @connect      api.allorigins.win
// @connect      api.augmentedsteam.com
// @connect      open.er-api.com
// @connect      steamcommunity.com
// @connect      store.epicgames.com
// @run-at       document-start
// ==/UserScript==

(function () {
    'use strict';
    const LOG = '[SteamHL助手]';
    console.log(`%c✅ ${LOG} V1.1.5 已启动`, 'background:#1b2838; color:#a4d007; font-size:14px; padding:4px 8px; border-radius:4px;');

    // ==================== 菜单命令：管理 API Key ====================

    function showApiKeyModal() {
        // 移除已存在的模态框
        const existing = document.getElementById('tm-apikey-modal');
        if (existing) existing.remove();

        // 创建模态框样式
        const style = document.createElement('style');
        style.textContent = `
            #tm-apikey-modal {
                position: fixed; top: 0; left: 0; right: 0; bottom: 0;
                background: rgba(0,0,0,0.8); z-index: 10000;
                display: flex; align-items: center; justify-content: center;
                font-family: sans-serif;
            }
            .tm-modal-content {
                background: #1b2838; border: 1px solid #66c0f4;
                padding: 20px; border-radius: 8px; width: 400px;
                box-shadow: 0 0 20px rgba(102, 192, 244, 0.2);
                color: #c7d5e0;
            }
            .tm-title { font-size: 18px; color: #66c0f4; margin-bottom: 15px; font-weight: bold; }
            .tm-input {
                width: 100%; padding: 8px; margin-bottom: 15px;
                background: #0e1a27; border: 1px solid #2a475e;
                color: #fff; border-radius: 4px; box-sizing: border-box;
            }
            .tm-input:focus { outline: none; border-color: #66c0f4; }
            .tm-buttons { display: flex; gap: 10px; justify-content: flex-end; }
            .tm-btn {
                padding: 8px 16px; border: none; border-radius: 4px;
                cursor: pointer; font-size: 13px; color: #fff;
                transition: opacity 0.2s;
            }
            .tm-btn:hover { opacity: 0.8; }
            .tm-btn-jump { background: #67c1f5; color: #fff; margin-right: auto; }
            .tm-btn-save { background: #a4d007; }
            .tm-btn-cancel { background: #3d4450; }
        `;
        document.head.appendChild(style);

        // 创建模态框 DOM
        const modal = document.createElement('div');
        modal.id = 'tm-apikey-modal';
        modal.innerHTML = `
            <div class="tm-modal-content">
                <div class="tm-title">⚙️ 配置 Steam Web API Key</div>
                <input type="text" id="tm-apikey-input" class="tm-input" placeholder="在此粘贴您的 API Key..." value="${GM_getValue('steam_api_key', '')}">
                <div class="tm-buttons">
                    <button id="tm-btn-jump" class="tm-btn tm-btn-jump">跳转申请</button>
                    <button id="tm-btn-cancel" class="tm-btn tm-btn-cancel">取消</button>
                    <button id="tm-btn-save" class="tm-btn tm-btn-save">保存</button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);

        // 绑定事件
        document.getElementById('tm-btn-jump').onclick = () => window.open('https://steamcommunity.com/dev/apikey', '_blank');
        document.getElementById('tm-btn-cancel').onclick = () => modal.remove();
        document.getElementById('tm-btn-save').onclick = () => {
            const key = document.getElementById('tm-apikey-input').value.trim();
            if (key) {
                GM_setValue('steam_api_key', key);
                alert('✅ API Key 已保存！\n请刷新网页以生效。');
                modal.remove();
            } else {
                alert('❌ 请输入有效的 API Key');
            }
        };
    }

    function deleteApiKey() {
        if (confirm('⚠️ 确定要删除已保存的 Steam API Key 吗？')) {
            GM_deleteValue('steam_api_key');
            alert('🗑️ API Key 已清除');
        }
    }

    GM_registerMenuCommand("⚙️ 添加 Steam API Key", showApiKeyModal);
    GM_registerMenuCommand("🗑️ 删除 Steam API Key", deleteApiKey);

    // ==================== 汇率定时更新引擎 ====================

    const RATE_CACHE_KEY = 'exchange_rates';
    const RATE_CACHE_TS_KEY = 'exchange_rates_ts';
    const RATE_UPDATE_INTERVAL = 3600000; // 1小时 = 3600000ms

    function updateExchangeRates() {
        const lastTs = GM_getValue(RATE_CACHE_TS_KEY, 0);
        const now = Date.now();
        if (now - lastTs < RATE_UPDATE_INTERVAL) {
            console.log(`${LOG} 汇率缓存有效，跳过更新`);
            return;
        }

        console.log(`${LOG} 开始更新汇率...`);

        // 优先请求 augmentedsteam
        GM_xmlhttpRequest({
            method: "GET",
            url: "https://api.augmentedsteam.com/rates/v1",
            timeout: 10000,
            onload: function (response) {
                if (response.status === 200) {
                    try {
                        const data = JSON.parse(response.responseText);
                        GM_setValue(RATE_CACHE_KEY, data);
                        GM_setValue(RATE_CACHE_TS_KEY, Date.now());
                        console.log(`${LOG} 汇率更新成功 (augmentedsteam)`);
                        return;
                    } catch (e) {
                        console.warn(`${LOG} augmentedsteam 解析失败，降级到 er-api`);
                    }
                } else {
                    console.warn(`${LOG} augmentedsteam 请求失败 (${response.status})，降级到 er-api`);
                }
                // 降级到 er-api
                fetchErApi();
            },
            onerror: function () {
                console.warn(`${LOG} augmentedsteam 网络错误，降级到 er-api`);
                fetchErApi();
            },
            ontimeout: function () {
                console.warn(`${LOG} augmentedsteam 超时，降级到 er-api`);
                fetchErApi();
            }
        });
    }

    function fetchErApi() {
        GM_xmlhttpRequest({
            method: "GET",
            url: "https://open.er-api.com/v6/latest/CNY",
            timeout: 10000,
            onload: function (response) {
                if (response.status === 200) {
                    try {
                        const data = JSON.parse(response.responseText);
                        GM_setValue(RATE_CACHE_KEY, data);
                        GM_setValue(RATE_CACHE_TS_KEY, Date.now());
                        console.log(`${LOG} 汇率更新成功 (er-api)`);
                    } catch (e) {
                        console.error(`${LOG} er-api 解析失败:`, e);
                    }
                } else {
                    console.error(`${LOG} er-api 请求失败 (${response.status})`);
                }
            },
            onerror: function () {
                console.error(`${LOG} er-api 网络错误`);
            }
        });
    }

    // 启动时立即检查一次
    updateExchangeRates();
    // 每1小时定时更新
    setInterval(updateExchangeRates, RATE_UPDATE_INTERVAL);

    // ==================== 数据持久化与通信 ====================

    // 1. 初始化：向网页发送存储的数据 (API Key 状态/值, 好友码, 愿望单, 收藏, 汇率)
    window.addEventListener('load', () => {
        // 等待一点时间确保网页 JS 已就绪
        setTimeout(() => {
            GM_xmlhttpRequest({
                method: "GET",
                url: "https://steamcommunity.com/chat/clientjstoken",
                timeout: 5000,
                onload: function (response) {
                    let loggedInUser = null;
                    if (response.status === 200) {
                        try {
                            const res = JSON.parse(response.responseText);
                            if (res.logged_in) {
                                loggedInUser = {
                                    steamid: res.steamid,
                                    accountid: res.accountid,
                                    account_name: res.account_name
                                };
                            }
                        } catch (e) {
                            console.error(`${LOG} 解析 clientjstoken 失败:`, e);
                        }
                    }
                    sendInitPayload(loggedInUser);
                },
                onerror: function () { sendInitPayload(null); },
                ontimeout: function () { sendInitPayload(null); }
            });

            function sendInitPayload(loggedInUser) {
                const apiKey = GM_getValue('steam_api_key', '');
                const payload = {
                    hasApiKey: !!apiKey,
                    apiKey: apiKey,
                    favorites: GM_getValue('favorites', []),
                    friendCodes: GM_getValue('steam_friend_codes', []),
                    wishlist: GM_getValue('steam_wishlist', null),
                    exchangeRates: GM_getValue(RATE_CACHE_KEY, null),
                    loggedInUser: loggedInUser
                };
                console.log(`${LOG} 发送初始化数据:`, payload);
                window.dispatchEvent(new CustomEvent('STEAM_DATA_INIT', { detail: payload }));
            }
        }, 1000);
    });

    // 2. 监听网页数据更新请求 (收藏, 好友码, 愿望单, API Key)
    window.addEventListener('STEAM_DATA_UPDATE', function (e) {
        const { type, data } = e.detail;
        if (type === 'favorites') {
            GM_setValue('favorites', data);
            console.log(`${LOG} 💾 已保存收藏: ${data.length} 个`);
        } else if (type === 'friendCodes') {
            GM_setValue('steam_friend_codes', data);
            console.log(`${LOG} 💾 已保存好友码: ${data.length} 个`);
        } else if (type === 'wishlist') {
            GM_setValue('steam_wishlist', data);
            console.log(`${LOG} 💾 已保存愿望单数据`);
        } else if (type === 'apiKey') {
            GM_setValue('steam_api_key', data);
            console.log(`${LOG} 💾 已保存 API Key`);
        }
    });

    // 3. [V1.1.1] Package 凭证持久化 —— 接管 HTML 的 localStorage
    window.addEventListener('SAVE_PACKAGE_DATA', function (e) {
        const { steamId, data } = e.detail || {};
        if (!steamId) {
            console.warn(`${LOG} SAVE_PACKAGE_DATA: 缺少 steamId，跳过`);
            return;
        }
        try {
            GM_setValue('steam_packages_' + steamId, data);
            console.log(`${LOG} 💾 已保存 Package 凭证 (steamId=${steamId}, ${Array.isArray(data) ? data.length : 0} 个)`);
        } catch (err) {
            console.error(`${LOG} Package 凭证保存失败:`, err);
        }
    });

    window.addEventListener('LOAD_PACKAGE_DATA', function (e) {
        const { steamId } = e.detail || {};
        if (!steamId) {
            console.warn(`${LOG} LOAD_PACKAGE_DATA: 缺少 steamId，跳过`);
            return;
        }
        try {
            const saved = GM_getValue('steam_packages_' + steamId, []);
            console.log(`${LOG} 📦 读取 Package 凭证 (steamId=${steamId}, ${Array.isArray(saved) ? saved.length : 0} 个)`);
            window.dispatchEvent(new CustomEvent('PACKAGE_DATA_LOADED', {
                detail: { steamId: steamId, data: saved }
            }));
        } catch (err) {
            console.error(`${LOG} Package 凭证读取失败:`, err);
            window.dispatchEvent(new CustomEvent('PACKAGE_DATA_LOADED', {
                detail: { steamId: steamId, data: [] }
            }));
        }
    });

    // 兼容旧版事件 (FAVORITES_QUERY/UPDATE) 以防万一
    window.addEventListener('FAVORITES_QUERY', function () {
        const favData = GM_getValue('favorites', []);
        window.dispatchEvent(new CustomEvent('FAVORITES_RESPONSE', { detail: { favorites: favData } }));
    });
    window.addEventListener('FAVORITES_UPDATE', function (e) {
        GM_setValue('favorites', e.detail || []);
    });

    // ==================== API 请求转发引擎 ====================

    window.addEventListener('STEAM_API_FETCH', function (e) {
        const apiKey = GM_getValue('steam_api_key');
        if (!apiKey) {
            console.error(`${LOG} ❌ 未配置 API Key`);
            window.dispatchEvent(new CustomEvent('STEAM_API_RESPONSE', {
                detail: { success: false, error: 'NO_API_KEY' }
            }));
            return;
        }

        const { type, steamIds, reqId } = e.detail;
        let url = '';

        if (type === 'summary') {
            // v2: GetPlayerSummaries
            url = `https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key=${apiKey}&steamids=${steamIds}`;
        } else if (type === 'owned') {
            // v1: GetOwnedGames
            url = `https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?key=${apiKey}&steamid=${steamIds}&include_appinfo=1&include_played_free_games=1&format=json`;
        } else if (type === 'wishlist') {
            // IWishlistService: GetWishlist（支持主账号 + 家庭组成员）
            url = `https://api.steampowered.com/IWishlistService/GetWishlist/v1/?key=${apiKey}&steamid=${steamIds}`;
        } else {
            return;
        }

        console.log(`${LOG} 📡 代理请求 Steam API: ${type} (${reqId})`);

        GM_xmlhttpRequest({
            method: "GET",
            url: url,
            onload: function (response) {
                // API 错误精准捕获：401/403 或包含 Unauthorized
                if (response.status === 401 || response.status === 403 ||
                    (response.responseText && response.responseText.includes('Unauthorized'))) {
                    const errorMsg = 'API_KEY_INVALID (Key无效或无权限，请检查Key及资料隐私设置)';
                    console.error(`${LOG} ❌ API Key 验证失败 [${response.status}]:`, response.responseText);
                    // 触发 API_KEY_INVALID 事件
                    window.dispatchEvent(new CustomEvent('API_KEY_INVALID', {
                        detail: { status: response.status, message: response.responseText }
                    }));
                    window.dispatchEvent(new CustomEvent('STEAM_API_RESPONSE', {
                        detail: { reqId, type, success: false, error: errorMsg }
                    }));
                    return;
                }

                if (response.status !== 200) {
                    console.error(`${LOG} ❌ Steam API Error: ${response.status}`, response.responseText);
                    window.dispatchEvent(new CustomEvent('STEAM_API_RESPONSE', {
                        detail: { reqId, type, success: false, error: `HTTP_ERROR_${response.status}` }
                    }));
                    return;
                }

                try {
                    const data = JSON.parse(response.responseText);
                    window.dispatchEvent(new CustomEvent('STEAM_API_RESPONSE', {
                        detail: { reqId, type, success: true, data }
                    }));
                } catch (err) {
                    console.error(`${LOG} ❌ JSON 解析失败:`, err);
                    window.dispatchEvent(new CustomEvent('STEAM_API_RESPONSE', {
                        detail: { reqId, type, success: false, error: 'PARSE_ERROR' }
                    }));
                }
            },
            onerror: function (err) {
                console.error(`${LOG} ❌ 网络请求失败:`, err);
                window.dispatchEvent(new CustomEvent('STEAM_API_RESPONSE', {
                    detail: { reqId, type, success: false, error: 'NETWORK_ERROR' }
                }));
            }
        });
    });

    // ==================== [V1.1.2] 数据自动同步 (UserData) ====================
    window.addEventListener('AUTO_SYNC_USERDATA_REQUEST', function (e) {
        console.log(`${LOG} 📡 收到网页端应用自动同步 userdata 请求`);
        const ts = new Date().getTime();
        GM_xmlhttpRequest({
            method: "GET",
            url: `https://store.steampowered.com/dynamicstore/userdata/?_=${ts}`,
            timeout: 10000,
            onload: function (response) {
                if (response.status === 200) {
                    try {
                        const data = JSON.parse(response.responseText);
                        console.log(`${LOG} ✅ 自动同步 userdata 成功`);
                        window.dispatchEvent(new CustomEvent('AUTO_SYNC_USERDATA_RESPONSE', {
                            detail: { success: true, data: response.responseText }
                        }));
                    } catch (err) {
                        console.error(`${LOG} ❌ userdata JSON解析失败`, err);
                        window.dispatchEvent(new CustomEvent('AUTO_SYNC_USERDATA_RESPONSE', {
                            detail: { success: false, error: 'PARSE_ERROR' }
                        }));
                    }
                } else {
                    console.error(`${LOG} ❌ userdata 请求失败 status=${response.status}`);
                    window.dispatchEvent(new CustomEvent('AUTO_SYNC_USERDATA_RESPONSE', {
                        detail: { success: false, error: `HTTP_ERROR_${response.status}` }
                    }));
                }
            },
            onerror: function (err) {
                console.error(`${LOG} ❌ userdata 网络错误`, err);
                window.dispatchEvent(new CustomEvent('AUTO_SYNC_USERDATA_RESPONSE', {
                    detail: { success: false, error: 'NETWORK_ERROR' }
                }));
            },
            ontimeout: function () {
                console.error(`${LOG} ❌ userdata 请求超时`);
                window.dispatchEvent(new CustomEvent('AUTO_SYNC_USERDATA_RESPONSE', {
                    detail: { success: false, error: 'TIMEOUT' }
                }));
            }
        });
    });

    // ==================== [V1.2.0] TOP100 热榜已迁移至后端 ====================
    // TOP100 热榜现在由后端脚本 sync_steam_top100.ts 定时拉取并缓存到 Redis。
    // 前端通过 /api/v1/steam/top100 或 /api/v1/games?sort=top100 直接获取。
    // 此处保留兼容监听器：如果旧版 HTML 仍发送 STEAM_FETCH_TOP100 事件，
    // 返回空数据提示前端使用后端 API。
    window.addEventListener('STEAM_FETCH_TOP100', function (e) {
        console.log(`${LOG} ℹ️ TOP100 已迁移至后端，请使用 /api/v1/steam/top100`);
        window.dispatchEvent(new CustomEvent('STEAM_TOP100_RESPONSE', {
            detail: { appIds: [], migrated: true }
        }));
    });

    // ==================== 跨域价格查询（V1.1.3 原版逻辑，保持不变）====================
    /**
     * STEAMPY_REQUEST 事件格式: { appId, subId, containerId }
     * 同时触发 SteamPY 和 SteamCICI 两路查询，分别回传各自 RESPONSE 事件
     */
    window.addEventListener('STEAMPY_REQUEST', function (e) {
        const { appId, subId, containerId } = e.detail;
        if (!appId || !subId) return;

        GM_xmlhttpRequest({
            method: "GET",
            url: `https://steampy.com/xboot/common/plugIn/getGame?subId=${subId}&appId=${appId}&type=subid`,
            onload: function (response) {
                try {
                    const data = JSON.parse(response.responseText);
                    window.dispatchEvent(new CustomEvent('STEAMPY_RESPONSE', {
                        detail: { containerId, success: true, data }
                    }));
                } catch (err) {
                    window.dispatchEvent(new CustomEvent('STEAMPY_RESPONSE', { detail: { containerId, success: false } }));
                }
            },
            onerror: function () {
                window.dispatchEvent(new CustomEvent('STEAMPY_RESPONSE', { detail: { containerId, success: false } }));
            }
        });

        GM_xmlhttpRequest({
            method: "GET",
            url: `https://steamcici.com/prod-api/user/system/shopGame/list?parentId=${appId}`,
            onload: function (response) {
                try {
                    const data = JSON.parse(response.responseText);
                    window.dispatchEvent(new CustomEvent('STEAMCICI_RESPONSE', {
                        detail: { containerId, subId, success: true, data }
                    }));
                } catch (err) {
                    window.dispatchEvent(new CustomEvent('STEAMCICI_RESPONSE', { detail: { containerId, success: false } }));
                }
            },
            onerror: function () {
                window.dispatchEvent(new CustomEvent('STEAMCICI_RESPONSE', { detail: { containerId, success: false } }));
            }
        });
    });

    // ==================== [购物车] 无感结算 - Steam 加购请求 ====================
    window.addEventListener('STEAM_ADD_TO_CART_REQUEST', function (e) {
        const subids = e.detail && e.detail.subids;
        if (!subids) {
            console.warn(`${LOG} STEAM_ADD_TO_CART_REQUEST: 缺少 subids，跳过`);
            window.dispatchEvent(new CustomEvent('STEAM_ADD_TO_CART_RESPONSE'));
            return;
        }
        console.log(`${LOG} 🛒 开始静默加购 SubID: ${subids}`);
        GM_xmlhttpRequest({
            method: 'GET',
            url: `https://store.steampowered.com/api/addtocart/?packageids=${subids}`,
            // 不设置任何 Cookie 相关 Header，让浏览器自然携带 Steam 登录态
            onload: function (response) {
                console.log(`${LOG} 🛒 加购请求完成，状态码: ${response.status}`);
                // 只要 HTTP 200，无论返回内容，均视为成功
                window.dispatchEvent(new CustomEvent('STEAM_ADD_TO_CART_RESPONSE'));
            },
            onerror: function (err) {
                console.error(`${LOG} ❌ 加购请求失败:`, err);
                // 即便失败也派发响应，由前端处理后续
                window.dispatchEvent(new CustomEvent('STEAM_ADD_TO_CART_RESPONSE'));
            },
            ontimeout: function () {
                console.warn(`${LOG} ⚠️ 加购请求超时`);
                window.dispatchEvent(new CustomEvent('STEAM_ADD_TO_CART_RESPONSE'));
            }
        });
    });

    // ==================== [V1.1.6] Epic 跨域多地区查价代理 ====================
    window.addEventListener('EPIC_PRICE_REQUEST', function (e) {
        const { gameName, containerId } = e.detail;
        if (!gameName) return;

        console.log(`${LOG} 📡 代理请求 Epic 地区比价: ${gameName}`);
        
        const regions = ["CN", "TR"];
        const results = {};
        let completed = 0;

        regions.forEach(code => {
            const payload = {
                query: `query searchStoreQuery($keywords: String, $country: String!, $locale: String) {
                          Catalog {
                            searchStore(keywords: $keywords, country: $country, locale: $locale) {
                              elements {
                                title
                                id
                                namespace
                                productSlug
                                urlSlug
                                price(country: $country) {
                                  totalPrice {
                                    discountPrice
                                    originalPrice
                                    currencyCode
                                  }
                                }
                              }
                            }
                          }
                        }`,
                variables: {
                    keywords: gameName,
                    country: code,
                    locale: "zh-CN"
                }
            };

            GM_xmlhttpRequest({
                method: "POST",
                url: "https://store.epicgames.com/graphql",
                data: JSON.stringify(payload),
                headers: {
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                },
                timeout: 10000,
                onload: function (response) {
                    if (response.status === 200) {
                        try {
                            const res = JSON.parse(response.responseText);
                            const elements = res.data.Catalog.searchStore.elements || [];
                            results[code] = elements;
                        } catch (err) {
                            results[code] = [];
                        }
                    } else {
                        results[code] = [];
                    }
                    checkCompletion();
                },
                onerror: function () {
                    results[code] = [];
                    checkCompletion();
                },
                ontimeout: function () {
                    results[code] = [];
                    checkCompletion();
                }
            });
        });

        function checkCompletion() {
            completed++;
            if (completed === regions.length) {
                console.log(`${LOG} ✅ Epic 地区比价数据收集完毕，准备回传...`);
                window.dispatchEvent(new CustomEvent('EPIC_PRICE_RESPONSE', {
                    detail: { containerId, gameName, success: true, results }
                }));
            }
        }
    });

})();
