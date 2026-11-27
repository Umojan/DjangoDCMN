"""The one action form of both panels (DESIGN §8.4).

Every button of the page posts the same form with `op` = `create` | `preview` (the ticked services) or
`<action>:<service>[:<record id>]` for one line. Which options a panel may send is checked afterwards by
`crm.panel.options.check_options`, so a staff-only field posted to the token panel is refused and audited,
never silently dropped.
"""

import re

from django import forms

from ..registry import REGISTRY

MULTI_ACTIONS = ('create', 'preview')
LINE_ACTIONS = ('create', 'preview', 'link', 'create_new', 'create_again', 'dismiss')
SERVICE_CHOICES = [(key, spec.label) for key, spec in REGISTRY.items()]
ZOHO_ID = re.compile(r'^\d{1,32}$')


class PanelActionForm(forms.Form):
    op = forms.CharField(max_length=80)
    services = forms.MultipleChoiceField(choices=SERVICE_CHOICES, required=False)
    record_id = forms.RegexField(ZOHO_ID, required=False)
    stage = forms.CharField(max_length=120, required=False)
    comment = forms.CharField(max_length=500, required=False)
    initials = forms.RegexField(r'^[A-Za-z]{2,4}$', required=False,
                                error_messages={'invalid': 'Initials are 2 to 4 letters.'})
    include_suggested = forms.BooleanField(required=False)
    force_spam = forms.BooleanField(required=False)
    owner_id = forms.RegexField(ZOHO_ID, required=False)

    def clean(self):
        data = super().clean()
        action, _, rest = str(data.get('op') or '').partition(':')
        service, _, record_id = rest.partition(':')
        if action not in LINE_ACTIONS or (not service and action not in MULTI_ACTIONS):
            raise forms.ValidationError('Unknown action.')
        if service and service not in REGISTRY:
            raise forms.ValidationError('Unknown service.')
        if record_id and not ZOHO_ID.match(record_id):
            raise forms.ValidationError('Not a Zoho record id.')
        data['action'] = action
        data['service_keys'] = [service] if service else list(data.get('services') or [])
        data['link_id'] = record_id or data.get('record_id') or ''
        if not data['service_keys']:
            raise forms.ValidationError('Tick at least one service.')
        if action == 'link' and not data['link_id']:
            raise forms.ValidationError('Pick the record to link.')
        if data.get('stage') and len(data['service_keys']) != 1:
            raise forms.ValidationError('A stage can be chosen for one service at a time.')
        return data

    def mode(self) -> str:
        return 'preview' if self.cleaned_data['action'] == 'preview' else 'create'

    def engine_options(self) -> dict:
        """The engine `options` this action asks for (only the set ones)."""
        data = self.cleaned_data
        options: dict = {}
        if data['action'] == 'link':
            options['link_existing'] = data['link_id']
        elif data['action'] in ('create_new', 'create_again'):
            options[data['action']] = True
        for name in ('include_suggested', 'force_spam'):
            if data.get(name):
                options[name] = True
        overrides = {name: data.get(field) for name, field in
                     (('stage', 'stage'), ('comment_append', 'comment'), ('owner_id', 'owner_id')) if data.get(field)}
        if overrides:
            options['overrides'] = overrides
        return options
