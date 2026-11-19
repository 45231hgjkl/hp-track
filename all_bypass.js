// wechat_ssl.js — 微信 native TLS 绕过 + 调用计数
// 关键点: BoringSSL 的 custom_verify 回调才是真正的 pinning 入口，
//         必须把回调指针换成恒返回 ssl_verify_ok(0) 的函数。

'use strict';

const VERIFY_FUNCS = ['SSL_verify_cert_chain', 'X509_verify_cert'];
const ZERO_FUNCS = ['SSL_get_verify_result', 'X509_STORE_CTX_get_error'];
const CUSTOM_VERIFY_FUNCS = ['SSL_CTX_set_custom_verify', 'SSL_set_custom_verify'];
const CONNECT_FUNCS = ['SSL_connect', 'SSL_do_handshake'];

// ssl_verify_ok = 0, ssl_verify_invalid = 1, ssl_verify_retry = 2
const VERIFY_OK_CB = new NativeCallback(function (ssl, out_alert) {
    return 0;
}, 'int', ['pointer', 'pointer']);

const hits = Object.create(null);
let hookedCount = 0;
const seen = new Set();

function bump(name) {
    hits[name] = (hits[name] || 0) + 1;
    return hits[name];
}

function attach(mod, exp) {
    if (seen.has(exp.address.toString())) return false;
    seen.add(exp.address.toString());
    const name = exp.name;

    try {
        // ① custom_verify: 替换回调指针，这是真正的 pinning 入口
        if (CUSTOM_VERIFY_FUNCS.indexOf(name) >= 0) {
            Interceptor.attach(exp.address, {
                onEnter(args) {
                    const n = bump(name);
                    args[1] = ptr(0);                 // mode = SSL_VERIFY_NONE
                    args[2] = VERIFY_OK_CB;           // callback 恒返回 ok
                    if (n <= 3) send(`[BYPASS] ${name}#${n} callback -> always OK`);
                }
            });
            hookedCount++;
            send(`[hook] ${mod.name}!${name} (callback override)`);
            return true;
        }

        // ② SSL_connect / SSL_do_handshake: 只观察，不改结果
        if (CONNECT_FUNCS.indexOf(name) >= 0) {
            Interceptor.attach(exp.address, {
                onLeave(retval) {
                    const n = bump(name);
                    if (n <= 3) send(`[handshake] ${name} ret=${retval.toInt32()}`);
                }
            });
            hookedCount++;
            return true;
        }

        // ③ verify 结果类: 强制通过
        const isZero = ZERO_FUNCS.indexOf(name) >= 0;
        Interceptor.attach(exp.address, {
            onLeave(retval) {
                const n = bump(name);
                if (n <= 3) send(`[verify] ${name}#${n} -> ${isZero ? 0 : 1}`);
                retval.replace(isZero ? 0 : 1);
            }
        });
        hookedCount++;
        return true;
    } catch (e) {
        return false;
    }
}

function scanAll(tag) {
    let added = 0;
    Process.enumerateModules().forEach(mod => {
        let exps;
        try { exps = mod.enumerateExports(); } catch (e) { return; }
        for (const exp of exps) {
            if (exp.type !== 'function') continue;
            const n = exp.name;
            if (VERIFY_FUNCS.indexOf(n) >= 0 || ZERO_FUNCS.indexOf(n) >= 0 ||
                CUSTOM_VERIFY_FUNCS.indexOf(n) >= 0 || CONNECT_FUNCS.indexOf(n) >= 0) {
                if (attach(mod, exp)) added++;
            }
        }
    });
    if (added) send(`[scan:${tag}] +${added} total=${hookedCount}`);
}

setTimeout(() => scanAll('boot'), 600);
[3000, 10000, 20000, 40000].forEach(ms => setTimeout(() => scanAll('t' + ms), ms));

setInterval(() => {
    send('[stats] ' + JSON.stringify(hits) + ' hooked=' + hookedCount);
}, 6000);

send('[ready] bypass v2 loaded (custom_verify override active)');

// ===== Cronet bypass (isolated scope) =====
(function(){
// cronet_bypass.js — 绕过 Cronet（Chromium 网络栈）的证书校验
// 微信小程序走 Cronet，不是系统 libssl，所以之前的 hook 覆盖不到这里。
const CRONET_RE = /libcronet[\d\.]*\.so/i;

// 需要强制为 true / 1 的开关
const BOOL_OVERRIDES = [
    'Cronet_EngineParams_verify_user_ca_set',
    'Cronet_EngineParams_enable_public_key_pinning_bypass_for_local_trust_anchors_set',
    'Cronet_EngineParams_enable_http2_set',
];

// 需要拦截并追加我们 CA 的函数
const TRUST_ANCHOR_ADD = 'Cronet_EngineParams_additional_trust_anchors_base64_add';
// 强制 mock verifier（传 NULL 表示使用内置的"不校验"实现）
const MOCK_VERIFIER = 'Cronet_Engine_SetMockCertVerifierForTesting';

const hits = Object.create(null);
let installed = 0;

function bump(k) {
    hits[k] = (hits[k] || 0) + 1;
    if (hits[k] <= 3) send(`[hit] ${k} #${hits[k]}`);
}

function install() {
    Process.enumerateModules().forEach(mod => {
        if (!CRONET_RE.test(mod.name)) return;

        BOOL_OVERRIDES.forEach(fn => {
            const addr = mod.findExportByName(fn);
            if (!addr) return;
            try {
                Interceptor.attach(addr, {
                    onEnter(args) {
                        bump(fn);
                        args[1] = ptr(1);          // 强制 true
                    }
                });
                installed++;
                send(`[hook] ${mod.name}!${fn} -> forced true`);
            } catch (e) {}
        });

        const addAddr = mod.findExportByName(TRUST_ANCHOR_ADD);
        if (addAddr) {
            try {
                Interceptor.attach(addAddr, {
                    onEnter(args) { bump(TRUST_ANCHOR_ADD); }
                });
                installed++;
                send(`[hook] ${mod.name}!${TRUST_ANCHOR_ADD} (observe)`);
            } catch (e) {}
        }

        const mockAddr = mod.findExportByName(MOCK_VERIFIER);
        if (mockAddr) {
            try {
                Interceptor.attach(mockAddr, {
                    onEnter(args) {
                        bump(MOCK_VERIFIER);
                        args[1] = ptr(0);          // NULL verifier = 接受一切
                    }
                });
                installed++;
                send(`[hook] ${mod.name}!${MOCK_VERIFIER} -> NULL`);
            } catch (e) {}
        }

        // quic_set_certverify_callback 也要拿下（QUIC 用）
        const quicAddr = mod.findExportByName('quic_set_certverify_callback');
        if (quicAddr) {
            try {
                Interceptor.attach(quicAddr, {
                    onEnter(args) { bump('quic_set_certverify_callback'); }
                });
                installed++;
            } catch (e) {}
        }
    });

    send(`[installed] ${installed} hooks, modules scanned`);
}

setTimeout(install, 700);
[4000, 12000, 25000].forEach(ms => setTimeout(install, ms));

setInterval(() => {
    send('[stats] ' + JSON.stringify(hits));
}, 7000);

send('[ready] cronet bypass loaded');

})();
