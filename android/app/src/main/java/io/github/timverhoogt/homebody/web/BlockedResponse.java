package io.github.timverhoogt.homebody.web;

import android.webkit.WebResourceResponse;

import java.io.ByteArrayInputStream;
import java.util.Collections;

/** Empty 403 returned in place of any request outside the configured origin. */
final class BlockedResponse {
    private BlockedResponse() {}

    static WebResourceResponse create() {
        return new WebResourceResponse("text/plain", "utf-8", 403, "Blocked by Homebody",
                Collections.singletonMap("Cache-Control", "no-store"), new ByteArrayInputStream(new byte[0]));
    }
}
