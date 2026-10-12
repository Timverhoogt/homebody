package io.github.timverhoogt.homebody.web;

import android.net.Uri;
import android.os.Message;
import android.view.View;
import android.webkit.ConsoleMessage;
import android.webkit.GeolocationPermissions;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebView;

/**
 * Refuses every page capability request. Robot camera viewing is receive-only WebRTC and needs no
 * phone camera or microphone, so the app requests no such Android permission and grants none of
 * them to the page. Element fullscreen (the camera viewer) is supported.
 */
public final class DashboardChromeClient extends WebChromeClient {
    /** Implemented by the activity that hosts fullscreen views. */
    public interface Host {
        void showCustomView(View view, CustomViewCallback callback);

        void hideCustomView();
    }

    private final Host host;

    public DashboardChromeClient(Host host) {
        this.host = host;
    }

    @Override
    public void onPermissionRequest(PermissionRequest request) {
        request.deny();
    }

    @Override
    public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback callback) {
        callback.invoke(origin, false, false);
    }

    @Override
    public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> filePathCallback,
            FileChooserParams fileChooserParams) {
        filePathCallback.onReceiveValue(null);
        return true;
    }

    @Override
    public boolean onCreateWindow(WebView view, boolean isDialog, boolean isUserGesture, Message resultMsg) {
        return false;
    }

    @Override
    public boolean onConsoleMessage(ConsoleMessage consoleMessage) {
        // Keep page console output (which can include private status) out of logcat.
        return true;
    }

    @Override
    public void onShowCustomView(View view, CustomViewCallback callback) {
        host.showCustomView(view, callback);
    }

    @Override
    public void onHideCustomView() {
        host.hideCustomView();
    }
}
