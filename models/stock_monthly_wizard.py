# -*- coding: utf-8 -*-
import base64
import calendar
import io
import logging
import zipfile

from odoo import models, fields, api, _
from odoo.exceptions import UserError

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
    year = fields.Integer(
        string="Year",
        required=True,
        default=lambda self: fields.Date.today().year,
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

    # ===== Filters =====
    location_id = fields.Many2one(
        "stock.location",
        string="Location",
        domain=[("usage", "=", "internal")],
        required=True,
    )
    include_sub_locations = fields.Boolean(
        string="Include Sub-locations",
        default=True,
        help="If checked, include all child locations under the selected location.",
    )

    categ_id = fields.Many2one(
        "product.category",
        string="Product Category",
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
    @api.onchange("categ_id")
    def _onchange_categ_id(self):
        if self.categ_id and self.product_ids:
            allowed_products = self.env["product.product"].search([
                ("categ_id", "child_of", self.categ_id.id)
            ])
            # ✅ ต้องเป็น & (intersection) ไม่ใช่ &amp;
            self.product_ids = self.product_ids & allowed_products

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

    # ------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------
    def _get_location_ids(self):
        self.ensure_one()
        if self.include_sub_locations:
            return self.env["stock.location"].search([("id", "child_of", self.location_id.id)]).ids
        return [self.location_id.id]

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
        elif self.categ_id:
            domain.append(("product_id.categ_id", "child_of", self.categ_id.id))

        return self.env["stock.quant"].search(domain)

    def _get_period_range(self):
        self.ensure_one()
        year = self.year
        m_start = int(self.month_start)
        m_end = int(self.month_end)
        date_from = fields.Datetime.to_datetime(f"{year}-{m_start:02d}-01 00:00:00")
        last_day = calendar.monthrange(year, m_end)[1]
        date_to = fields.Datetime.to_datetime(f"{year}-{m_end:02d}-{last_day:02d} 23:59:59")
        return date_from, date_to

    # ------------------------------------------------------------
    # Main action
    # ------------------------------------------------------------
    def action_open_stock_quantity_history(self):
        self.ensure_one()

        # อ่าน action มาตรฐานของ Odoo
        action = self.env.ref("stock.view_stock_quantity_history").read()[0]

        # ส่งค่า default / context เข้าไป
        action["context"] = {
            # วันที่/เวลา (Inventory at Date)
            "inventory_datetime": self._get_period_range()[1],  # ใช้ date_to หรือ as-of ที่คุณต้องการ
            # default location
            "default_location_id": self.location_id.id,
            # รวม sub-location (ถ้าเปิดใช้ Storage Locations)
            "search_default_location_id": self.location_id.id,
        }

        return action

    def action_generate_reports(self):
        self.ensure_one()
        self._validate_inputs()

        quants = self._get_quants()
        if not quants:
            raise UserError(_("No stock data found for selected criteria."))

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

            pdf_content, _report_type = pdf_action._render_qweb_pdf(quants.ids, data=report_data)
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

            xlsx_content, _report_type = xlsx_action._render_xlsx(quants.ids, data=report_data)
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
