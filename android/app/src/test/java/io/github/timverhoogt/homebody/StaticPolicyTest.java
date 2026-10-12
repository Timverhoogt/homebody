package io.github.timverhoogt.homebody;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Collectors;
import java.util.stream.Stream;
import javax.xml.parsers.DocumentBuilderFactory;

import org.junit.Test;
import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.NodeList;

/** Source-level guards for the manifest, network/backup configuration and forbidden APIs. */
public class StaticPolicyTest {
    private static final String ANDROID = "http://schemas.android.com/apk/res/android";
    private static final Path MAIN = new File(System.getProperty("homebody.sourceDir")).toPath();

    private static Document xml(String relative) throws Exception {
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        factory.setNamespaceAware(true);
        return factory.newDocumentBuilder().parse(MAIN.resolve(relative).toFile());
    }

    private static List<Element> elements(Document document, String tag) {
        NodeList nodes = document.getElementsByTagName(tag);
        List<Element> result = new ArrayList<>();
        for (int i = 0; i < nodes.getLength(); i++) result.add((Element) nodes.item(i));
        return result;
    }

    private static String attr(Element element, String name) {
        return element.getAttributeNS(ANDROID, name);
    }

    @Test
    public void manifestRequestsOnlyInternet() throws Exception {
        Document manifest = xml("AndroidManifest.xml");
        List<String> permissions = elements(manifest, "uses-permission").stream()
                .map(e -> attr(e, "name")).collect(Collectors.toList());
        assertEquals(List.of("android.permission.INTERNET"), permissions);
        assertTrue(elements(manifest, "uses-feature").isEmpty());
        assertTrue(elements(manifest, "permission").isEmpty());
    }

    @Test
    public void manifestDisablesBackupCleartextAndDebugging() throws Exception {
        Element app = elements(xml("AndroidManifest.xml"), "application").get(0);
        assertEquals("false", attr(app, "allowBackup"));
        assertEquals("@xml/data_extraction_rules", attr(app, "dataExtractionRules"));
        assertEquals("@xml/backup_rules", attr(app, "fullBackupContent"));
        assertEquals("false", attr(app, "usesCleartextTraffic"));
        assertEquals("@xml/network_security_config", attr(app, "networkSecurityConfig"));
        assertEquals("", attr(app, "debuggable"));
        assertEquals("", attr(app, "requestLegacyExternalStorage"));
    }

    @Test
    public void onlyTheLauncherActivityIsExported() throws Exception {
        Document manifest = xml("AndroidManifest.xml");
        for (String tag : new String[] {"service", "receiver", "provider", "activity-alias"}) {
            assertTrue(tag, elements(manifest, tag).isEmpty());
        }
        List<Element> activities = elements(manifest, "activity");
        assertEquals(2, activities.size());
        for (Element activity : activities) {
            String name = attr(activity, "name");
            boolean launcher = name.equals(".MainActivity");
            assertEquals(name, launcher ? "true" : "false", attr(activity, "exported"));
        }
        // MAIN/LAUNCHER only: no VIEW, BROWSABLE or deep-link data.
        assertEquals(1, elements(manifest, "intent-filter").size());
        assertTrue(elements(manifest, "data").isEmpty());
        List<String> categories = elements(manifest, "category").stream()
                .map(e -> attr(e, "name")).collect(Collectors.toList());
        assertEquals(List.of("android.intent.category.LAUNCHER"), categories);
    }

    @Test
    public void webViewMetricsAreOptedOut() throws Exception {
        List<String> meta = elements(xml("AndroidManifest.xml"), "meta-data").stream()
                .map(e -> attr(e, "name") + "=" + attr(e, "value")).collect(Collectors.toList());
        assertTrue(meta.contains("android.webkit.WebView.MetricsOptOut=true"));
        assertTrue(meta.contains("android.webkit.WebView.EnableSafeBrowsing=false"));
    }

    @Test
    public void networkSecurityConfigForbidsCleartextAndUserCas() throws Exception {
        Document config = xml("res/xml/network_security_config.xml");
        assertEquals("false", elements(config, "base-config").get(0).getAttribute("cleartextTrafficPermitted"));
        assertTrue(elements(config, "domain-config").isEmpty());
        assertTrue(elements(config, "debug-overrides").isEmpty());
        List<String> sources = elements(config, "certificates").stream()
                .map(e -> e.getAttribute("src")).collect(Collectors.toList());
        assertEquals(List.of("system"), sources);
    }

    @Test
    public void backupRulesExcludeEveryDomain() throws Exception {
        List<String> domains = List.of("root", "file", "database", "sharedpref", "external");
        Document extraction = xml("res/xml/data_extraction_rules.xml");
        for (String section : new String[] {"cloud-backup", "device-transfer"}) {
            Element element = elements(extraction, section).get(0);
            assertTrue(section, element.getElementsByTagName("include").getLength() == 0);
            assertEquals(section, domains, excludedDomains(element));
        }
        Element full = xml("res/xml/backup_rules.xml").getDocumentElement();
        assertEquals(0, full.getElementsByTagName("include").getLength());
        assertEquals(domains, excludedDomains(full));
    }

    private static List<String> excludedDomains(Element parent) {
        NodeList excludes = parent.getElementsByTagName("exclude");
        List<String> result = new ArrayList<>();
        for (int i = 0; i < excludes.getLength(); i++) {
            Element exclude = (Element) excludes.item(i);
            assertEquals(".", exclude.getAttribute("path"));
            result.add(exclude.getAttribute("domain"));
        }
        return result;
    }

    @Test
    public void sourceHasNoBridgeLoggingOrTlsBypass() throws IOException {
        String[] forbidden = {
            "addJavascriptInterface", "setWebContentsDebuggingEnabled(true)", ".proceed(",
            "android.util.Log", "Log.d(", "Log.i(", "Log.v(", "Log.w(", "Log.e(", "System.out", "System.err",
            "printStackTrace", "setAllowFileAccess(true)", "setAllowContentAccess(true)",
            "setAllowUniversalAccessFromFileURLs(true)", "setAllowFileAccessFromFileURLs(true)",
            "MIXED_CONTENT_ALWAYS_ALLOW", "MIXED_CONTENT_COMPATIBILITY_MODE", "request.grant(",
            "TrustManager", "HostnameVerifier", "evaluateJavascript", "MODE_WORLD_READABLE",
        };
        List<String> violations = new ArrayList<>();
        try (Stream<Path> files = Files.walk(MAIN.resolve("java"))) {
            for (Path file : (Iterable<Path>) files.filter(p -> p.toString().endsWith(".java"))::iterator) {
                String source = new String(Files.readAllBytes(file), StandardCharsets.UTF_8);
                for (String pattern : forbidden) {
                    if (source.contains(pattern)) violations.add(MAIN.relativize(file) + ": " + pattern);
                }
            }
        }
        assertTrue(violations.toString(), violations.isEmpty());
    }

    @Test
    public void policyPackageHasNoAndroidDependencies() throws IOException {
        try (Stream<Path> files = Files.walk(MAIN.resolve("java/io/github/timverhoogt/homebody/policy"))) {
            for (Path file : (Iterable<Path>) files.filter(p -> p.toString().endsWith(".java"))::iterator) {
                String source = new String(Files.readAllBytes(file), StandardCharsets.UTF_8);
                assertFalse(file.toString(), source.contains("import android."));
            }
        }
    }
}
