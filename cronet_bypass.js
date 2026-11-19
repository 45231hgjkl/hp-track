// cronet_bypass.js — 绕过 Cronet（Chromium 网络栈）的证书校验
// 微信小程序走 Cronet，不是系统 libssl，所以之前的 hook 覆盖不到这里。
'use strict';

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
