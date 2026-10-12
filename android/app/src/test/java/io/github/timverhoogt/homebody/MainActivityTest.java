package io.github.timverhoogt.homebody;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNotSame;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertSame;
import static org.junit.Assert.assertTrue;
import static org.robolectric.Shadows.shadowOf;

import android.app.AlertDialog;
import android.content.Intent;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.TextView;

import io.github.timverhoogt.homebody.policy.DashboardOrigin;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.android.controller.ActivityController;
import org.robolectric.annotation.Config;
import org.robolectric.shadows.ShadowAlertDialog;
import org.robolectric.shadows.ShadowLooper;
import org.robolectric.shadows.ShadowWebView;

@RunWith(RobolectricTestRunner.class)
@Config(shadows = WebFakes.ShadowServiceWorkerController.class)
public class MainActivityTest {
    private static final String ROOT = DashboardOrigin.DEFAULT_URL;

    private ActivityController<MainActivity> controller;
    private MainActivity activity;

    @Before
    public void setUp() {
        WebFakes.ShadowServiceWorkerController.reset();
        controller = Robolectric.buildActivity(MainActivity.class).setup();
        activity = controller.get();
    }

    @After
    public void tearDown() {
        ShadowAlertDialog.reset();
    }

    private WebView web() {
        return activity.webViewForTest();
    }

    private static ShadowWebView shadow(WebView view) {
        return shadowOf(view);
    }

    private WebViewClient client() {
        return shadow(web()).getWebViewClient();
    }

    private WebChromeClient chrome() {
        return shadow(web()).getWebChromeClient();
    }

    private View view(int id) {
        return activity.findViewById(id);
    }

    private String text(int id) {
        return ((TextView) view(id)).getText().toString();
    }

    @Test
    public void launchLoadsTheConfiguredDashboardWithLockedDownSettings() {
        assertEquals(ROOT, shadow(web()).getLastLoadedUrl());
        assertEquals(View.VISIBLE, web().getVisibility());
        WebSettings settings = web().getSettings();
        assertTrue(settings.getJavaScriptEnabled());
        assertTrue(settings.getDomStorageEnabled());
        assertFalse(settings.getAllowFileAccess());
        assertFalse(settings.getAllowContentAccess());
        assertFalse(settings.getAllowFileAccessFromFileURLs());
        assertFalse(settings.getAllowUniversalAccessFromFileURLs());
        assertEquals(WebSettings.MIXED_CONTENT_NEVER_ALLOW, settings.getMixedContentMode());
        assertFalse(settings.supportMultipleWindows());
        assertFalse(settings.getJavaScriptCanOpenWindowsAutomatically());
        assertTrue(CookieManager.getInstance().acceptCookie());
        assertFalse(CookieManager.getInstance().acceptThirdPartyCookies(web()));
        for (String name : new String[] {"Android", "Homebody", "homebody", "native", "bridge"}) {
            assertNull(shadow(web()).getJavascriptInterface(name));
        }
    }

    @Test
    public void serviceWorkersAreHeldToTheSameOriginPolicy() {
        WebFakes.FakeServiceWorkerController sw = WebFakes.ShadowServiceWorkerController.instance;
        assertFalse(sw.settings.getAllowFileAccess());
        assertFalse(sw.settings.getAllowContentAccess());
        assertNotNull(sw.client);
        assertNull(sw.client.shouldInterceptRequest(WebFakes.request(ROOT + "static/main.js", false, false)));
        WebResourceResponse blocked = sw.client.shouldInterceptRequest(
                WebFakes.request("https://cdn.example/x.js", false, false));
        assertEquals(403, blocked.getStatusCode());
    }

    @Test
    public void pauseUnloadsThePageAndStopDestroysTheWebView() {
        WebView original = web();
        controller.pause();
        assertEquals(MainActivity.BLANK, shadow(original).getLastLoadedUrl());
        assertEquals(View.INVISIBLE, original.getVisibility());
        assertTrue(shadow(original).wasOnPauseCalled());
        assertFalse(shadow(original).wasDestroyCalled());

        controller.stop();
        assertTrue(shadow(original).wasDestroyCalled());
        assertNull(original.getParent());
        assertNull(activity.webViewForTest());

        controller.start().resume();
        WebView fresh = web();
        assertNotSame(original, fresh);
        assertEquals(ROOT, shadow(fresh).getLastLoadedUrl());
        assertEquals(View.VISIBLE, fresh.getVisibility());
    }

    @Test
    public void openingSettingsTearsTheDashboardDown() {
        WebView original = web();
        view(R.id.settings_button).performClick();
        Intent started = shadowOf(activity).getNextStartedActivity();
        assertEquals(SettingsActivity.class.getName(), started.getComponent().getClassName());
        controller.pause().stop(); // what Android does when SettingsActivity covers this one
        assertTrue(shadow(original).wasDestroyCalled());
        assertNull(activity.webViewForTest());
    }

