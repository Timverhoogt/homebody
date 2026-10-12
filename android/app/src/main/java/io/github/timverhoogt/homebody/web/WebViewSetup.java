package io.github.timverhoogt.homebody.web;

import android.content.Context;
import android.webkit.CookieManager;
import android.webkit.ServiceWorkerClient;
import android.webkit.ServiceWorkerController;
import android.webkit.ServiceWorkerWebSettings;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;

import io.github.timverhoogt.homebody.policy.NavigationPolicy;

/**
 * Creates the dashboard WebView with a locked-down configuration. Intentionally, no JavaScript
 * interface is registered: the page gets no JS-to-native bridge of any kind.
 */
public final class WebViewSetup {
    private WebViewSetup() {}

    @SuppressWarnings("deprecation") // file-URL settings are deprecated but still set explicitly
    public static WebView create(Context context, NavigationPolicy policy,
            DashboardWebViewClient.Host clientHost, DashboardChromeClient.Host chromeHost) {
        WebView.setWebContentsDebuggingEnabled(false);
        WebView webView = new WebView(context);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true); // the dashboard is a JavaScript app
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setGeolocationEnabled(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setSupportMultipleWindows(false); // target=_blank goes through shouldOverrideUrlLoading
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setSafeBrowsingEnabled(false); // single private origin; see README
        // The robot camera <video> is muted and starts after a WebRTC handshake that outlives the
        // tap's activation window. This affects playback only, never phone capture.
        settings.setMediaPlaybackRequiresUserGesture(false);

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true); // Owner session: Secure, HttpOnly, SameSite=Strict
        cookies.setAcceptThirdPartyCookies(webView, false);

        webView.setWebViewClient(new DashboardWebViewClient(policy, clientHost));
        webView.setWebChromeClient(new DashboardChromeClient(chromeHost));
        restrictServiceWorkers(policy);
        return webView;
    }

    /** The dashboard registers a service worker; hold its fetches to the same policy. */
    private static void restrictServiceWorkers(NavigationPolicy policy) {
        ServiceWorkerController controller = ServiceWorkerController.getInstance();
        ServiceWorkerWebSettings settings = controller.getServiceWorkerWebSettings();
        settings.setAllowContentAccess(false);
        settings.setAllowFileAccess(false);
        controller.setServiceWorkerClient(new ServiceWorkerClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebResourceRequest request) {
                return policy.allowsSubresource(request.getUrl().toString()) ? null : BlockedResponse.create();
            }
        });
    }
}
