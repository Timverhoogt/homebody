package io.github.timverhoogt.homebody;

import android.content.Context;
import android.webkit.CookieManager;
import android.webkit.WebStorage;

/**
 * Removes everything the dashboard stored on this phone: cookies (including the Owner session),
 * DOM storage, service worker caches and, on the next dashboard start, the HTTP cache.
 *
 * <p>This only forgets the phone's copy. The robot still lists the device until it is revoked
 * with "Forget this device" in the dashboard, which is the step that actually unpairs it.
 */
final class SiteData {
    private SiteData() {}

    static void clearAll(Context context, Runnable done) {
        new DashboardPrefs(context).requestWebCacheClear();
        WebStorage.getInstance().deleteAllData();
        CookieManager cookies = CookieManager.getInstance();
        cookies.removeAllCookies(removed -> {
            cookies.flush();
            done.run();
        });
    }
}
