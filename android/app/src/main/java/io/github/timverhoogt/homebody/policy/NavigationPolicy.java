package io.github.timverhoogt.homebody.policy;

import java.util.Locale;

/**
 * Decides what the WebView may load. Immutable and thread-safe: WebView calls
 * {@code shouldInterceptRequest} on a background thread.
 *
 * <ul>
 *   <li>Top-level documents: only the configured origin. Other http(s) links leave the app for the
 *       system browser, and only after a user gesture. Every other scheme (javascript:, file:,
 *       content:, intent:, data:, market:, ...) is blocked.
 *   <li>Frames: the configured origin plus about:blank/about:srcdoc. No external iframes.
 *   <li>Subresources: the configured origin, data: and same-origin blob: URLs.
 * </ul>
 */
public final class NavigationPolicy {
    /** Outcome for a navigation request. */
    public enum Decision {
        LOAD_IN_APP,
        OPEN_IN_BROWSER,
        BLOCK,
    }

    private final DashboardOrigin origin;

    public NavigationPolicy(DashboardOrigin origin) {
        this.origin = origin;
    }

    public DashboardOrigin origin() {
        return origin;
    }

    public Decision forNavigation(String url, boolean mainFrame, boolean userGesture) {
        if (url == null) return Decision.BLOCK;
        if (origin.isSameOrigin(url)) return Decision.LOAD_IN_APP;
        if (!mainFrame) return isLocalFrameDocument(url) ? Decision.LOAD_IN_APP : Decision.BLOCK;
        String scheme = schemeOf(url);
        boolean web = ("https".equals(scheme) || "http".equals(scheme))
                && DashboardOrigin.authorityOf(url, scheme) != null;
        return web && userGesture ? Decision.OPEN_IN_BROWSER : Decision.BLOCK;
    }

    /** Whether a committed top-level document is acceptable (defence in depth). */
    public boolean allowsMainFrameDocument(String url) {
        return origin.isSameOrigin(url) || "about:blank".equals(url);
    }

    public boolean allowsSubresource(String url) {
        if (url == null) return false;
        if (origin.isSameOrigin(url)) return true;
        String scheme = schemeOf(url);
        if ("data".equals(scheme)) return true;
        if ("blob".equals(scheme)) return origin.isSameOrigin(url.substring("blob:".length()));
        return false;
    }

    private static boolean isLocalFrameDocument(String url) {
        return "about:blank".equals(url) || "about:srcdoc".equals(url);
    }

    static String schemeOf(String url) {
        int colon = url.indexOf(':');
        return colon <= 0 ? "" : url.substring(0, colon).toLowerCase(Locale.ROOT);
    }
}
