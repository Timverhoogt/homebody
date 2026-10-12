package io.github.timverhoogt.homebody.policy;

import java.util.Locale;
import java.util.regex.Pattern;

/**
 * The single HTTPS origin (scheme, host, port) the app is allowed to display.
 *
 * <p>Parsing is deliberately strict: owners type an origin, not an arbitrary URL. Anything that is
 * ambiguous between URL parsers (userinfo, backslashes, non-canonical IPv4 forms, trailing dots,
 * non-ASCII hosts) is rejected rather than normalised. Comparisons use the exact canonical
 * authority; there is no prefix or suffix matching.
 *
 * <p>This class has no Android dependencies so it can be unit tested on a plain JVM.
 */
public final class DashboardOrigin {
    public static final String DEFAULT_URL = "https://timverhoogtnano.tail51823f.ts.net/";
    static final int MAX_INPUT_LENGTH = 2048;

    private static final Pattern LABEL = Pattern.compile("[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?");
    private static final Pattern DIGITS = Pattern.compile("[0-9]+");
    // WHATWG URL parsers treat a trailing numeric or 0x-hex label as an IPv4 address.
    private static final Pattern NUMERIC_LABEL = Pattern.compile("[0-9]+|0[xX][0-9a-fA-F]*");

    /** Why an owner-entered address was refused. */
    public enum Problem {
        EMPTY,
        TOO_LONG,
        INVALID_CHARACTERS,
        MALFORMED,
        NOT_HTTPS,
        USERINFO,
        FRAGMENT,
        QUERY,
        PATH,
        INVALID_HOST,
        INVALID_PORT,
    }

    /** Either a valid origin or the reason it was rejected. */
    public static final class ParseResult {
        private final DashboardOrigin origin;
        private final Problem problem;

        private ParseResult(DashboardOrigin origin, Problem problem) {
            this.origin = origin;
            this.problem = problem;
        }

        public boolean isValid() {
            return origin != null;
        }

        public DashboardOrigin origin() {
            if (origin == null) throw new IllegalStateException("Invalid origin: " + problem);
            return origin;
        }

        public Problem problem() {
            return problem;
        }
    }

    private final String host;
    private final int port; // -1 for the HTTPS default port 443
    private final String authority;

    private DashboardOrigin(String host, int port) {
        this.host = host;
        this.port = port;
        this.authority = port == -1 ? host : host + ":" + port;
    }

    public static DashboardOrigin defaultOrigin() {
        return parse(DEFAULT_URL).origin();
    }

    public static ParseResult parse(String input) {
        if (input == null || input.trim().isEmpty()) return fail(Problem.EMPTY);
        String text = input.trim();
        if (text.length() > MAX_INPUT_LENGTH) return fail(Problem.TOO_LONG);
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            // Rejects whitespace, controls, non-ASCII (enter IDNs as punycode) and backslashes,
            // which browsers and java.net.URI interpret differently.
            if (c <= 0x20 || c >= 0x7f || c == '\\') return fail(Problem.INVALID_CHARACTERS);
        }
        int colon = text.indexOf(':');
        if (colon <= 0 || !text.substring(0, colon).equalsIgnoreCase("https")) {
            return fail(Problem.NOT_HTTPS);
        }
        if (!text.startsWith("//", colon + 1)) return fail(Problem.MALFORMED);
        if (text.indexOf('#') >= 0) return fail(Problem.FRAGMENT);
        if (text.indexOf('?') >= 0) return fail(Problem.QUERY);
        int authorityStart = colon + 3;
        int pathStart = text.indexOf('/', authorityStart);
        String rawAuthority = pathStart < 0 ? text.substring(authorityStart) : text.substring(authorityStart, pathStart);
        if (rawAuthority.indexOf('@') >= 0) return fail(Problem.USERINFO);
        if (pathStart >= 0 && !text.substring(pathStart).equals("/")) return fail(Problem.PATH);
        // IPv6 literals are refused: exact comparison would need Chromium's canonical (RFC 5952)
        // spelling. Use the MagicDNS name or an IPv4 address instead.
        if (rawAuthority.isEmpty() || rawAuthority.startsWith("[")) return fail(Problem.INVALID_HOST);

