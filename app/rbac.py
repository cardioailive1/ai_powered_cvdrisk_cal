"""Role-based access control. Every API route declares the permission it needs (app.auth.require);
roles are a fixed set of permission bundles following least privilege / minimum necessary."""
from __future__ import annotations

PERMISSIONS = {
    "models:read": "View calculators, model cards and performance",
    "predict": "Run risk calculations (handles clinical inputs)",
    "assessments:own": "View own saved assessments",
    "users:read": "View user accounts and access requests",
    "users:manage": "Approve, create, change role, disable, unlock, reset password or MFA",
    "audit:read": "View, verify and export the audit log",
}

ROLES: dict[str, dict] = {
    "viewer": {"label": "Viewer", "description": "Read-only: model cards and performance; cannot enter patient data.",
               "permissions": ["models:read"]},
    "clinician": {"label": "Clinician", "description": "Runs the calculators and sees their own assessments.",
                  "permissions": ["models:read", "predict", "assessments:own"]},
    "auditor": {"label": "Compliance auditor", "description": "Reviews the audit log and user access; no clinical data.",
                "permissions": ["models:read", "users:read", "audit:read"]},
    "admin": {"label": "Administrator", "description": "Manages accounts and roles; full access.",
              "permissions": list(PERMISSIONS)},
}

STATUSES = ("pending", "active", "disabled", "rejected")


def permissions_for(role: str) -> list[str]:
    return list(ROLES.get(role, {"permissions": []})["permissions"])


def has(role: str, perm: str) -> bool:
    return perm in permissions_for(role)
