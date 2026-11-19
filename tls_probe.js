// tls_probe.js — 在微信进程内验证 TLS 信任链是否打通
// 成功说明：系统 CA 被接受，问题不在证书
// 失败说明：微信有 pinning 或自定义校验

setTimeout(function () {
    Java.perform(function () {
        send("probe start");

        // 1) 列出 TrustManager 里有没有我们的 CA
        try {
            var TrustManagerFactory = Java.use("javax.net.ssl.TrustManagerFactory");
            var KeyStore = Java.use("java.security.KeyStore");
            var tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
            tmf.init(KeyStore.getInstance(KeyStore.getDefaultType()));
            var tms = tmf.getTrustManagers();
            for (var i = 0; i < tms.length; i++) {
                var tm = Java.cast(tms[i], Java.use("javax.net.ssl.X509TrustManager"));
                var issuers = tm.getAcceptedIssuers();
                var hit = [];
                for (var j = 0; j < issuers.length; j++) {
                    var dn = issuers[j].getSubjectX500Principal().getName();
                    if (dn.indexOf("mitmproxy") >= 0 || dn.indexOf("Reqable") >= 0) hit.push(dn.substring(0, 40));
                }
                send("TrustManager[" + i + "] total=" + issuers.length + " ourCA=" + JSON.stringify(hit));
            }
        } catch (e) {
            send("trustmanager probe err: " + e);
        }

        // 2) 实际发起一次 HTTPS 连接
        try {
            var URL = Java.use("java.net.URL");
            var u = URL.$new("https://run.ecust.edu.cn/");
            var conn = u.openConnection();
            conn.setConnectTimeout(8000);
            conn.setReadTimeout(8000);
            conn.connect();
            send("HTTPS_RESULT: OK code=" + conn.getResponseCode());
        } catch (e) {
            send("HTTPS_RESULT: FAIL " + e);
        }
    });
}, 1500);
