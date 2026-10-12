package io.github.timverhoogt.homebody;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Bundle;
import android.view.View;
import android.widget.EditText;
import android.widget.TextView;

import io.github.timverhoogt.homebody.policy.DashboardOrigin;

/**
 * Lets the owner change the dashboard origin and clear this phone's sign-in data. While this
 * screen is open the dashboard activity is stopped, so its WebView has been destroyed.
 */
public class SettingsActivity extends Activity {
    private DashboardPrefs prefs;
    private EditText address;
    private TextView addressError;
    private TextView siteDataStatus;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_settings);
        prefs = new DashboardPrefs(this);
        address = findViewById(R.id.address_input);
        addressError = findViewById(R.id.address_error);
        siteDataStatus = findViewById(R.id.site_data_status);
        if (savedInstanceState == null) address.setText(prefs.origin().startUrl());
        findViewById(R.id.save_button).setOnClickListener(v -> save());
        findViewById(R.id.default_button).setOnClickListener(v -> {
            address.setText(DashboardOrigin.DEFAULT_URL);
            addressError.setVisibility(View.GONE);
        });
        findViewById(R.id.clear_site_data_button).setOnClickListener(v -> confirmClearSiteData());
    }

    void save() {
        DashboardOrigin.ParseResult result = DashboardOrigin.parse(address.getText().toString());
        if (!result.isValid()) {
            addressError.setText(ErrorText.forAddressProblem(result.problem()));
            addressError.setVisibility(View.VISIBLE);
            return;
        }
        prefs.setOrigin(result.origin());
        finish();
    }

    private void confirmClearSiteData() {
        new AlertDialog.Builder(this)
                .setTitle(R.string.clear_site_data_title)
                .setMessage(R.string.clear_site_data_message)
                .setPositiveButton(R.string.clear_site_data_confirm, (dialog, which) ->
                        SiteData.clearAll(this, () -> {
                            siteDataStatus.setText(R.string.clear_site_data_done);
                            siteDataStatus.setVisibility(View.VISIBLE);
                        }))
                .setNegativeButton(android.R.string.cancel, null)
                .show();
    }
}
