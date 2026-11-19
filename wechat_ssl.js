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