    @Test
    public void resumeAfterPauseRestoresTheTabAndClearsBlankFromHistory() {
        WebView original = web();
        client().doUpdateVisitedHistory(original, ROOT + "#robot", false);
        controller.pause();
        assertEquals(MainActivity.BLANK, shadow(original).getLastLoadedUrl());
        controller.resume();
        assertSame(original, web());
        assertEquals(ROOT + "#robot", shadow(original).getLastLoadedUrl());
        assertTrue(shadow(original).wasOnResumeCalled());
        assertFalse(shadow(original).wasClearHistoryCalled());
        client().onPageFinished(original, ROOT + "#robot");
        assertTrue(shadow(original).wasClearHistoryCalled());
    }

    @Test
    public void changedOriginIsLoadedAfterSettingsAndOldOriginBecomesForeign() {
        String other = "https://other-robot.example-tail.ts.net:8443/";
        new DashboardPrefs(activity).setOrigin(DashboardOrigin.parse(other).origin());
        controller.pause().stop().start().resume();
        assertEquals(other, shadow(web()).getLastLoadedUrl());
        assertTrue(client().shouldOverrideUrlLoading(web(), WebFakes.request(ROOT, true, false)));
        assertFalse(client().shouldOverrideUrlLoading(web(), WebFakes.request(other + "#robot", true, false)));
    }

    @Test
    public void pendingSiteDataClearAlsoClearsTheHttpCache() {
        new DashboardPrefs(activity).requestWebCacheClear();
        controller.pause().stop().start().resume();
        assertTrue(shadow(web()).wasClearCacheCalled());
        assertTrue(shadow(web()).didClearCacheIncludeDiskFiles());
        assertFalse(new DashboardPrefs(activity).consumeWebCacheClear()); // one-shot
    }

    @Test
    public void externalLinksOpenInTheBrowserOnlyAfterAGesture() {
        assertTrue(client().shouldOverrideUrlLoading(web(), WebFakes.request("https://github.com/Timverhoogt", true, true)));
        Intent intent = shadowOf(activity).getNextStartedActivity();
        assertEquals(Intent.ACTION_VIEW, intent.getAction());
        assertEquals("https://github.com/Timverhoogt", intent.getDataString());
        assertTrue(intent.hasCategory(Intent.CATEGORY_BROWSABLE));
        assertNull(intent.getComponent());

        assertTrue(client().shouldOverrideUrlLoading(web(), WebFakes.request("https://github.com/x", true, false)));
        assertNull(shadowOf(activity).getNextStartedActivity());
    }

    @Test
    public void hostileNavigationsAreBlockedWithoutLaunchingAnything() {
        String[] urls = {
            "intent://scan/#Intent;scheme=zxing;end", "javascript:alert(1)", "file:///sdcard/a.html",
            "content://com.android.contacts/contacts", "data:text/html,x", "market://details?id=x",
            "https://timverhoogtnano.tail51823f.ts.net.evil.example/", "http://timverhoogtnano.tail51823f.ts.net/x",
        };
        for (String url : urls) {
            boolean gesture = !url.startsWith("http");
            assertTrue(url, client().shouldOverrideUrlLoading(web(), WebFakes.request(url, true, gesture)));
            assertNull(url, shadowOf(activity).getNextStartedActivity());
        }
        assertTrue(client().shouldOverrideUrlLoading(web(), WebFakes.request("https://evil.example/", false, true)));
        assertFalse(client().shouldOverrideUrlLoading(web(), WebFakes.request(ROOT + "#agent", true, false)));
        assertFalse(client().shouldOverrideUrlLoading(web(), WebFakes.request("about:blank", false, false)));
    }

    @Test
    public void crossOriginRequestsGetAnEmpty403() throws Exception {
        assertNull(client().shouldInterceptRequest(web(), WebFakes.request(ROOT + "api/status", false, false)));
        WebResourceResponse response = client().shouldInterceptRequest(web(),
                WebFakes.request("https://tracker.example/pixel.gif", false, false));
        assertEquals(403, response.getStatusCode());
        assertEquals(-1, response.getData().read());
        WebResourceResponse frame = client().shouldInterceptRequest(web(),
                WebFakes.request("https://evil.example/form", true, true));
        assertEquals(403, frame.getStatusCode());
    }

