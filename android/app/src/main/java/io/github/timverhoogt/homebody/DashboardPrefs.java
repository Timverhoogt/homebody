package io.github.timverhoogt.homebody;

import android.content.Context;
import android.content.SharedPreferences;

import io.github.timverhoogt.homebody.policy.DashboardOrigin;

/**
 * Non-secret app settings. Holds only the configured origin and a pending cache-clear flag; owner
 * sessions stay in WebView's cookie store. Backups and device transfer are disabled in the
 * manifest, so neither leaves the phone.
 */
final class DashboardPrefs {
    private static final String FILE = "homebody_settings";
    private static final String KEY_ORIGIN = "dashboard_origin";
    private static final String KEY_CLEAR_WEB_CACHE = "clear_web_cache";

    private final SharedPreferences prefs;

    DashboardPrefs(Context context) {
        prefs = context.getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    DashboardOrigin origin() {
        DashboardOrigin.ParseResult stored = DashboardOrigin.parse(prefs.getString(KEY_ORIGIN, null));
        return stored.isValid() ? stored.origin() : DashboardOrigin.defaultOrigin();
    }

    void setOrigin(DashboardOrigin origin) {
        prefs.edit().putString(KEY_ORIGIN, origin.startUrl()).apply();
    }

    void requestWebCacheClear() {
        prefs.edit().putBoolean(KEY_CLEAR_WEB_CACHE, true).apply();
    }

    /** True once after {@link #requestWebCacheClear()}; the next WebView then clears its cache. */
    boolean consumeWebCacheClear() {
        if (!prefs.getBoolean(KEY_CLEAR_WEB_CACHE, false)) return false;
        prefs.edit().remove(KEY_CLEAR_WEB_CACHE).apply();
        return true;
    }
}