        int portColon = rawAuthority.indexOf(':');
        int port = -1;
        if (portColon >= 0) {
            String portText = rawAuthority.substring(portColon + 1);
            if (!DIGITS.matcher(portText).matches() || portText.length() > 5) return fail(Problem.INVALID_PORT);
            port = Integer.parseInt(portText);
            if (port < 1 || port > 65535) return fail(Problem.INVALID_PORT);
            if (port == 443) port = -1;
        }
        String host = (portColon < 0 ? rawAuthority : rawAuthority.substring(0, portColon)).toLowerCase(Locale.ROOT);
        if (!isValidHost(host)) return fail(Problem.INVALID_HOST);
        return new ParseResult(new DashboardOrigin(host, port), null);
    }

    private static ParseResult fail(Problem problem) {
        return new ParseResult(null, problem);
    }

    /** Lower-case LDH host names, or canonical dotted-decimal IPv4. */
    static boolean isValidHost(String host) {
        if (host.isEmpty() || host.length() > 253 || host.endsWith(".")) return false;
        String[] labels = host.split("\\.", -1);
        String last = labels[labels.length - 1];
        if (NUMERIC_LABEL.matcher(last).matches()) return isCanonicalIpv4(labels);
        for (String label : labels) {
            if (!LABEL.matcher(label).matches()) return false;
        }
        return true;
    }

    private static boolean isCanonicalIpv4(String[] labels) {
        if (labels.length != 4) return false;
        for (String label : labels) {
            if (!DIGITS.matcher(label).matches() || label.length() > 3) return false;
            if (label.length() > 1 && label.charAt(0) == '0') return false; // no octal-looking forms
            if (Integer.parseInt(label) > 255) return false;
        }
        return true;
    }

    /** "https://host[:port]" with no trailing slash. */
    public String serialize() {
        return "https://" + authority;
    }

    /** The dashboard root, e.g. "https://host/". */
    public String startUrl() {
        return serialize() + "/";
    }

    public String host() {
        return host;
    }

    /**
     * True only if {@code url} is an https URL whose authority is exactly this origin's authority.
     * WebView reports Chromium-canonicalised URLs, so a plain extraction is exact; anything that
     * does not look canonical (userinfo, odd characters, other schemes) fails closed.
     */
    public boolean isSameOrigin(String url) {
        String candidate = authorityOf(url, "https");
        return candidate != null && candidate.equals(authority);
    }

    /**
     * Extracts the lower-cased authority of a hierarchical {@code scheme://authority/...} URL, or
     * null if the URL has a different scheme or no authority. ":443" is dropped for https.
     */
    static String authorityOf(String url, String scheme) {
        if (url == null) return null;
        int colon = url.indexOf(':');
        if (colon <= 0 || !url.substring(0, colon).equalsIgnoreCase(scheme)) return null;
        if (!url.startsWith("//", colon + 1)) return null;
        int start = colon + 3;
        int end = url.length();
        for (int i = start; i < url.length(); i++) {
            char c = url.charAt(i);
            if (c == '/' || c == '?' || c == '#') {
                end = i;
                break;
            }
        }
        String value = url.substring(start, end).toLowerCase(Locale.ROOT);
        if (value.isEmpty()) return null;
        if ("https".equals(scheme) && value.endsWith(":443")) value = value.substring(0, value.length() - 4);
        return value;
    }

    /**
     * A display hint only: hosts under ".ts.net" or in Tailscale's 100.64.0.0/10 range normally
     * need the Tailscale app connected. Never used for any trust decision.
     */
    public boolean isLikelyTailnet() {
        if (host.endsWith(".ts.net")) return true;
        String[] labels = host.split("\\.");
        if (labels.length != 4 || !isCanonicalIpv4(labels)) return false;
        int first = Integer.parseInt(labels[0]);
        int second = Integer.parseInt(labels[1]);
        return first == 100 && second >= 64 && second <= 127;
    }

    @Override
    public boolean equals(Object other) {
        return other instanceof DashboardOrigin && ((DashboardOrigin) other).authority.equals(authority);
    }

    @Override
    public int hashCode() {
        return authority.hashCode();
    }

    @Override
    public String toString() {
        return serialize();
    }
}