    @Test
    public void tlsErrorsAreAlwaysCancelledAndExplained() {
        WebFakes.RecordingSslHandler handler = new WebFakes.RecordingSslHandler();
        client().onReceivedSslError(web(), handler, null); // the error details are never consulted
        assertTrue(handler.cancelled);
        assertFalse(handler.proceeded);
        assertTrue(activity.isErrorShowing());
        assertEquals(View.VISIBLE, view(R.id.error_panel).getVisibility());
        assertEquals(activity.getString(R.string.error_tls_title), text(R.id.error_title));
        assertEquals(View.GONE, view(R.id.error_hint).getVisibility());
        assertEquals(View.INVISIBLE, web().getVisibility());
        assertEquals(MainActivity.BLANK, shadow(web()).getLastLoadedUrl());
    }

    @Test
    public void offlineOrTailscaleDisconnectedShowsGuidanceAndRetryReloads() {
        client().onReceivedError(web(), WebFakes.request(ROOT, true, false), WebFakes.error(WebViewClient.ERROR_HOST_LOOKUP));
        assertEquals(activity.getString(R.string.error_host_title), text(R.id.error_title));
        assertEquals(View.VISIBLE, view(R.id.error_hint).getVisibility());

        view(R.id.retry_button).performClick();
        assertFalse(activity.isErrorShowing());
        assertEquals(View.GONE, view(R.id.error_panel).getVisibility());
        assertEquals(View.VISIBLE, web().getVisibility());
        assertEquals(ROOT, shadow(web()).getLastLoadedUrl());

        client().onReceivedError(web(), WebFakes.request(ROOT, true, false), WebFakes.error(WebViewClient.ERROR_CONNECT));
        assertEquals(activity.getString(R.string.error_connect_title), text(R.id.error_title));
        client().onReceivedError(web(), WebFakes.request(ROOT, true, false), WebFakes.error(WebViewClient.ERROR_TIMEOUT));
        assertEquals(activity.getString(R.string.error_connect_title), text(R.id.error_title));
        client().onReceivedError(web(), WebFakes.request(ROOT, true, false),
                WebFakes.error(WebViewClient.ERROR_FAILED_SSL_HANDSHAKE));
        assertEquals(activity.getString(R.string.error_tls_title), text(R.id.error_title));
    }

    @Test
    public void subresourceFailuresDoNotReplaceTheDashboard() {
        client().onReceivedError(web(), WebFakes.request(ROOT + "api/status", false, false),
                WebFakes.error(WebViewClient.ERROR_CONNECT));
        assertFalse(activity.isErrorShowing());
        client().onReceivedHttpError(web(), WebFakes.request(ROOT + "api/x", false, false),
                new WebResourceResponse("text/plain", "utf-8", 502, "Bad Gateway", null, null));
        assertFalse(activity.isErrorShowing());
        client().onReceivedHttpError(web(), WebFakes.request(ROOT, true, false),
                new WebResourceResponse("text/plain", "utf-8", 401, "Unauthorized", null, null));
        assertFalse(activity.isErrorShowing());
    }

    @Test
    public void serverRestartShowsUnavailable() {
        client().onReceivedHttpError(web(), WebFakes.request(ROOT, true, false),
                new WebResourceResponse("text/plain", "utf-8", 502, "Bad Gateway", null, null));
        assertEquals(activity.getString(R.string.error_server_title), text(R.id.error_title));
    }

    @Test
    public void foreignTopLevelDocumentIsStopped() {
        client().onPageStarted(web(), "https://evil.example/", null);
        assertEquals(activity.getString(R.string.error_blocked_title), text(R.id.error_title));
        assertEquals(MainActivity.BLANK, shadow(web()).getLastLoadedUrl());
        client().onPageStarted(web(), MainActivity.BLANK, null); // the blank page itself is fine
        assertEquals(activity.getString(R.string.error_blocked_title), text(R.id.error_title));
    }

    @Test
    public void everyPagePermissionRequestIsDenied() {
        WebFakes.RecordingPermissionRequest request = new WebFakes.RecordingPermissionRequest(
                android.webkit.PermissionRequest.RESOURCE_VIDEO_CAPTURE,
                android.webkit.PermissionRequest.RESOURCE_AUDIO_CAPTURE,
                android.webkit.PermissionRequest.RESOURCE_PROTECTED_MEDIA_ID,
                android.webkit.PermissionRequest.RESOURCE_MIDI_SYSEX);
        chrome().onPermissionRequest(request);
        assertTrue(request.denied);
        assertNull(request.granted);

        boolean[] geolocation = {true, true};
        chrome().onGeolocationPermissionsShowPrompt(ROOT, (origin, allow, retain) -> {
            geolocation[0] = allow;
            geolocation[1] = retain;
        });
        assertFalse(geolocation[0]);
        assertFalse(geolocation[1]);

        Object[] chosen = {"unset"};
        assertTrue(chrome().onShowFileChooser(web(), value -> chosen[0] = value, null));
        assertNull(chosen[0]);
        assertFalse(chrome().onCreateWindow(web(), false, true, null));
        assertTrue(chrome().onConsoleMessage(null));
    }

