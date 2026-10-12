package io.github.timverhoogt.homebody;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.WebBackForwardList;
import android.webkit.WebChromeClient;
import android.webkit.WebView;
import android.widget.FrameLayout;
import android.widget.TextView;
import android.widget.Toast;
import android.window.OnBackInvokedCallback;
import android.window.OnBackInvokedDispatcher;

import io.github.timverhoogt.homebody.policy.BackPolicy;
import io.github.timverhoogt.homebody.policy.ConnectionProblem;
import io.github.timverhoogt.homebody.policy.DashboardOrigin;
import io.github.timverhoogt.homebody.policy.DashboardSession;
import io.github.timverhoogt.homebody.policy.NavigationPolicy;
import io.github.timverhoogt.homebody.web.DashboardChromeClient;
import io.github.timverhoogt.homebody.web.DashboardWebViewClient;
import io.github.timverhoogt.homebody.web.WebViewSetup;

/**
 * Hosts the Homebody dashboard for one configured HTTPS origin.
 *
 * <p>Lifecycle privacy contract: the dashboard only runs while this activity is resumed.
 * <ul>
 *   <li>{@code onPause}: leave fullscreen, navigate to about:blank (the page unloads, so its
 *       WebRTC camera session, WebSockets, polls and timers end), hide the view, pause it.
 *   <li>{@code onStop}: destroy the WebView outright, so nothing of the page survives in the
 *       background even if the about:blank navigation had not committed.
 *   <li>{@code onResume}: load the dashboard again. Owner cookies are kept by CookieManager, so
 *       no re-pairing is needed. Reopening never starts the camera; that remains an explicit
 *       opt-in inside the dashboard.
 * </ul>
 * Opening Settings stops this activity, so the dashboard is torn down while settings are shown.
 */
