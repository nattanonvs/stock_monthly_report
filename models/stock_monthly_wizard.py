# -*- coding: utf-8 -*-
import base64
import calendar
import io
import logging
import zipfile
from datetime import datetime

import pytz
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

_MONTH_SELECTION = [
    ("1", "มกราคม"),
    ("2", "กุมภาพันธ์"),
    ("3", "มีนาคม"),
    ("4", "เมษายน"),
    ("5", "พฤษภาคม"),
    ("6", "มิถุนายน"),
    ("7", "กรกฎาคม"),
    ("8", "สิงหาคม"),
    ("9", "กันยายน"),
    ("10", "ตุลาคม"),
    ("11", "พฤศจิกายน"),
    ("12", "ธันวาคม"),
]


class StockMonthlyWizard(models.TransientModel):
    _name = "stock.monthly.wizard"
    _description = "Monthly Stock Report Wizard"

    # ===== Period =====
    year = fields.Char(
        string="Year",
        required=True,
        size=4,
        default=lambda self: str(fields.Date.today().year),
    )
    month_start = fields.Selection(
        _MONTH_SELECTION,
        string="Start Month",
        required=True,
        default="1",
    )
    month_end = fields.Selection(
        _MONTH_SELECTION,
        string="End Month",
        required=True,
        default="1",
    )
    asof_time = fields.Char(string="As-of Time (HH:MM)", help="Optional. If empty, uses current local time.")

    # ===== Filters =====
    location_ids = fields.Many2many(
        "stock.location",
        string="Locations",
        domain=[("usage", "=", "internal")],
        required=True,
    )
    include_sub_locations = fields.Boolean(
        string="Include Sub-locations",
        default=True,
        help="If checked, include all child locations under the selected location.",
    )

    include_categ_ids = fields.Many2many(
        "product.category",
        "stock_monthly_wizard_include_categ_rel",
        "wizard_id",
        "categ_id",
        string="Include Product Categories",
    )
    exclude_categ_ids = fields.Many2many(
        "product.category",
        "stock_monthly_wizard_exclude_categ_rel",
        "wizard_id",
        "categ_id",
        string="Exclude Product Categories",
    )
    product_ids = fields.Many2many(
        "product.product",
        string="Products",
    )

    include_zero_qty = fields.Boolean(
        string="Include Zero Qty",
        default=False,
        help="If unchecked, quants with zero quantity will be excluded (recommended for performance).",
    )

    show_movements = fields.Boolean(
        string="Show Movements (Vendor/Customer/Internal)",
        default=True,
        help="If unchecked, movement columns will be hidden in the XLSX report.",
    )
    summary_only = fields.Boolean(
        string="Summary Only",
        default=False,
        help="If checked, show only subtotals per Category/Location and Grand Total (no product lines).",
    )

    # ===== Output =====
    include_pdf = fields.Boolean(string="Include PDF", default=False)
    include_excel = fields.Boolean(string="Include Excel", default=True)

    # ===== Email (Optional) =====
    send_email = fields.Boolean(string="Send by Email", default=False)
    email_to = fields.Char(string="Recipients (comma-separated)")
    email_cc = fields.Char(string="CC (comma-separated)")
    email_bcc = fields.Char(string="BCC (comma-separated)")
    email_body = fields.Html(
        string="Email Body",
        sanitize=False,
        # ✅ ใน Python ใส่ HTML ตรง ๆ ไม่ต้อง escape &lt; &amp;
        default="<p>Attached are the Stock On Hand reports (PDF &amp; Excel).</p>",
    )

    # ------------------------------------------------------------
    # UX: ถ้าเลือก Category แล้ว Products ต้องอยู่ใน category subtree เท่านั้น
    # ------------------------------------------------------------
    @api.constrains("year")
    def _check_year(self):
        for wiz in self:
            if not wiz.year or not wiz.year.isdigit() or len(wiz.year) != 4:
                raise ValidationError(_("Year must be a 4-digit number."))

    def _get_category_filter_sets(self):
        self.ensure_one()
        include_subtree_ids = set()
        if self.include_categ_ids:
            include_subtree_ids = set(self.env["product.category"].search([
                ("id", "child_of", self.include_categ_ids.ids)
            ]).ids)

        exclude_subtree_ids = set()
        if self.exclude_categ_ids:
            exclude_subtree_ids = set(self.env["product.category"].search([
                ("id", "child_of", self.exclude_categ_ids.ids)
            ]).ids)

        if include_subtree_ids:
            include_subtree_ids -= exclude_subtree_ids

        return include_subtree_ids, exclude_subtree_ids

    @api.onchange("include_categ_ids", "exclude_categ_ids")
    def _onchange_category_filters(self):
        domain = []
        include_subtree_ids, exclude_subtree_ids = self._get_category_filter_sets()
        if include_subtree_ids:
            domain.append(("categ_id", "in", list(include_subtree_ids)))
        if exclude_subtree_ids:
            domain.append(("categ_id", "not in", list(exclude_subtree_ids)))

        if domain and self.product_ids:
            allowed_products = self.env["product.product"].search(domain)
            self.product_ids = self.product_ids & allowed_products

        return {"domain": {"product_ids": domain}}

    # ------------------------------------------------------------
    # Validate
    # ------------------------------------------------------------
    def _validate_inputs(self):
        self.ensure_one()
        m_start = int(self.month_start)
        m_end = int(self.month_end)
        if m_end < m_start:
            raise UserError(_("End Month must be greater than or equal to Start Month."))
        if not (self.include_pdf or self.include_excel):
            raise UserError(_("Please select at least one output: PDF or Excel."))
        self._parse_asof_time()

    def _parse_asof_time(self):
        self.ensure_one()
        s = (self.asof_time or "").strip()
        if not s:
            return None
        parts = s.split(":")
        if len(parts) not in (2, 3):
            raise ValidationError(_("As-of Time must be in HH:MM or HH:MM:SS format."))
        try:
            hh = int(parts[0])
            mm = int(parts[1])
            ss = int(parts[2]) if len(parts) == 3 else 0
        except Exception:
            raise ValidationError(_("As-of Time must be in HH:MM or HH:MM:SS format."))
        if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
            raise ValidationError(_("As-of Time must be a valid time (00:00 to 23:59)."))
        return hh, mm, ss

    # ------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------
    def _get_location_ids(self):
        self.ensure_one()
        if self.include_sub_locations:
            return self.env["stock.location"].search([
                ("id", "child_of", self.location_ids.ids),
                ("usage", "=", "internal"),
            ]).ids
        return self.location_ids.ids

    def _get_quants(self):
        """
        Fetch stock.quant based on:
        - location subtree (child_of)
        - product_ids OR category subtree
        - exclude qty=0 for performance (default)
        """
        self.ensure_one()
        loc_ids = self._get_location_ids()

        domain = [("location_id", "in", loc_ids)]
        if not self.include_zero_qty:
            domain.append(("quantity", "!=", 0))

        if self.product_ids:
            domain.append(("product_id", "in", self.product_ids.ids))
        else:
            include_subtree_ids, exclude_subtree_ids = self._get_category_filter_sets()
            if include_subtree_ids:
                domain.append(("product_id.categ_id", "in", list(include_subtree_ids)))
            if exclude_subtree_ids:
                domain.append(("product_id.categ_id", "not in", list(exclude_subtree_ids)))

        return self.env["stock.quant"].search(domain)

    def _get_period_range(self):
        self.ensure_one()
        year = int(self.year)
        m_start = int(self.month_start)
        m_end = int(self.month_end)
        last_day = calendar.monthrange(year, m_end)[1]
        tz = pytz.timezone(self.env.user.tz or "UTC")

        time_parts = self._parse_asof_time()
        if time_parts is None:
            now_local = fields.Datetime.context_timestamp(self, fields.Datetime.now())
            time_parts = (now_local.hour, now_local.minute, now_local.second)
        hh, mm, ss = time_parts

        date_from_local = tz.localize(datetime(year, m_start, 1, 0, 0, 0))
        date_to_local = tz.localize(datetime(year, m_end, last_day, hh, mm, ss))

        date_from = date_from_local.astimezone(pytz.UTC).replace(tzinfo=None)
        date_to = date_to_local.astimezone(pytz.UTC).replace(tzinfo=None)
        return date_from, date_to

    # ------------------------------------------------------------
    # Main action
    # ------------------------------------------------------------
    def action_open_stock_quantity_history(self):
        self.ensure_one()

        date_to = self._get_period_range()[1]

        tree_view_id = self.env.ref("stock.view_stock_product_tree").id
        form_view_id = self.env.ref("stock.product_form_view_procurement_button").id

        domain = [("type", "in", ["product", "consu"])]
        if self.product_ids:
            domain.append(("id", "in", self.product_ids.ids))
        else:
            include_subtree_ids, exclude_subtree_ids = self._get_category_filter_sets()
            if include_subtree_ids:
                domain.append(("product_tmpl_id.categ_id", "in", list(include_subtree_ids)))
            if exclude_subtree_ids:
                domain.append(("product_tmpl_id.categ_id", "not in", list(exclude_subtree_ids)))

        ctx = dict(self.env.context or {})
        ctx.update({
            "to_date": date_to,
            "location": self.location_ids.ids,
            "compute_child": bool(self.include_sub_locations),
        })

        return {
            "type": "ir.actions.act_window",
            "name": _("Inventory at Date"),
            "res_model": "product.product",
            "views": [(tree_view_id, "tree"), (form_view_id, "form")],
            "view_mode": "tree,form",
            "domain": domain,
            "context": ctx,
        }

    def action_generate_reports(self):
        self.ensure_one()
        self._validate_inputs()

        quants = self._get_quants()
        quant_ids = quants.ids

        # data payload ให้ report อ่าน wizard ได้ (PDF/XLSX จะใช้ wizard_id + flags)
        report_data = {
            "wizard_id": self.id,
            "show_movements": self.show_movements,
            "summary_only": self.summary_only,
        }

        attachments = []
        pdf_content = None
        xlsx_content = None

        def _download_url(attachment_id):
            return f"/web/content?model=ir.attachment&id={attachment_id}&field=datas&filename_field=name&download=true"

        # ===== PDF =====
        if self.include_pdf:
            pdf_action = self.env.ref(
                "stock_monthly_report.action_stock_monthly_report_pdf",
                raise_if_not_found=False,
            )
            if not pdf_action:
                raise UserError(_("Missing PDF report action: stock_monthly_report.action_stock_monthly_report_pdf"))

            pdf_content, _report_type = pdf_action._render_qweb_pdf(quant_ids, data=report_data)
            if not (pdf_content or b""):
                raise UserError(_("PDF generation returned empty content."))
            if not pdf_content.startswith(b"%PDF"):
                raise UserError(_("PDF generation failed (non-PDF content). Please verify wkhtmltopdf and report.url/web.base.url settings."))
            pdf_att = self.env["ir.attachment"].create({
                "name": "Stock_On_Hand.pdf",
                "type": "binary",
                "datas": base64.b64encode(pdf_content).decode("utf-8"),
                "mimetype": "application/pdf",
                "res_model": self._name,
                "res_id": self.id,
            })
            attachments.append(pdf_att.id)

            if not self.send_email and self.include_pdf and not self.include_excel:
                return {
                    "type": "ir.actions.act_url",
                    "url": _download_url(pdf_att.id),
                    "target": "self",
                }

        # ===== XLSX =====
        if self.include_excel:
            xlsx_action = self.env.ref(
                "stock_monthly_report.action_stock_monthly_report_xlsx",
                raise_if_not_found=False,
            )
            if not xlsx_action:
                raise UserError(_("Missing XLSX report action: stock_monthly_report.action_stock_monthly_report_xlsx"))

            xlsx_content, _report_type = xlsx_action._render_xlsx(quant_ids, data=report_data)
            if not (xlsx_content or b""):
                raise UserError(_("XLSX generation returned empty content."))
            if not xlsx_content.startswith(b"PK"):
                raise UserError(_("XLSX generation failed (non-XLSX content)."))
            xlsx_att = self.env["ir.attachment"].create({
                "name": "Stock_On_Hand.xlsx",
                "type": "binary",
                "datas": base64.b64encode(xlsx_content).decode("utf-8"),
                "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "res_model": self._name,
                "res_id": self.id,
            })
            attachments.append(xlsx_att.id)

            if not self.send_email and self.include_excel and not self.include_pdf:
                return {
                    "type": "ir.actions.act_url",
                    "url": _download_url(xlsx_att.id),
                    "target": "self",
                }

        if not self.send_email and self.include_pdf and self.include_excel:
            if not pdf_content or not xlsx_content:
                raise UserError(_("Failed to build the report package."))

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("Stock_On_Hand.pdf", pdf_content)
                zf.writestr("Stock_On_Hand.xlsx", xlsx_content)

            zip_att = self.env["ir.attachment"].create({
                "name": "Stock_On_Hand.zip",
                "type": "binary",
                "datas": base64.b64encode(buf.getvalue()).decode("utf-8"),
                "mimetype": "application/zip",
                "res_model": self._name,
                "res_id": self.id,
            })

            return {
                "type": "ir.actions.act_url",
                "url": _download_url(zip_att.id),
                "target": "self",
            }

        # ===== Email =====
        if self.send_email:
            recipients = self.email_to or self.env["ir.config_parameter"].sudo().get_param(
                "stock_monthly_report.recipients"
            ) or "team@yourcompany.com"

            mail_values = {
                "subject": _("Stock On Hand Report"),
                "body_html": self.email_body or "<p>Attached are the Stock On Hand reports.</p>",
                "email_to": recipients,
                "email_cc": self.email_cc or "",
                "email_bcc": self.email_bcc or "",
                "attachment_ids": [(6, 0, attachments)],
            }
            self.env["mail.mail"].create(mail_values).send()
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Stock On Hand Report"),
                    "message": _("Email sent successfully."),
                    "sticky": False,
                    "type": "success",
                },
            }

        # ===== Show attachments (แก้ Unnamed ด้วย name) =====
        return {
            "type": "ir.actions.act_window",
            "name": _("Generated Reports"),
            "res_model": "ir.attachment",
            "view_mode": "tree,form",
            "domain": [("id", "in", attachments)],
            "target": "new",
            "context": {"create": False},
        }
