package io.github.timverhoogt.homebody.policy;

/**
 * Back goes back within the dashboard's own history; at the root, on an error screen or when the
 * previous entry is not the dashboard (for example the about:blank used while suspended), the
 * app asks before leaving.
 */
public final class BackPolicy {
    /** What the Back gesture should do. */
    public enum Action {
        GO_BACK,
        CONFIRM_EXIT,
    }

    private BackPolicy() {}

    public static Action onBack(DashboardOrigin origin, boolean errorShowing, boolean canGoBack, String previousUrl) {
        if (!errorShowing && canGoBack && origin.isSameOrigin(previousUrl)) return Action.GO_BACK;
        return Action.CONFIRM_EXIT;
    }
}