    @Test
    public void backAtTheRootAsksBeforeLeaving() {
        activity.handleBack();
        AlertDialog dialog = ShadowAlertDialog.getLatestAlertDialog();
        assertTrue(dialog.isShowing());
        assertEquals(0, shadow(web()).getGoBackInvocations());
        dialog.getButton(AlertDialog.BUTTON_NEGATIVE).performClick();
        ShadowLooper.idleMainLooper();
        assertFalse(activity.isFinishing());

        activity.handleBack();
        ShadowAlertDialog.getLatestAlertDialog().getButton(AlertDialog.BUTTON_POSITIVE).performClick();
        ShadowLooper.idleMainLooper();
        assertTrue(activity.isFinishing());
    }

    @Test
    @Config(sdk = 36)
    public void systemBackOnAndroid16UsesTheRegisteredCallback() {
        // Hidden framework accessor (visible because tests compile against AOSP android-all).
        // Activity hands out a proxy; the window's real dispatcher lives on the ViewRootImpl.
        android.window.WindowOnBackInvokedDispatcher dispatcher =
                activity.getWindow().getDecorView().getViewRootImpl().getOnBackInvokedDispatcher();
        dispatcher.getTopCallback().onBackInvoked();
        assertTrue(ShadowAlertDialog.getLatestAlertDialog().isShowing());
        assertFalse(activity.isFinishing());
    }

    @Test
    @Config(sdk = 26)
    @SuppressWarnings("deprecation")
    public void systemBackOnAndroid8UsesOnBackPressed() {
        activity.onBackPressed();
        assertTrue(ShadowAlertDialog.getLatestAlertDialog().isShowing());
        assertFalse(activity.isFinishing());
    }

    @Test
    public void exitDialogOffersServerSettings() {
        activity.handleBack();
        ShadowAlertDialog.getLatestAlertDialog().getButton(AlertDialog.BUTTON_NEUTRAL).performClick();
        ShadowLooper.idleMainLooper();
        Intent started = shadowOf(activity).getNextStartedActivity();
        assertEquals(SettingsActivity.class.getName(), started.getComponent().getClassName());
    }

    @Test
    public void backWalksDashboardHistory() {
        shadow(web()).pushEntryToHistory(ROOT + "#dashboard");
        shadow(web()).pushEntryToHistory(ROOT + "#robot");
        activity.handleBack();
        assertEquals(1, shadow(web()).getGoBackInvocations());
        assertNull(ShadowAlertDialog.getLatestAlertDialog());
    }

    @Test
    public void backOnAnErrorScreenAsksInsteadOfGoingBack() {
        shadow(web()).pushEntryToHistory(ROOT + "#dashboard");
        shadow(web()).pushEntryToHistory(ROOT + "#robot");
        client().onReceivedError(web(), WebFakes.request(ROOT, true, false), WebFakes.error(WebViewClient.ERROR_CONNECT));
        activity.handleBack();
        assertEquals(0, shadow(web()).getGoBackInvocations());
        assertTrue(ShadowAlertDialog.getLatestAlertDialog().isShowing());
    }

    @Test
    public void fullscreenCameraViewEndsOnBackAndOnPause() {
        boolean[] hidden = {false};
        View video = new View(activity);
        chrome().onShowCustomView(video, () -> hidden[0] = true);
        assertEquals(View.VISIBLE, view(R.id.fullscreen_container).getVisibility());
        activity.handleBack();
        assertTrue(hidden[0]);
        assertEquals(View.GONE, view(R.id.fullscreen_container).getVisibility());
        assertNull(ShadowAlertDialog.getLatestAlertDialog());

        hidden[0] = false;
        chrome().onShowCustomView(video, () -> hidden[0] = true);
        controller.pause();
        assertTrue(hidden[0]);
        assertEquals(View.GONE, view(R.id.fullscreen_container).getVisibility());
        assertNull(video.getParent());
    }

    @Test
    public void rendererCrashReplacesTheWebView() {
        WebView crashed = web();
        assertTrue(client().onRenderProcessGone(crashed, null));
        assertTrue(shadow(crashed).wasDestroyCalled());
        assertNotSame(crashed, web());
        assertEquals(activity.getString(R.string.error_crash_title), text(R.id.error_title));
        view(R.id.retry_button).performClick();
        assertEquals(ROOT, shadow(web()).getLastLoadedUrl());
    }

    @Test
    public void callbacksFromADestroyedWebViewAreIgnored() {
        WebView old = web();
        WebViewClient oldClient = client();
        controller.pause().stop().start().resume();
        oldClient.onReceivedError(old, WebFakes.request(ROOT, true, false), WebFakes.error(WebViewClient.ERROR_CONNECT));
        assertFalse(activity.isErrorShowing());
    }
}
