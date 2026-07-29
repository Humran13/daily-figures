from datetime import datetime, timezone

from webapp.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CompanySettings(db.Model):
    """
    Single-row white-label branding/configuration table (id always 1).
    Company configuration is deliberately kept separate from operational
    business data (dispatches, customers, daily figures) — this table is
    the only thing this enhancement is allowed to touch.

    This is a single-company deployment: no company_id/tenant column here.
    The single-row-by-fixed-id shape keeps a future multi-company migration
    straightforward (add a company_id FK and drop the fixed-id constraint)
    without requiring a rewrite now.
    """
    __tablename__ = "company_settings"

    id = db.Column(db.Integer, primary_key=True)
    display_name = db.Column(db.String(160), nullable=False)
    legal_name = db.Column(db.String(160), nullable=True)
    logo_path = db.Column(db.String(255), nullable=True)  # relative path under the persistent uploads dir
    address = db.Column(db.Text, nullable=True)
    phone = db.Column(db.String(40), nullable=True)
    email = db.Column(db.String(160), nullable=True)
    website = db.Column(db.String(200), nullable=True)
    currency_code = db.Column(db.String(10), nullable=True)
    tax_registration_number = db.Column(db.String(80), nullable=True)
    report_footer_text = db.Column(db.Text, nullable=True)
    primary_contact_name = db.Column(db.String(120), nullable=True)
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    def to_dict(self):
        return {
            "display_name": self.display_name,
            "legal_name": self.legal_name,
            "logo_url": f"/api/branding/logo?v={self.updated_at.timestamp():.0f}" if self.logo_path else None,
            "address": self.address,
            "phone": self.phone,
            "email": self.email,
            "website": self.website,
            "currency_code": self.currency_code,
            "tax_registration_number": self.tax_registration_number,
            "report_footer_text": self.report_footer_text,
            "primary_contact_name": self.primary_contact_name,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def public_dict(self):
        """Safe subset for the unauthenticated login page and in-app
        headers — never the address/phone/email/tax/contact fields."""
        return {
            "display_name": self.display_name,
            "logo_url": f"/api/branding/logo?v={self.updated_at.timestamp():.0f}" if self.logo_path else None,
        }
