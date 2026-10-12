package io.github.timverhoogt.homebody;

import io.github.timverhoogt.homebody.policy.ConnectionProblem;
import io.github.timverhoogt.homebody.policy.DashboardOrigin;

/** String resources for connection problems and rejected server addresses. */
final class ErrorText {
    final int title;
    final int message;

    private ErrorText(int title, int message) {
        this.title = title;
        this.message = message;
    }

    static ErrorText forProblem(ConnectionProblem problem) {
        switch (problem) {
            case HOST_NOT_FOUND:
                return new ErrorText(R.string.error_host_title, R.string.error_host_message);
            case CANNOT_CONNECT:
                return new ErrorText(R.string.error_connect_title, R.string.error_connect_message);
            case SERVER_UNAVAILABLE:
                return new ErrorText(R.string.error_server_title, R.string.error_server_message);
            case TLS_REFUSED:
                return new ErrorText(R.string.error_tls_title, R.string.error_tls_message);
            case BLOCKED_NAVIGATION:
                return new ErrorText(R.string.error_blocked_title, R.string.error_blocked_message);
            case PAGE_CRASHED:
                return new ErrorText(R.string.error_crash_title, R.string.error_crash_message);
            default:
                return new ErrorText(R.string.error_other_title, R.string.error_other_message);
        }
    }

    static int forAddressProblem(DashboardOrigin.Problem problem) {
        switch (problem) {
            case EMPTY:
                return R.string.address_empty;
            case NOT_HTTPS:
                return R.string.address_not_https;
            case USERINFO:
                return R.string.address_userinfo;
            case FRAGMENT:
            case QUERY:
            case PATH:
                return R.string.address_origin_only;
            case INVALID_PORT:
                return R.string.address_port;
            case INVALID_CHARACTERS:
                return R.string.address_characters;
            default:
                return R.string.address_invalid;
        }
    }
}
