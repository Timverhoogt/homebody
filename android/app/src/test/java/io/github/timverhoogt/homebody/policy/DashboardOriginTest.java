package io.github.timverhoogt.homebody.policy;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class DashboardOriginTest {
    private static final String HOST = "robot.example-tail.ts.net";
    private static final DashboardOrigin ORIGIN = DashboardOrigin.parse("https://" + HOST + "/").origin();

    private static void assertProblem(DashboardOrigin.Problem expected, String input) {
        DashboardOrigin.ParseResult result = DashboardOrigin.parse(input);
        assertFalse("accepted " + input, result.isValid());
        assertEquals(input, expected, result.problem());
    }

    private static void assertCanonical(String expectedStartUrl, String input) {
        DashboardOrigin.ParseResult result = DashboardOrigin.parse(input);
        assertTrue("rejected " + input + ": " + result.problem(), result.isValid());
        assertEquals(expectedStartUrl, result.origin().startUrl());
    }

    @Test
    public void defaultOriginIsTheReferenceTailnetDashboard() {
        DashboardOrigin origin = DashboardOrigin.defaultOrigin();
        assertEquals("https://timverhoogtnano.tail51823f.ts.net/", origin.startUrl());
        assertEquals("https://timverhoogtnano.tail51823f.ts.net", origin.serialize());
        assertTrue(origin.isLikelyTailnet());
    }

    @Test
    public void acceptsAndCanonicalisesOrigins() {
        assertCanonical("https://" + HOST + "/", "https://" + HOST);
        assertCanonical("https://" + HOST + "/", "  HTTPS://Robot.Example-Tail.TS.NET/  ");
        assertCanonical("https://" + HOST + "/", "https://" + HOST + ":443/");
        assertCanonical("https://" + HOST + ":8443/", "https://" + HOST + ":8443");
        assertCanonical("https://100.101.102.103/", "https://100.101.102.103/");
        assertCanonical("https://192.168.1.20:8042/", "https://192.168.1.20:8042/");
        assertCanonical("https://xn--bcher-kva.example/", "https://xn--bcher-kva.example/");
        assertCanonical("https://localhost/", "https://localhost");
    }

    @Test
    public void rejectsEmptyAndOversizedInput() {
        assertProblem(DashboardOrigin.Problem.EMPTY, null);
        assertProblem(DashboardOrigin.Problem.EMPTY, "   ");
        StringBuilder longHost = new StringBuilder("https://");
        while (longHost.length() <= DashboardOrigin.MAX_INPUT_LENGTH) longHost.append("a.");
        assertProblem(DashboardOrigin.Problem.TOO_LONG, longHost.toString());
    }

    @Test
    public void rejectsEverySchemeButHttps() {
        String[] inputs = {
            "http://" + HOST + "/", "ws://" + HOST + "/", "wss://" + HOST + "/", "ftp://" + HOST + "/",
            "javascript:alert(1)", "javascript://" + HOST + "/%0Aalert(1)", "file:///sdcard/x.html",
            "content://com.example/x", "intent://" + HOST + "/#Intent;scheme=https;end", "data:text/html,hi",
            "about:blank", HOST, "//" + HOST + "/", "httpsx://" + HOST + "/", "https-" + HOST,
        };
        for (String input : inputs) assertProblem(DashboardOrigin.Problem.NOT_HTTPS, input);
    }

    @Test
    public void rejectsUserinfo() {
        assertProblem(DashboardOrigin.Problem.USERINFO, "https://owner@" + HOST + "/");
        assertProblem(DashboardOrigin.Problem.USERINFO, "https://owner:secret@" + HOST + "/");
        assertProblem(DashboardOrigin.Problem.USERINFO, "https://@" + HOST + "/");
        assertProblem(DashboardOrigin.Problem.USERINFO, "https://" + HOST + "@evil.example/");
        assertProblem(DashboardOrigin.Problem.USERINFO, "https://evil.example@" + HOST);
    }

    @Test
    public void rejectsFragmentsQueriesAndPaths() {
        assertProblem(DashboardOrigin.Problem.FRAGMENT, "https://" + HOST + "/#");
        assertProblem(DashboardOrigin.Problem.FRAGMENT, "https://" + HOST + "/#robot");
        assertProblem(DashboardOrigin.Problem.FRAGMENT, "https://evil.example#@" + HOST + "/");
        assertProblem(DashboardOrigin.Problem.QUERY, "https://" + HOST + "/?");
        assertProblem(DashboardOrigin.Problem.QUERY, "https://" + HOST + "/?token=abc");
        assertProblem(DashboardOrigin.Problem.QUERY, "https://evil.example?@" + HOST + "/");
        assertProblem(DashboardOrigin.Problem.PATH, "https://" + HOST + "/settings");
        assertProblem(DashboardOrigin.Problem.PATH, "https://" + HOST + "//");
        assertProblem(DashboardOrigin.Problem.PATH, "https://" + HOST + "/./");
        assertProblem(DashboardOrigin.Problem.PATH, "https://" + HOST + "/%2e/");
    }

    @Test
    public void rejectsAmbiguousCharacters() {
        assertProblem(DashboardOrigin.Problem.INVALID_CHARACTERS, "https://" + HOST + "\\@evil.example/");
        assertProblem(DashboardOrigin.Problem.INVALID_CHARACTERS, "https://evil.example\\." + HOST + "/");
        assertProblem(DashboardOrigin.Problem.INVALID_CHARACTERS, "https://robot .ts.net/");
        assertProblem(DashboardOrigin.Problem.INVALID_CHARACTERS, "https://robot\t.ts.net/");
        assertProblem(DashboardOrigin.Problem.INVALID_CHARACTERS, "https://robot.ts.net\n/");
        assertCanonical("https://robot.ts.net/", "https://robot.ts.net/\n"); // pasted trailing newline
        assertProblem(DashboardOrigin.Problem.INVALID_CHARACTERS, "https://bücher.example/");
        assertProblem(DashboardOrigin.Problem.INVALID_CHARACTERS, "https://robot\u3002ts.net/");
        assertProblem(DashboardOrigin.Problem.INVALID_CHARACTERS, "https://robot.ts.net\u0000/");
    }

    @Test
    public void rejectsInvalidOrAmbiguousHosts() {
        String[] inputs = {
            "https:///", "https://", "https://robot..ts.net/", "https://robot.ts.net./", "https://.robot.ts.net/",
            "https://-robot.ts.net/", "https://robot-.ts.net/", "https://ro_bot.ts.net/", "https://robot%2ets.net/",
            "https://" + "a".repeat(64) + ".ts.net/", "https://[fd7a:115c:a1e0::1]/", "https://[::1]:8443/",
            // WHATWG IPv4 forms that Chromium would reinterpret.
            "https://0x7f.0.0.1/", "https://0177.0.0.1/", "https://127.1/", "https://2130706433/",
            "https://1.2.3/", "https://1.2.3.4.5/", "https://256.1.1.1/", "https://010.0.0.1/",
            "https://example.123/", "https://example.0x10/", "https://robot.ts.net.0/",
        };
        for (String input : inputs) {
            DashboardOrigin.ParseResult result = DashboardOrigin.parse(input);
            assertFalse("accepted " + input, result.isValid());
        }
        assertProblem(DashboardOrigin.Problem.INVALID_HOST, "https://0x7f.0.0.1/");
        assertProblem(DashboardOrigin.Problem.INVALID_HOST, "https://ro_bot.ts.net/");
    }

    @Test
    public void rejectsInvalidPorts() {
        assertProblem(DashboardOrigin.Problem.INVALID_PORT, "https://" + HOST + ":0/");
        assertProblem(DashboardOrigin.Problem.INVALID_PORT, "https://" + HOST + ":65536/");
        assertProblem(DashboardOrigin.Problem.INVALID_PORT, "https://" + HOST + ":99999999999/");
        assertProblem(DashboardOrigin.Problem.INVALID_PORT, "https://" + HOST + ":abc/");
        assertProblem(DashboardOrigin.Problem.INVALID_PORT, "https://" + HOST + ":/");
        assertProblem(DashboardOrigin.Problem.INVALID_PORT, "https://" + HOST + ":-1/");
    }

    @Test
    public void sameOriginRequiresExactSchemeHostAndPort() {
        assertTrue(ORIGIN.isSameOrigin("https://" + HOST + "/"));
        assertTrue(ORIGIN.isSameOrigin("https://" + HOST));
        assertTrue(ORIGIN.isSameOrigin("https://" + HOST + "/api/status?x=1#robot"));
        assertTrue(ORIGIN.isSameOrigin("HTTPS://ROBOT.EXAMPLE-TAIL.TS.NET/"));
        assertTrue(ORIGIN.isSameOrigin("https://" + HOST + ":443/"));
        assertTrue(ORIGIN.isSameOrigin("https://" + HOST + "?q"));

        String[] foreign = {
            null, "", "http://" + HOST + "/", "wss://" + HOST + "/", "https://" + HOST + ":8443/",
            "https://" + HOST + ".evil.example/", "https://evil" + HOST + "/", "https://evil.example/" + HOST,
            "https://x." + HOST + "/", "https://" + HOST.substring(1) + "/", "https://user@" + HOST + "/",
            "https://" + HOST + "@evil.example/", "https://evil.example/?next=https://" + HOST + "/",
            "https://evil.example/#https://" + HOST + "/", "https://evil.example\\@" + HOST + "/",
            "https:" + HOST + "/", "https:/" + HOST + "/", "//" + HOST + "/", "javascript://" + HOST + "/%0Aalert(1)",
            "blob:https://" + HOST + "/uuid", "about:blank", "data:text/html,https://" + HOST,
            "intent://" + HOST + "/#Intent;scheme=https;end", "file://" + HOST + "/etc/passwd",
            "https://" + HOST + ":443@evil.example/", "https://" + HOST + "./",
        };
        for (String url : foreign) assertFalse("matched " + url, ORIGIN.isSameOrigin(url));
    }

    @Test
    public void portIsPartOfTheOrigin() {
        DashboardOrigin custom = DashboardOrigin.parse("https://" + HOST + ":8443").origin();
        assertTrue(custom.isSameOrigin("https://" + HOST + ":8443/api"));
        assertFalse(custom.isSameOrigin("https://" + HOST + "/api"));
        assertFalse(custom.isSameOrigin("https://" + HOST + ":84430/api"));
        assertFalse(custom.isSameOrigin("https://" + HOST + ":844/api"));
        assertFalse(custom.equals(ORIGIN));
        assertEquals(ORIGIN, DashboardOrigin.parse("https://ROBOT.example-tail.ts.net:443").origin());
    }

    @Test
    public void tailnetHintIsDisplayOnly() {
        assertTrue(ORIGIN.isLikelyTailnet());
        assertTrue(DashboardOrigin.parse("https://100.64.0.1/").origin().isLikelyTailnet());
        assertTrue(DashboardOrigin.parse("https://100.127.255.255/").origin().isLikelyTailnet());
        assertFalse(DashboardOrigin.parse("https://100.63.255.255/").origin().isLikelyTailnet());
        assertFalse(DashboardOrigin.parse("https://100.128.0.0/").origin().isLikelyTailnet());
        assertFalse(DashboardOrigin.parse("https://evilts.net/").origin().isLikelyTailnet());
        assertFalse(DashboardOrigin.parse("https://ts.net.example/").origin().isLikelyTailnet());
        assertFalse(DashboardOrigin.parse("https://192.168.1.20/").origin().isLikelyTailnet());
    }
}