public class MainActivity extends Activity
        implements DashboardWebViewClient.Host, DashboardChromeClient.Host {
    static final String BLANK = "about:blank";

    private FrameLayout webContainer;
    private FrameLayout fullscreenContainer;
    private View errorPanel;
    private TextView errorTitle;
    private TextView errorMessage;
    private TextView errorHint;

    private DashboardPrefs prefs;
    private DashboardSession session;
    private WebView webView;
    private boolean errorShowing;
    private boolean clearHistoryOnLoad;
    private View customView;
    private WebChromeClient.CustomViewCallback customViewCallback;
    private Object backCallback; // OnBackInvokedCallback on API 33+

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        webContainer = findViewById(R.id.web_container);
        fullscreenContainer = findViewById(R.id.fullscreen_container);
        errorPanel = findViewById(R.id.error_panel);
        errorTitle = findViewById(R.id.error_title);
        errorMessage = findViewById(R.id.error_message);
        errorHint = findViewById(R.id.error_hint);
        findViewById(R.id.retry_button).setOnClickListener(v -> showDashboard());
        findViewById(R.id.settings_button).setOnClickListener(v -> openSettings());
        prefs = new DashboardPrefs(this);
        if (Build.VERSION.SDK_INT >= 33) backCallback = Api33.install(this);
    }

    @Override
    protected void onStart() {
        super.onStart();
        DashboardOrigin configured = prefs.origin();
        if (session == null || !session.origin().equals(configured)) {
            session = new DashboardSession(configured);
            destroyWebView();
        }
        if (webView == null) createWebView();
    }

    @Override
    protected void onResume() {
        super.onResume();
        webView.onResume();
        showDashboard();
    }

    @Override
    protected void onPause() {
        suspendDashboard();
        super.onPause();
    }

    @Override
    protected void onStop() {
        destroyWebView();
        CookieManager.getInstance().flush();
        super.onStop();
    }

    @Override
    protected void onDestroy() {
        destroyWebView();
        if (backCallback != null) Api33.uninstall(this, backCallback);
        super.onDestroy();
    }

    /** Pre-Android 13 back handling; API 33+ uses the registered OnBackInvokedCallback. */
    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        handleBack();
    }

    private void createWebView() {
        webView = WebViewSetup.create(this, new NavigationPolicy(session.origin()), this, this);
        if (prefs.consumeWebCacheClear()) webView.clearCache(true);
        webContainer.addView(webView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
    }

    private void destroyWebView() {
        if (webView == null) return;
        leaveFullscreen();
        WebView old = webView;
        webView = null;
        webContainer.removeView(old);
        old.stopLoading();
        old.destroy();
    }

    void showDashboard() {
        errorShowing = false;
        errorPanel.setVisibility(View.GONE);
        webView.setVisibility(View.VISIBLE);
        clearHistoryOnLoad = true; // drop the about:blank/previous-session entries from Back
        webView.loadUrl(session.restoreUrl());
    }

    private void suspendDashboard() {
        leaveFullscreen();
        if (webView == null) return;
        webView.stopLoading();
        webView.loadUrl(BLANK);
        webView.setVisibility(View.INVISIBLE);
        webView.onPause();
    }

    void handleBack() {
        if (customView != null) {
            leaveFullscreen();
            return;
        }
        boolean canGoBack = webView != null && webView.canGoBack();
        String previous = null;
        if (canGoBack) {
            WebBackForwardList history = webView.copyBackForwardList();
            int index = history.getCurrentIndex();
            if (index > 0) previous = history.getItemAtIndex(index - 1).getUrl();
        }
        if (BackPolicy.onBack(session.origin(), errorShowing, canGoBack, previous) == BackPolicy.Action.GO_BACK) {
            webView.goBack();
        } else {
            new AlertDialog.Builder(this)
                    .setTitle(R.string.exit_title)
                    .setMessage(R.string.exit_message)
                    .setPositiveButton(R.string.exit_leave, (dialog, which) -> finish())
                    .setNegativeButton(R.string.exit_stay, null)
                    .setNeutralButton(R.string.action_server_settings, (dialog, which) -> openSettings())
                    .show();
        }
    }

    private void openSettings() {
        startActivity(new Intent(this, SettingsActivity.class));
    }

    // DashboardWebViewClient.Host

    @Override
    public void onDashboardProblem(WebView view, ConnectionProblem problem) {
        if (view != webView) return;
        errorShowing = true;
        view.stopLoading();
        view.loadUrl(BLANK);
        view.setVisibility(View.INVISIBLE);
        leaveFullscreen();
        ErrorText text = ErrorText.forProblem(problem);
        errorTitle.setText(text.title);
        errorMessage.setText(text.message);
        boolean hint = problem.showTailscaleHint(session.origin());
        errorHint.setVisibility(hint ? View.VISIBLE : View.GONE);
        errorPanel.setVisibility(View.VISIBLE);
    }

    @Override
    public void onDashboardVisited(WebView view, String url) {
        if (view == webView) session.onVisited(url);
    }

    @Override
    public void onDashboardLoaded(WebView view) {
        if (view != webView || !clearHistoryOnLoad) return;
        clearHistoryOnLoad = false;
        view.clearHistory();
    }

    @Override
    public void openInBrowser(String url) {
        Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
        intent.addCategory(Intent.CATEGORY_BROWSABLE);
        try {
            startActivity(intent);
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, R.string.no_browser, Toast.LENGTH_SHORT).show();
        }
    }

    @Override
    public void onRendererGone(WebView view) {
        if (view != webView) return;
        destroyWebView();
        createWebView();
        onDashboardProblem(webView, ConnectionProblem.PAGE_CRASHED);
    }

    // DashboardChromeClient.Host

    @Override
    public void showCustomView(View view, WebChromeClient.CustomViewCallback callback) {
        if (customView != null || webView == null) {
            callback.onCustomViewHidden();
            return;
        }
        customView = view;
        customViewCallback = callback;
        fullscreenContainer.addView(view);
        fullscreenContainer.setVisibility(View.VISIBLE);
        webContainer.setVisibility(View.INVISIBLE);
    }

    @Override
    public void hideCustomView() {
        removeCustomView();
    }

    /** Leaves element fullscreen and tells the page, which ends the camera control gesture. */
    private void leaveFullscreen() {
        WebChromeClient.CustomViewCallback callback = removeCustomView();
        if (callback != null) callback.onCustomViewHidden();
    }

    private WebChromeClient.CustomViewCallback removeCustomView() {
        if (customView == null) return null;
        fullscreenContainer.removeView(customView);
        fullscreenContainer.setVisibility(View.GONE);
        webContainer.setVisibility(View.VISIBLE);
        WebChromeClient.CustomViewCallback callback = customViewCallback;
        customView = null;
        customViewCallback = null;
        return callback;
    }

    /** API 33+ calls, kept in their own class so older runtimes never verify them. */
    private static final class Api33 {
        static Object install(MainActivity activity) {
            activity.setRecentsScreenshotEnabled(false); // no dashboard snapshot in recents
            OnBackInvokedCallback callback = activity::handleBack;
            activity.getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                    OnBackInvokedDispatcher.PRIORITY_DEFAULT, callback);
            return callback;
        }

        static void uninstall(MainActivity activity, Object callback) {
            activity.getOnBackInvokedDispatcher().unregisterOnBackInvokedCallback((OnBackInvokedCallback) callback);
        }
    }

    // Test seams (package-private, no behaviour).

    WebView webViewForTest() {
        return webView;
    }

    boolean isErrorShowing() {
        return errorShowing;
    }
}
