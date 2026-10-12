package io.github.timverhoogt.homebody.policy;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class SessionBackAndErrorPolicyTest {
    private static final String ROOT = "https://robot.example-tail.ts.net/";
    private static final DashboardOrigin ORIGIN = DashboardOrigin.parse(ROOT).origin();

    @Test
    public void sessionRestoresOnlyASimpleTabToken() {
        DashboardSession session = new DashboardSession(ORIGIN);
        assertEquals(ROOT, session.restoreUrl());
        session.onVisited(ROOT + "#robot");
        assertEquals(ROOT + "#robot", session.restoreUrl());
        session.onVisited("https://evil.example/#settings"); // ignored: other origin
        assertEquals(ROOT + "#robot", session.restoreUrl());
        session.onVisited(ROOT + "?code=123456#agent"); // query is never kept
        assertEquals(ROOT + "#agent", session.restoreUrl());
        for (String fragment : new String[] {"robot?x=1", "<script>", "Robot", "1robot", "a/b", "", "x".repeat(40)}) {
            session.onVisited(ROOT + "#" + fragment);
            assertEquals(fragment, ROOT, session.restoreUrl());
        }
        session.onVisited(ROOT + "#kids");
        session.onVisited(ROOT);
        assertEquals(ROOT, session.restoreUrl());
    }

    @Test
    public void backGoesBackOnlyWithinTheDashboard() {
        assertEquals(BackPolicy.Action.GO_BACK, BackPolicy.onBack(ORIGIN, false, true, ROOT + "#dashboard"));
        assertEquals(BackPolicy.Action.CONFIRM_EXIT, BackPolicy.onBack(ORIGIN, false, false, null));
        assertEquals(BackPolicy.Action.CONFIRM_EXIT, BackPolicy.onBack(ORIGIN, false, true, "about:blank"));
        assertEquals(BackPolicy.Action.CONFIRM_EXIT, BackPolicy.onBack(ORIGIN, false, true, "https://evil.example/"));
        assertEquals(BackPolicy.Action.CONFIRM_EXIT, BackPolicy.onBack(ORIGIN, true, true, ROOT));
        assertEquals(BackPolicy.Action.CONFIRM_EXIT, BackPolicy.onBack(ORIGIN, false, true, null));
    }

    @Test
    public void webViewErrorsMapToGuidance() {
        assertEquals(ConnectionProblem.HOST_NOT_FOUND, ConnectionProblem.fromWebViewError(-2));
        assertEquals(ConnectionProblem.CANNOT_CONNECT, ConnectionProblem.fromWebViewError(-6));
        assertEquals(ConnectionProblem.CANNOT_CONNECT, ConnectionProblem.fromWebViewError(-7));
        assertEquals(ConnectionProblem.CANNOT_CONNECT, ConnectionProblem.fromWebViewError(-8));
        assertEquals(ConnectionProblem.TLS_REFUSED, ConnectionProblem.fromWebViewError(-11));
        assertEquals(ConnectionProblem.OTHER, ConnectionProblem.fromWebViewError(-1));
        assertEquals(ConnectionProblem.SERVER_UNAVAILABLE, ConnectionProblem.fromHttpStatus(502));
        assertEquals(ConnectionProblem.SERVER_UNAVAILABLE, ConnectionProblem.fromHttpStatus(503));
        assertNull(ConnectionProblem.fromHttpStatus(200));
        assertNull(ConnectionProblem.fromHttpStatus(401)); // the dashboard handles Owner pairing itself
        assertNull(ConnectionProblem.fromHttpStatus(404));
    }

    @Test
    public void tailscaleHintOnlyForNetworkProblemsOnTailnetHosts() {
        DashboardOrigin lan = DashboardOrigin.parse("https://192.168.1.20/").origin();
        assertTrue(ConnectionProblem.HOST_NOT_FOUND.showTailscaleHint(ORIGIN));
        assertTrue(ConnectionProblem.CANNOT_CONNECT.showTailscaleHint(ORIGIN));
        assertFalse(ConnectionProblem.TLS_REFUSED.showTailscaleHint(ORIGIN));
        assertFalse(ConnectionProblem.SERVER_UNAVAILABLE.showTailscaleHint(ORIGIN));
        assertFalse(ConnectionProblem.BLOCKED_NAVIGATION.showTailscaleHint(ORIGIN));
        assertFalse(ConnectionProblem.HOST_NOT_FOUND.showTailscaleHint(lan));
    }
}
