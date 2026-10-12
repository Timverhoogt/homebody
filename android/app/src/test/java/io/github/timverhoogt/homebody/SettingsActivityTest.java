package io.github.timverhoogt.homebody;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import android.app.AlertDialog;
import android.view.View;
import android.webkit.CookieManager;
import android.widget.EditText;
import android.widget.TextView;

import io.github.timverhoogt.homebody.policy.DashboardOrigin;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.shadows.ShadowAlertDialog;
import org.robolectric.shadows.ShadowLooper;

@RunWith(RobolectricTestRunner.class)
public class SettingsActivityTest {
    private SettingsActivity activity;

    @Before
    public void setUp() {
        activity = Robolectric.buildActivity(SettingsActivity.class).setup().get();
    }

    @After
    public void tearDown() {
        ShadowAlertDialog.reset();
    }

    private EditText address() {
        return activity.findViewById(R.id.address_input);
    }

    private TextView error() {
        return activity.findViewById(R.id.address_error);
    }

    private void submit(String text) {
        address().setText(text);
        activity.findViewById(R.id.save_button).performClick();
    }

    @Test
    public void showsTheConfiguredOrigin() {
        assertEquals(DashboardOrigin.DEFAULT_URL, address().getText().toString());
        assertEquals(View.GONE, error().getVisibility());
    }

    @Test
    public void rejectedAddressesExplainWhyAndAreNotSaved() {
        String[][] cases = {
            {"http://robot.example.ts.net/", activity.getString(R.string.address_not_https)},
            {"robot.example.ts.net", activity.getString(R.string.address_not_https)},
            {"javascript:alert(1)", activity.getString(R.string.address_not_https)},
            {"https://owner:pw@robot.example.ts.net/", activity.getString(R.string.address_userinfo)},
            {"https://robot.example.ts.net/#robot", activity.getString(R.string.address_origin_only)},
            {"https://robot.example.ts.net/?token=x", activity.getString(R.string.address_origin_only)},
            {"https://robot.example.ts.net/admin", activity.getString(R.string.address_origin_only)},
            {"https://robot.example.ts.net:70000/", activity.getString(R.string.address_port)},
            {"https://robot example/", activity.getString(R.string.address_characters)},
            {"https://0x7f.0.0.1/", activity.getString(R.string.address_invalid)},
            {"", activity.getString(R.string.address_empty)},
        };
        for (String[] c : cases) {
            submit(c[0]);
            assertEquals(c[0], View.VISIBLE, error().getVisibility());
            assertEquals(c[0], c[1], error().getText().toString());
            assertFalse(c[0], activity.isFinishing());
        }
        assertEquals(DashboardOrigin.defaultOrigin(), new DashboardPrefs(activity).origin());
    }

    @Test
    public void validAddressIsSavedCanonicallyAndClosesSettings() {
        submit("  HTTPS://Other-Robot.Example.TS.NET:8443  ");
        assertTrue(activity.isFinishing());
        assertEquals("https://other-robot.example.ts.net:8443/", new DashboardPrefs(activity).origin().startUrl());
    }

    @Test
    public void defaultButtonRestoresTheDefaultAddress() {
        address().setText("https://elsewhere.example/");
        activity.findViewById(R.id.default_button).performClick();
        assertEquals(DashboardOrigin.DEFAULT_URL, address().getText().toString());
    }

    @Test
    public void clearingSiteDataRemovesCookiesAndQueuesCacheClear() {
        CookieManager cookies = CookieManager.getInstance();
        cookies.setCookie(DashboardOrigin.DEFAULT_URL, "homebody_owner=synthetic-test-value; Secure; HttpOnly");
        assertTrue(cookies.hasCookies());

        activity.findViewById(R.id.clear_site_data_button).performClick();
        AlertDialog dialog = ShadowAlertDialog.getLatestAlertDialog();
        dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick();
        ShadowLooper.idleMainLooper();

        assertFalse(cookies.hasCookies());
        assertNull(cookies.getCookie(DashboardOrigin.DEFAULT_URL));
        assertEquals(View.VISIBLE, activity.findViewById(R.id.site_data_status).getVisibility());
        assertTrue(new DashboardPrefs(activity).consumeWebCacheClear());
    }

    @Test
    public void cancellingClearKeepsCookies() {
        CookieManager cookies = CookieManager.getInstance();
        cookies.setCookie(DashboardOrigin.DEFAULT_URL, "homebody_owner=synthetic-test-value; Secure; HttpOnly");
        activity.findViewById(R.id.clear_site_data_button).performClick();
        ShadowAlertDialog.getLatestAlertDialog().getButton(AlertDialog.BUTTON_NEGATIVE).performClick();
        ShadowLooper.idleMainLooper();
        assertTrue(cookies.hasCookies());
        assertFalse(new DashboardPrefs(activity).consumeWebCacheClear());
    }
}
