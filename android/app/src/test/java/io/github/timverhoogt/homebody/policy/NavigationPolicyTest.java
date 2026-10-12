package io.github.timverhoogt.homebody.policy;

import static io.github.timverhoogt.homebody.policy.NavigationPolicy.Decision.BLOCK;
import static io.github.timverhoogt.homebody.policy.NavigationPolicy.Decision.LOAD_IN_APP;
import static io.github.timverhoogt.homebody.policy.NavigationPolicy.Decision.OPEN_IN_BROWSER;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class NavigationPolicyTest {
    private static final String ROOT = "https://robot.example-tail.ts.net/";
    private final NavigationPolicy policy = new NavigationPolicy(DashboardOrigin.parse(ROOT).origin());

    @Test
    public void dashboardNavigationStaysInApp() {
        assertEquals(LOAD_IN_APP, policy.forNavigation(ROOT, true, false));
        assertEquals(LOAD_IN_APP, policy.forNavigation(ROOT + "#robot", true, true));
        assertEquals(LOAD_IN_APP, policy.forNavigation(ROOT + "?view=agent", true, false));
        assertEquals(LOAD_IN_APP, policy.forNavigation(ROOT + "static/page.html", false, false));
    }

    @Test
    public void externalWebLinksOpenInBrowserOnlyAfterAGesture() {
        assertEquals(OPEN_IN_BROWSER, policy.forNavigation("https://github.com/Timverhoogt", true, true));
        assertEquals(OPEN_IN_BROWSER, policy.forNavigation("http://example.com/", true, true));
        assertEquals(OPEN_IN_BROWSER, policy.forNavigation("http://robot.example-tail.ts.net/", true, true));
        assertEquals(BLOCK, policy.forNavigation("https://github.com/Timverhoogt", true, false));
        assertEquals(BLOCK, policy.forNavigation("https://robot.example-tail.ts.net:8443/", true, false));
        assertEquals(BLOCK, policy.forNavigation("https://robot.example-tail.ts.net.evil.example/", true, false));
    }

    @Test
    public void hostileSchemesAreBlockedEvenWithAGesture() {
        String[] urls = {
            "javascript:alert(document.cookie)", "JavaScript:alert(1)", "file:///data/data/x/shared_prefs/a.xml",
            "content://com.android.contacts/contacts", "intent://scan/#Intent;scheme=zxing;package=x;end",
            "intent:#Intent;action=android.settings.SETTINGS;end", "android-app://com.example/https/x",
            "data:text/html,<script>alert(1)</script>", "about:blank", "about:srcdoc", "chrome://settings",
            "market://details?id=x", "mailto:a@example.com", "tel:123", "sms:123", "ftp://example.com/",
            "blob:https://robot.example-tail.ts.net/uuid", "ws://robot.example-tail.ts.net/", "https:", "http:/x",
            "", "not a url",
        };
        for (String url : urls) assertEquals(url, BLOCK, policy.forNavigation(url, true, true));
        assertEquals(BLOCK, policy.forNavigation(null, true, true));
    }

    @Test
    public void framesMayNotLeaveTheOrigin() {
        assertEquals(BLOCK, policy.forNavigation("https://evil.example/", false, true));
        assertEquals(BLOCK, policy.forNavigation("http://robot.example-tail.ts.net/", false, true));
        assertEquals(BLOCK, policy.forNavigation("javascript:alert(1)", false, true));
        assertEquals(BLOCK, policy.forNavigation("data:text/html,x", false, true));
        assertEquals(LOAD_IN_APP, policy.forNavigation("about:blank", false, false));
        assertEquals(LOAD_IN_APP, policy.forNavigation("about:srcdoc", false, false));
    }

    @Test
    public void subresourcesAreLimitedToTheOrigin() {
        assertTrue(policy.allowsSubresource(ROOT + "static/main.js?v=78"));
        assertTrue(policy.allowsSubresource(ROOT + "api/status"));
        assertTrue(policy.allowsSubresource("data:image/png;base64,AAAA"));
        assertTrue(policy.allowsSubresource("blob:https://robot.example-tail.ts.net/2f1c"));
        assertFalse(policy.allowsSubresource("blob:https://evil.example/2f1c"));
        assertFalse(policy.allowsSubresource("blob:null/2f1c"));
        assertFalse(policy.allowsSubresource("https://fonts.googleapis.com/css"));
        assertFalse(policy.allowsSubresource("https://robot.example-tail.ts.net:8443/api/camera/signaling"));
        assertFalse(policy.allowsSubresource("http://robot.example-tail.ts.net/static/main.js"));
        assertFalse(policy.allowsSubresource("file:///android_asset/x.js"));
        assertFalse(policy.allowsSubresource("content://media/external/images/1"));
        assertFalse(policy.allowsSubresource(null));
    }

    @Test
    public void onlyTheDashboardOrBlankMayBeTheTopDocument() {
        assertTrue(policy.allowsMainFrameDocument(ROOT));
        assertTrue(policy.allowsMainFrameDocument("about:blank"));
        assertFalse(policy.allowsMainFrameDocument("about:srcdoc"));
        assertFalse(policy.allowsMainFrameDocument("data:text/html,x"));
        assertFalse(policy.allowsMainFrameDocument("https://evil.example/"));
        assertFalse(policy.allowsMainFrameDocument(null));
    }
}
