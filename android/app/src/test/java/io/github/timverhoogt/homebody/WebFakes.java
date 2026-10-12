package io.github.timverhoogt.homebody;

import android.net.Uri;
import android.webkit.PermissionRequest;
import android.webkit.ServiceWorkerClient;
import android.webkit.ServiceWorkerController;
import android.webkit.ServiceWorkerWebSettings;
import android.webkit.SslErrorHandler;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;

import java.util.Collections;
import java.util.Map;

import org.robolectric.annotation.Implementation;
import org.robolectric.annotation.Implements;

/** Synthetic WebView callback arguments. No network access, no robot. */
final class WebFakes {
    private WebFakes() {}

    static WebResourceRequest request(String url, boolean mainFrame, boolean gesture) {
        return new WebResourceRequest() {
            @Override public Uri getUrl() { return Uri.parse(url); }
            @Override public boolean isForMainFrame() { return mainFrame; }
            @Override public boolean isRedirect() { return false; }
            @Override public boolean hasGesture() { return gesture; }
            @Override public String getMethod() { return "GET"; }
            @Override public Map<String, String> getRequestHeaders() { return Collections.emptyMap(); }
        };
    }

    static WebResourceError error(int code) {
        return new WebResourceError() {
            @Override public int getErrorCode() { return code; }
            @Override public CharSequence getDescription() { return "synthetic"; }
        };
    }

    static final class RecordingSslHandler extends SslErrorHandler {
        boolean cancelled;
        boolean proceeded;

        @Override public void cancel() { cancelled = true; }
        @Override public void proceed() { proceeded = true; }
    }

    static final class RecordingPermissionRequest extends PermissionRequest {
        private final String[] resources;
        boolean denied;
        String[] granted;

        RecordingPermissionRequest(String... resources) { this.resources = resources; }

        @Override public Uri getOrigin() { return Uri.parse("https://robot.example-tail.ts.net/"); }
        @Override public String[] getResources() { return resources; }
        @Override public void grant(String[] resources) { granted = resources; }
        @Override public void deny() { denied = true; }
    }

    static final class FakeServiceWorkerSettings extends ServiceWorkerWebSettings {
        int cacheMode;
        boolean contentAccess = true;
        boolean fileAccess = true;
        boolean blockNetworkLoads;

        @Override public void setCacheMode(int mode) { cacheMode = mode; }
        @Override public int getCacheMode() { return cacheMode; }
        @Override public void setAllowContentAccess(boolean allow) { contentAccess = allow; }
        @Override public boolean getAllowContentAccess() { return contentAccess; }
        @Override public void setAllowFileAccess(boolean allow) { fileAccess = allow; }
        @Override public boolean getAllowFileAccess() { return fileAccess; }
        @Override public void setBlockNetworkLoads(boolean flag) { blockNetworkLoads = flag; }
        @Override public boolean getBlockNetworkLoads() { return blockNetworkLoads; }
    }

    @SuppressWarnings("deprecation") // public constructor is deprecated but needed for a fake
    static final class FakeServiceWorkerController extends ServiceWorkerController {
        final FakeServiceWorkerSettings settings = new FakeServiceWorkerSettings();
        ServiceWorkerClient client;

        @Override public ServiceWorkerWebSettings getServiceWorkerWebSettings() { return settings; }
        @Override public void setServiceWorkerClient(ServiceWorkerClient client) { this.client = client; }
    }

    /** Robolectric has no ServiceWorkerController shadow; this one records the configuration. */
    @Implements(ServiceWorkerController.class)
    public static class ShadowServiceWorkerController {
        static FakeServiceWorkerController instance = new FakeServiceWorkerController();

        static void reset() {
            instance = new FakeServiceWorkerController();
        }

        @Implementation
        protected static ServiceWorkerController getInstance() {
            return instance;
        }
    }
}
