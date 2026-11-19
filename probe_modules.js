// probe_modules.js — 找出微信实际使用的 TLS 实现
'use strict';

function report() {
    const keywords = ['ssl', 'tls', 'crypto', 'cronet', 'boring', 'mbed', 'wolf', 'botan', 'openssl'];
    const interesting = [];
    let total = 0;

    Process.enumerateModules().forEach(m => {
        total++;
        const n = m.name.toLowerCase();
        if (keywords.some(k => n.indexOf(k) >= 0)) {
            interesting.push(m);
        }
    });

    send(`[modules] total=${total} tls_like=${interesting.length}`);
    interesting.forEach(m => {
        let sslExports = [];
        try {
            m.enumerateExports().forEach(e => {
                if (/^(SSL_|X509_|TLS_|ssl_)/.test(e.name) && sslExports.length < 6) {
                    sslExports.push(e.name);
                }
            });
        } catch (e) {}
        send(`[module] ${m.name} size=${m.size} exports=${sslExports.join(',') || '-'}`);
    });

    // 统计 SSL_write / SSL_read 是否存在且可 hook
    ['SSL_write', 'SSL_read', 'SSL_new', 'SSL_CTX_new'].forEach(fn => {
        let addr = null;
        try { addr = Module.findGlobalExportByName(fn); } catch (e) {}
        send(`[api] ${fn} = ${addr}`);
    });
}

setTimeout(report, 500);

// hook 所有模块里的 SSL_write / SSL_read，看是否有流量经过
let wcount = 0, rcount = 0;
setTimeout(() => {
    Process.enumerateModules().forEach(mod => {
        let exps;
        try { exps = mod.enumerateExports(); } catch (e) { return; }
        for (const e of exps) {
            if (e.type !== 'function') continue;
            if (e.name === 'SSL_write') {
                try {
                    Interceptor.attach(e.address, {
                        onEnter(args) { wcount++; if (wcount <= 3) send(`[SSL_write] ${mod.name} #${wcount}`); }
                    });
                    send(`[hooked] ${mod.name}!SSL_write`);
                } catch (err) {}
            } else if (e.name === 'SSL_read') {
                try {
                    Interceptor.attach(e.address, {
                        onEnter(args) { rcount++; if (rcount <= 3) send(`[SSL_read] ${mod.name} #${rcount}`); }
                    });
                    send(`[hooked] ${mod.name}!SSL_read`);
                } catch (err) {}
            }
        }
    });
    setInterval(() => send(`[io] write=${wcount} read=${rcount}`), 6000);
}, 1000);
