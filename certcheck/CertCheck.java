import javax.net.ssl.*;
import java.security.KeyStore;
import java.security.cert.X509Certificate;

public class CertCheck {
    public static void main(String[] a) throws Exception {
        TrustManagerFactory tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        tmf.init((KeyStore) null);
        int total = 0, hit = 0;
        for (TrustManager tm : tmf.getTrustManagers()) {
            if (!(tm instanceof X509TrustManager)) continue;
            X509TrustManager x = (X509TrustManager) tm;
            X509Certificate[] issuers = x.getAcceptedIssuers();
            if (issuers.length > total) total = issuers.length;
            for (X509Certificate c : issuers) {
                String s = c.getSubjectX500Principal().getName();
                if (s.contains("mitmproxy") || s.contains("Reqable")) {
                    hit++;
                    System.out.println("FOUND: " + s);
                }
            }
        }
        System.out.println("TOTAL=" + total + " HIT=" + hit);
    }
}