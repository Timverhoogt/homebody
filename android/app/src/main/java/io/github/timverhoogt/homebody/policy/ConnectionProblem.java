package io.github.timverhoogt.homebody.policy;

/**
 * User-facing categories for a dashboard that could not be shown. The numeric codes are the
 * public {@code android.webkit.WebViewClient.ERROR_*} constants (stable API values).
 */
public enum ConnectionProblem {
    /** DNS failed: phone offline, Tailscale disconnected or MagicDNS unavailable. */
    HOST_NOT_FOUND,
    /** Host resolved but did not answer: robot off, Tailscale Serve stopped, or timeout. */
    CANNOT_CONNECT,
    /** Reached the server, which answered 5xx (for example while Homebody restarts). */
    SERVER_UNAVAILABLE,
    /** The TLS certificate or handshake was refused. The app never bypasses this. */
    TLS_REFUSED,
    /** The page tried to leave the configured origin and was stopped. */
    BLOCKED_NAVIGATION,
    /** The WebView renderer stopped. */
    PAGE_CRASHED,
    /** Anything else. */
    OTHER;

    static final int ERROR_HOST_LOOKUP = -2;
    static final int ERROR_CONNECT = -6;
    static final int ERROR_IO = -7;
    static final int ERROR_TIMEOUT = -8;
    static final int ERROR_FAILED_SSL_HANDSHAKE = -11;

    public static ConnectionProblem fromWebViewError(int errorCode) {
        switch (errorCode) {
            case ERROR_HOST_LOOKUP:
                return HOST_NOT_FOUND;
            case ERROR_CONNECT:
            case ERROR_IO:
            case ERROR_TIMEOUT:
                return CANNOT_CONNECT;
            case ERROR_FAILED_SSL_HANDSHAKE:
                return TLS_REFUSED;
            default:
                return OTHER;
        }
    }

    /** Main-frame HTTP status worth replacing the page for; null keeps the server's own page. */
    public static ConnectionProblem fromHttpStatus(int status) {
        return status >= 500 && status <= 599 ? SERVER_UNAVAILABLE : null;
    }

    /** Whether to add the "open Tailscale and connect" hint for this problem and origin. */
    public boolean showTailscaleHint(DashboardOrigin origin) {
        boolean networkProblem = this == HOST_NOT_FOUND || this == CANNOT_CONNECT || this == OTHER;
        return networkProblem && origin.isLikelyTailnet();
    }
}
