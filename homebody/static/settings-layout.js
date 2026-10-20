/* Organise existing controls without duplicating state or changing permissions. */
(() => {
  const panel = document.getElementById('panel-settings');
  if (!panel) return;
  const groups = [
    ['settings-everyday', 'Everyday', 'Presence, suggestions and permissions',
      ['companion-settings', 'household-card', 'everyday-settings-card'], true],
    ['settings-sound', 'Sound', 'Speaker volume', ['speaker-card'], false],
    ['settings-connections', 'Voice & connections', 'Agent setup and voice options',
      ['agent-connection-details', 'agent-setup-card', 'voice-card'], false],
    ['settings-system', 'Software & system', 'Updates, app and host',
      ['updates-card', 'lifecycle-card'], false],
  ];
  const index = panel.querySelector('.settings-index');
  index.replaceChildren();
  for (const [id, title, hint, cards, open] of groups) {
    const group = document.createElement('details');
    group.id = id;
    group.className = 'settings-group';
    group.open = open;
    const summary = document.createElement('summary');
    const name = document.createElement('strong');
    name.textContent = title;
    const description = document.createElement('small');
    description.textContent = hint;
    summary.append(name, description);
    group.append(summary);
    cards.forEach(card => group.append(document.getElementById(card)));
    panel.append(group);
    const link = document.createElement('a');
    link.href = `#${id}`;
    link.textContent = title;
    link.addEventListener('click', event => {
      event.preventDefault();
      group.open = true;
      group.scrollIntoView({block: 'start'});
      summary.focus({preventScroll: true});
    });
    index.append(link);
  }
  // Reports are secondary to editable preferences. Retain the same IDs and polls.
  const diagnostics = document.getElementById('companion-details');
  diagnostics.prepend(document.querySelector('#companion-settings .companion-grid'));
  diagnostics.prepend(document.getElementById('companion-why'));
  const permissions = document.getElementById('everyday-permissions');
  permissions.open = true;
  const technical = document.getElementById('everyday-technical-details');
  technical.append(document.getElementById('everyday-card'));
  technical.append(document.querySelector('.everyday-voice-fact'));
  technical.append(document.getElementById('everyday-voice-note'));
  // One-time setup and migration notices belong with connections, not preferences.
  const connections = document.getElementById('settings-connections');
  connections.append(document.querySelector('.setup-review-entry'));
  connections.append(document.getElementById('assistant-mode-notice'));
})();
