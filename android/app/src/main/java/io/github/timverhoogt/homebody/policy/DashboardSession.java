package io.github.timverhoogt.homebody.policy;

import java.util.regex.Pattern;

/**
 * In-memory state for one configured origin: which dashboard tab to reopen after the page was
 * suspended. Only a short tab token such as "robot" is kept; full URLs (which could carry private
 * parameters) are never stored, persisted or logged.
 */
public final class DashboardSession {
    private static final Pattern TAB = Pattern.compile("[a-z][a-z0-9-]{0,31}");

    private final DashboardOrigin origin;
    private String tab;

    public DashboardSession(DashboardOrigin origin) {
        this.origin = origin;
    }

    public DashboardOrigin origin() {
        return origin;
    }

    /** Called for every committed same-document or document navigation. */
    public void onVisited(String url) {
        if (!origin.isSameOrigin(url)) return;
        int hash = url.indexOf('#');
        String fragment = hash < 0 ? null : url.substring(hash + 1);
        tab = fragment != null && TAB.matcher(fragment).matches() ? fragment : null;
    }

    /** URL to load when the dashboard is shown again. Reopening a tab never starts the camera. */
    public String restoreUrl() {
        return tab == null ? origin.startUrl() : origin.startUrl() + "#" + tab;
    }
}
