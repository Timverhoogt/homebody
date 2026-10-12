package io.github.timverhoogt.homebody.web;

import android.graphics.Bitmap;
import android.net.http.SslError;
import android.webkit.ClientCertRequest;
import android.webkit.HttpAuthHandler;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.SslErrorHandler;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import io.github.timverhoogt.homebody.policy.ConnectionProblem;
import io.github.timverhoogt.homebody.policy.NavigationPolicy;

/**
 * Applies {@link NavigationPolicy} and turns load failures into {@link ConnectionProblem}s.
 * TLS errors are always cancelled; there is no "proceed anyway" path. Nothing here logs URLs.
 */
public final class DashboardWebViewClient extends WebViewClient {
    /** Implemented by the activity that owns the WebView. */
    public interface Host {
        void onDashboardProblem(WebView view, ConnectionProblem problem);

        void onDashboardVisited(WebView view, String url);

        void onDashboardLoaded(WebView view);

        void openInBrowser(String url);

        void onRendererGone(WebView view);
    }

    private final NavigationPolicy policy;
    private final Host host;

    public DashboardWebViewClient(NavigationPolicy policy, Host host) {
        this.policy = policy;
        this.host = host;
    }

    @Override
    public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
        String url = request.getUrl().toString();
        switch (policy.forNavigation(url, request.isForMainFrame(), request.hasGesture())) {
            case LOAD_IN_APP:
                return false;
            case OPEN_IN_BROWSER:
                host.openInBrowser(url);
                return true;
            default:
                return true;
        }
    }

    @Override
    public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
        // Also covers POST navigations and redirects, which skip shouldOverrideUrlLoading.
        return policy.allowsSubresource(request.getUrl().toString()) ? null : BlockedResponse.create();
    }

    @Override
    public void onPageStarted(WebView view, String url, Bitmap favicon) {
        if (!policy.allowsMainFrameDocument(url)) {
            view.stopLoading();
            host.onDashboardProblem(view, ConnectionProblem.BLOCKED_NAVIGATION);
        }
    }

    @Override
    public void onPageFinished(WebView view, String url) {
        if (policy.origin().isSameOrigin(url)) host.onDashboardLoaded(view);
    }

    @Override
    public void doUpdateVisitedHistory(WebView view, String url, boolean isReload) {
        host.onDashboardVisited(view, url);
    }

    @Override
    public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
        if (request.isForMainFrame()) {
            host.onDashboardProblem(view, ConnectionProblem.fromWebViewError(error.getErrorCode()));
        }
    }

    @Override
    public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
        if (!request.isForMainFrame()) return;
        ConnectionProblem problem = ConnectionProblem.fromHttpStatus(response.getStatusCode());
        if (problem != null) host.onDashboardProblem(view, problem);
    }

    @Override
    public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
        handler.cancel();
        host.onDashboardProblem(view, ConnectionProblem.TLS_REFUSED);
    }

    @Override
    public void onReceivedClientCertRequest(WebView view, ClientCertRequest request) {
        request.cancel();
    }

    @Override
    public void onReceivedHttpAuthRequest(WebView view, HttpAuthHandler handler, String host, String realm) {
        handler.cancel();
    }

    @Override
    public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
        host.onRendererGone(view);
        return true;
    }
}
