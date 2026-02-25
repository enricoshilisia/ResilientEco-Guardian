"""
organizations/routing.py

Central routing registry for org-type dashboards.
Add a new org type here and create its view + template — that's all.

ORG_TYPE_MAP structure:
    '<model_org_type>': {
        'dashboard_name':  named URL to redirect to on login / "Go to Dashboard"
        'wizard_type':     the wizard's org_type string (from RegisterOrganizationView.ORG_TYPE_MAP values)
        'label':           human-readable name
        'icon':            emoji
        'color':           brand accent color (hex)
        'template_prefix': folder under organizations/templates/organizations/
    }
"""

ORG_DASHBOARD_REGISTRY = {
    # ── Agricultural ─────────────────────────────────────────────
    'enterprise_agriculture': {  # wizard type → 'agriculture'
        'dashboard_name': 'org_dashboard_agriculture',
        'label': 'Agricultural Organization',
        'icon': '🌾',
        'color': '#16a34a',
        'template_prefix': 'agricultural',
    },

    # ── Disaster Relief / NGO ─────────────────────────────────────
    'ngo': {
        'dashboard_name': 'org_dashboard_ngo',
        'label': 'Disaster Relief / NGO',
        'icon': '🏥',
        'color': '#dc2626',
        'template_prefix': 'ngo',
    },

    # ── Meteorological / Institution ─────────────────────────────
    'institution': {
        'dashboard_name': 'org_dashboard_meteorological',
        'label': 'Meteorological Department',
        'icon': '🌦️',
        'color': '#2563eb',
        'template_prefix': 'meteorological',
    },

    # ── Enterprise (aviation, developer, default) ─────────────────
    'enterprise': {
        'dashboard_name': 'org_dashboard_enterprise',
        'label': 'Enterprise',
        'icon': '💻',
        'color': '#7c3aed',
        'template_prefix': 'enterprise',
    },

    # ── Government ────────────────────────────────────────────────
    'government': {
        'dashboard_name': 'org_dashboard_government',
        'label': 'Government / NGO',
        'icon': '🏛️',
        'color': '#0369a1',
        'template_prefix': 'government',
    },

    # ── Community ─────────────────────────────────────────────────
    'community': {
        'dashboard_name': 'org_dashboard_community',
        'label': 'Community Group',
        'icon': '👥',
        'color': '#0891b2',
        'template_prefix': 'community',
    },
}

# Wizard org_type string → model org_type
# Mirrors RegisterOrganizationView.ORG_TYPE_MAP but we also track sub-type
WIZARD_TO_REGISTRY_KEY = {
    'agriculture':     'enterprise_agriculture',
    'disaster_relief': 'ngo',
    'meteorological':  'institution',
    'aviation':        'enterprise',
    'developer':       'enterprise',
    'government':      'government',
}


def get_dashboard_url_name(org):
    """
    Given a guardian.models.Organization instance, return the
    named URL for that org type's dashboard.
    Falls back to the guardian default dashboard.
    """
    key = _resolve_key(org)
    config = ORG_DASHBOARD_REGISTRY.get(key)
    if config:
        return config['dashboard_name']
    return 'dashboard'   # guardian default


def get_org_config(org):
    """Return full config dict for an org, or sensible defaults."""
    key = _resolve_key(org)
    return ORG_DASHBOARD_REGISTRY.get(key, {
        'dashboard_name': 'dashboard',
        'label': org.get_org_type_display(),
        'icon': '🏢',
        'color': '#4f46e5',
        'template_prefix': None,
    })


def _resolve_key(org):
    """
    Try to match the org to a registry key.
    Checks for a stored wizard_type on the org first (via description hack),
    then falls back to model org_type.
    """
    model_type = org.org_type  # e.g. 'enterprise', 'ngo', 'institution', ...

    # Special case: agriculture is stored as 'enterprise' in the model.
    # We use the org's wizard_type field if you add it, or we check description/slug.
    # For now we check if 'agriculture' or 'farm' appears in description/slug as a heuristic.
    # A better approach is the org_subtype field added below.
    if hasattr(org, 'org_subtype') and org.org_subtype:
        # e.g. org_subtype = 'agriculture'
        return WIZARD_TO_REGISTRY_KEY.get(org.org_subtype, model_type)

    # Fallback heuristic for existing orgs
    if model_type == 'enterprise':
        slug_desc = (org.slug + ' ' + (org.description or '')).lower()
        if any(kw in slug_desc for kw in ('agri', 'farm', 'crop', 'harvest', 'irrigation')):
            return 'enterprise_agriculture'

    return model_type